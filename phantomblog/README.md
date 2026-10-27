# PhantomBlog

Static bilingual publishing with a private editorial dashboard and agent proposal
workflow. Python 3.11+, standard library for builds; the private dashboard requires
the signed-access extra for Schnorr verification. Public websites receive
HTML, CSS and image files; they need no Python service or new build step.

## What works in v0.1

- Articles, bilingual categories, dates, drafts, featured styling and image uploads.
- Supplied English/Spanish HTML bodies, inline or in body source folders. No AI
  writing or translation happens during a build.
- Site → category → article design defaults, three layouts, section ordering,
  scoped article CSS and a shared HTML template editor.
- Deterministic archives of 12 records, localized metadata, raster social previews,
  language links and visitor share destinations.
- Validation before writes, source revision checks, manifest ownership and atomic
  file replacement. Existing legacy pages keep their bytes and URLs.
- Authenticated loopback dashboard; MCP stdio tools read/build/propose. Humans
  review agent proposals and control external publication.
- Configurable MCP deployment, social and PhantomChat messaging adapters. The
  reviewed workflow builds, deploys if selected, verifies exact live HTML and then
  shares, with per-connection receipts and duplicate protection.

Platform names in the dashboard are **configuration choices**, not bundled account
connections. A working MCP server, provider authorization, and its actual tool
schema must be configured on the target Phantombot machine. This checkout contains
no credentials and has not posted to any platform. PhantomChat opens its existing
client and prepares article context; in-dashboard sending requires a verified
messaging MCP adapter. It does not embed or copy the GPL PhantomChat client.

## Start locally

From the `phantomblog/` directory:

```sh
python bin/phantomblog --root /absolute/path/to/my-blog init
python bin/phantomblog --root /absolute/path/to/my-blog --json dry-run
python bin/phantomblog --root /absolute/path/to/my-blog build
python bin/phantomblog --root /absolute/path/to/my-blog check
```

Replace the path with your workspace. The default workspace is beside this tool,
not relative to the caller's current directory. Explicit relative `--root` paths
are resolved from the caller. Every content/asset/output path is resolved from
that workspace. On Windows use `python bin\phantomblog` or `bin\phantomblog.cmd`.
No package installation is required. `pip install .` is optional.

Dashboard entry uses only signed, short-lived links; see the access section below.
There is no static administrator credential, token reference, web login form or
Bearer authentication. The service binds only to loopback.

## Authoring and integration

Read [the agent workflow](docs/AUTHORING.md), [connector configuration](docs/CONNECTORS.md),
[architecture and ownership](docs/ARCHITECTURE.md), and [verification](docs/VERIFICATION.md).

Sources live in `blog.json`, optional `bodies/`, `assets/` and `templates/`.
Generated files live in `public/`. Commit sources **and** generated files to your
website repo. Your existing GitHub-to-server sync can publish them unchanged.
PhantomBlog itself never silently commits, pushes, merges or changes server cron.

Exit codes: `0` success; `1` stale outputs from `check`; `2` invalid input, unsafe
ownership or unavailable local source; `3` external integration failure or partial
sharing failure; `130` interrupted. `check` and `dry-run` write no files and make
no network calls. Their results do not mean a page is live.

## Install and test

Linux/macOS: `./install.sh` symlinks the reviewed checkout's wrapper into
`~/.local/bin` (or `$PREFIX/bin`). It refuses to overwrite divergent installed
copies. Windows: add this tool's `bin` folder to PATH through system settings.

```sh
python -m pip install ".[dev]"
python -m pytest tests -v
node --check phantomblog/resources/dashboard.js
python tests/preview.py --port 8790
```

The preview creates a disposable test workspace with clearly labelled synthetic
articles, images and an ephemeral signing identity. Its printed link lasts five minutes.
It performs no deployments or messages. Stop it with Ctrl+C. Fixtures are never
installed into a real publication.

MIT. See [LICENSE](LICENSE).

## Signed dashboard access

Install `python -m pip install ".[access]"` for coincurve BIP-340 signing and
verification. Builds remain standard-library only. Configure authorized public
keys exclusively at runtime with repeatable `--allow-identity <64-hex-pubkey>`.
No identities are bundled. The dashboard is fail-closed until at least one
`--allow-identity` is configured: an empty allowlist cannot issue links or create
sessions. The dashboard checks for the cryptographic extra before binding its
listener, including with an empty allowlist. A missing extra stops startup with
an actionable installation error.

### Breaking migration from dashboard tokens

The static dashboard administrator token flow has been removed. Remove
`PHANTOMBLOG_ADMIN_TOKEN`, dashboard `--token-ref` and dashboard `--persona` from
service definitions, launch scripts and client integrations. `/api/login` and
Bearer-token dashboard authentication are no longer supported. `--persona`
remains available only on the **access-link client**, to select the signing
identity; it is not a dashboard login option.

Install the optional package extra, which is now required to run the dashboard
and issue/verify signed links:

```sh
python3 -m pip install 'phantomblog[access]'
# From this checkout's phantomblog/ directory instead:
python3 -m pip install '.[access]'
```

The extra installs `coincurve`. A client submitting an already-signed event does
not need a local signer; the dashboard still requires the extra to verify it.
Static builds do not require the extra. Use these command forms (parentheses,
brackets and the vertical bar below denote alternatives/optional arguments):

```text
phantomblog dashboard --allow-identity <64-hex-pubkey> [--public-origin <https-origin>] [--port N]
phantomblog access-link (--persona <id> --persona-dir <store> | --signed-event <file>) --port N [--public-origin <origin>]
```

Use the lowercase 64-hex public key, not an `npub` string or private key; repeat
`--allow-identity` for each authorized identity. Configure the same port and
public delivery origin on server and client. Request a new link through the
authorized persona or signed-event client and open the returned URL. Existing
token-based clients must migrate; there is no compatibility login fallback.
Links expire after five minutes and can be redeemed once by a single browser
session. Credentials are delivered only in the `#c=...` fragment, never a query
parameter. Restarting the dashboard invalidates its in-memory grants/sessions.

```sh
python bin/phantomblog --root <publication-workspace> dashboard \
  --allow-identity <authorized-public-key-hex>
python bin/phantomblog access-link --persona <persona-id> \
  --persona-dir <private-persona-store>
```

The store contains `<persona-id>/identity.json` with the runtime-owned `nsec`
field (NIP-19 nsec or 32-byte hex). `--persona-dir` names the **parent store**,
not an individual persona directory. Alternatively set `PHANTOMBLOG_PERSONA_DIR`
to that store. There is no guessed/default persona or store. The tool reads only
the selected identity, never creates or rewrites persona state, and never logs or
prints the key. Any persona id using letters, digits, underscores or hyphens is
supported; its derived public key must be explicitly authorized on the server.
Keep the store outside the public repository and restrict its filesystem access.
Python cannot guarantee immediate erasure of secret bytes from process memory.

The command signs a fresh NIP-98 kind 27235 event at runtime, sends it to the
loopback issuer, and prints one link. `--port` must match the dashboard port.
A random nonce allows independent requests within the same second. Alternatively,
`access-link --signed-event <private-event-file>` submits an already signed event;
`--signed-event` and `--persona` are mutually exclusive. No secret goes in argv.
The command does not send a message or broadcast anything on a relay.

NIP-98 requires empty content, integer `created_at` within 60 seconds, a canonical
NIP-01 event id, and a real BIP-340 signature. Required tags are `u` (exact issuance
origin plus `/api/access-link`), `method` (`POST`), and `payload` (SHA-256 of the
exact body bytes `{}`). Send `Authorization: Nostr <base64-signed-event>` and
`Content-Type: application/json`. Event-id replay is refused. Display names,
chat text and relay identities cannot authorize access. The persona CLI signs
the local issuance URL; remote signed requests must bind the configured public URL.

### Public delivery origin

By default the link is `http://127.0.0.1:<port>/#c=<code>`. To deliver an HTTPS URL,
configure the same `--public-origin <https-origin>` on **both** dashboard and
access-link commands. This is an origin only: no path, userinfo, query or fragment.
HTTP delivery is restricted to loopback. No instance host is bundled.

The server still binds to `127.0.0.1`; this flag does not install TLS, a proxy or
DNS. Supply a trusted HTTPS reverse proxy/tunnel that forwards the configured
Host unchanged, retains the browser Origin, and reaches the loopback listener.
The server accepts only its loopback origin and the explicitly configured public
origin, with matching Host and same-origin fetch metadata. Foreign Origins remain
403; forwarded-origin headers are never trusted. Configure the proxy to avoid
logging authorization headers, redemption bodies and issued link responses.
No external deployment is performed by these commands.

### Browser entry and leaked links

Links last **five minutes** and work once. The only way to create a session is
`/#c=<43-character-code>` followed by the browser's JSON POST to `/api/redeem`.
Issuance stores only in-memory hashes and redemption is atomic, consuming the
code only after session creation succeeds. There is no `/api/login` route or
Bearer fallback. Sessions use HttpOnly, SameSite=Strict, Secure cookies; existing
sessions remain usable until logout or server restart. Secure loopback cookie
support depends on the browser; do not drop Secure to work around it.

The code is delivered only in the URL fragment, never the path or query. The
browser clears it with `history.replaceState` before any API request and keeps no
browser-storage copy. Fragments are excluded from HTTP page requests and Referer,
but pasted URLs may be captured by browser history sync, extensions or clipboard
managers before JavaScript runs. Do not promise zero history exposure.

Anyone holding an unused link can redeem it. If it leaks, restart the dashboard
to invalidate all outstanding links and sessions, then request a fresh link.
Remove a compromised identity from runtime flags before restarting. Never commit
keys, signed events or access links. Only explicitly allowlisted identities may
request links; human review still controls publication and sharing.

Development checks (install `.[dev]` first):

```sh
python -m ruff check phantomblog tests
python -m ruff format --check phantomblog tests
python -m bandit -r phantomblog -q
python -m pytest tests -v
```
