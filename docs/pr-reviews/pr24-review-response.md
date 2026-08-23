# PR #24 — PhantomBridge: response to the phantomyard review

Date: 2026-08-19

## Overall status

The requested hardening changes were applied and the test suite is **green
end to end** (`npm test` → EXIT 0):

- `test-audit-hardening.js` — 10 passed
- `test-routing.js` — 14 passed
- `test-org-routing.js` — 18 passed
- `test-pause.js` — 9 ok
- `test-antiloop.js` — 54 ok (including the 4 signed-envelope cases)
- `test-giftwrap-adversarial.js` + `test-giftwrap-fix.js` — ok
- `test-pause-persist.js` — 11 ok (outside the `npm test` runner, runs green)
- `test-backpressure-recovery.js` — 8 ok (outside the runner, runs green)

## Points resolved in this review

1. **Forgeable anti-loop envelope (robertclawson §3).** The `[env]` is now
   sealed with an HMAC-SHA256 MAC over `(metadata + payload)` using the
   bridge key, verified with `timingSafeEqual`. An envelope without a valid
   `sig` is discarded and a fresh one is started (fail-closed). The envelope
   tests were updated to sign with the bridge key (`envelopeMac` exported +
   `signEnvelope` helper in the test).

2. **Malformed org.yaml failing open (kaieriksen §3, robertclawson §3).**
   `loadOrgRouting()` now throws (EINVALID) on broken YAML, a schema without
   `version: 1`, invalid roles/actors/escalation_matrix, broken references,
   and malformed actors. `validateOrgReferences()` + `ORG_SCHEMA_VERSION`.

3. **Permissions documented but not enforced (kaieriksen §2, robertclawson §2).**
   `permissions: null`/malformed is fail-closed (does not fall back to
   legacy); every agent-controlled route (join/leave/inject/recordings) is
   gated by sender + room scope (AUDIT M01).

4. **Stale agents after org.yaml changes (robertclawson §4).** With
   `org.yaml` present, it is the single source of truth for identity/routing;
   manual `config.json` agents are ignored (MEDIO-5).

5. **Long-lived secrets without permissions (kaieriksen §4, robertclawson §4).**
   `nsec`, `relayNsec`, `password`, and the admin token are read via
   `readSecret()` from file (`*File`) or inline, with `assertPrivateFile`
   (0600 or stricter). Config/state/pause temporaries are created 0600.

6. **HTTP API without authentication (lenaparkhodges, robertclawson §4).**
   All endpoints (including `/status` and `/recordings`) require
   `Authorization: Bearer <admin-token>`. The MCP helper (`mcp-bridge.mjs`)
   validates the token and pins the bind to loopback.

7. **Unauthenticated downloads + symlinks (lenaparkhodges §recordings).**
   The recordings listing uses `lstatSync` (does not follow symlinks) and
   filters by safe name; the download secret is validated as a private file.

8. **Global TLS monkey-patch (lenaparkhodges §SHOULD).** Removed; it now uses
   `xmpps://` with real certificate verification.

9. **"Any room" surface (robertclawson §1, bridge.js:1182).**
   The bridge ignores messages from unmanaged rooms (`if (!rooms.has(room))`).

10. **Repo hygiene (robertclawson §6/§7).** Removed the scratch scripts from
    the package root (21+). No real `nsec` or `npub` hardcoded in what is
    versioned.

## Outstanding point — NOT part of this PR (phantombot side)

**`relay_npubs` / untrusted-sender tier in phantombot.**

The underlying blocker all three reviewers raised — bridge DMs must not reach
the persona as a trusted principal — is resolved **on the half that belongs
to the bridge**:

- Room content is published with a **separate relay identity**
  (`nostr.relayNsecFile` / `relayNsec`), never with the main bridge key.
  The bridge **refuses to start** in Jitsi mode without that separate identity.
- The payload is structured as `[phantombridge-relay:v1] {origin, room,
  speaker, text}` with sanitized `speaker`/`text` (no raw interpolation into
  a position syntactically identical to an agent command).
- The unmanaged room no longer expands the room set.

What **is missing is on the phantombot side**, as the reviewer himself put it:
*"that piece is ours, not yours, and I would rather build it than have you
work around its absence."* — i.e., a `relay_npubs` tier alongside
`allowed_npubs` in `phantomchat` so the bridge's relay key is classified as
*untrusted* and the persona passes it through the threat judge.

Until that tier exists in phantombot, the bridge **must not point at
production personas**. There is no configuration value that makes the current
model safe without that change in the receiver.

## Action requested

- phantomyard: implement the `relay_npubs` tier in `phantomchat` (a sender
  allowed to *deliver* but not treated as a principal; passes through the
  threat judge). It is an additive change of ~a dozen lines.
- Until then: keep the bridge out of production (or only with test rooms and
  non-sensitive personas).
