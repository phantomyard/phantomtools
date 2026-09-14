# Trust anchor & seal key lifecycle

This document defines the *operational* security procedure around the
namespace trust anchor (the `--org-pubkey`) and the organization **seal key**.
It closes audit finding #6: "root seal trust procedure must be operationally
defined", and the follow-up requirement to document seal-key rotation as
separate from actor-key rotation (issue #76).

## 1. The trust anchor: where `org-pubkey` comes from

The namespace root MAC is:

```
root_mac = H( org_id || org_pubkey || namespace )     # identity.py
```

The `org_pubkey` is therefore **part of the namespace's cryptographic
identity** — not merely an operational key. `verify --org-pubkey` recomputes
the root MAC from that key and checks the head seal against it. If an attacker
could substitute their own `org_pubkey`, they could forge a root and a seal
over arbitrary content.

**Rule (fail-closed):** the trusted `org_pubkey` MUST come from an *external,
out-of-band* trust source — never from the repository/namespace being
verified. Concretely:

```
trusted org key (out-of-band)
        ↓
pd verify --org-pubkey <npub>
        ↓
recompute root MAC   → must equal manifest.rootMac
        ↓
verify head seal     → seal must be made by that key
        ↓
verify head state    → sealedHeadSeq == headSeq (no advance past the seal)
```

In the PhantomOrg model the external source is the organization's Nostr
identity: the `npub` declared in `org.yaml` and provisioned by `po build`.
The operator obtains that `npub` from the org's own key ceremony / vault, and
passes it to `verify` out-of-band — it is never read from the manifest or the
namespace under verification.

## 2. Two keys, two roles

- **`org_pubkey` (identity key)** — baked into `root_mac`; it *is* the
  namespace identity. It MUST NOT rotate: rotating it changes the root MAC and
  invalidates every node MAC (a new namespace).
- **seal key** — signs the head seal (`rootMac` + `headSeq` + `headMac` +
  `auditSeq` + `auditHead`). This is an *operational* key and should rotate on
  compromise, loss, or staff turnover.

These must be tracked as separate lifecycles. Actor-key rotation (#76) is a
third, distinct lifecycle (`key_valid_at` over actor `keys` in `org.yaml`).

## 3. Seal key rotation lifecycle

The namespace header carries the lifecycle (issue #104):

- **`sealIdentityNpub`** — the org **identity** key, recorded at the first
  seal. It is baked into `root_mac` and therefore never rotates;
  `verify --org-pubkey` cross-checks it against the operator's out-of-band
  anchor, so a manifest that declares its own seal identity is rejected.
- **`sealKeys`** — one record per org-authorized seal key:
  `{npub, valid_from, valid_until, delegation}`. `delegation` is the identity
  key's signature over `{identity_npub, root_mac, seal_npub, valid_from,
  valid_until}`, so an entry is trustworthy only if the anchor authorized it:
  the history is never self-attested (anyone who can edit the manifest could
  otherwise declare a key of their own and re-seal a forged head). The record
  is *not* rewritten when a key is revoked — see `revocations` below.
- **`revocations`** — the append-only revocation chain, one record per
  `pd revoke-seal-key`: `{n, npub, revoked_at, prevHash, delegation}`. `n` is a
  1-based generation, `prevHash` is the hash of the chain before this record,
  and `delegation` is the identity key's signature over all of it. Revoking an
  authorization by overwriting it in place — the original #104 shape — let
  anyone able to edit the manifest bring the key back by restoring the older
  record, because the org had signed that window once and that signature never
  stopped being valid. The chain removes that: a revocation is a permanent,
  org-signed *addition*, so erasing one shortens a history instead of
  restoring a valid one.
- **`seals`** — the append-only seal history, one event per `pd seal`:
  `{npub, ts, cs, headMac, auditSeq, auditHead, sig, requireSignatures,
  cryptoVersion}`. A re-seal at the same head appends; the latest matching
  event is the live seal.

**Generation** — a new seal keypair is generated off-namespace.

**Rotation (re-seal)** — the identity key authorizes the new seal key, then
that key seals the head:

```bash
# A mutation advanced the head; authorize a new seal key and rotate to it.
pd seal --nsec-file seal-b.nsec --org-nsec-file org-identity.nsec --root ./docs

# Later re-seals with an already-authorized key need no org-key ceremony.
pd seal --nsec-file seal-b.nsec --root ./docs
```

`root_mac` is unchanged — only the sealing key rotates. Seals made under
earlier keys stay verifiable under the key that made them: `pd verify`
re-checks every recorded event, and `pd seal-keys` shows the chain.

**Revocation** — authorized by the identity key (a revocation the anchor did
not sign is refused), *permanent* (a second revocation of the same key is
refused) and fail-closed: a seal made by that key at or after `revoked_at` is
rejected. Seals made *before* the revocation stay valid — a compromised key
does not retroactively invalidate the evidence of earlier heads — but the
revoked key can no longer seal, so the namespace picks up a new authorized key
with `pd seal --org-nsec-file`.

```bash
# Publish the checkpoint at revocation time (see below) so the revocation is
# anchored outside the namespace from the moment it is made.
pd revoke-seal-key <npub> --org-nsec-file org-identity.nsec \
  --checkpoint-out ./anchor/revocations.json --root ./docs
```

**Serialization (SPEC §6.3):** revoking, sealing and rendering a checkpoint all
run under the namespace's inter-process `manifest.lock`, like the document
mutation paths. The revocation cycle is a read/derive/sign/append/save
sequence, so without the lock two concurrent revocations could both read
generation *N*, sign different records as *N+1* and overwrite each other —
both commands reporting success while a permanent revocation disappears. The
lock also keeps `seal` from saving a manifest it read before a concurrent
`add` committed (which would erase that document), and makes a checkpoint
describe the committed state rather than an unlocked read.

### Revocation anchoring (checkpoint)

The chain above is monotonic and tamper-evident *inside* the namespace, but a
whole-state rollback — restoring a copy taken before the revocation — is still
indistinguishable from a legitimately older namespace, and a holder of a seal
key can rebuild a consistent alternative history. The anchor is a
**checkpoint**: a document signed by the org identity key that commits to the
revocation history (`revocation_generation`, `revocations_hash`) and to the
head (`head_seq`, `head_mac`), published outside the namespace.

```bash
# Emit (or re-emit) the checkpoint for the current state.
pd checkpoint --org-nsec-file org-identity.nsec --out ./anchor/revocations.json

# Hand it to a verifier — a path or an https URL. No value is typed by hand:
# the operator supplies a *source*, the tool reads the current value.
pd verify --org-pubkey <npub> --checkpoint https://…/revocations.json --root ./docs
```

Checks, all fail-closed:

- the checkpoint signature must verify against the trusted `--org-pubkey`;
- the anchored generation must not exceed the manifest's, and the manifest's
  first `generation` revocation records must hash to the anchored value (so a
  deleted or rewritten revocation fails);
- the manifest head must not be *older* than the anchored head, and must carry
  the anchored `head_mac` when it is at the anchored sequence (so a rolled-back
  copy fails — this is what replaces a hand-supplied `--expected-head-seq`);
- a namespace that records revocations is **refused** when no checkpoint is
  supplied: revocations are not trusted on the namespace's word alone.

Publish the checkpoint to a medium the namespace writer cannot rewrite (an
append-only mirror, a relay, or a cron job that PUTs to `--publish-url`), and
keep it outside the namespace being verified.

**Residual limit (documented):** erasing the *entire* revocation history is a
whole-state rollback. It is caught by the anchor above when a checkpoint is
supplied; without one, `verify` cannot tell the copy from a namespace that
never revoked — the same day-to-day limit SPEC §6.2 accepts for the head
rollback defense, and why the checkpoint is required for high-assurance
verification.

**Verification** — `verify --org-pubkey` checks the head seal against the seal
key that was valid *at the seal timestamp* (analogous to `key_valid_at` for
actors, #76), not against a single fixed key, and re-verifies the whole
history.

**What cannot rotate** — `org_pubkey` itself is part of `root_mac`, so
rotating it is a namespace re-issue (`pd init`), never a header edit.

## 4. Status

- **Implemented (issue #104):** seal-key history (`manifest.seals`),
  identity-key delegations (`manifest.sealKeys`), the append-only revocation
  chain (`manifest.revocations`) with org-signed checkpoints, `pd seal`
  rotation (and refusal to seal with an unauthorized key),
  `pd revoke-seal-key` (permanent), `pd seal-keys`, `pd checkpoint`, and
  `pd verify` checking the seal key valid at the seal timestamp, re-verifying
  every recorded seal, and enforcing `--checkpoint` when revocations exist.
- **Legacy (pre-#104) manifests:** carry no history, so `verify` keeps the
  original rule — the single `sealPubkey` must be the org identity key.
- **Out of scope:** encryption-at-rest (SPEC §14, decision 4).

## 5. Reference

- `pd seal`, `pd revoke-seal-key`, `pd seal-keys`, `pd checkpoint`,
  `verify --org-pubkey --checkpoint` — phantomdocs/src/phantomdocs/cli.py
- seal-key and revocation helpers (`record_seal_event`, `seal_key_valid_at`,
  `append_revocation`, `revocations_hash`, `revocation_chain_issues`) —
  phantomdocs/src/phantomdocs/manifest.py
- `seal_envelope` / `sign_seal` / `verify_seal` / `delegation_envelope` /
  `sign_delegation` / `verify_delegation` / `revocation_envelope` /
  `sign_revocation` / `verify_revocation` / `checkpoint_envelope` /
  `sign_checkpoint` / `verify_checkpoint` — phantomdocs/src/phantomdocs/signing.py
- `root_mac` — phantomdocs/src/phantomdocs/identity.py
