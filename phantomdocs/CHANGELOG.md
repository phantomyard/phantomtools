# Changelog

All notable changes to PhantomDocs are documented in this file.

## Unreleased

**Hierarchical categories (#31), index by reference (#35), update package (#33), mutation signing (#30 v2).**

- **Hierarchical / project-scoped categories** (`access.py`, `cli.py`):
  category ids are now `category-...` strings with a project umbrella
  (`category-4` → `category-4-almaponia`). Holding a parent grants every
  descendant (prefix rule); per-project grants live at the actor level
  (`actor_exceptions`), not the shared role. `resolved_categories` returns
  strings; ints are normalized on read (backward compatible).
- **Index by reference** (`storage.py::read_reference`, `cli.py`):
  `pd add --ref gdrive://… | file://… | ssh://…` indexes an external object
  without copying it into the content-addressed store. `pd get` / `pd verify`
  re-read the reference to serve bytes / check integrity.
- **§13 update package** (`setup.py`, `cli.py::setup`): `pd setup` renders
  `kb/procedures/Documents.md`, a `MEMORY.md` pointer and `tools/documents.sh`
  (wrapper pinning `--actor`), all bounded by `<!-- phantomdocs:start/end -->`
  markers; idempotent and re-runnable, preserving persona-authored content.
- **Product decision — no phantombot-side hooks** (`docs/SPEC.md` §9/§13,
  `setup.py`, `cli.py`): the phantombot-side `PHANTOMDOCS_ACTOR` injection is
  dropped. PhantomDocs stays fully additive: the per-persona
  `tools/documents.sh` wrapper pins `--actor`, and `PHANTOMDOCS_ACTOR` remains
  an operator-supplied override — neither requires any change to phantombot.
- **Mutation signing (#30 v2)** (`signing.py`, `cli.py`, `audit.py`): a
  mutating command may sign the node MAC with the actor's Nostr nsec
  (`PHANTOMDOCS_NSEC` / `--nsec-file`); the BIP-340 Schnorr signature + x-only
  pubkey are recorded on the node and audit entry. `pd verify --org-yaml`
  verifies signatures and rejects keys not declared as an actor npub.

**PR #27 review hardening — enforce the advertised ACL.**

- **ACL is enforced, not just declared** (`cli.py`): `get`, `search`,
  `versions`, `add`, `mkdir` and `tag` now require `--org-yaml` (the
  authoritative org model) and an authenticated OS actor, and gate every
  read/write on `can_read` / `can_write`. Absent either is denied (fail-closed).
- **Actor identity is the authenticated OS credential** (`cli.py::_os_actor`):
  resolved from the real user id (`pwd.getpwuid(os.getuid())`), never from an
  environment variable or a `--actor` flag — both are caller-forgeable by a
  capable turn (CONTRIBUTING.md §4.2/§4.3). The `--actor` flag is removed from
  ACL-gated commands (the `acl` introspection command still takes `--actor` as
  a resolution query, which is not an authorization path).
- **Write-scope enforces SPEC §9** (`access.py::can_write`): write requires an
  explicit, non-empty `owners` list plus read access, and `owners` is matched by
  actor id **or** role id. No owners → denied (the §9 reporting-chain default is
  deferred rather than re-derived).
- **Audit records the authenticated actor** (`cli.py`): ACL-gated commands pass
  the verified OS actor into the hash-chained log, not a self-asserted label.
- **Cross-org root collision fixed** (`identity.py`): `root_mac` is now
  `H(len(org_id)||org_id||len(pubkey)||pubkey||len(namespace)||namespace)`,
  so two orgs can never collide under the documented defaults. Folder/doc
  components are length-prefixed too.
- **Manifest mutations serialized** (`manifest.py`): `save` uses a unique
  `mkstemp` file, and mutating commands hold an inter-process lock
  (`manifest.lock`) across the full read-modify-write, so concurrent personas
  no longer lose updates.
- **`local://<root>` two-slash URI fixed** (`storage.py`): `urlparse` puts the
  root in `netloc`; it is now recombined with `path` instead of silently
  resolving to the current directory.
- **Integrity on the read path** (`storage.py`): `LocalBackend.get` and
  `SshBackend.get` re-hash the bytes and refuse a mutated blob.
- **Hash-chained audit log** (`audit.py`): each entry carries `prev` = SHA-256
  of the previous line; `pd verify` walks the chain and reports tampering.
- **Honest wording** (`README.md`, `docs/SPEC.md`): the unkeyed chain is
  described as detecting corruption/accidental divergence, with
  authenticity/tamper-evidence deferred to the optional §4.3 HMAC.

## [0.3.0] - 2026-08-19

- Per-URN versioning: re-`add` with new content creates a new version (MAC)
  linked via `previous`; identical content is a no-op (`unchanged`).
- `pd versions` — list a document's MAC history (current marked).
- `pd get --mac` — retrieve a specific version.
- `refs` now point at version MACs (not URNs); `pd verify` checks version links.

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
