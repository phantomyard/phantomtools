# Phantom Facebook connector

A focused, local Facebook Page MCP server for PhantomTools. It runs independently
of PhantomBridge and registers in Phantombot's existing persona-scoped MCP registry.
PhantomBlog remains the editorial interface. No paid MCP intermediary is required
by this implementation; Meta's account eligibility, permissions and limits still apply.

Python 3.10+ and its standard library are sufficient. There are no dependencies to
download. Tested on Windows with Python 3.14. This connector does not install or
alter Phantombot, switch personas, edit identities, replace vaults, or change trust
lists. It exposes no deletion, moderation, direct messaging or scheduling tools.

## Architecture

`PhantomBlog → phantombot mcp call → Facebook connector → Meta Graph API`

- Phantombot owns persona identity, registration and credential storage/injection.
- PhantomBlog owns authored content, translations and approval of the publication.
- This connector validates the destination, verifies Page identity and live article
  bytes, sends the approved message/link and returns a public post receipt.
- PhantomBridge continues routing Jitsi/Nostr communication independently.

One MCP registration/configuration represents one Page and token. Register separate
instances with distinct config files/server IDs for multiple Pages or personas.
Instances do not share ledgers or credentials. No Facebook account is included.

## Run locally

From this directory (use an absolute wrapper path from another working directory):

```powershell
python bin/phantom-facebook --version
New-Item -ItemType Directory -Force workspace
Copy-Item config.example.json workspace/facebook.json
python bin/phantom-facebook check
python bin/phantom-facebook status
python bin/phantom-facebook mcp
```

Copy the example only when the destination does not already exist; never overwrite
an existing connection. Default config is relative to this tool, not the caller's
working directory. `--config` accepts an operator-selected file. That file owns
exactly its two companion paths: `<config-name>.operations.json` and
`<config-name>.lock`. Choose a private location outside any published site.

The example deliberately leaves Page ID/API version unset and publishing disabled.
Discovery and local status work before account setup. `check` validates local
configuration; its success is not Facebook access or a live publication.

All CLI commands emit JSON; errors use stderr. Exit codes: 0 success, 2 invalid
input/local setup or state, 3 external failure/uncertain result. MCP stdout contains
only JSON-RPC. Unicode English/Spanish authored messages are passed unchanged.

## Human setup — perform after implementation verification

1. Sign in to [Meta for Developers](https://developers.facebook.com/tools/explorer/)
   using the account managing the destination **Page**. Confirm this is a Page
   eligible for Pages API publication; a numeric profile URL alone is not proof.
2. Create/select an eligible Meta app, follow its current Pages use-case flow,
   approve the Page permissions and obtain a **Page access token**. Publishing
   commonly uses `pages_manage_posts`; discovery/read scopes and review requirements
   depend on the app/account. Follow Meta's current interface. The connector cannot
   bypass review, verification, token expiry or platform restrictions.
3. Store the token using the installed Phantombot vault UI, under the intended
   persona, with a key such as `facebook-page-token`. Never send it in chat or save
   it in a config, repository, task prompt, article, screenshot or shell history.
   Consult `phantombot vault --help` for that installed version's secure entry flow.
4. Set `pageId`, the **currently supported** `apiVersion`, and allowed HTTPS article
   origins in the private config. Keep `allowPublishing: false`. We do not guess
   the supported API version; null requires deliberate operator setup.
5. Register through Phantombot. Replace paths, persona and server ID below:

```powershell
phantombot mcp add facebook-page --persona editorial --stdio --command C:\Python314\python.exe --args 'C:\tools\phantomtools\facebook-connector\bin\phantom-facebook,--config,C:\private\facebook.json,mcp' --env-secret FACEBOOK_PAGE_ACCESS_TOKEN=facebook-page-token
phantombot mcp describe facebook-page --persona editorial
phantombot mcp call facebook-page facebook_status --persona editorial --args '{}'
phantombot mcp call facebook-page facebook_verify_page --persona editorial --args '{}'
phantombot mcp call facebook-page facebook_check_page_access --persona editorial --args '{}'
```

Use the actual installed executable's absolute path if Phantombot is not on PATH.
The native Windows v1.1.422 registration accepts comma-separated argv; paths with
commas are not supported by that format. No batch wrapper or shell interpolation
is required. The vault reference injects the token only into the MCP child process;
our code never directly opens a vault or resolves a default persona.

`facebook_verify_page` confirms ID/name readable through the token. It explicitly
does **not** prove publishing permission. Account acceptance needs a separately
authorized real test. Complete that verification before enabling publishing.

`facebook_check_page_access` additionally checks whether the credential acts as the
configured Page and whether its feed is readable. It returns no credential or post
content, writes no files, and does not verify publication permission. Failed HTTP
requests expose only the HTTP status and numeric Meta error codes; provider
messages, trace identifiers and response bodies remain private.

## PhantomBlog connection

Add a Facebook connection in PhantomBlog's Connections screen. Use the persona
and registered MCP server ID above. The equivalent catalogue entry is:

```json
{
  "facebook-page": {
    "provider": "facebook",
    "persona": "editorial",
    "server": "facebook-page",
    "capabilities": {
      "share": {
        "tool": "facebook_publish_article",
        "arguments": {
          "url": "${url}",
          "message": "${title}",
          "contentHash": "${content_hash}",
          "idempotencyKey": "${idempotency_key}"
        },
        "successField": "ok",
        "receiptField": "postId"
      }
    }
  }
}
```

This example is a mapping, not evidence that its server/account exists. No token
belongs in it. Save/check the mapping, then discover tools. Once setup and the
specific test post are authorized, the operator can enable `allowPublishing`.
PhantomBlog executes only a reviewed, current plan and verifies the published
article. The connector independently requires matching live HTML bytes before POST.
Both checks refuse redirects and HTML-transforming CDNs. Use the final canonical
HTTPS article URL with its deployed byte hash; a generated URL is never proof live.

The caller controls editorial authorization. MCP access to an enabled instance
grants the ability to publish to its configured Page: restrict registration to the
intended persona. `facebook_prepare_article` and tool descriptions are not a security
boundary or an approval token for MCP calls. PhantomBlog supplies its existing
human-reviewed execution boundary. There is no automatic Nostr/meeting-to-Facebook relay.

## CLI review and publication

Create a private `request.json` with exactly `url`, `message`, `contentHash` (SHA-256
of deployed HTML bytes) and `idempotencyKey` (64 lowercase hex characters, stable
for this intended operation). It must contain authored content, never credentials.

```powershell
python bin/phantom-facebook --config C:\private\facebook.json prepare --request C:\private\request.json
python bin/phantom-facebook --config C:\private\facebook.json publish --request C:\private\request.json --approve REVIEWED_FINGERPRINT
python bin/phantom-facebook --config C:\private\facebook.json operation-status --key OPERATION_KEY
```

The second command performs a public side effect and requires the exact fingerprint
from the reviewed first result, publishing enabled and the injected token. CLI use
requires an operator-supplied process environment; the supported vault injection
route is Phantombot MCP. No credentials are included in these commands.

## Recovery and limits

Each operation reserves its key on disk **before** POST. Repeated completed calls
return the receipt; reusing a key with different content fails. A crash, timeout,
malformed receipt or failed receipt persistence leaves `pending`/`uncertain` and
blocks retry. Exactly-once publication across Facebook/network/disk is not claimed.
Do not erase the ledger to retry. Check Page history and reconcile explicitly:

```powershell
python bin/phantom-facebook --config C:\private\facebook.json reconcile --key OPERATION_KEY --post-id PAGEID_POSTID --operator-confirmed
python bin/phantom-facebook --config C:\private\facebook.json reconcile --key OPERATION_KEY --confirmed-not-sent --operator-confirmed
```

Choose only one after inspecting Facebook. Receipt reconciliation checks that the
post belongs to the configured Page and exists. It does not independently prove
that its text matches the uncertain request: that is part of the operator's review.
Confirmed-not-sent permits a later explicit retry; reconciliation never sends.
It is CLI-only. Also reconcile PhantomBlog's ledger before retrying through its UI.

Ledger writes are atomic per file; concurrent calls use an exclusive lock. After a
crash, confirm no process is executing this instance before removing only its stale
lock. Keep backups of config/ledger; do not move a config without its companions.
Ten thousand operations or 20 MB stops new publication until operator maintenance.
History is operational transaction state, not a competing persona memory store.

Graph API host is fixed; redirects are refused and tokens use an Authorization
header, never URL/body/logs. HTTP errors are sanitized, bounded and timed out.
Articles must use configured public HTTPS origins; private addresses and private
DNS responses are rejected. DNS verification is a preliminary check, not protection
against a malicious origin changing DNS between check and request. Only allowlist
operator-controlled origins. Image delivery and Facebook preview rendering require
separate acceptance checks; only HTML bytes are verified here.

## Tests

```powershell
python -m unittest discover -s tests -v
python -m compileall -q facebook_connector
python tests/runtime_acceptance.py --executable C:\absolute\phantombot.exe --persona YOUR_TEST_PERSONA
```

The first commands use temporary fixture directories and synthetic transports.
The opt-in last test uses the actual runtime, creates/removes only a unique temporary
registration and has no token or Meta calls. It never publishes. See
[verification](docs/VERIFICATION.md) for observed results and outstanding human work.

Adapted Graph Page operation structure from
[Hagai Hen's facebook-mcp-server](https://github.com/HagaiHen/facebook-mcp-server).
See [third-party notices](THIRD_PARTY_NOTICES.md). This is not an upstream-supported
or Meta-certified integration.
