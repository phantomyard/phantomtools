# Architecture and ownership

PhantomBlog is a self-contained MIT tool under `phantomblog/`, ready for a future
reviewed submission into PhantomTools. It imports no other repository's internals.
Existing tools, persona directories, identities, vault databases, allowlists and
agent task queues are untouched. Installation uses source-backed CLI wrappers.

## Components

`core.py` owns validation and deterministic static rendering. `server.py` exposes
the authenticated dashboard API; browser resources contain no external libraries.
`mcp.py` exposes a newline-delimited JSON-RPC stdio session to agent clients.
`connectors.py` delegates configured external calls to the existing Phantombot
MCP facade and reports outcomes through its memory capture API with an explicit
persona. `cli.py` gives operators the same build/validation operations.

The dashboard and agent proposals use one catalogue, not independent copies of
content. `blog.json` is UTF-8 JSON with a SHA-256 revision. Save uses compare-and-swap
under a workspace lock; stale writes fail. Agents write a tool-owned proposal file,
not the catalogue, and humans apply it after reviewing the exact proposed source.

HTML text and translations are authored before building. Body files can be external
to the catalogue but remain contained under `bodies/`. No dictionary translator,
language model, network service or research operation runs during generation.

## Files owned by this tool

- Initialization creates only `blog.json`, `templates/page.html`, `bodies/README.md`
  and `assets/README.md`, refusing existing targets.
- The dashboard edits that catalogue, its configured shared template and explicit
  uploaded asset names. It never replaces an entire directory.
- Generation owns only manifest-listed HTML, `phantomblog.css` and referenced
  assets. Existing output files cannot be adopted by matching their contents.
- A public deterministic manifest records byte hashes. A mismatch stops normal
  builds, protecting hand edits. Legacy pages are excluded from ownership.
- Only obsolete numbered archive pages with an ownership marker and matching
  manifest hash are deleted. Old article URLs and other assets are retained.
- Private proposal, publishing ledger and transient lock files stay outside public
  output. The ledger is publishing transaction state, not a replacement persona
  memory store. Never deploy the workspace root; deploy `public/` alone.

File paths reject traversal, absolute paths, Windows drive paths, symlinks and
escape through parent directories. Body markup rejects active content, unsafe
URLs and unbalanced tags. Metadata is escaped at render time. CSS supports flat
rules scoped to article content, with no imports, resource URLs, at-rules or
markup. Full site structure can be authored through the validated shared template.

## Build and crash behaviour

All source validation and rendering complete in memory before output changes.
Only changed files are atomically replaced; manifest is committed last. This is
atomic **per file**, not a filesystem-wide transaction. The source lock serializes
dashboard, MCP and CLI writes but does not lock third-party editors. Re-rendering
under the lock detects changes to referenced bodies/templates/assets before commit.

An OS error or interruption after one file lands can leave a partial valid build.
`recover-build` accepts only files already recorded in the old ownership manifest
whose current bytes equal the newly validated render. It does not adopt unrelated
files or overwrite unknown edits. Review first, then:

```sh
python bin/phantomblog --root /absolute/blog --json recover-build
python bin/phantomblog --root /absolute/blog --json check
```

If the process dies while holding `.phantomblog-lock`, verify that no CLI/dashboard
operation is running before removing **that single stale lock file**. Do not
delete directories, sources, persona files or the publishing ledger to recover.

An existing server sync can leave obsolete archive files online if it
only copies files that exist locally. PhantomBlog's local prune does not remove
remote copies. Server/cron changes require a separately reviewed operator action;
this implementation does not modify either.

## Trust boundary

Dashboard sessions can only be created by redeeming five-minute, single-use
access codes. `access.py` verifies NIP-98 kind 27235 signatures with optional
coincurve, binds them to the exact origin/method/payload, and prevents event-id
replay. Only runtime `--allow-identity` flags authorize public keys; empty means
fail closed. There is no static admin token, web login or Bearer authentication.
Codes are generated randomly, stored as hashes, and consumed under a lock only
after successful session creation. Restart invalidates grants and sessions.

The persona CLI reads `<persona-store>/<explicit-id>/identity.json` at runtime,
decodes its nsec, signs locally and prints only the returned fragment link. It
never creates a persona, modifies the store, writes a key or uses a default
persona. The store comes from `--persona-dir` or `PHANTOMBLOG_PERSONA_DIR`.
The optional access extra is required for signing or configured verification;
ordinary builds and disabled issuance require only the standard library.

The dashboard binds exclusively to loopback. Optional `--public-origin` changes
link delivery and adds exactly that Host/Origin pair for a trusted HTTPS proxy;
it does not expose a public listener. Forwarded headers grant no authority.
Localhost alone grants no access. API requests enforce Host/Origin and fetch
metadata; authenticated sessions are HttpOnly, SameSite=Strict and Secure.
Responses are uncached. Fragment codes are removed before API requests and are
not logged. Browser history/clipboard exposure before script execution cannot be
prevented. Previews are sandboxed with scripts disabled. Secrets are never stored
in the catalogue or browser storage.

MCP runs under its configured OS account and root; registration must follow that
account's publication access. It can prepare external work but cannot execute it.
The dashboard/CLI requires review of a plan identifying the source/content/build
revision and selected connections. Changed source or assets invalidate the plan.

Chat messages are not authorization. Actual PhantomChat sender authentication and
threat screening stay in the channel adapter. No relay is granted principal trust.
An incoming article, body fragment, external tool result or chat envelope never
authorizes changing personas, deploying, posting, or applying a proposal.

## Limits of v0.1

- English and Spanish are supported; additional public languages require labels,
  URL rules and validation extensions.
- Authentication currently grants full access to one configured publication. There
  is no multi-user role or multi-tenant permission model.
- Native Nostr transport, a chat transcript panel and embedded PhantomChat client
  are outside this release. A configured messaging MCP adapter is required to send.
- Provider account authorization and real platform tests require the target
  Phantombot installation. Names alone do not create a working connector.
- Provider/local-disk transactions cannot guarantee exactly-once delivery; uncertain
  operations require explicit reconciliation.
- The live check confirms HTML bytes only, not image delivery, DNS propagation to
  every region or server deletion of old files. It rejects HTML-transforming CDNs.
- Raw body-file edits are made outside the dashboard; inline HTML is edited inside.

These limits are surfaced in the interface and documentation rather than treated
as successful integrations.
