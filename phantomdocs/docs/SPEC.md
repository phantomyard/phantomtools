# PhantomDocs — Specification v0.1 (agnostic)

**Status:** Draft for review — 2026-08-19
**Project language:** English (all code, docs and comments)

---

## 1. Purpose

PhantomDocs is a **self-contained document management tool** for the
phantomyard ecosystem. It gives phantombot personas of any
PhantomOrg-provisioned organization the ability to **autonomously manage
their entity's documents and document requests**, with guarantees of:

- **Identity** — every document and folder carries a unique, verifiable
  identifier.
- **Integrity** — content is hash-bound; corruption and accidental
  divergence are detectable by re-verification. (The chain is unkeyed:
  authenticity/tamper-evidence is the optional HMAC in §4.3.)
- **Access control** — resolved from PhantomOrg (access levels, security
  categories, role/actor exceptions) and **enforced** on every read/write;
  PhantomDocs only declares each node's classification.
- **Location** — every node maps to a physical path or URL, independent of
  backend and operating system.
- **Search** — index-, full-text- and backend-native search.

It is **standalone** (a tool subdirectory, same pattern as PhantomOrg /
PhantomBridge / PhantomMeet): it *consumes* PhantomOrg but does not modify it.

## 2. Concepts (generic)

| Concept | Meaning |
|---|---|
| **Namespace** | The document tree of one organization (e.g. `ecda`) |
| **Node** | A folder or a document in the tree |
| **Identity header (MAC)** | The per-node cryptographic identifier in the chained identity scheme (§4) — a SHA-256 hash-chain header (not a Message Authentication Code; see §4.1) |
| **Content hash** | SHA-256 of a document's bytes |
| **Ref** | A mutable pointer to a version MAC (e.g. `latest`, `approved-<date>`) — the `git` branch/tag equivalent |
| **Manifest** | The single source of truth mapping identity → location → metadata → classification → relations |
| **Backend** | A storage adapter (`local`, `ssh`, `gdrive`) that reads/writes blobs |
| **Blob** | The raw bytes of a document, addressed by its content hash |
| **Actor** | A persona (resolved via PhantomOrg) performing an operation |
| **Category** | A document's security classification, referencing PhantomOrg's `security_categories` |

## 3. Architecture (generic)

```
┌────────────────────────── phantomdocs CLI (pd) ──────────────────────────┐
│  init · derive-manifest · add · get · search · verify · acl · status     │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  reads org model (roles/actors/access levels/categories)
                ▼  from PhantomOrg org.yaml — access is resolved, not redefined
┌────────────────────────── Manifest (per namespace) ───────────────────────┐
│  nodes: urn · MAC · parent MAC · kind · slug · contentHash · locations   │
│          · meta · category · relations                                   │
│  audit: append-only operation log                                        │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  blobs (bytes) addressed by content hash
                ▼
┌────────────────────────── Storage adapters (OS-agnostic) ─────────────────┐
│  local://  filesystem        ssh://  remote host        gdrive:// Drive   │
└───────────────────────────────────────────────────────────────────────────┘
```

## 4. Identity model: chained headers (MAC)

Every node's identifier **inherits its parent's header and appends its own** —
a hash chain with inheritance:

```
H(x) = SHA-256(x)

root MAC   = H( org_pubkey || namespace )      # tree root (org + namespace)
node MAC   = H( parent_MAC || component )      # inherit parent + append own

component(folder) = slug                       # folders are structural
component(doc)    = slug || H(content)         # docs bind name + content
```

Properties this provides:

- **Lineage embedded** — a node's MAC encodes its full path (chain of parents).
  To locate a node you walk the tree; to verify it you recompute from the root.
- **Tamper cascade** — altering a parent (rename / move / corruption)
  invalidates the MACs of all its descendants automatically. Fail-closed by
  construction.
- **Content binding** — a document's component includes `H(content)`, so its
  identity is bound to its bytes; any edit yields a new MAC.
- **Deterministic and auditable** — every chain segment maps to a readable slug.

### 4.1 Terminology note

"MAC" here means the **identity header** — a SHA-256 hash-chain value that
identifies a node — *not* a Message Authentication Code. (RFC 6920 calls the
general idea "naming things with hashes"; the only true MAC in this design is
the optional HMAC authenticity signature in §4.3.)

Each node is addressed at two levels:

| Level    | Form                                              | Consumer |
|----------|---------------------------------------------------|----------|
| Logical  | `actas/2026-08-19-reunion-junta.md`               | humans   |
| Identity | `urn:<org>:doc:<path>` + `sha2-256-256:<64 hex>`  | bots     |

### 4.2 Identifier format (self-describing)

Identifiers are **self-describing**, following the `multihash` / RFC 6920
convention `<algorithm>-<bits>:<hex>` rather than a bare truncated hash, so the
scheme can evolve without lock-in:

- **Full (verification):** `sha2-256-256:<64 hex>` — the whole MAC, used for
  integrity checks and stored in the manifest.
- **Display (human):** `sha2-256-128:<32 hex>` — a 128-bit truncation for
  readable short forms (RFC 6920 registers `sha-256-128` for exactly this).
- **Truncation floor:** never truncate below 128 bits for anything security-
  relevant (a 64-bit "short id" has a ~2^32 birthday bound and is display-only,
  if used at all).

This mirrors `multihash` (`<fn-code><length><digest>`) and RFC 6920's `ni://`
scheme, and the two-tier split (stable logical name + content-derived ID) is
the same pattern as `git` refs/SHAs, IPFS paths/CIDs, and Software Heritage
SWHIDs (`swh:1:<type>:<hash>`).

### 4.3 Authenticity & aggregation (optional)

- **Authenticity** — the manifest's root MAC is signed with an HMAC using the
  entity key (same envelope-MAC pattern as PhantomBridge), proving origin and
  integrity.
- **Merkle aggregation (v2)** — a folder's MAC aggregates the hashes of its
  children so a folder also "summarizes" its whole subtree. Not required for
  v1; the chain already covers identity + per-document verification.

## 5. Operating model: GitHub/git (reference)

GitHub is the reference operating model: git is already a **content-addressed
Merkle DAG**, so it validates the chained-MAC identity scheme and gives a proven
vocabulary for every other PhantomDocs concern.

| GitHub / git concept      | PhantomDocs equivalent                                  |
|---------------------------|---------------------------------------------------------|
| repository                | namespace                                               |
| directory / file          | folder / document                                       |
| blob (SHA of content)     | blob (SHA-256 of content)                               |
| commit (parent + tree)    | chained MAC: `H(parent_MAC ∥ component)`               |
| history / DAG             | versioning + audit trail                                |
| branch / tag              | logical pointers to versions                            |
| org teams & members       | PhantomOrg roles & actors (identity)                    |
| CODEOWNERS (per path)     | per-node classification + optional owners (permissions) |
| pull request review       | document request → review → approve → merge workflow    |
| issues                    | user document requests (solicitudes)                    |
| blame / history           | audit log (`who changed what, when`)                    |
| remote + local            | agnostic backends (`local://`, `ssh://`, `gdrive://`)   |

The **pull-request flow** is the operational answer to the goal of autonomous
request handling: a user opens an *issue* (request) → a persona reviews/edits a
*branch* → approval → *merge* (commit) → history updated (audit). The org↔repo
split (org owns teams, repo maps teams→permissions via CODEOWNERS) is exactly
the PhantomOrg↔PhantomDocs split of §9.

## 6. Manifest-driven design

All organization-specific values live in a single YAML manifest per namespace.
PhantomDocs ships with **zero hardcoded values**.

```yaml
manifest:
  version: 1
  org: "ecda"
  tenant: "single"               # v1 fixed; "multi" => error reported below
  rootMac: "a3f1c9..."           # H(org_pubkey || namespace)
  signedRootMac: "..."           # optional HMAC with the entity key

nodes:
  - urn: "urn:ecda:folder:actas"
    mac: "8b2c44..."
    parentMac: "a3f1c9..."
    kind: folder
    slug: "actas"
    category: 1                  # classification → PhantomOrg security_categories

  - urn: "urn:ecda:doc:actas/2026-08-19-reunion-junta.md"
    mac: "9d4e7a..."
    parentMac: "8b2c44..."
    kind: doc
    slug: "2026-08-19-reunion-junta.md"
    contentHash: "e7f0aa..."
    size: 12345
    category: 2                  # confidential — access resolved from org.yaml
    owners: [cfo]                # optional — PhantomOrg role ids allowed to write
    locations:
      - backend: "gdrive"
        url: "https://drive.google.com/file/d/..."
        verifiedAt: "2026-08-19T16:00:00Z"
    meta:
      title: "Board meeting minutes 2026-08-19"
      author: "cfo"
    relations:
      references: ["urn:ecda:doc:normativa/purchasing-policy.md"]
```

### 6.1 Single-tenant v1 (multi-tenancy reported as unsupported)

Version 1 supports exactly **one organization per deployment** (derived from
the PhantomOrg org model). The manifest reserves the `tenant` field so v2 can
add multi-tenancy without breaking the schema. If a manifest declares
`tenant: multi` (or references more than one org namespace), the tool
**reports "multi-tenancy is not supported in this version"** and refuses to
operate (fail-closed). The architecture leaves the slot for v2; the capability
is simply not advertised as available.

## 7. Naming convention

Deterministic, human-readable and bot-parseable:

```
<domain>/<type>/<date>-<slug>.<ext>
```

Examples:

- `minutes/2026-08-19-board-meeting.md`
- `policy/purchasing-policy.md`
- `contracts/2026-08-01-venue-lease.pdf`

Rules:

- lowercase, kebab-case, ASCII (no accents or spaces).
- date in ISO-8601 (`YYYY-MM-DD`).
- `ext` = the real file format.
- **Do not** encode version in the name: real versioning is given by the
  content hash. Slugs must stay stable.
- `domain` and `type` come from a **controlled vocabulary** defined in
  `org.yaml`, so naming is consistent across the whole entity (and machine-
  checkable).

## 8. Storage adapters (agnostic)

A storage-adapter layer. Every node is a **blob** (bytes) + its hash; the
adapter only reads / writes / lists bytes. The OS is irrelevant because
everything flows through this layer.

- `local://` — local filesystem (Linux / macOS / Windows).
- `ssh://` — remote host over SSH (reuses the ecosystem's existing SSH aliases).
- `gdrive://` — Google Drive via OAuth2 (already solved by `gog` /
  `workspace.py`).

A node may declare several `locations` (replicas), each verified by hash.

## 9. Access control (resolved from PhantomOrg)

PhantomOrg is the **authoritative access model**. PhantomDocs does **not**
define its own ACL; it reuses what PhantomOrg already provides and enforces it
on documents.

What PhantomOrg already owns (see `org.yaml` + `compiler/access.py` +
`compiler/scopes.py`):

- **`policies.access_levels`** — RBAC tiers with numeric category sets
  (e.g. `level-3 → [1,2,3]`, `level-2 → [1,2]`, `level-1 → [1]`).
- **`policies.security_categories`** — document classifications
  (`category-1` public, `category-2` confidential, `category-3` credentials /
  sensitive financial, `category-0` absolute exception).
- **`merge_access()`** — hybrid resolution: base RBAC (`access_level`) +
  role exceptions (`security_exceptions`) + actor exceptions (`actor_exceptions`).
- **`derive_scopes()`** — per-actor visibility (rules `chain` / `department`).

What PhantomDocs adds per node is **one field**: `category` — the node's
security classification, referencing PhantomOrg's `security_categories`.

**Enforcement (fail-closed):**

- The **actor identity** is resolved with layered precedence
  (issue #29): an explicit `--actor` flag → the `PHANTOMDOCS_ACTOR` environment
  variable → the OS username (`pwd.getpwuid(os.getuid())`). In the
  PhantomOrg/phantombot deployment model N personas live as directories under
  ONE OS account and phantombot gives focus to one persona at a time, so the OS
  username is **not** the persona identity; the harness (which knows which
  persona has focus) supplies the actor via `--actor` / `PHANTOMDOCS_ACTOR`.
  The OS username is kept only as a fallback for deployments that genuinely run
  one persona per OS account (e.g. the VPS Virtualmin model). The resolved id
  must be a declared actor `id` in `org.yaml`; an unmapped actor is refused.
  Without `--org-yaml` (the authoritative org model) and a resolvable actor,
  read and write are denied — never fail-open.

**Threat model (issue #30):** integrity and authorization are two different
guarantees, and only one of them is cryptographically enforced.

- **Integrity is guaranteed by the MAC chain.** Every node's identity is a
  chained hash (`H(parent_MAC ∥ component)`) and every document binds
  `H(content)`; `pd verify` recomputes the chain and detects any tampering,
  regardless of who did it.
- **Authorization is a guardrail, not a cryptographic boundary.** The ACL
  (category + owners) is resolved from PhantomOrg and enforced at the CLI
  layer. The manifest is a plain file: a process with filesystem write access
  can add nodes with valid MACs directly, bypassing both the ACL and the
  append-only audit log. In the phantombot threat model the personas are
  trusted processes sharing one account, and the ACL exists to constrain what
  the model's tool use may touch — not to stop a malicious process that can
  already write the filesystem.

  Binding authorship cryptographically is the v2 authorization boundary,
  now implemented (issue #30): a mutating command MAY sign the node MAC with
  the actor's Nostr nsec (``PHANTOMDOCS_NSEC`` or ``--nsec-file``); the
  BIP-340 Schnorr signature + x-only pubkey are recorded on the node and in
  the audit entry. ``pd verify --org-yaml`` then (a) verifies each signature
  against the recorded pubkey and (b) rejects a signature whose key is not a
  declared actor ``npub`` in org.yaml. This detects an unauthorized write
  signed with the wrong key (or tampered after signing); it is an opt-in
  boundary — unsigned mutations remain valid for namespaces that have not
  adopted signing.
- **Read** — allowed iff the actor's resolved access (`merge_access`) covers
  the node's `category` (i.e. the category number is in the actor's resolved
  category set, or the actor holds a category exception). `category-0` is
  readable only by actors with the `category-0` exception.
- **Write / delete** — allowed iff read is allowed **and** the actor is in the
  node's write scope. In v1 the write scope is the node's explicit `owners`
  list (PhantomOrg role ids **or** actor ids) and is **required**: a write
  with no declared owners is denied. (The §9 fallback — the actors in the same
  reporting `chain` scope — is deferred rather than re-derived in a second,
  drifting implementation of PhantomOrg's `derive_scopes`.) No rule → denied.

This mirrors the GitHub split exactly: PhantomOrg = the *org* (teams/members =
roles/actors), the PhantomDocs manifest = the *repo* (CODEOWNERS-style
classification + optional owners). The manifest is **derived, not hand-edited**:
`pd derive-manifest` reads `org.yaml` to resolve roles/actors and validate the
naming vocabulary, so nothing is duplicated.

## 10. Search

- **Index (always available):** grep over the manifest (urn, slug, meta).
- **Local:** SQLite FTS5 over extracted text + names + metadata.
- **gdrive:** Google Drive search API (`files.list q=...`).
- Result = ranked URNs; the bot returns the logical (readable) path.

## 11. Relationships

Graph in the manifest: `references`, `derived-from`, `version-of`, `related-to`.
Enables queries like "which minutes cite this policy?".

## 12. Integrity: versioning, backup, verification, audit

- **Versioning** — content-addressed: each version is a new blob with its own
  hash; the manifest keeps the current version + the MAC history per URN. A
  version *is* its MAC (immutable) — filenames never encode version numbers.
- **Refs (mutable pointers)** — `latest` / `approved-<date>` pointers in the
  manifest resolve a logical name to a version MAC (the `git` branch/tag
  equivalent); approving a document is tagging a MAC.
- **Backup** — `pd backup` copies blobs + manifest to a target backend,
  verifiable by hash.
- **Verification (cotejo)** — `pd verify` recomputes the MAC chain and content
  hashes against the manifest and reports any divergence.
- **Audit** — an append-only, **hash-chained** log (`audit.log`) records
  `{ts, actor, action, urn, mac, hash, prev}` where `prev` is the SHA-256 of
  the preceding line; `pd verify` walks the chain and reports a deleted or
  reordered entry. The `actor` field is the **authenticated OS identity** — the
  same verified actor used for authorization, never a self-asserted label.
  Without this there is no "total guarantee" worth the name.

## 13. Update package (what PhantomDocs applies)

Idempotent, applied on top of a PhantomOrg persona installation:

- `kb/procedures/Documents.md` — document-management protocol per persona
  (rendered from templates, bounded by `<!-- phantomdocs:start/end -->` markers).
- `MEMORY.md` — a compact pointer to the protocol (same marker convention).
- Document-management tooling installed into `tools/` of the personas that need
  it.

## 14. Security

- ACL resolved from PhantomOrg and enforced fail-closed; no rule → denied.
- Identity + integrity via the chained MAC; optional HMAC authenticity.
- Secrets never stored in the manifest; backends reuse the persona's existing
  credentials (SSH keys, OAuth2).
- Audit log is append-only and hash-chained (`prev` = SHA-256 of the prior line).

## 15. Integration with PhantomOrg

- Personas are provisioned by PhantomOrg (`po build` / `po deploy`) from an org
  model.
- PhantomDocs sits **on top** and consumes `org.yaml` for: roles/actors, the
  access model (`access_levels` + `security_categories` + exceptions), the scope
  rules, and the naming vocabulary. It never modifies PhantomOrg itself.

## 16. Roadmap

- **MVP (v0.1):** manifest + chained MAC + adapters `local`/`ssh`/`gdrive` +
  add/read + `verify` + index search + ACL resolved from PhantomOrg
  (fail-closed) + basic append-only audit + `pd update`.
- **v0.2:** versioning + relationships + backup + queryable audit with retention.
- **v2 (future):** multi-tenancy, folder Merkle aggregation, full local FTS5,
  sync/replication.

## 17. Open questions

- Confirm the exact interface PhantomOrg exposes at build time (branch
  `feat/phantomorg`) so `pd derive-manifest` can consume it without duplicating
  the access model.
- Confirm whether the chain root uses the org's Nostr `org_pubkey` or a tool-
  owned identifier.
- Define the initial `domain`/`type` vocabulary and `security_categories` usage
  for the first target org.
- Confirm the CLI binary name (`pd`) and package layout (`phantomdocs/`).

## 18. References

- **RFC 6920** — "Naming Things with Hashes" (`ni://` URI scheme, truncated
  hashes, binary + human-speakable `nih` formats).
- **multiformats/multihash** — self-identifying hash format
  (`<fn-code><length><digest>`).
- **multiformats/cid (IPLD)** — self-describing content identifiers (CIDv1).
- **Software Heritage SWHID** — persistent object identifiers
  (`swh:1:<type>:<hash>`).
- **git object model** — content-addressed blobs + commit parent-chain
  (SHA-1 → SHA-256 transition); the operating-model reference (§5).
