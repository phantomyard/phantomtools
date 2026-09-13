# Consistency model

This document defines *what PhantomDocs guarantees* about the durability and
consistency of a mutation, which failure windows exist, how `pd verify`
detects each one, and how `pd recover` repairs the one recoverable case. It
closes issue #101: the transactional *bug* was fixed by #74/#91 (atomic blob
writes, transactional manifest+audit commit), the crash path got a repair
command in #108 (`pd recover`), and #109 added crash-atomic audit truncation
plus parent-directory `fsync` — this document is the *model* those fixes
implement.

A mutation is not a single distributed transaction. It is a **three-step
sequence with one ordered crash window**, chosen deliberately over a
two-phase commit, because the single-writer deployment (SPEC §6.3) already
serializes writers and the ordering makes every interleaving detectable.

## 1. The three durable artifacts

| Artifact | Written by | Identity it carries |
|---|---|---|
| **Blob** | backend `put` (`local://`, `ssh://`, `gdrive://`) | content address = SHA-256 of the bytes |
| **Manifest** | `manifest.save` | `headSeq`, `headMac`, `auditSeq`, `auditHead`, node/ref state |
| **Audit log** | `audit.append` | append-only hash chain (`prev` = SHA-256 of the prior line), plus `seq` |

The manifest is the *state*; the audit log is the *evidence*. The manifest
records the audit position it committed alongside (`auditSeq` + `auditHead`),
so the two can be compared after a crash instead of trusting either one.

## 2. Commit order

```
1. blob write      → content-addressed, idempotent, invisible until committed
2. audit append    → the evidence lands first
3. manifest commit → the state lands second, referencing (2) via auditSeq/auditHead
```

Two properties follow from the ordering:

- **The blob is never the problem.** It is written to a unique temp file,
  `fsync`'d and `os.replace`'d into its content address, then the shard
  directory is `fsync`'d. A crash either leaves the blob fully present under
  its hash or leaves an unreferenced `.tmp` file. Because the address *is* the
  hash, a present-but-corrupt blob can never be silently used — every read
  re-checks it. `gdrive://` differs in kind, not in guarantee: it is a
  *reference* backend (spec §8), so the upload carries the content hash as its
  idempotency key and the returned file id is read-back hash-verified before
  it becomes a location (audit #8); the content-addressed path above is the
  `local://`/`ssh://` one.
- **The audit log is never behind the manifest.** Writing evidence before
  state means the crash window is *audit ahead of manifest* — a detectable
  extra entry — rather than a committed mutation with no evidence. The append
  itself is a single `O_APPEND` write, not an atomic replace, so a crash
  *inside* step 2 has its own window (W2: a short or torn final line).

## 3. Failure windows

Let `N` be the last successfully committed manifest state.

### W1 — crash after blob write, before audit append

- **Left behind:** an unreferenced blob (or a `.tmp` file), nothing else.
- **Guarantee:** no manifest or audit change. The mutation did not happen.
- **Detected by:** nothing to detect — the state is clean. Unreferenced blobs
  are inert; a later `add` of the same content simply reuses the address.
- **Repaired by:** nothing required.

### W2 — crash during the audit append (short or torn final line)

- **Left behind:** `audit.log` ends in an incomplete line. A short write or a
  power loss mid-`write` can persist fewer bytes than the entry, and `append`
  checks neither the returned byte count nor a commit marker. The line is
  counted by `head()` (it is non-blank) but is not a valid entry.
- **Guarantee:** the entry never became complete, so the mutation is **not**
  committed. A torn line can never be mistaken for committed evidence: the
  manifest commit is anchored on `auditSeq`/`auditHead`, and the chain walk
  rejects the line before either anchor is compared.
- **Detected by:** `pd verify` — `FAIL audit: audit line <n>: not valid JSON`
  (the chain walk reports the torn line, then the count/head mismatches that
  follow from it).
- **Repaired by:** nothing — see §4. This window is **fail-closed**: neither
  `pd recover` nor the next mutation will touch it, because a torn write and a
  hand-edited line are indistinguishable from the log alone.

### W3 — crash after audit append, before manifest commit

- **Left behind:** `audit.log` has one (or more) complete extra entries
  relative to `manifest.auditSeq`; the manifest still reads `N`.
- **Guarantee:** the mutation is **not** committed. The document state does not
  include it.
- **Detected by:** `pd verify` — the audit count disagrees with `auditSeq`, and
  `headSeq` disagrees with the audit log's authoritative counter.
- **Repaired by:** `pd recover` — *the* recoverable window (§4).

### W4 — crash during the manifest replace

- **Left behind:** the manifest is either the old file or the new one — never a
  mixture. `manifest.save` writes a unique temp file, `fsync`'s it, then
  `os.replace`s it into place (atomic on POSIX and Windows) and `fsync`'s the
  parent directory, so the rename itself is durable.
- **Guarantee:** the manifest is always *some* complete, previously-written
  state.
- **Detected by:** nothing to detect at this layer.
- **Repaired by:** nothing required.

### W5 — the audit log's tail is truncated or reordered

- **Left behind:** a log that is *behind* the manifest, has a broken `prev`
  chain, or whose head hash disagrees with `auditHead`.
- **Guarantee:** this is **not** a crash signature — no crash path removes
  committed lines. Behind/broken means tampering or manual edits.
- **Detected by:** `pd verify` (chain walk + `auditSeq`/`auditHead` comparison).
- **Repaired by:** nothing — `pd recover` refuses fail-closed. An operator must
  investigate; the recovery command will not "fix" evidence of tampering.

## 4. `pd recover`

```
pd recover --root <root>
```

`pd recover` reconciles the audit log with the manifest and handles **exactly
one** case: an orphaned audit tail from W3. It:

1. walks the audit hash chain and refuses (fail-closed) if a single link is
   broken — which is also what makes a torn append (W2) unrecoverable here: the
   torn line fails the walk before the counts are compared;
2. if `actual_count < auditSeq`, refuses — the log is behind the manifest
   (tampering, not a crash);
3. if `actual_count > auditSeq`, checks that the *kept prefix* still chains
   cleanly off the recorded `auditHead`, then truncates the orphaned tail
   (atomic truncate + directory `fsync`, #109);
4. if the counts match but the last line's hash differs from `auditHead`,
   refuses — that is a substitution, not a crash.

Output is either `nothing to recover: audit log is consistent with the
manifest` or a count of discarded orphaned entries. It never rewrites a
committed mutation and never deletes evidence — it only discards entries whose
manifest commit provably never landed.

**W2 (torn append) is not recovered.** A short or torn final line fails the
chain walk, so `pd recover` refuses it as tampering:

```
Error: refusing to recover (tampering, not a crash): audit hash chain is broken
(tampering, not a crash): audit line <n>: not valid JSON
```

The next mutation refuses in the same way, from `_next_head()`'s reconciliation:

```
Error: audit log diverges from the manifest in a way that is not a clean crash;
refusing to commit: audit hash chain is broken (tampering, not a crash):
audit line <n>: not valid JSON
```

So a namespace in W2 does **not** self-heal: it stays blocked until an operator
repairs it. `pd` deliberately offers no repair command for it, because a torn
write and a hand-edited line are indistinguishable from the log alone — the
recovery boundary is a trusted operator action (restore `audit.log` from a
backup, or drop the incomplete trailing line so the log ends at the prefix the
manifest's `auditSeq`/`auditHead` anchor describes). Closing this window in code
would need a per-line commit marker or a whole-file temp+replace publication,
neither of which exists today.

**Recovery is automatic on the next mutation for W3.** `_next_head()`
reconciles the audit log before computing the next sequence, so a namespace
orphaned by W3 self-heals on the next `pd add`/`pd tag`/`pd rollback` —
`pd recover` exists so an operator can *see and confirm* that, and so a
namespace with no further mutations can be cleaned up explicitly.

## 5. What is atomic vs eventually consistent

- **Crash-atomic (temp file + `fsync` + atomic replace):** blob publication,
  manifest publish, and audit truncation.
- **Single append, not crash-atomic:** the audit append (§2, step 2) is one
  `O_APPEND` write followed by `fsync`. The log is never truncated, but the
  write is not a publication primitive, so a crash *during* it can leave a
  short or torn final line — window W2, detected by `pd verify` and repaired
  only by an operator (`pd recover` refuses it).
- **Ordered but not atomic (across steps):** blob → audit → manifest. A crash
  between steps leaves a *detectable* intermediate state, never a silent
  inconsistency.
- **Eventually consistent (across a crash):** the audit/manifest pair. It
  converges when `pd recover` runs or the next mutation commits — except in W2,
  where convergence requires an operator repair.
- **Not attempted:** distributed transactions, two-phase commit, or any
  cross-host coordination. SPEC §6.3 (Model A, single authoritative writer
  host) is the boundary that makes this model sufficient — with one writer,
  ordering plus a recovery scan replaces a coordinator.

## 6. Durability assumptions

`fsync_dir` is best-effort by design: directory `fsync` is unsupported on
Windows and some network filesystems, and a commit must not fail merely because
the platform cannot honour the extra step. On the supported deployment (POSIX,
local disk, or GDrive via the persona's OAuth2) the directory sync turns
"probably durable" into "durable" for the rename itself.

PhantomDocs v1 provides **integrity and authenticity, not
encryption-at-rest** (SPEC §14, decision 4): a backend reader can read blob
contents, but cannot alter them undetected. Confidentiality relies on the
storage layer's own encryption until the phase-2 encryption-at-rest
capability lands.

## 7. Where this is implemented

| Concern | Location |
|---|---|
| Blob atomic write + symlink guard | `storage.py` (`LocalBackend.put`, `SshBackend.put`) |
| Manifest atomic write | `manifest.py` (`save`) |
| Audit append + atomic truncate | `audit.py` (`append`, `truncate`) |
| Reconciliation (the only repair) | `audit.py` (`reconcile`) |
| Commit ordering | `documents.py` (`DocumentService._commit_transaction`) |
| Pre-mutation self-heal | `documents.py` (`DocumentService._next_head`) |
| Detection | `cli.py` (`pd verify`), `manifest.py` (`structural_issues`, `mutation_sequence_issues`) |
| Repair command | `cli.py` (`pd recover`) |
| Directory durability | `fsutil.py` (`fsync_dir`) |

## 8. Reference

- SPEC §6.3 — single authoritative writer host (Model A)
- SPEC §12 — integrity, versioning, backup, verification, audit
- `docs/SECURITY-TRUST-ANCHOR.md` — the trust anchor and seal-key lifecycle
- Issues #74 / #91 (atomicity), #108 (`pd recover`), #109 (crash-atomic audit),
  #101 (this document)
