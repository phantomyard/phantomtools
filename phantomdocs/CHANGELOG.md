# Changelog

All notable changes to PhantomDocs are documented in this file.

## Unreleased

- **Category catalog enforcement:** direct access-level and role/actor grants
  cannot authorize reads or writes to undeclared categories. Removing a catalog
  entry now denies stale direct grants. Completes test catalogs and adds access
  and document-read regressions; no document classification or history changes.

- **Strict schema version type:** reject YAML booleans and floats that compare
  equal to `1`; only the integer `version: 1` is supported. Adds direct validation
  and file-loading regressions without changing document categories or history.

- **Workflow review fixes**: CI now covers main pushes and pull requests;
  reference deduplication accepts logical folder slugs as well as paths; runtime
  inventory excludes prefixed credential/service-account/token filenames; generated
  workflow wrappers derive actor/org options from Click metadata. Documents
  inactive AU bindings, approved Drive intake prerequisites, strict MIME/redirect
  behavior, protocol compatibility and preflight ledger locking.
  Corrects the AU meeting example to Jibri's `video/mp4` and documents the required
  canonical raw-intake upload change for ALMAPONIA before workflow activation.

- **Declared PhantomView installation map** (`pd workflow installation-map`):
  exports protocol/artifact/tool dependencies in the collector's v1 manifest,
  including the independent archiver before indexing processed results. Preserves
  an explicit base manifest, rejects conflicts and collector limit violations,
  and exports no job observations or document references. Adds inactive AU
  candidate byte pins from the operator's 2026-10-12 inventory and records runtime
  gaps; no deployment configuration or private UI is changed.

- **Read-only runtime inventory** (`pd workflow inventory`): observes hashes and
  file metadata for explicitly declared tools/probes/resources, with optional
  in-memory configuration overlays. Reports missing, zero, mismatched or changing
  pins and private-directory blockers without executing tools, scanning trees,
  reading credential-named files or writing state. Matching observations still
  require operator review and do not claim runtime compatibility/activation.

- **Installed processing adapter** (`pd workflow prepare`): generic private
  staging, authorized intake membership/MIME/size checks, WAV/text/PDF validators
  and pinned media-probe support. Runs explicitly pinned transcription, summary
  and branding argv with output validation and durable backend receipts. Lost
  journal responses reconcile without rerunning tools; uncertain files require
  review. Processing profiles/resources are installation-pinned and an inactive
  AU scaffold is included. Stops at awaiting-archive; no upload/index or live
  configuration is enabled automatically.

- **Separated processing/archive/index handover**: `WorkflowExecutor.prepare`
  exports authoritative processed-results manifests before indexing. Adds the
  additive meeting-processing protocol, `pd workflow results/archive-receipt/
  index-archive`, strict archive receipts bound to completed results, actual Drive
  parent/size checks and SHA-256 verification before node creation. Confirmed
  references skip downloads/hashing on retries, including recovery after a lost
  index response. The independent archiver retains naming/sequence/publication
  authority; no upload, rename, move, deletion or Drive operation-ID API is added.
  Archive observations feed the existing graph export; no UI is activated.

- **Workflow executor and reconciliation boundary**: a trusted adapter API runs
  pinned protocols with durable started/completed receipts, host-local runner
  locking and crash reconciliation. Uncertain operations are never repeated
  without a confirmed backend receipt. Replanning is blocked once execution
  begins. Adds a hash-pinned, timeout-bound local argv runner with source/output
  checks, and authorized observed dependency export through workflow status.
  No AU adapter, Drive publisher or PhantomView UI is activated automatically.
- **Reference deduplication before reads** (`pd add --skip-existing-ref`):
  atomically checks existing refs before download/hash and requires explicit
  category/owners. Conflicting classification, destination or document identity
  fails closed. Existing add/version behavior is retained without this option.

- **Authenticated workflow intake and durable retry records**:
  `pd workflow sign-request/accept/status` bind request intent and explicit UTC
  validity to the requester's existing declared Nostr key. A private SQLite
  ledger commits accepted plans and events atomically, rejects conflicting
  request ids, preserves processor/policy pins on identical retries, and only
  refreshes plans through an explicit authorized `--replan` without changing
  classification or owners. Status is read-only and category-filtered. Adds
  pure argument-list construction and reviewed AU transformation descriptors;
  no upload/cleanup orchestrator, tool execution, inbox scan or live deployment
  is enabled. Uses existing BIP-340 signing/key-lifecycle primitives.

- **Versioned document workflow planning** (`pd workflow validate/plan`):
  additive read-only JSON contracts, installed protocol instructions, validated
  dependency graphs, and capability-to-tool bindings for arbitrary document
  media types. Plans resolve a configured Phantombot default persona, validate
  its declared tools and PhantomOrg category/ownership authorization, and pin
  policy, protocol, instructions, installation and request hashes. Missing tools
  block a plan; no tools or document bytes are executed/read. Declared graph
  export supports a future observatory adapter. Includes AU meeting policy
  examples, shared package assets, and nested persona-wrapper support. This
  planning layer does not execute tools, scan inboxes, apply branding, route
  Drive files, or implement invitee-only access. Signed admission is described
  separately above.

**Hierarchical categories (#31), index by reference (#35), update package (#33), mutation signing (#30 v2).**

- **Store files keep their permissions across an atomic replace**
  (`manifest.save`, `audit.truncate`): `mkstemp` creates the temp file 0600, so
  renaming it over the manifest or the audit log silently dropped any group
  access an operator had granted — a store shared through a group (a
  multi-persona deployment) stopped being writable for every account but the
  last writer. The destination's mode is now carried onto the temp file before
  the rename (`fsutil.preserve_mode`).

- **Hierarchical / project-scoped categories** (`access.py`, `cli.py`):
  category ids are now `category-...` strings with a project umbrella
  (`category-4` → `category-4-almaponia`). Holding a parent grants every
  descendant (prefix rule); per-project grants live at the actor level
  (`actor_exceptions`), not the shared role. `resolved_categories` returns
  strings; ints are normalized on read (backward compatible).
- **Index by reference** (`storage.py::read_reference`, `cli.py`):
  `pd add --ref gdrive://… | file://… | ssh://…` indexes an external object
  without copying it into the content-addressed store. `pd get` / `pd verify`
  re-read the reference to serve bytes / check integrity. `ssh://` references
  store the full canonical URI (host/user/port preserved) and shell-quote the
  remote path, so a reference path with metacharacters is read literally and
  round-trips through `get`/`verify`.
- **§13 update package** (`setup.py`, `cli.py::setup`): `pd setup` renders
  `kb/procedures/Documents.md`, a `MEMORY.md` pointer and `tools/documents.sh`
  (wrapper pinning `--actor`), all bounded by `<!-- phantomdocs:start/end -->`
  markers; idempotent and re-runnable, preserving persona-authored content.
  The wrapper injects `--actor`/`--org-yaml` **after** the subcommand (they are
  command-local Click options) and only for subcommands that accept them.
- **Product decision — no phantombot-side hooks** (`docs/SPEC.md` §9/§13,
  `setup.py`, `cli.py`): the phantombot-side `PHANTOMDOCS_ACTOR` injection is
  dropped. PhantomDocs stays fully additive: the per-persona
  `tools/documents.sh` wrapper pins `--actor`, and `PHANTOMDOCS_ACTOR` remains
  an operator-supplied override — neither requires any change to phantombot.
- **Mutation signing (#30 v2)** (`signing.py`, `cli.py`, `audit.py`): a
  mutating command may sign the node with the actor's Nostr nsec
  (`PHANTOMDOCS_NSEC` / `--nsec-file`); the BIP-340 Schnorr signature + x-only
  pubkey are recorded on the node and audit entry. The signed message is the
  **canonical mutation envelope** (MAC + actor + action + category + owners +
  locations + urn), so the signature binds authorship and the authorization
  metadata. `pd verify --org-yaml` verifies each signature over the
  reconstructed envelope and rejects a key that does not match the specific
  actor recorded on the node (no cross-actor impersonation; changing
  category/owners after signing invalidates it).
- **Signed refs (#30 v2)** (`cli.py::tag`/`verify`, `manifest.py`): a signed
  `tag` stores a signed record (`mac` + `actor` + `sig`/`sigPubkey`) whose
  signature binds the **ref name** to its target MAC (the ref name is part of
  the canonical envelope). `pd verify --org-yaml` verifies each signed ref, so
  renaming or repointing `manifest.refs` after tagging invalidates the
  signature and fails verification.

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
