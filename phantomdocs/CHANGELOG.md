# Changelog

All notable changes to PhantomDocs are documented in this file.

## [0.1.0] - 2026-08-19

Initial scaffold:

- Chained identity headers (MAC) — SHA-256 hash chain with inheritance (spec §4).
- Self-describing identifiers (`sha2-256-256` / `sha2-256-128`, RFC 6920 / multihash).
- Manifest-driven namespace (YAML, single-tenant v1).
- `local://` content-addressed blob store; `ssh://` / `gdrive://` stubbed.
- CLI `pd`: `init`, `add`, `get`, `search`, `verify`, `acl`, `status`.
- Access control resolved from a PhantomOrg `org.yaml` (fail-closed; spec §9).
