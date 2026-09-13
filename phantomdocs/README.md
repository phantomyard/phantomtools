# PhantomDocs

**Agnostic document management for PhantomOrg-provisioned personas.**

PhantomDocs gives AI personas of any PhantomOrg-provisioned organization a
self-managed document store with **identity, integrity and access control**.
It is **standalone**: it consumes PhantomOrg but does not modify it.

- **Identity** — every node carries a chained, self-describing header (MAC).
- **Integrity** — content is hash-bound; `pd verify` re-checks the chain and
  detects **corruption and accidental divergence** (the chain is unkeyed;
  authenticity/tamper-evidence lands with the optional HMAC in SPEC §4.3).
- **Access control** — resolved from a PhantomOrg `org.yaml` (access levels,
  security categories, role/actor exceptions) and **enforced** on every read
  and write (`get`, `search`, `versions`, `add`, `mkdir`, `tag`). The actor is
  the authenticated OS account; writes require an explicit `owners` scope.
  Fail-closed: no rule → denied.
- **Location** — content-addressed blob stores: `local://` filesystem, `ssh://`
  remote, `gdrive://` (delegates to the persona's `workspace.py`).
- **Search** — index search over the manifest (filtered by the reader's access).

The operating model is **git / GitHub** (see `docs/SPEC.md` §5): the chained
MAC is exactly git's commit parent-chain, "refs" are branches/tags, and the
PhantomOrg↔PhantomDocs split mirrors org↔repo (CODEOWNERS).

## Manifest-driven

Zero hardcoded values. Everything lives in a per-namespace YAML manifest
(`manifest.yaml`), derived from a PhantomOrg org model:

```yaml
manifest:
  version: 1
  org: example-org
  namespace: docs
  tenant: single          # v1 fixed; "multi" reported as unsupported
  rootMac: "..."          # H(org_pubkey || namespace)
refs: {}
nodes: []
```

See `examples/example-org.yaml` and `docs/SPEC.md`.

## Usage

```bash
# Create a namespace (or derive it from a PhantomOrg org model)
pd init --org my-org --root ./docs
pd derive-manifest --org-yaml organizations/<org>/org.yaml --out ./docs/manifest.yaml

# Access-controlled commands require the actor + the org model. The actor is
# the authenticated OS account the persona runs under (never an env var or a
# flag): the OS username must be a declared actor `id` in org.yaml.
# PhantomOrg deploys each persona under its own OS account.

# Create a folder, then ingest a document under it. Writes require --owners
# (a PhantomOrg role id or actor id).
pd mkdir --name reports --owners cfo --org-yaml organizations/<org>/org.yaml --root ./docs
pd add ./report.pdf --slug "reports/2026-08-19-q3.pdf" --category 2 \
  --owners cfo --folder reports --org-yaml organizations/<org>/org.yaml --root ./docs

# Ingest to a remote / cloud backend
pd add ./report.pdf --slug "reports/q3.pdf" --owners cfo --backend ssh://user@vps:22/var/phantomdocs \
  --org-yaml organizations/<org>/org.yaml --root ./docs
pd add ./report.pdf --slug "reports/q3.pdf" --owners cfo --backend gdrive:// \
  --org-yaml organizations/<org>/org.yaml --root ./docs

# Resolve / retrieve (by urn, path, slug, or ref name)
pd get "reports/2026-08-19-q3.pdf" --org-yaml organizations/<org>/org.yaml --root ./docs
pd get "reports/2026-08-19-q3.pdf" --cat --org-yaml organizations/<org>/org.yaml --root ./docs

# Search the index (filtered by the reader's access)
pd search "q3" --org-yaml organizations/<org>/org.yaml --root ./docs

# Version pointers (refs)
pd tag latest "reports/2026-08-19-q3.pdf" --org-yaml organizations/<org>/org.yaml --root ./docs
pd refs --root ./docs

# Version history (re-add with new content creates a new version)
pd versions "reports/2026-08-19-q3.pdf" --org-yaml organizations/<org>/org.yaml --root ./docs
pd get "reports/2026-08-19-q3.pdf" --mac <mac> --cat --org-yaml organizations/<org>/org.yaml --root ./docs

# Verify integrity (MAC chain + content hashes + audit chain)
pd verify --root ./docs

# Resolve an actor's access from a PhantomOrg org.yaml
pd acl --org-yaml organizations/<org>/org.yaml --actor cfo --category 2

# Audit trail + summary
pd audit --root ./docs
pd status --root ./docs

# Self-update check
pd update --repo owner/phantomdocs
```

## Repository layout

```
phantomdocs/
├── README.md
├── LICENSE            # MIT
├── CHANGELOG.md
├── pyproject.toml     # package phantomdocs, Python ≥3.10, PyYAML + click
├── install.sh         # portable install (symlinks bin/ to PATH)
├── bin/               # CLI wrappers: pd, phantomdocs (+ .cmd for Windows)
├── docs/SPEC.md       # specification
├── docs/CONSISTENCY.md        # durability: commit order, crash windows, recovery
├── docs/CRYPTO-AGILITY.md     # crypto-suite versioning
├── docs/SECURITY-TRUST-ANCHOR.md  # trust anchor + seal-key lifecycle
├── examples/          # reference manifest (org-agnostic placeholders)
├── src/phantomdocs/   # identity, manifest, storage, access, audit, derive, update, cli
├── tests/             # unit + smoke tests
└── .github/workflows/ci.yml  # lint (ruff/bandit) + tests + smoke
```

## Durability and recovery

A mutation is blob → audit append → manifest commit, in that order, with a
single ordered crash window rather than a distributed transaction. The only
recoverable state is an **orphaned audit tail** (the audit entry landed, the
manifest commit did not):

```bash
# Discard the orphaned tail and re-align the audit log with the manifest.
pd recover --root ./docs
```

`pd verify` detects that window, and `pd recover` repairs it; the next
mutation also self-heals automatically. A crash *during* the audit append is a
separate, **fail-closed** window: it can leave a short or torn final line, and
`pd recover` refuses it — as it refuses a broken hash chain, the log *behind*
the manifest, or a head-hash mismatch — because a torn write and an edit are
indistinguishable; that one needs an operator repair. See
[`docs/CONSISTENCY.md`](docs/CONSISTENCY.md) for the artifact model, the exact
failure windows, and what is atomic vs eventually consistent.

## Status

- **2026-08-19** — v0.3.0: per-URN versioning (`previous` chain, `pd versions`,
  `pd get --mac`, refs → MACs) on top of v0.2.0 (folders, refs, audit log,
  `derive-manifest`, `ssh://`/`gdrive://`, `pd update --check`).
  `ssh://`/`gdrive://` live I/O needs a reachable host / the persona's
  `workspace.py`; `pd update` install lands once the tool is published as a
  release.

## Large documents

CLI document operations use temporary disk snapshots and 1 MiB copy/hash
chunks instead of buffering entire documents in Python memory. This covers
`add` (including references), `get --cat`, `verify`, and rollback reads.
SSH document transfers use file descriptors; GDrive uploads and read-back
verification use files. Hashes and version MACs retain their existing format.

Allow temporary disk space proportional to document size (multiple copies
can coexist during a transfer). Downloads are verified before `get --cat`
emits document bytes. Temporary files are closed after use, including failed
operations. The persona's external GDrive tool controls its own memory use.

Library callers can pass an owned `FileContent` snapshot to `add_document`
or backend `put`, and request `streaming=True` from reference/location reads
and local/SSH `get`. Close returned snapshots with a context manager. Existing
byte-based calls remain supported and still allocate the full result.

## Storage recovery

For stored local/SSH blobs, `get --cat`, `verify`, and `rollback` honor an
explicit `--backend` as a replacement content-addressed store. Without that
option they use the manifest's recorded location. If a recorded local blob
path is missing, they also support recovery from `<root>/blobs/<prefix>/<hash>`
under the current `--root`. A present but corrupt or unreadable original is
reported rather than silently replaced; use `--backend` to select a backup.
Restored bytes are hash-checked before use.

Reads do not rewrite stored locations, signatures, or version MACs. A rollback
still appends a new version. File/SSH/GDrive external references remain pinned
to their exact URI and are not redirected by `--backend`. Get and rollback
try the next replica when a location is malformed; verify reports each failed
location and continues checking the others.

## Dependency

PhantomDocs consumes the PhantomOrg `org.yaml` schema (org/version 1:
`policies.access_levels`, `policies.security_categories`, `roles`, `actors`,
`organization.id`). It requires a PhantomOrg org model with that shape;
`pd derive-manifest` reads `organization.id` and validates the access model at
resolution time.

**Schema version contract:** PhantomDocs requires `version: 1` (top-level
integer) in the `org.yaml`, matching PhantomOrg's `Organization.version`
model. The version is enforced fail-closed on every `org.yaml` load (all
ACL-gated commands and `derive-manifest`): a missing or unknown `version` is
refused rather than assumed compatible. A future PhantomOrg schema bump must
be accommodated here explicitly — do not silently accept a newer schema.

## License

MIT — see [LICENSE](LICENSE).
