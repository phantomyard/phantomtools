# PhantomMeet — Specification v0.2 (agnostic)

**Status:** Draft for review — 2026-08-07
**Project language:** English (all code, docs and comments)

---

## 1. Purpose

PhantomMeet is an **agnostic capability layer** that gives AI personas of any
PhantomForge-provisioned organization everything they need to run, join and
follow up on meetings:

- **Participate** in meeting rooms as text-only attendees via a bridge
- **Record** meetings and **list** recordings
- **Transcribe** and **store** each meeting's artifacts where its responsible decides
- **Logistics**: create/invite/open meetings from Google Calendar

It is an **update package applied on top of a PhantomForge installation**: it
adds knowledge, tools and configuration to the provisioned personas without
breaking the existing installation. It is **agnostic** — it does not hardcode
any organization, persona, bridge endpoint or storage location. Everything is
driven by a **manifest** (see §5).

The goal is **autonomy**: once deployed, meetings are operated by the personas
and their human users without the technicians intervening day-to-day.

## 2. Concepts (generic)

| Concept | Meaning |
|---|---|
| **Organization** | Any persona ecosystem provisioned by PhantomForge |
| **Responsible** | A directive persona that owns a domain. Decides **where** each meeting's artifacts are stored and handles the **whole process** (create, invite, open, store, transcribe) |
| **Support persona** | A non-directive persona that contributes by **specific expertise** (coordination, domain know-how). Does not manage meetings on its own |
| **Room** | A meeting room (e.g. Jitsi MUC) with a name following a convention |
| **Bridge** | The component connecting rooms to the persona messaging network (e.g. Nostr) |
| **Artifacts** | Recording (video/audio) and transcription of a meeting |
| **Storage** | The location where artifacts are kept — **decided by the responsables together with the human users**. PhantomMeet never imposes one |

## 3. Architecture (generic)

```
┌────────────────────────── Meeting host ──────────────────────────┐
│  • Rooms (MUCs)                                                  │
│  • Recording service → artifacts (e.g. MP4)                      │
│  • Bridge: joins rooms, mirrors room chat ↔ persona DMs          │
└───────────────────────────────────────────────────────────────────┘

┌────────────────────────── Persona host ──────────────────────────┐
│  Phantombot personas (provisioned by PhantomForge)               │
│  • Messaging network (e.g. private relay + NIP-17 gift-wraps)    │
│  • identity (nsec), SOUL.md, MEMORY.md, kb/, memory/, tools/     │
└───────────────────────────────────────────────────────────────────┘
```

The bridge capabilities that PhantomMeet relies on (all configurable via
manifest):

| Capability | Mechanism |
|---|---|
| Join a room | `POST /join {"room": ...}` (localhost only) — or DM `join [sala]` / `join [https://meet…/sala]` |
| Leave a room | `POST /leave {"room": ...}` — or DM `leave [sala]` |
| Join flags | DM/HTTP accept optional `--nick <name>` and `--password <secret>` (password-locked rooms) |
| Status | `GET /status` (rooms, room nicks, personas, XMPP state) |
| List artifacts | `GET /recordings` / DM command (e.g. `grabaciones`) |
| Download artifact | `GET /recordings/:name` (path-traversal guarded) |
| Room → personas | room chat mirrored as encrypted DMs to authorized personas |
| Personas → room | persona DM `[room] text` injected into the room as `[name] text` |

## 4. Permission model (generic)

- **Ticket = invitation URL.** The way to enter a room is the **invitation
  link** (or the bare room name): a persona sends it to the bridge as a DM
  (`join [room]` / `join [https://meet…/room]`, optional `--nick`/`--password`)
  and the bridge joins that room. No room-name-prefix enforcement.
- **Auto-join via scheduled task:** on receiving an invitation (Telegram
  group/DM) with the room link **and the meeting date/time**, the persona
  schedules a `phantombot task add … --at <ISO-time>` whose prompt sends the
  `join` DM when it fires. Self-service: no technician/SSH needed.
- **Recipients are explicit:** the invitation carries a recipients line with
  mentions right after the title (`👥 Destinatarios: @pepa, @paco`). Only a
  persona whose mention is on that line may auto-join the room, and it always
  joins with **its own nick** — never another persona's. Personas not on the
  list ignore the invitation.
- **Full access:** a set of personas (responsables) — all rooms, no ticket
  needed; they can also manage rooms via the local HTTP API.
- **Support personas:** any configured persona that received the link — the
  invitation is the ticket, there is no automatic prefix restriction.
- **No access:** personas absent from the manifest are not configured.
- **Enforcement order (bridge):** room must be **active/joined** → sender must
  be a configured persona → otherwise the DM is ignored/rejected.
- **Room password (optional):** password-locked rooms require the password
  via `--password`; the bridge passes it in the XEP-0045 join.
- The manifest may still *declare* `restricted` prefixes as metadata
  (informational: who is expected to attend which rooms), but the bridge no
  longer enforces them.
- The exact mapping is defined in the manifest (§5).

## 5. Manifest-driven design

All organization-specific values live in a single YAML manifest. PhantomMeet
ships with **zero hardcoded values**.

```yaml
org: <organization id>
language: en | es            # language of generated persona content
version: <package version>
bridge:
  relay: ws://<host>:<port>  # persona messaging relay
  npub: <bridge public key>
rooms:
  suffix: "@conference.<domain>"
  naming: "{YYYY-MM-DD}-{HH-MM}_{topic}"
  active_room_required: true
roles:                       # persona → directive role
  <persona_id>: responsible | support
permissions:
  full: [<persona_id>, ...]
  restricted:                # prefix → personas allowed in those rooms
    <prefix>: [<persona_id>, ...]
storage:
  decided_by: responsible    # the tool never imposes a location
  cleanup_after_confirm: true
invite:                      # optional — meeting-invite tool (§7)
  phantombot_bin: phantombot # phantombot executable on the persona host
  coordinator_chat: "@coord"  # coordination group chat handle
  meet_base_url: https://meet.<domain>
  send_via: phantombot-notify # how invitations are delivered
  card: |                     # optional — announcement card format (fully
    📅 Meeting: %TITLE%       #   user-configurable branding). MANDATORY
    👥 Recipients: %RECIPIENTS% # tokens: %TITLE%, %DATETIME%, %LINK% — a
    🕐 %DATETIME%            #   card missing any of them is REJECTED at
    🔗 %LINK%                #   load/apply time. Optional: %RECIPIENTS%,
    %PASSWORD_LINE%          #   %ROOM%, %PASSWORD_LINE% (empty if no
                             #   password). Blank lines stripped on send.
  tool:
    template: tools/meeting-invite.sh.j2
    dest: tools/meeting-invite.sh
    chmod: 0o755
  roles: []                  # personas allowed to schedule; decided at
                             # apply time (interactive or --invite-roles)
infra:                       # optional — probes for `pm check-infra` (§5.2)
  checks:
    - name: jitsi
      type: http            # http | ws | command | file | env
      url: https://meet.<domain>
    - name: relay
      type: ws
      url: ws://<relay-host>:<port>
    - name: bridge
      type: http
      url: http://127.0.0.1:8090/status   # localhost-bound by design
      host: vps                          # only run when --host vps
    - name: whisper-venv
      type: file
      path: /opt/whisper-transcribe/venv/bin/python
      host: vps
    - name: summary-env
      type: env
      path: /opt/whisper-transcribe/.env
      key: DEEPSEEK_API_KEY
      host: vps
  persona_checks:            # optional — per-persona capability probes
    - persona: <persona_id>
      type: command
      cmd: "<read-only shell command>"
```

See `examples/example-org.yaml` for a complete reference manifest.

### 5.1 Deriving the manifest from a PhantomForge org model

The manifest can be **generated automatically** from the organization's
PhantomForge org model (`org.yaml`), so the meeting capability is granted
*intrinsically* to the roles declared as directive/support — no
hand-maintained persona list to keep in sync:

```bash
pm derive-manifest \
    --org organizations/<org-id>/org.yaml \
    --base examples/<org-id>.base.yaml \
    --out examples/<org-id>.yaml
```

The org model supplies the hierarchy (departments, roles, actors); a small
**base manifest** supplies everything the org model does not know about
(bridge endpoint, room naming, storage policy, org knowledge) plus the
**derive rules**:

```yaml
derive:
  directive_roles: [ceo, chief_of_staff, cfo]   # → responsible (full access)
  support_roles: [project_lead, training_lead]  # → support (restricted)
  restricted_prefixes:
    project_lead: almaponia    # support room-name prefix per role
    training_lead: formacion
```

- **Directive** org roles → `responsible` personas (full access).
- **Support** org roles → `support` personas, restricted to rooms whose name
  starts with the role's prefix.
- Actors whose role is not in the derive rules get **no** meeting capability
  (they are reported as warnings, never silently granted).
- Adding an actor to `org.yaml` and re-deriving is enough to grant (or revoke)
  the capability — the manifest is derived, not edited.

### 5.2 Infrastructure verification (`pm check-infra`)

`pm check-infra` **verifies that the infrastructure a deployment depends on is
reachable and healthy**, and that every persona is fully applied. It is
**read-only** — it never writes, starts or stops anything; it only probes.

```bash
# Infrastructure probes + per-persona applied-state checks
pm check-infra --manifest examples/<org>.yaml \
               --target ~/.local/share/phantombot/personas
```

Probe types (declared in `infra.checks[]`):

| Type | What it verifies | Example |
|---|---|---|
| `http` | GET a URL answers (2xx by default, or `expect: <code>`) | bridge status API, Jitsi front door |
| `ws` | Real WebSocket handshake + NIP-01 `REQ`/`EOSE` round-trip | private Nostr relay |
| `command` | A read-only shell command exits 0 | org-specific probes (auth checks, services) |
| `file` | A path exists (optional `contains:` substring, `non_empty: true`) | transcription tooling installed |
| `env` | A KEY=VALUE file has a non-empty value for `key:` | LLM/API key for summaries |

**Machine-scoped checks:** a check may declare `host:` (e.g. `host: vps`). It
only runs when `--host` matches (`pm check-infra --host vps`); otherwise it is
reported as `SKIP`, not FAIL. This lets orgs declare machine-local probes
(whisper venv, API keys, bridge on 127.0.0.1) without noisy failures when the
check runs from another machine.

With `--target`, it also verifies each persona from the manifest is **fully
applied**: `Meetings.md` present, legacy kb files removed, MEMORY markers in
place, private relay first + bridge pubkey allowed in `phantomchat.json`.
This mirrors the `apply` logic exactly (read-only).

The report goes to the **screen** and (unless `--no-log`) is **appended to a
log file**: `--log FILE` to choose the path, default
`~/.local/state/phantommeet/check-infra.log`. This makes `check-infra` the
deployment **prerequisites check** (§11.5): the persistent log is the
evidence that the host was verified at deploy time.

**Where to run it:** every probe runs where its endpoint is reachable. Checks
with `host: vps` (bridge HTTP API on 127.0.0.1, whisper venv, summary env,
finalize hook) are verified with `--host vps` on the meeting host. Exit code
is 0 when every non-skipped check passes, 1 otherwise (CI-friendly).

## 6. Meeting lifecycle (generic)

> Human-facing walkthrough: see [Meeting Workflow — Human User Guide](meeting-workflow.md).

The generic lifecycle (see the user guide for a concrete, narrative example):

| Phase | Actors | Steps |
|---|---|---|
| 1. Plan | Responsible | Create calendar event (topic, date, invitees) |
| 2. Open | Responsible | Create/join room per naming convention; ensure room active in bridge |
| 3. Participate | Humans + personas | Humans: browser (AV). Personas: text via bridge (`[room] text`) |
| 4. Record | System | Recording service → artifact |
| 5. Retrieve | Responsible / support | Ask bridge for the list; download via HTTP API |
| 6. Store | Responsible | Upload to the human-established location |
| 7. Transcribe | Responsible (or support by expertise) | Transcription of the artifact |
| 8. Cleanup | Responsible | Confirm storage, delete artifact from host |

### 6.1 Storing artifacts — Google Drive API upload (reference case)

The **Store** phase is intentionally implemented against the **Google Drive
API** when the persona's Google Workspace access is available. This matches
the reference deployment, where the responsible persona already has Drive
access through her own workspace tooling (e.g. a service account with
domain-wide delegation impersonating an org account); no extra credentials
are provisioned by PhantomMeet.

Tool provided to the persona:

```
workspace.py drive-upload <url-or-local-path> --folder <folder-name>
```

- **Input**: either a **signed HTTPS URL** served by the meeting host
  (token-protected, expiring — bridge `downloadBase`/`downloadSecretFile`/
  `downloadExpiryHours` config) or a **local file** path on the persona host.
- **Behavior**: downloads the artifact if given a URL, creates the target
  Drive folder if missing, uploads with a **multipart request** via the
  Google Drive API, and returns the share link.
- **Auth model**: whatever the persona already uses — service account with
  domain-wide delegation or OAuth2 tokens. PhantomMeet only
  documents the capability; it never manages Google credentials itself.
- **Verify**: the persona confirms with its existing Drive listing tool
  (`workspace.py drive`).

The same pattern applies to any persona whose host has Google Drive API
access: the artifact URL + upload tool make the flow fully autonomous without
a technician (see §10 Roadmap → “autonomy”).

## 7. Update package (what PhantomMeet applies)

Applied **idempotently** to a PhantomForge persona installation:

- `kb/procedures/Meetings.md` — meeting protocol per persona
  (rendered from templates; role-aware: responsible vs support).
- `MEMORY.md` — compact "Meetings" section (bounded by
  `<!-- phantommeet:start -->` / `<!-- phantommeet:end -->` markers).
- `phantomchat.json` (or equivalent) — ensure private relay first in relays
  list; ensure bridge pubkey in `allowed_npubs`.
- Meeting invitation tool (`meeting-invite.sh`) — installed into `tools/` of
  the personas listed in `invite.roles`, rendered from the manifest with no
  hardcoded values.

### 7.1 Deciding who may schedule (`invite.roles`)

The manifest ships with an empty `invite.roles` list. The first `pm apply`
prompts the operator interactively:

1. `discover()` scans the target for installed personas (presence of
   `identity.json`, `SOUL.md` or `phantomchat.json`) and, when available,
   cross-references the PhantomForge org model (`organizations/<org>/org.yaml`)
   to annotate each persona with its org role.
2. The operator picks who may create invitations — by number or name,
   comma-separated. The choice is persisted to the manifest as
   `invite.roles` (skipped with `--dry-run`).
3. Re-applying is idempotent: the prompt shows current selections and is
   skipped entirely unless `--ask-roles` is passed, or until a fresh install
   has no `invite.roles` yet.
4. Non-interactive installs use `--invite-roles <a,b>` (one-shot; not
   persisted to the manifest).

Only the selected personas get `meeting-invite.sh` in their `tools/`; the
others receive invitations but never schedule their own meetings.

### 7.2 The `meeting-invite.sh` tool

A bash script rendered from `templates/tools/meeting-invite.sh.j2`:

- derives the room name from the `rooms.naming` convention
  (`{YYYY-MM-DD}-{HH-MM}_{topic}` by default; the user-entered topic is
  lowercased, accent-stripped and its spaces become `_`; `--topic` is optional
  and its slot is dropped when omitted);
- builds the invitation text — the **announcement card** — from the
  `invite.card` template (fully user-configurable branding: company name,
  slogan, separators, emoji, signature). Mandatory tokens `%TITLE%`,
  `%DATETIME%`, `%LINK%` are enforced at load/apply time — a card missing
  any of them is rejected. Optional tokens: `%RECIPIENTS%`, `%ROOM%`,
  `%PASSWORD_LINE%` (empty if no password). Blank lines are stripped on
  send. Without `invite.card`, a built-in language-aware format is used
  (📅 title, 👥 recipients, 🕐 start time, 🔗 room link, optional 🔒
  password). The card is configured at install time (edit the manifest and
  `pm apply`) and can be changed any time afterwards the same way;
- optionally schedules the persona's own auto-join task via
  `phantombot task add` (`--self-join`);
- sends the invitation to `invite.coordinator_chat` via `phantombot notify`
  (delivery mechanism is manifest-driven: `send_via`);
- never schedules tasks or sends messages on behalf of other personas —
  recipients self-program on receipt, per the protocol;
- `--dry-run` prints everything without side effects.

Applying PhantomMeet must never break the existing installation:
`--dry-run` reports every change before writing; all operations are
idempotent (safe to re-run).

## 8. Security

- Private messaging network: whitelist + encrypted DMs (e.g. NIP-17 gift-wraps).
- Bridge HTTP API bound to localhost.
- Artifact downloads path-traversal guarded.
- Secrets in persona identity files with restricted permissions; bridge
  credentials only on the host.
- Artifacts removed from the host after confirmed storage.

## 9. Integration with PhantomForge

- Personas are provisioned by PhantomForge (`pf build` / `pf deploy`) from an
  org model.
- PhantomMeet sits **on top**: it does not modify PhantomForge itself, only the
  deployed personas. Later, when PhantomForge is translated to English, deeper
  model-level integration (meeting capabilities as org fields) becomes possible.

## 10. Roadmap

- **Phase 1** — Agnostic spec (this document) + manifest schema.
- **Phase 2** — Update package (`src/`): templates, apply script, CLI, examples.
- **Phase 3** — Apply to a real installation (reference deployment:
  the reference deployment); solve issues as they arise.
- **Phase 4** — E2E verification of the full meeting lifecycle (**done** in the
  reference deployment: Jibri recording → local Whisper transcription →
  DeepSeek summary → token-protected download URLs → Google Drive API upload
  by the responsible persona via `workspace.py drive-upload`).
- **Phase 5** — GitHub publication (English) **+ self-update cycle** mirroring
  phantombot's: `pm update --check/--force/--restart` from GitHub Releases
  (SHA256-verified, cron-friendly exit codes 0/1/2). The same cycle is
  planned for **PhantomForge** (`pf update`) — both tools will be
  updateable like phantombot once published.

## 11. Infrastructure, third-party software & maintenance

PhantomMeet is a **thin layer on top of a substantial stack of self-hosted
and third-party components**. Anyone deploying it must understand and accept
the infrastructure and maintenance obligations below. This is **not an
"install and forget" project**: most of the value lives in components that
the community or external vendors maintain, and they all require ongoing
care.

### 11.1 Required infrastructure

| Component | Role | Minimum footprint | Notes |
|---|---|---|---|
| **Meeting host** | Meeting rooms, recording service, bridge | VPS: 2+ vCPU, 4+ GB RAM, 20+ GB disk | Reference deployment: Ubuntu 24.04 LTS, Apache TLS → nginx → Prosody/JVB/Jicofo/Jibri |
| **Persona host** | Phantombot personas (provisioned by PhantomForge) | Always-on machine running the personas | Can be a desktop or laptop (reference deployment) |
| **Domain + TLS** | Public URL for meeting rooms | DNS record + TLS certificate | Required for browser access; keep the certificate auto-renewal working (e.g. Let's Encrypt) |
| **Private messaging relay** | Persona ↔ bridge encrypted DMs | Lightweight process (e.g. nostr-rs-relay) | Can co-locate with the meeting host; needs whitelist + NIP-17 gift-wrap support |
| **Storage** | Recordings before archival | Disk on the meeting host + organization Drive | Cleaned up after confirmed upload (§11.3) |

Reference network layout:

```
Internet ──► 443/4443  meeting host (Jitsi web/media)
                 ├─ 5222  Prosody XMPP (internal, localhost/firewalled)
                 ├─ 7777  private relay (whitelist-only)
                 └─ 8090  bridge HTTP API (bound to localhost only)
```

### 11.2 Third-party software & services

| Component | What it does | Maintainer | Maintenance matters because… |
|---|---|---|---|
| **Jitsi Meet** (Prosody, Jicofo, Jitsi Videobridge, Jibri) | Meeting rooms + recording | 8x8 / community | Security and stability; recording reliability; version upgrades change config keys |
| **nostr-rs-relay** | Private relay | Community (Rust) | The reference deployment **patches it locally** (whitelist + NIP-17 gift-wraps); upstream updates must be **re-based on the local patch** |
| **Node.js + nostr-tools / @xmpp** | Bridge runtime and libraries | Community | Keep Node on a supported LTS; library updates can change APIs |
| **Phantombot** | Persona runtime | Internal | Internal project; updates must not break persona configs |
| **PhantomForge** | Persona provisioning engine | Internal | Internal project; PhantomMeet sits on top of it |
| **Google Workspace** (Calendar, Drive) | Meeting logistics + artifact storage | Google | API changes; OAuth refresh tokens expire; account policies can change |
| **Whisper** (faster-whisper, local) | Transcription / meeting summaries | Community (open source) | Self-hosted in an isolated venv on the meeting host; no API costs, no external dependency |
| **LLM API for summaries** (user-chosen provider) | Automatic meeting summaries | External (user-chosen, e.g. DeepSeek) | Provider is **not fixed** — each deployment picks its own LLM; cost, rate limits and key rotation are the deployer's to manage. Configured via the `infra` `env` probe (e.g. `DEEPSEEK_API_KEY`) |

> PhantomMeet itself has exactly two runtime prerequisites (checked by
> `pm check-infra`, see §11.5): a **Python 3.10+** interpreter for the CLI
> and **bash** for the deployed `meeting-invite` tool. Everything else in
> this table is the stack PhantomMeet documents but does not install.

### 11.3 Maintenance obligations

- **Security updates** — OS packages, Jitsi, relay, Node and Rust toolchains.
- **Relay patch maintenance** — the local whitelist/gift-wrap patch must be
  re-applied after every upstream relay update.
- **Service health monitoring** — systemd units; bridge health via `GET /status`.
- **Recording cleanup** — delete artifacts from the host after confirmed upload
  to the organization storage (the responsible persona does this).
- **Disk space & log rotation** — recordings and logs grow without bound if
  nobody watches them.
- **TLS certificate renewal** — broken renewal = meeting links stop working.
- **OAuth refresh tokens** — Google Calendar/Drive access requires periodic
  re-authentication.
- **Toolchain hygiene** — the Rust toolchain on the meeting host exists only
  to build the patched relay; once the relay is updated/built it can be
  removed from the host.

### 11.4 Operational risk summary

If the obligations above are neglected, the most likely failures are:

| Failure | Likely cause |
|---|---|
| Bridge crashes on restart (TLS error: *client network socket disconnected before secure TLS connection was established*) | Startup race: the bridge connected to the Nostr relay before XMPP was online, replayed historical DMs (incl. `join`), and sent the focus IQ while the STARTTLS handshake was still in progress → Prosody closed the socket. Fix: wait for XMPP `online` before connecting to the relay (implemented in the deployed bridge). |
| Bridge stops mirroring messages | XMPP connection drops after Prosody/relay changes |
| Recordings stop being produced | Jibri misconfiguration or upgrade drift |
| Persona DMs stop arriving | Relay upgrade overwrote the whitelist/gift-wrap patch |
| Meeting links break | TLS certificate expired |
| Host disk fills up | Recordings/logs not cleaned or rotated |

`pm check-infra` (§5.2) turns the first three rows into a **single automated
probe**: run it on the meeting host (bridge + relay + Jitsi) and on the
persona host (`--target`) to confirm the whole deployment is healthy before a
meeting. It does not replace the maintenance obligations — it makes their
failure visible early.

**Bottom line:** deploying PhantomMeet means taking responsibility for a
self-hosted Jitsi + Nostr relay + Google Workspace stack. Plan maintenance
before you plan a production roll-out.

### 11.5 Deployment prerequisites check (`pm check-infra --log`)

PhantomMeet **documents** its third-party needs (§11.1, §11.2) but **never
installs them** — provisioning is the deployer's job on their own hardware,
and the machine being local or remote is a user decision. What PhantomMeet
**does** provide is a read-only availability check to run **at deployment
time**: `pm check-infra` probes the declared needs and reports what is
present and what is missing.

```bash
# On every host that is part of the deployment:
pm check-infra --manifest examples/<org>.yaml \
               --host <this-machine> \
               --target ~/.local/share/phantombot/personas   # persona host only
```

- **Screen**: every probe is printed with `[OK]` / `[FAIL]` / `[SKIP]` and a
detail line.
- **Log**: the same report is appended to a **log file** —
`--log FILE` to choose the path, default
`~/.local/state/phantommeet/check-infra.log` (disable with `--no-log`). The
log is the persistent record the deployer keeps as evidence that the host
was verified.
- **Exit code**: `0` when every non-skipped check passed, `1` otherwise —
CI-friendly.

**How to react to a `[FAIL]`:** the check only reports; it never installs
anything. Fix the missing component on your hardware (install the package,
start the service, refresh the key…) and re-run. A `[SKIP]` just means the
check belongs to another machine (`host:` mismatch) — re-run with the right
`--host` or on the right box.

The generic checks in the reference manifest (`python3`, `bash`) cover
PhantomMeet's own two runtime prerequisites; the org-specific checks cover
that deployment's stack (Jitsi, relay, bridge, Whisper venv, summary key,
finalize hook, phantombot).
