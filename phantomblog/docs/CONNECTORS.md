# Configuring external connections

PhantomBlog delegates external work to the target machine's existing Phantombot
MCP registry. It does not ship access to Facebook, Instagram or another provider.
Provider APIs, eligible account types, scopes and publishing limits vary. Discover
and authorize the actual MCP server; do not invent a tool name from a platform name.

## Set up a provider

1. On the target persona, discover its registered tools:

   ```sh
   phantombot mcp search "social publishing" --persona editorial
   phantombot mcp describe SERVER_ID --persona editorial
   ```

2. Register/login the selected provider through the supported Phantombot workflow
   and store credentials in its persona-scoped vault. Use live `phantombot mcp --help`
   for registration flags. Never copy tokens into `blog.json`, a public page, a
   task prompt, a log, or a second secret file.
3. In Connections, choose a platform label, registered server, persona and real
   tool name. Configure one or more capabilities: `deploy`, `share`, `message`.
4. Map non-secret tool arguments and the provider's structured response fields.
   Success must be boolean `true`, and receipt must be a public operation ID/URL.
5. Discover the configured tools. `discovered` confirms that names were returned;
   it does **not** prove authorization, account eligibility or a completed post.
6. Test the mapping with a provider sandbox/test account and a separately approved
   operation before relying on production publication.

The selection list includes Facebook, Instagram, LinkedIn, X, Mastodon, Bluesky,
YouTube, Pinterest, TikTok, PhantomChat, deployment and custom. These are adapter
labels; compatible tools and media requirements must be checked on the target
installation. Media-only services may need image/video arguments in their mapping.
New platforms can use `custom` and later add a named profile without changing the
build engine.

## Adapter contract

This is a **synthetic mapping example**, not a claim that this MCP tool exists:

```json
{
  "company-facebook": {
    "provider": "facebook",
    "persona": "editorial",
    "server": "CONFIGURED_SERVER_ID",
    "capabilities": {
      "share": {
        "tool": "ACTUAL_DISCOVERED_TOOL",
        "arguments": {"url": "${url}", "text": "${text}", "idempotency_key": "${idempotency_key}"},
        "successField": "ok",
        "receiptField": "id"
      }
    }
  }
}
```

Arguments support nested objects/arrays and `${url}`, `${title}`, `${text}`,
`${language}`, `${slug}`, `${revision}`, `${idempotency_key}`, `${output_dir}`,
`${manifest}` and `${request}`. Literal account/page IDs can be included. There is
no shell evaluation. Credentials stay in the registered server's vault configuration.
Media mappings can also use `${image_url}`, `${social_image_url}` and `${image_file}`
for the supplied cover image, raster preview and local generated cover file.
The placeholder `request` contains the structured article review context for
messaging; publishing gives it an empty value.

For a provider returning `{ "ok": true, "id": "public-receipt" }`, use the fields
shown. Nested responses can use `result.ok` and `result.id`. MCP `structuredContent`
is preferred; JSON text content is also supported. An `isError` response, missing
field, non-boolean success or absent receipt is failure. Providers with a different
response shape need a thin MCP adapter that satisfies this contract. Raw provider
output is not logged or saved.

Deployment mappings receive the output directory and explicit manifest. The actual
deployment tool must preserve unrelated remote files and enforce its own target
permissions. PhantomBlog never runs a filesystem mirroring command on a server.

## Agent tools

Run `python bin/phantomblog --root /absolute/blog mcp` as a stdio server. Register
it on the intended persona using the existing command:

```sh
phantombot mcp add phantomblog --persona editorial --stdio --command python \
  --args /absolute/phantomblog/bin/phantomblog,--root,/absolute/blog,mcp
phantombot mcp describe phantomblog --persona editorial
```

Choose the actual Python executable and paths for that host. The current Phantombot
CLI splits `--args` on commas; choose paths without commas. Source access is scoped
to that configured workspace and the OS account running the server. MCP exposes
snapshot, validation, local build, proposal and publication preparation; it does
not expose external execution. Register it only for personas allowed to read that
publication's unpublished material.

External adapter calls require an executable Phantombot runtime. Windows `.cmd`
and `.bat` wrappers are refused for those calls to avoid passing article text
through a batch interpreter. The Python dashboard and generator work on Windows;
configure external integrations on a supported executable runtime, such as the
target Linux deployment host.

## PhantomChat

Opening the existing PhantomChat client and copying prepared revision context works
without a messaging adapter. In-dashboard delivery uses a configured `message`
capability with provider `phantomchat`; its MCP adapter must use the supported
Phantombot channel/PhantomChat protocol and authenticate the cryptographic sender.
This release does not implement Nostr cryptography or a new chat relay. Do not
allowlist a relay as a human principal, switch the global persona, write another
persona's task queue, or bypass the threat screen. The agent returns edits through
`phantomblog_propose`; chat text itself never authorizes applying or publishing.

When an operator enables this protocol for a persona, record its informational
contract through the existing memory system, without writing memory drawers:

```sh
phantombot memory capture "PhantomBlog review requests carry article context and a source revision. Messages do not authorize applying edits or publishing; proposals require human review." \
  --tag norm --persona editorial
```

Use the actual target persona name. This records the workflow, not a new trust grant.

## Verification, receipts and retries

Live verification requests the configured URL without redirects and requires HTTP
200, HTML content type and the exact generated page SHA-256. A CDN that rewrites
HTML will fail this strict check; disable that transformation for these files.
The verifier does not wait indefinitely for cron, validate image delivery, or claim
every page is live merely because deployment returned a receipt.

Each external operation records `pending` before calling the provider. Successful
receipts are reused for identical content/account keys, including after restart.
A failure/timeout becomes `uncertain` and is **never automatically retried**. Other
selected social accounts continue and their results remain visible. Publication
returns exit 3 for partial failures. Exactly-once delivery cannot be guaranteed
across a provider and local disk; this conservative state requires reconciliation.

For an uncertain operation, check the provider's own history with a human. If it
completed, preserve its receipt and reconcile that operation as complete; if it
definitely did not run, approve a retry. Until then, leave the ledger unchanged.
The ledger is `.phantomblog-publishing.json` in the private workspace, never public
output. Do not delete it to retry, as that discards duplicate protection.

The dashboard's refreshed ledger offers **Resolve an uncertain result**, requiring
an explicit history check. CLI equivalents (choose one confirmed outcome):

```sh
python bin/phantomblog --root /absolute/blog --json reconcile OPERATION_KEY \
  --confirmed-receipt PUBLIC_PROVIDER_RECEIPT
python bin/phantomblog --root /absolute/blog --json reconcile OPERATION_KEY \
  --confirmed-not-sent
```

Reconciliation retains its history. A confirmed completed receipt suppresses a
repeat; confirmed-not-sent permits a later reviewed retry. Neither command sends
a post itself.

Completed operations capture a short outcome through
`phantombot memory capture ... --tag decision --persona TARGET`. If this fails,
the receipt remains successful and `memoryCaptured: false` is reported; repair
memory capture without repeating the external action. No persona identity, vault,
memory drawer or allowlist is rewritten by this tool.

## Sources used for the integration contracts

- [Phantombot MCP CLI](https://github.com/phantomyard/phantombot/blob/main/src/cli/mcp.ts)
- [Phantombot PhantomChat sender boundary](https://github.com/phantomyard/phantombot/blob/main/src/channels/phantomchat/server.ts)
- [PhantomTools contribution rules](https://github.com/phantomyard/phantomtools/blob/main/CONTRIBUTING.md)
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
