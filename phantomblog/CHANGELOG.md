# Changelog

## Unreleased

### Breaking

- Replace static dashboard administrator tokens with five-minute, single-use
  signed access links. Dashboard `--token-ref` / `--persona`, the
  `PHANTOMBLOG_ADMIN_TOKEN` login flow, Bearer authentication and `/api/login`
  are removed. `--persona` is now a signer selection on `access-link` only.
- Require the optional `phantomblog[access]` extra (`coincurve`) for dashboard
  verification and persona signing. The server checks it before opening a
  listener and exits with installation instructions if unavailable. Static
  publishing remains standard-library only.
- Require an explicit `--allow-identity <64-hex-pubkey>` allowlist to obtain
  dashboard access. An empty allowlist cannot issue links or create sessions.
  See the [README migration section](README.md#breaking-migration-from-dashboard-tokens)
  for the exact server and client command forms.

### Maintenance

- Preserve canonical NIP-01 event IDs, coincurve Schnorr verification, NIP-98
  kind 27235, the 60-second freshness window, exact URL/method/payload bindings,
  event replay protection, locked single-use redemption, origin/host checks and
  fragment-only credential delivery. These security rules are unchanged.
- Repair the PhantomBlog workflow: select Python 3.11, install PhantomBlog's
  development extra, and run its full pytest suite instead of unittest discovery.
  Run Ruff checks and formatting in the PhantomBlog job.
- Configure Ruff for Python 3.11 and fix import ordering, mutable class
  constants, loop binding, context management and explicit subprocess checks.
  No rules in the selected ruleset are suppressed. Long-line enforcement
  (`E501`) is not selected because legacy templates and embedded HTML use long
  literals; unrelated style/docstring rule families are outside this focused
  correctness and maintainability ruleset.
