# Verification and account handoff

Implementation checked on 2026-10-07, Windows, Python 3.14 and native Phantombot
v1.1.422. Tests ran against temporary directories and synthetic provider responses.

- Connector suite: 28 tests, 27 passed, one symlink fixture skipped because Windows
  did not grant symlink creation privileges. The same test is enabled on Linux CI.
- PhantomBlog regression/integration suite: 59 tests, 58 passed, one Windows
  symlink fixture skipped. New coverage verifies the exact approved HTML hash
  reaches the Facebook mapping alongside its destination and operation key.
- A cross-tool fixture exercised both real implementations together: English and
  Spanish catalogue titles/URLs flowed through PhantomBlog's reviewed plan and
  MCP mapping into the connector, then returned normalized receipts through a
  synthetic Graph transport. A repeated publication did not send again.
- Installed Phantombot: all five connector tools discovered; local setup status
  returned without a Facebook token; publication refused before configuration;
  temporary registration removed. No Meta requests, messages or posts occurred.
- Dashboard browser checks: one-click local Facebook mapping supplied the actual
  tool name, URL/title/hash/key arguments and `ok`/`postId` receipt fields. Local
  validation succeeded and saved source stayed unchanged. Verified at desktop
  1440 × 900 and mobile 390 × 844 with no horizontal page overflow.
- Python compile and dashboard JavaScript syntax checked locally. CI is configured
  for Windows/Linux but no remote CI success is claimed.

Synthetic tests cover invalid input preservation, local read-only modes, matching
Page identity, explicit link publication, live HTML verification, credential header
handling, sanitized API errors, redirects, bounded responses, disabled publishing,
operation collisions, duplicate receipts, locks, interrupted/uncertain requests,
operator reconciliation, MCP errors and execution from another working directory.

## Still requires a human and real account acceptance

1. Sign in to Meta and establish that Aquaponics United is an eligible managed Page.
2. Create/select the app, approve the required permissions and choose a supported
   Graph API version. Official Meta documentation fetches were rate-limited during
   implementation; no version or account eligibility is assumed by the code.
3. Store its Page token in the intended persona's Phantombot vault and register the
   connector with `--env-secret`. Configure the actual article HTTPS origin.
4. Run read-only Page verification, then authorize one concrete test post using a
   confirmed live article and reviewed message. Only then enable publishing.
5. Confirm the resulting post in Facebook, including its title/image/link preview.

At the initial implementation checkpoint, no real token had been collected, no
permissions granted, no permanent Facebook
registration created and no public publication or upstream deployment performed.
The subsequent account checks are recorded below.
The optional local workspace contains incomplete, publishing-disabled setup only;
it is ignored by Git. Existing PhantomBridge runtime/configuration is untouched.

MCP callers must enforce human publishing authorization. Tool annotations and
prepare results do not create a second approval/security system. The CLI requires
a reviewed fingerprint; PhantomBlog uses its existing exact-plan review boundary.
There is no exactly-once guarantee for remote API/disk failures. Both publishing
ledgers require reconciliation after uncertain outcomes. See README for recovery.

## Account diagnostic update: 2026-10-08

- Connector suite: 31 tests, 30 passed, one Windows symlink fixture skipped.
  New coverage checks read-only token identity/feed diagnostics and sanitization
  of numeric provider error codes without leaking provider text or credentials.
- The installed Phantombot registration with an explicit persona successfully invoked
  `facebook_check_page_access` using its vault-injected credential. The token
  acted as the configured Page, but feed access failed with
  HTTP 400 / Meta code 10. This does not prove publishing permission.
- Publishing remains disabled. An earlier human-approved publication attempt
  has an uncertain local ledger result and has not been retried. No successful
  public publication is claimed; reconciliation requires confirming the actual
  remote outcome. Private configuration and ledgers remain ignored by Git.

## Hardening update: 2026-10-08

- Live verification dials the address it validated instead of resolving again,
  keeping the URL hostname for TLS and disabling proxies.
- Releasing the connector lock tolerates a lock file that already disappeared.
- Suites: 34 connector tests OK; 65 PhantomBlog tests OK.
