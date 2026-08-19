# PhantomDocs

**Agnostic document management for PhantomOrg-provisioned personas.**

PhantomDocs gives AI personas of any PhantomOrg-provisioned organization a
self-managed document store with hard guarantees of **identity, integrity and
access control**. It is **standalone**: it consumes PhantomOrg but does not
modify it.

- **Identity** — every node carries a chained, self-describing header (MAC).
- **Integrity** — content is hash-bound; `pd verify` re-checks the chain.
- **Access control** — resolved from a PhantomOrg `org.yaml` (access levels,
  security categories, role/actor exceptions); PhantomDocs only declares each
  node's `category`. Fail-closed: no rule → denied.
- **Location** — content-addressed blob stores: `local://` filesystem, `ssh://`
  remote, `gdrive://` (delegates to the persona's `workspace.py`).
- **Search** — index search over the manifest.

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

# Create a folder, then ingest a document under it
pd mkdir --name reports --root ./docs
pd add ./report.pdf --slug "reports/2026-08-19-q3.pdf" --category 2 --folder reports --root ./docs

# Ingest to a remote / cloud backend
pd add ./report.pdf --slug "reports/q3.pdf" --backend ssh://user@vps:22/var/phantomdocs --root ./docs
pd add ./report.pdf --slug "reports/q3.pdf" --backend gdrive:// --root ./docs

# Resolve / retrieve (by urn, path, slug, or ref name)
pd get "reports/2026-08-19-q3.pdf" --root ./docs
pd get "reports/2026-08-19-q3.pdf" --cat --root ./docs

# Search the index
pd search "q3" --root ./docs

# Version pointers (refs)
pd tag latest "reports/2026-08-19-q3.pdf" --root ./docs
pd refs --root ./docs

# Version history (re-add with new content creates a new version)
pd versions "reports/2026-08-19-q3.pdf" --root ./docs
pd get "reports/2026-08-19-q3.pdf" --mac <mac> --cat --root ./docs

# Verify integrity (MAC chain + content hashes)
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
├── examples/          # reference manifest (org-agnostic placeholders)
├── src/phantomdocs/   # identity, manifest, storage, access, audit, derive, update, cli
├── tests/             # unit + smoke tests
└── .github/workflows/ci.yml  # lint (ruff/bandit) + tests + smoke
```

## Status

- **2026-08-19** — v0.3.0: per-URN versioning (`previous` chain, `pd versions`,
  `pd get --mac`, refs → MACs) on top of v0.2.0 (folders, refs, audit log,
  `derive-manifest`, `ssh://`/`gdrive://`, `pd update --check`).
  `ssh://`/`gdrive://` live I/O needs a reachable host / the persona's
  `workspace.py`; `pd update` install lands once the tool is published as a
  release.

## License

MIT — see [LICENSE](LICENSE).
