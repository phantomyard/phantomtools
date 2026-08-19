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
- **Location** — a `local://` content-addressed blob store today; `ssh://` and
  `gdrive://` adapters are stubbed for the next release.
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
# Create a namespace
pd init --org my-org --root ./docs

# Ingest a document (local:// blob store)
pd add ./report.pdf --slug "reports/2026-08-19-q3.pdf" --category 2 --root ./docs

# Resolve / retrieve
pd get "reports/2026-08-19-q3.pdf" --root ./docs
pd get "reports/2026-08-19-q3.pdf" --cat --root ./docs

# Search the index
pd search "q3" --root ./docs

# Verify integrity (MAC chain + content hashes)
pd verify --root ./docs

# Resolve an actor's access from a PhantomOrg org.yaml
pd acl --org-yaml organizations/<org>/org.yaml --actor cfo --category 2

# Summary
pd status --root ./docs
```

## Repository layout

```
phantomdocs/
├── README.md
├── LICENSE            # MIT
├── CHANGELOG.md
├── pyproject.toml     # package phantomdocs v0.1.0, Python ≥3.10, PyYAML + click
├── install.sh         # portable install (symlinks bin/ to PATH)
├── bin/               # CLI wrappers: pd, phantomdocs (+ .cmd for Windows)
├── docs/SPEC.md       # specification
├── examples/          # reference manifest (org-agnostic placeholders)
├── src/phantomdocs/   # package: identity, manifest, storage, access, cli
├── tests/             # unit + smoke tests
└── .github/workflows/ci.yml  # lint (ruff/bandit) + tests + smoke
```

## Status

- **2026-08-19** — v0.1.0 scaffold: chained MAC identity, manifest,
  `local://` store, `pd init/add/get/search/verify/acl/status`, ACL resolved
  from PhantomOrg `org.yaml`, smoke-tested. `ssh://`/`gdrive://` adapters,
  folders, refs (version pointers) and the audit log are the next increments.

## License

MIT — see [LICENSE](LICENSE).
