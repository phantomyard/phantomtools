# PhantomBridge

Bidirectional messaging bridge **Jitsi (XMPP MUC) ↔ Nostr** for the
phantombot ecosystem. It is an **optional extra**: without it there is no
communication between bots nor between bot and humans inside Jitsi rooms,
but users who do not want it do not have to deploy it.

Rescued from the VPS (`/opt/jitsi-nostr-bridge/`) on 2026-08-11 — it was
the only critical ecosystem service without version control. The ad-hoc
backup history (07-08 Aug 2026) is kept locally in `history/` (NOT
versioned: the `config.json.bak` files contain secrets).

## What it does

```
┌────────── Jitsi (XMPP MUC) ──────────┐      ┌────────── Nostr ──────────┐
│  meet.example.com                    │      │  relay (private/public)   │
│  • joins rooms as "secretario"       │◄────►│  • mirrors room chat      │
│  • injects agent DMs into the room   │      │    as NIP-17 gift-wraps   │
└──────────────────────────────────────┘      │  • receives agent DMs     │
                                              └───────────────────────────┘
```

- **Local HTTP API** (`httpPort`, default 8090):
  - All endpoints require `Authorization: Bearer <admin-token>` (or `X-Admin-Token`), including read-only `/status` and recording endpoints. `127.0.0.1` is not treated as an authorization boundary on shared hosts.
  - `POST /join {room, nick?, password?, timeout?}` — join a room
  - `POST /leave {room}` — leave a room
  - `POST /pause {side: jitsi|nostr|both, paused: bool}` — **per-side kill-switch**: pause/resume the Jitsi and/or Nostr side independently. nostr paused = agent DMs are silently ignored (bots get no replies → no token burn); jitsi paused = rooms are left and commands answer "paused". `config.paused` sets the initial state at startup.
  - `GET /status` — rooms, nicks, agents, XMPP state, **paused state**
  - `GET /recordings` / `GET /recordings/:name` — list/download recordings
    (both require the **admin token** — AUDIT M05 BLOQUEANTE 1; the listing no
    longer hands out signed download URLs anonymously; path-traversal guarded).
- **Agent DMs** (NIP-17 gift-wrap to the bridge): `join [room]`,
  `leave [room]`, `[room] text` (injects the message into the room),
  `recordings`.
- **Jitsi → Nostr room relay is untrusted by design.** Room-attendee text is
  published with a separate relay identity (`nostr.relayNsec`, a
  `vault:`/`env:` reference — never the bridge principal) and a structured
  `[phantombridge-relay:v1]` payload. The receiving phantombot must classify that
  npub as a relay/untrusted sender (`relay_npubs`) so the threat judge screens
  the content. The bridge refuses to start Jitsi mode without that separate
  identity.
- **Permissions enforcement** (config):
  - `permissions.full` — agents with full access (may join/leave/inject in
    ANY room and list recordings).
  - `permissions.restricted` — `{ room: [agents] }` allowing only the listed
    agents to operate (join/leave/inject) in that specific room.
  - `roomAgents` — specific room → which agents receive room messages
    (destinators; does not authorise the sender to inject).
  - `roomTimeouts` — per-room inactivity timeout.

  Every agent-controlled path (join/leave/inject/recordings) is **gated by
  sender + room scope** (AUDIT M01): an agent may only operate a room it is
  explicitly allowed to. Resolution is **fail closed**: if
  `permissions.restricted` lists a room, only that room's agents (plus
  `permissions.full`) may touch it; rooms not in the map are only for `full`
  agents. Only if **no `permissions` block exists at all** does the bridge
  keep the legacy behaviour (any authenticated agent). Important (AUDIT M01):
  a **present** block (`permissions: {}`, or a malformed `full`/`restricted`)
  is **fail-closed** — it does NOT fall back to legacy; an operator who
  configures `permissions` but grants nothing gets "nobody may operate", not
  "everyone may operate".

  **Recovery watermark vs sender-controlled timestamps (AUDIT M01-OPCION2):**
  the durable recovery watermark (`recoveryWatermark`) represents **confirmed
  forward progress of the relay stream**, and advances **only** via the
  bridge's OWN local clock (`advanceRecoveryWatermark()`), never from a
  `created_at` stamped by an emitter. A sender — authorized or not — cannot
  influence the cursor: the bridge's clock is not exposed or forgerable by a
  Nostr event. This closes the attack where an authorized agent pushed a
  fabricated future `created_at` (+~23h) into the watermark to evict
  still-recoverable delivered messages (break exactly-once). Rejected /
  non-admitted wraps never move it, and the watermark is monotonic and
  requires prior real progress to advance (AUDIT-10).

## Installation

### One-line (standalone)

```sh
curl -fsSL https://raw.githubusercontent.com/phantomyard/phantomtools/main/phantombridge/install.sh | bash
```

This fetches the repo, symlinks `phantombridge` into `~/.local/bin` and runs
`npm install` for the bridge deps. Requires **Node.js 20.19+** and `npm` in
PATH; `git` is needed the first time (the script clones the repo).

### From a checkout

```bash
npm install          # installs dependencies (see package.json)
./install.sh         # symlinks bin/phantombridge into ~/.local/bin
cp config.example.json config.json
# Secrets are REFERENCES (vault:NAME / env:VAR), never plaintext values:
# store each once in the phantombot vault —
#   phantombot vault set bridge-nsec <nsec>
#   phantombot vault set bridge-relay-nsec <nsec>
#   phantombot vault set bridge-xmpp-password <password>
#   phantombot vault set bridge-admin-token <token>
phantombridge        # run (or: phantombridge /path/to/config.json)
```

> **Source of truth:** the repo is the single source of truth — `install.sh`
> symlinks `bin/phantombridge`, so never edit the installed copy; edit the
> source and re-run `install.sh`. If an installed copy was edited in place
> (symlink became a diverged file), `github-app-auth report-drift` will flag it.

The bridge must be able to connect to:
- the Jitsi XMPP server using normal TLS certificate verification. The bridge
  never disables TLS verification or monkey-patches `node:tls`. For a private
  CA, install the CA in the host trust store or set `NODE_EXTRA_CA_CERTS` before
  starting the process.
- the Nostr relay (WS).

## Per-side kill-switch (pause jitsi/nostr) — v1.2.0

The bridge can be paused per side **independently**: pausing Jitsi does not
affect Nostr routing and vice versa. Designed as a safety key (if the bots
start burning tokens, their communication is cut immediately) and so the
operator decides which sub-systems are active.

### Hot (runtime)

Local API (`127.0.0.1:httpPort`, default 8090):

```bash
# Set the admin token once in your shell (prefer a protected token file in production).
export PHANTOMBRIDGE_ADMIN_TOKEN="<admin-token>"

# Pause / resume the nostr side (bot↔bot DMs — anti-token kill-switch)
curl -X POST http://127.0.0.1:8090/pause -H "Authorization: Bearer $PHANTOMBRIDGE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"side":"nostr","paused":true}'
curl -X POST http://127.0.0.1:8090/pause -H "Authorization: Bearer $PHANTOMBRIDGE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"side":"nostr","paused":false}'

# Same for jitsi (XMPP rooms) or both at once
curl -X POST http://127.0.0.1:8090/pause -H "Authorization: Bearer $PHANTOMBRIDGE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"side":"jitsi","paused":true}'
curl -X POST http://127.0.0.1:8090/pause -H "Authorization: Bearer $PHANTOMBRIDGE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"side":"both","paused":true}'

# Current state
curl http://127.0.0.1:8090/status -H "Authorization: Bearer $PHANTOMBRIDGE_ADMIN_TOKEN"
# → {..., "paused": {"jitsi": false, "nostr": false}}
```

Behavior when paused:

- **nostr paused** → incoming gift-wraps are ignored **silently**: no reply
  to the sender, so bots process nothing and burn no tokens. The other side
  (jitsi) keeps operating.
- **jitsi paused** → all active rooms are left; room commands via DM answer
  `⏸ The Jitsi bridge is paused...`; the HTTP API `POST /join`, `/promote`
  and `/register` return `{ok: false, error: 'jitsi paused'}`. Nostr
  routing keeps operating.
- **both** → pauses/resumes both sides at once.
- Invalid side (`/pause` with another name) → HTTP 400.

### Durability & fail-closed (v1.8+)

The pause (kill-switch) is **durable**: `POST /pause` persists the per-side
state to a dedicated file (`pauseFile`, default `./.bridge-pause.json` next
to the config) so it survives a service restart/reconnect **in any mode**
(jitsi, nostr, both). Writing is atomic and crash-safe
(`tmp`+`writeSync`+`fsync(fd)`+`close`+`rename`+`fsync(dir)`):

- `POST /pause` answers **`ok: true` only when the state is durably
  persisted**. If persistence fails it answers `ok: false` with an error —
it never claims a successful kill-switch that lives only in RAM.
- **fail-closed on resume**: to resume (`paused:false`) the bridge persists
  first and only flips the in-memory state once the disk confirms, so a
  failed resume leaves the side paused (RAM and disk stay consistent). A
  failed `paused:true` still leaves the side paused in RAM (safest outcome).
- **fail-closed on corrupt state** (startup): a `pauseFile` that *exists*
  but is unreadable / invalid JSON / wrong schema **aborts startup** with a
  clear error instead of silently starting unpaused. Only a *missing* file
  falls back to `config.paused`. The bridge will refuse to boot until the
  operator repairs or removes the corrupt file — a kill-switch must never
  boot with unknown state.
- **legacy migration**: on boot, if the dedicated file is absent, the
  bridge migrates any `paused` field from the older `stateFile`
  (`.bridge-state.json`) — but only when the persisted relay matches the
  configured one, and the migration itself must persist durably or startup
  aborts (it is never completed only in RAM).
- **Multi-instance**: the model is *one bridge per pauseFile*. Concurrent
  processes sharing the same file are not supported (last writer wins), and
  temp names use `crypto.randomBytes(16)`.

### At startup (initial state)

In `config.json`, optional field:

```json
"paused": {
  "jitsi": false,
  "nostr": false
}
```

If omitted, the bridge starts with everything active. On boot the **durable
state** (`.bridge-pause.json`) takes priority over `config.paused`; the
config value is only the fallback when no durable state exists yet.

## Anti-backlog on restarts — v1.3.0

The bridge persists its nostr subscription state in a file
(`stateFile`, default `./.bridge-state.json` next to the config) so it does
not reprocess ALL the relay history on every restart/reconnect:

- **`lastSeen`**: **receive cursor** — the local reception time (`Date.now()`)
  of the last gift-wrap that reached the WebSocket, advanced BEFORE the event
  is authenticated/admitted. It is NOT a Nostr cursor and never controls the
  `since`: a relay can deliver events out of `created_at` order, so a local
  timestamp cannot mean "everything before this was already traversed". It is
  used only for dedup. On reconnect, the subscription `since` is anchored to the
  **process cursor** `recoveryWatermark` (the range the relay confirmed as
  traversed by actually admitted events), with `pendingSince` (STICKY) as a
  conservative anchor while drops are unrecovered, minus an overlap margin
  (`STATE_OVERLAP_SECS`) so boundary events are not lost.
- **`recoveryWatermark`**: **process cursor** — monotonic, advances ONLY when
a real confirmed event is processed, and each event concedes at most one
bounded step of backtrack (`RECOVERY_WATERMARK_STEP_SECS`), never a free jump
to `Date.now()`. After a long downtime a single event moves the cursor one
step, not the elapsed wall-clock time — the relay's backlog re-delivery walks
it forward iteratively, so `deliveredCanExpire()` never evicts delivered the
relay has not actually traversed. This is the legitimate anchor for the
`since`; it represents what the relay has traversed, not a wall-clock
timestamp. `processWatermark(ts)` was removed (AUDIT-M01-OPCION2-FIX):
feeding the cursor from any external timestamp (an emitter's `created_at`)
reintroduces the attack surface.
- **`seenIds`**: buffer (max 200) of already-processed gift-wrap IDs.
  Relay re-sends within the overlap are ignored as duplicates, so a quick
  restart does not re-run commands nor re-route already-seen REQUESTs.
- Debounced write (5s) + flush on exit (SIGTERM/SIGINT/exit).
- First start without state (or different relay): full backlog
  (original behavior), and from then on only the new events.

Optional field in `config.json`:

```json
"stateFile": "./.bridge-state.json"
```

## Bot anti-loop — v1.5.6

The bridge is the only bot↔bot path of the ecosystem (Telegram blocks
bot→bot via privacy mode; the phantombot bot-gate stops bot→bot in
PhantomChat except through the bridge). As a choke point, loops are cut
here without touching humans.

**The check is TRANSACTIONAL** (v1.5.2, audit F2-R01): first ALL defenses
are checked (envelope → request → dedup → rate) without mutating state,
and only if the message passes them all is the admission recorded (hash,
pair mark, request_id count/edge). A message dropped by dedup or rate does
NOT consume request_id quota nor seed edges: the counter reflects only
messages actually admitted.

**The rollback is by ADMISSION TOKEN** (v1.5.3, audit F2-R02): the COMMIT
returns the exact identity of each mutation and the rollback after a failed
publish undoes ONLY that admission. Between COMMIT and rollback there is an
`await publishDM()` that releases the event loop: another concurrent
admission may touch the same structures. That is why the rollback never
looks up by (from,to,text) nor does `pop()`: it removes the SPECIFIC pair
mark (indexOf+splice, not pop), validates that the request entry is the
SAME admitted instance (protects against requestMax eviction + RID
re-creation) and decrements the edge by occurrence (Map, not Set).

**Admission identity is MONOTONIC, not temporal** (v1.5.4, audit
F2-R03/R04): `Date.now()` is NOT a unique identifier — two admissions can
land in the same millisecond. Marks are stored as `{id, ts}` (id
incremental per admission) in pairs and hashes, and the rollback looks up
by **id**, never by timestamp: an `indexOf(ts)` would remove the FIRST
match (the mark of ANOTHER admission in the same ms) and a hash
re-registered after eviction in the same ms would be confused with the
original.

### Mechanics (config `config.antiloop`, defaults in parentheses)

1. **Protocol envelope (norma v1.3)** — every routed message carries a
   first line `[env] {json}` that the bridge stamps on each re-send:
   `{"rid":"...","hops":N,"trace":["a","b"],"expires":...,"sig":"..."}`.
   The `sig` is an HMAC over the envelope metadata AND payload, so sender-supplied
   hop/trace/expiry values cannot be forged or altered without the bridge secret.
   - **The envelope is ALWAYS the first real line of the delivered
     message** (v1.5.1, audit F2-01): the sender is indicated AFTER, as
     `[agent]` metadata on the next line. This makes "copy the `[env]`
     line as-is" unambiguous for the receiving bot.
   - `hops >= maxHops (3)` → drop. Matches `communication.max_hops` of the org.
   - `expires` past → drop. The bridge sets it if missing (`expireMs`, 6h =
     `ttl_hours` of the org).
   - **Already-traversed edge** (sender→target present as a consecutive
     pair in `trace`) → drop. Kills the A→B→A→B... oscillation even when
     each message uses a NEW request_id and NEW text (the "creative" loop).
   - **Strict type validation** (v1.5.1, F2-02): an envelope with
     non-integer/negative `hops`, invalid `expires` or non-string `trace`
     is treated as NONEXISTENT (never crashes the bridge nor is immortal)
     and the message falls to the remaining defenses.
   - The bridge stamps even if the bot does not cooperate: if the message
     has no envelope, it creates one (rid from text, hops=1,
     trace=[sender,target]).
2. **request_id short-circuit** (`reqMax: 8` in 10 min) — same rid
   (format `{org_id}-{yyyymmdd}-{seq4}`) repeated → drop + warn ⚠.
   Additionally, edges are tracked per rid: repeating (sender→target) in
   the same thread → drop (works without envelope; it is bridge state).
   - v1.5.1 (F2-04): if the message carries an envelope, only its `rid`
     is tracked (authoritative); free text can no longer pollute the
     counter of a legitimate rid. The textual fallback remains only for
     messages without envelope.
3. **Logical dedup by CONTENT FINGERPRINT** (1h, v1.5.6/F2-05 +
   v1.5.7/F3-01) — same (sender, receiver, content) → drop. The
   fingerprint is robust to:
   - **Trivial reformatting**: the djb2 hash operates on the CANONICAL
     form (NFKC → lowercase → no diacritics → only [a-z0-9] + spaces).
     Spaces, uppercase, punctuation, accents and emoji no longer evade
     the dedup.
   - **Near-identical paraphrase**: unigrams + Jaccard ≥ `fuzzyThreshold`
     (0.85, configurable) against recent messages of the SAME pair →
     drop (`dropped.fuzzy`). Conservative threshold: legitimate
     variations ("confirma la reunion manana" vs "...hoy" = 0.6) are NOT
     dropped.
   - **New rids (closes F2-05)**: the `{org}-{yyyymmdd}-{seq4}` pattern
     is metadata, not content — it is stripped before the fingerprint. A
     bot that removes the envelope and re-publishes with a new rid and
     the same body falls into dedup.
   - v1.5.1 (F2-09/F2-10): `hashMax (200)` evicts the oldest and counts
     it (`evictedHashes` in /status) — observable degradation under
     bursts; and if the DM publish fails, the consumed state is reverted
     (`antiLoopRollback`) so the sender retry does not hit a false
     positive.
4. **Pair rate** (`pairMax: 10` msgs/min) — from→to bursts → drop.
5. **HOURLY pair rate** (`pairHourMax: 10` msgs/h, v1.5.7/F3-01) —
   defends against **slow** loops (1 msg/15-30 min): with different
   content each time (radical rewrite) they evade dedup and the per-minute
   rate; the hourly limit cuts them after 10 messages in 1h. Legitimate
   traffic between two bots (one-off requests) is far below that.
   The dedup window extended to 1h also cuts slow loops with the SAME
   content on the first repetition (at 15-30 min).

The drop is **silent** (log + counter, no reply to the sender): replying to
the looping bot would feed the loop and burn more tokens (same philosophy
as the kill-switch). State is in-memory (loops are short-lived; a restart
resets) with a hard entry cap (`requestMax: 500`, LRU) so a bot generating
unlimited new rids cannot grow the map unbounded (F2-08).

### Norma v1.3 (envelope)

People must **keep the `[env]` line** when replying to a message that
carries one (do not edit it; the bridge updates it). The bridge validates
and stamps; people only propagate it. If you reply to a message without
`[env]`, do not invent it: the bridge creates it.

The `[env]` marker is a **protocol constant** (F2-03): PhantomBridge has it
hardcoded and PhantomOrg rejects any other value in org.yaml.
`maxHops`/`expireMs` of the bridge must match
`communication.max_hops`/`ttl_hours` of the org.yaml — `/status` exposes
them (`antiloop.config`) to detect drift (F2-07).

Telemetry in `GET /status`:

```json
"antiloop": {
  "routed": 42,
  "dropped": {"hash": 1, "pair": 0, "request": 2, "cycle": 3, "hops": 1, "expired": 0},
  "activePairs": ["carol|dave"],
  "activeRequests": [{"id": "example-org-20250101-0007", "count": 5, "agents": ["carol", "dave"], "edges": ["carol|dave", "dave|carol"]}],
  "config": {"maxHops": 3, "expireMs": 21600000, "reqMax": 8, "requestMax": 500, "hashMax": 200, "pairMax": 10, "marker": "[env]"},
  "evictedHashes": 0
}
```

## Configuration

See `config.example.json` (with placeholders). The real `config.json` is
**not versioned** (.gitignore) and carries **no secrets**: each secret is a
`vault:NAME` or `env:VAR` reference resolved at startup via `phantombot vault
get NAME` or the operator's environment (see `docs/SPEC.md` §9 and
CONTRIBUTING.md §4.6). Plaintext values and legacy `*File` keys are rejected
(fail-closed).

### Recovery watermark step (`recoveryWatermarkStepSecs`)

`advanceRecoveryWatermark()`, invoked when a real event is confirmed
processed, advances `recoveryWatermark` incrementally: at most
`recoveryWatermarkStepSecs` per confirmed event (default **300** = 5 min),
never a free jump to `Date.now()`. This bounds how fast the cursor may
"confirm" relay progress: after a long downtime a single event moves the
watermark one step, and the relay's backlog re-delivery walks it forward
iteratively, so `deliveredCanExpire()` never evicts delivered the relay has
not actually traversed (preserves exactly-once). Tune it to the expected
backlog throughput of your relay: a larger value reclaims ledger space
faster after downtime; a smaller value is more conservative (keeps delivered
longer). Must be > 0.

```json
"recoveryWatermarkStepSecs": 300
```

## Test scripts

The `test-*.js` and `giftwrap-*.js` scripts are diagnostic tools used
during development (08 Aug 2026). Those that had hardcoded secrets were
sanitized to read from environment variables:

```bash
RELAY=ws://... AGENT_NSEC=nsec... BRIDGE_NSEC=nsec... node test-relay-auth.js
XMPP_PASSWORD=... node e2e-inject.js [room] [msg] [nick]
```

`refs/` contains captures/diagnostics (pcaps ignored in git).

## MCP (Model Context Protocol) server — v1.8.0

`mcp-bridge.mjs` exposes the bridge's local HTTP API as MCP tools over
**stdio**, so an MCP-native harness or orchestrator (phantombot, Claude,
Codex, OpenClaw...)
can drive the bridge without curl or SSH.

It uses the official
[`@modelcontextprotocol/sdk`](https://www.npmjs.com/package/@modelcontextprotocol/sdk)
(the same SDK phantombot itself uses) — not hand-rolled JSON-RPC — and reads `httpPort` from the same `config.json` and authenticates every call with the bridge admin token. The MCP client refuses arbitrary remote HTTP hosts, so the bearer token cannot be redirected by config to an unexpected server.
Logs go to **stderr**; stdout stays clean for the MCP wire.

### Tools (4, line-aware)

| Tool | Bridge endpoint | Input | Line |
|---|---|---|---|
| `bridge_status` | `GET /status` | — | both |
| `bridge_pause` | `POST /pause` | `{side: jitsi\|nostr\|both, paused: bool}` | both |
| `bridge_join` | `POST /join` | `{room, nick?, password?, timeout?}` | **Jitsi only** |
| `bridge_leave` | `POST /leave` | `{room}` | **Jitsi only** |

`bridge_pause` is the safety kill-switch: pausing the **nostr** side makes
agent DMs silently ignored (bots process nothing → no token burn).

**Line-aware behaviour (scope cotejo with the bridge, bridge-mcp #1):**
the bridge runs two independent lines — **Telegram bot↔bot** (Nostr DM↔DM
routing) and **Jitsi** (XMPP MUC rooms). `config.mode = "both"` refers to the
pre-MCP infra (jitsi+nostr), NOT to Telegram-vs-Jitsi granularity. So this
MCP server always exposes `bridge_status` and `bridge_pause`, but only
**dynamically exposes `bridge_join`/`bridge_leave` when the Jitsi line is
actually available** (post `/status` reports `xmpp: "online"`). When Jitsi
is offline or the mode is Telegram-only, those tools return a clear error
instead of the bridge's `joinRoom is not defined`.

### Register with phantombot

`phantombot mcp add bridge --stdio --command node --args /abs/path/mcp-bridge.mjs`

then verify with `phantombot mcp status bridge`. This writes the entry to the
persona's `mcp.json` via the native CLI flow.

### Standalone verification

```bash
node mcp-bridge.mjs                    # start the stdio server
```

The admin-token resolution seam (vault:/env: references resolving to the same
value the bridge uses) is covered by `test-mcp-auth.js`. End-to-end integration
is verified with phantombot's built-in MCP tools:

```bash
phantombot mcp add bridge --stdio --command node --args /abs/path/mcp-bridge.mjs
phantombot mcp status bridge
```

Requires the extra dependency `@modelcontextprotocol/sdk` (added to
`package.json`).

## Ecosystem integration

- **PhantomMeet** checks it at install time (`pm check-infra` — HTTP probe
  to `127.0.0.1:8090/status` and WS to the relay) and configures its npub
  in each person's allowlist.
- **PhantomOrg** references it at build time (org.yaml channels) and can
  install it from GitHub if not present.
- **Communication norm v1.2**: the agent channel declares
  `phantomchat` (relay) as the bot↔bot and bot↔human path.

## org.yaml as source of truth — v1.6.0

Norma v1.6: the org.yaml compiled by PhantomOrg is the single source of
truth for the organization hierarchy, and the bridge replicates it in
bot↔bot communications. If an `org.yaml` is available next to the config
file (or at the path in `config.orgFile`), the bridge **derives** its
agents and DM↔DM routing from it and ignores the manual
`routing.permissions`.

The contract is explicit: `org.yaml` must contain `version: 1`, plus `roles`,
`actors`, and `escalation_matrix` arrays. Actor identities, role references,
and escalation references are validated strictly. A missing file is the only
case that permits legacy manual routing. A present but malformed, unreadable,
or unsupported-version file **fails closed and aborts startup**; it never
widens access by falling back to an older config.

Derivation rules (see `org-routing.js`):

1. `roles.reports_to` → **bidirectional** edges between each actor and
   every actor holding the boss role (manager ↔ report).
2. `escalation_matrix` → **directional** edges from→to (the escalator
   talks to the escalation target). A `"*"` from means every actor may
   escalate to that role (e.g. `"*"→ceo` = everyone can escalate to the
   CEO).
3. `default: "deny"` — pairs without an explicit rule cannot talk.

When org.yaml is valid, its actor identities are authoritative; config.json
agents do not complement or override them. If org.yaml is missing, legacy
manual routing may be used. If it exists but is invalid, startup stops.

```bash
# deploy an org.yaml next to config.json (or set config.orgFile)
cp org.yaml /opt/jitsi-nostr-bridge/org.yaml
# /status now shows the derived routing
curl -s http://127.0.0.1:8090/status | jq .routing
```

Test: `node test-org-routing.js` (15 tests: unit + bridge integration).

## Changelog

- **v1.7.3** — AUDIT kaieriksen/ChatGPT OPCION2 FIX (🔴 BLOQUEANTE): el
  avance del `recoveryWatermark` queda ACOTADO — un único evento tras un
  downtime ya NO puede saltar hasta `Date.now()` (rompía exactly-once al
  expirar delivered que el relay no había demostrado recorrer).
  - `advanceRecoveryWatermark()` avanza INCREMENTALMENTE a
    `min(prev + recoveryWatermarkStepSecs, now)`: cada evento confirmado
    concede a lo sumo un paso de backlog (default 300s), nunca un salto
    libre al reloj local. Tras un downtime de 30 días, un solo evento
    mueve el cursor un paso, no 30 días; la ráfaga de backlog lo recorre
    poco a poco.
  - `processWatermark(ts)` ELIMINADO por completo (función + export):
    alimentar el cursor desde un timestamp externo (el `created_at` del
    emisor) reintroduce la superficie de ataque. El único avance legítimo
    es el paso acotado.
  - Nueva opción de configuración `recoveryWatermarkStepSecs` (ver
    sección Configuration).
  - Tests: `test-audit-m01-blocker2-watermark-gate.js` (caso real
    "watermark hace 30 días + 1 evento → NO salta a now" + ráfaga de
    backlog incremental); `test-audit10-watermark-recovery.js`,
    `test-audit11-processwatermark-fed.js` y
    `test-audit-m01-permissions-gate.js` adaptados al avance acotado sin
    `processWatermark`.

- **v1.7.2** — AUDIT kaieriksen/ChatGPT OPCION2 (🔴 BLOQUEANTE): el
  `recoveryWatermark` ya NO depende del `created_at` del emisor.
  - `finishDelivery()` y el routing ya no reciben `wrapTs` (el `created_at`
    del gift-wrap, input controlado por el remitente). El watermark avanza
    SOLO por el reloj LOCAL del bridge (`advanceRecoveryWatermark()`) cuando
    se confirma procesamiento real (markDelivery delivered); el reloj del
    bridge no es forjable desde un evento Nostr.
  - Cierra el ataque: un sender AUTORIZADO para una sala ya NO puede empujar
    un `created_at` fabricado (+~23h) al watermark y evictar delivered
    recuperables (break exactly-once). Garantía declarada: el cursor refleja
    progreso confirmado del stream, nunca un timestamp del remitente.
  - Invariante AUDIT-10 preservado: monotónico, no retrocede, primer salto
    0->X solo por evidencia real de watermark previo, rechazados no lo mueven.
  - Tests: `test-audit-m01-blocker2-watermark-gate.js` ampliado (11 casos,
    incluye "sender autorizado con created_at futuro +23h NO adelanta el
    watermark"); `test-audit11-processwatermark-fed.js` y
    `test-audit-m01-permissions-gate.js` actualizados a la nueva fuente.

- **v1.7.1** — AUDIT kaieriksen blocking review (M01/M04/M05) follow-ups:
  - **M01 fail-closed `permissions`**: a *present* but empty/malformed block
    (`permissions: {}`, `full: "x"`) is now **fail-closed** (nobody operates),
    NOT legacy — only an *absent* block keeps legacy behaviour. New pure
    `evalRoomPermission(permConfig, sender, room)` export makes the permission
    matrix directly testable.
  - **M01 BLOQUEANTE 2 (no-loss)**: `recoveryWatermark` no longer advances
    before the M01 authorization gate. It now moves only inside
    `finishDelivery(ok=true)` / routing-success, i.e. after a command passed
    auth + M01 + succeeded. A `created_at` more than 24h ahead of the local
    clock is ignored (fail-safe against fabricated future stamps that could
    evict still-recoverable delivered messages).
  - **M04 cola envenenada (ALTO)**: `persistConfig()` no longer leaves
    `writeConfigChain` permanently rejected after one I/O failure — the caller
    gets the real promise (can act on the error) while the chain stays
    recoverable via `.catch(() => {})`.
  - **M04 durabilidad (fsync)**: config writes now fsync the file contents
    AND the parent directory after rename, so the write survives crash/
    power-loss (not just atomic rename).
  - **M05 BLOQUEANTE 1**: `GET /recordings` now requires the admin token
    (fail-closed) — it no longer hands out anonymous expiring signed download
    URLs that bypassed the `/recordings/:name` admin gate.
  - **Tests adversariales reales**: `test-audit-m01-blocker2-watermark-gate.js`
    (watermark vs M01 gate, 8 cases), `test-audit-m01-permissions-gate.js`
    rewritten to exercise the full permission matrix (14 cases, including
    `{}`/malformed fail-closed), `test-http-api.js` asserts `/recordings` 401
    without admin token.

- **v1.8.0** — security hardening follow-up: production startup now refuses to run without an explicitly configured HTTP admin token; MCP authenticates every local API call and refuses remote host redirection; unmanaged Jitsi rooms are never auto-joined from incoming stanzas; recording listings/downloads reject symlinks and require a private download secret; room names/nicks/passwords are bounded and validated; configuration persistence is awaited before reporting success; and anti-loop envelopes are HMAC-authenticated over both metadata and payload so in-band control data cannot be forged.

- **v1.7.0** — MCP server (`mcp-bridge.mjs`) over stdio with the official
  `@modelcontextprotocol/sdk` (same SDK phantombot uses). Exposes 4 tools:
  `bridge_status`, `bridge_pause`, `bridge_join`, `bridge_leave`. Reads
  `httpPort` from `config.json`; authenticates the local API with the admin token; logs to stderr (clean MCP wire).
  New dependency: `@modelcontextprotocol/sdk`. Admin-token resolution is
  shared with the bridge via `secrets.js` (same vault:/env: reference
  semantics), verified by `test-mcp-auth.js`.

- **v1.6.0** — org.yaml as source of truth (norma v1.6). New
  `org-routing.js` module: derives agents (npub→hex) and DM↔DM routing
  from `roles.reports_to` (bidirectional) + `escalation_matrix`
  (directional, `*` = everyone). Bridge integration: if an org.yaml
  exists next to the config (or at `config.orgFile`), the derived
  routing replaces the manual one (warning logged); config agents still
  complement. Fallback to manual routing when no org.yaml. New
  dependency: `js-yaml`. Tests: `test-org-routing.js` (15).

- **v1.0.0** — rescue from the VPS (2026-08-11), sanitized. No behavior
  changes vs. the deployed `bridge.js` (698 lines).
- **v1.1.0** — generic modes `jitsi | nostr | both` + DM↔DM routing
  (`@agent text`) with permissions. E2E test with real identities.
- **v1.2.0** — per-side kill-switch: `POST /pause {side, paused}` +
  `config.paused` (initial state). Tests: `test-routing.js` (14) +
  `test-pause.js` (9) + E2E with pause phase.
- **v1.3.0** — anti-backlog on restarts: persisted subscription state
  (`stateFile`, default `./.bridge-state.json`) with `lastSeen` (receive
  cursor) + `seenIds` (dedup of already-seen gift-wraps). NOTE: since
  AUDIT-14/15 the subscription `since` is anchored to the PROCESS cursor
  `recoveryWatermark` (not `lastSeen`), with `pendingSince` as conservative
  drop anchor.
  Fixes from the v1.1.0 refactor: `subscribeIncoming` with REQ after AUTH
  (nip42, live streaming) and `handleJoinLeave` inside the JITSI_MODE
  scope (`joinRoom is not defined`). E2E with restart + dedup phase.
- **v1.4.0** — bot anti-loop: logical dedup (sender+receiver+content),
  pair rate (`pairMax` msgs/min) and request_id short-circuit
  (`reqMax` occurrences in a window) with silent drop + telemetry in
  `/status` (`antiloop.routed/dropped/activePairs/activeRequests`).
  Configurable via `config.antiloop`. Tests: `test-antiloop.js` (10) +
  E2E with loop phase (duplicated REQUEST dropped).
- **v1.5.0** — protocol envelope (norma v1.3): the bridge stamps every
  routed message with `[env] {rid,hops,trace,expires}` and validates
  `hops` (max_hops), `expires` and already-traversed edges (A→B→A→B
  oscillation). Cuts CREATIVE loops (new request_id and text each hop)
  that the v1.4.0 mechanics did not see. Edges per rid (bridge state,
  works without bot cooperation). New counters
  `dropped.{cycle,hops,expired}` in `/status`; `activeRequests` shows
  edges. Tests: `test-antiloop.js` (20) + E2E with creative-loop phase
  (legitimate hops 1-2 arrive; 3-4 cut by cycle/hops).
- **v1.5.1** — Phase 2 audit (12 GPT findings cross-checked, all real):
  - **F2-01** — the envelope is ALWAYS the first line of the delivered
    message; `[from]` (sender) goes as metadata on the next line.
    Previously the delivery was `[carol] [env] {...}` and parseEnvelope
    required `^\[env\]`, breaking the "copy the line as-is" norm with
    LLM bots.
  - **F2-02** — strict type/range validation of the envelope
    (`hops` integer ≥0, `expires` integer >0, `trace` strings): an
    invalid envelope is treated as nonexistent (never immortal, never
    crashes). Goodbye to the `hops: -Infinity` and `expires: "garbage"`
    holes.
  - **F2-03/F2-07** — `[env]` is a protocol constant (PhantomOrg
    rejects any other marker) and `/status` exposes
    `antiloop.config` (maxHops/expireMs/marker) to detect drift against
    the org.yaml.
  - **F2-04** — with an envelope, only `env.rid` is tracked
    (authoritative); free text no longer pollutes the counter of a
    legitimate rid.
  - **F2-06** — the envelope JSON is parsed from the full first line
    (supports `}` inside strings), not with a non-greedy regex.
  - **F2-08/F2-09** — hard cap on requests map entries
    (`requestMax: 500`, LRU) and hash eviction counter
    (`evictedHashes`) — observable, not silent degradation.
  - **F2-10** — if the DM publish fails, `antiLoopRollback` reverts the
    consumed state (hash/pair/rid): the sender retry does not hit a
    false positive because of a network failure.
  - **F2-11/F2-12** — strict `Envelope.from_dict` (no silent coercion)
    and `trace_agents` removed from PhantomOrg (dead config with no
    runtime effect).
  Tests: `test-antiloop.js` (31, +11 adversarial) + E2E with F2-01
  (delivery format), F2-02 (invalid envelopes) and F2-06 (braces in
  JSON) phases. PhantomOrg: 488 pytest + 45 subtests green.
- **v1.5.4** — audit 3 bis (Copilot cross-check): MONOTONIC admission
  identity (F2-R03 HIGH + F2-R04 MEDIUM): the admission token used
  `Date.now()` as identity (`hashTs`/`pairTs`), but the timestamp is NOT
  unique — two admissions in the same ms share the ts. The rollback by
  `indexOf(pairTs)` removed the FIRST match (the mark of ANOTHER
  admission) and a hash evicted and re-registered in the same ms was
  confused with the original. Now each admission gets an `admissionId`
  (monotonic `ANTILOOP.nextAdmissionId` counter) and marks are stored as
  `{id, ts}`: rollback by `findIndex(e.id === admissionId)` in pairs and
  `hashes.get(hash).id === admissionId` in hashes. Timestamps only as
  data (sweep windows), never as identity. Fake-clock tests (fixed
  Date.now) forcing the auditor scenario: 2 admissions with the SAME ts
  and rollback in both orders + hash eviction/re-registration in the same
  ms. Tests: `test-antiloop.js` (42, +3 F2-R03/R04) + routing (14) +
  pause (9) + E2E exit 0.
- **v1.5.5** — Copilot audit 4 (full project review, outside the
  anti-loop):
  - **Scoped TLS**: the global `tls.connect` monkeypatch
    (rejectUnauthorized:false for the WHOLE process — MITM risk when
    using wss://) was replaced by a SCOPED patch: it only applies when
    `tls.connect` receives the `socket` option (the STARTTLS upgrade in
    xmpp.js) and only if `CONFIG.xmpp.rejectUnauthorized === false`.
    Key discovery: `@xmpp/starttls` does NOT propagate
    `rejectUnauthorized` from the config (it only arrives in direct TLS
    xmpps://), which is why the patch was load-bearing; now it is safe
    and localized.
  - **JSON.parse without try/catch** in `publishDM` and
    `subscribeIncoming`: an invalid relay frame could kill the process.
    Now it is ignored with a log.
  - **Configurable focus**: `allocateConference` used
    `focus.meet.example.com` hardcoded. Now
    `CONFIG.xmpp.focus` (default `focus.<meet-domain>`).
  - **/join with real state**: `joinRoom` returns `{ok, allocError?}`;
    the handler answers 502 on failure (previously ok:true always).
  - **Hardened HTTP API**: `readBody` with 64KB limit (413), stream error
    handling, invalid JSON -> 400 (previously 200 with ok:false),
    /recordings with a clear 500 error if the directory is missing.
  - **Config.antiloop validated** at startup (`num()` with clamp):
    non-numeric values no longer degrade silently (e.g. maxHops:"abc" ->
    NaN never dropped by hops).
  - **/register agents=[] documented** as intentional broadcast
    (persisted in CONFIG to survive restarts).
  - New tests: `test-http-api.js` (10) — /status, /pause, invalid
    JSON 400, large body 413, 404, /recordings.
  Suite: antiloop (42) + routing (14) + pause (9) + http-api (10) +
  E2E exit 0.
- **v1.5.6** — F2-05: content fingerprint (dedup robust to reformatting
  and paraphrase):
  - **Canonical**: NFKC -> lowercase -> no diacritics -> only [a-z0-9]
    + spaces. The dedup djb2 hash now operates on the canonical form:
    space/uppercase/punctuation/accents/emoji no longer evade dedup.
  - **Fuzzy**: unigrams + Jaccard >= `fuzzyThreshold` (default 0.85,
    configurable) against recent messages of the SAME pair:
    near-identical paraphrase (the pattern of an LLM loop that rewrites
    the message) is dropped. Conservative threshold: "confirma la
    reunion manana" vs "...hoy" gives 0.6 -> not dropped.
  - **Rids ignored** (metadata, not content): the
    {org}-{yyyymmdd}-{seq4} pattern is stripped from the content before
    the fingerprint. Closes F2-05: the bot that REMOVES the envelope and
    re-publishes with a new rid and the same body falls into dedup.
  - **/status**: exposes `fuzzyThreshold` in config and the
    `dropped.fuzzy` counter.
  Tests: `test-antiloop.js` (50, +8 F2-05) + routing (14) + pause (9)
  + http-api (10) + E2E exit 0.
- **v1.5.3** — audit 3 (Copilot cross-check): rollback by ADMISSION TOKEN
  (F2-R02, HIGH): `antiLoopCheck` returns `{ok:true, admission}` and
  `antiLoopRollback(admission)` undoes exactly that admission. The
  `await publishDM()` releases the event loop between COMMIT and
  rollback; another concurrent admission could touch the same structures
  and the blind rollback (pair pop, rid decrement, edge removal by
  (from,to,text)) undid foreign state. Now: the pair mark is removed by
  concrete timestamp (indexOf+splice), the request entry is only touched
  if it is still the SAME instance (eviction+re-creation of the RID
  protected) and edges are Map<edge,occurrences> (two concurrent
  admissions can share one; it is decremented, not removed).
  Tests: `test-antiloop.js` (39, +5 F2-R02) + routing (14) + pause (9) +
  E2E exit 0.
- **v1.5.7** — F3-01: defense against SLOW loops:
  - **hashWindowMs 10min -> 1h**: a slow loop repeats content every
    15-30 min; with 10 min the repetition fell outside the window. With
    1h, the first repetition falls into dedup (hashMax 200 is still
    trivial for 5 bots).
  - **pairHourMax 10/h (new)**: hourly pair limit. A loop with different
    content each time (radical rewrite) evaded dedup and the per-minute
    rate; now it is cut after 10 messages in 1h. Own mark in COMMIT with
    the same monotonic identity (rollback compensates). Legitimate
    traffic between two bots (one-off requests) is far below that.
  - **/status**: exposes `pairHourMax` in config.
  - Tests: antiloop (54, +4 F3-01: 1h dedup with fake clock, hourly
    limit, legitimate traffic not broken, hourly mark rollback) +
    routing (14) + pause (9) + http-api (10) + E2E exit 0.
