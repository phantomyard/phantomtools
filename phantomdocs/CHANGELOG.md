# Changelog

All notable changes to PhantomDocs are documented in this file.

## [0.2.0] - 2026-08-19

- Folders (`pd mkdir`) — the hierarchical chained-MAC tree.
- Refs (`pd tag` / `pd refs`) — mutable version pointers.
- Append-only audit log (`pd audit`).
- `pd derive-manifest` — derive the manifest from a PhantomOrg `org.yaml`.
- Storage adapters `ssh://` and `gdrive://` (delegates to the persona's `workspace.py`).
- `pd update` — self-update check (exit 0 up-to-date / 1 available / 2 error).
- `--backend` URI wiring on `add`/`get`/`verify`; `--actor` on mutating commands.

## [0.1.0] - 2026-08-19

Initial scaffold:

- Chained identity headers (MAC) — SHA-256 hash chain with inheritance (spec §4).
- Self-describing identifiers (`sha2-256-256` / `sha2-256-128`, RFC 6920 / multihash).
- Manifest-driven namespace (YAML, single-tenant v1).
- `local://` content-addressed blob store.
- CLI `pd`: `init`, `add`, `get`, `search`, `verify`, `acl`, `status`.
- Access control resolved from a PhantomOrg `org.yaml` (fail-closed; spec §9).
