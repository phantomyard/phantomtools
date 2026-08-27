# Changelog

## Unreleased

- **Add `sala-send.js` — the speak-in-room tool, now a managed template.**
  `sala-send` was a hand-maintained persona tool (Mac-local, unversioned). It
  is now a PhantomMeet template (`tools/sala-send.js.j2`) deployed to every
  persona like `meeting-join.js`, with the same `ws.onerror` recursion fix.

- **`_upsert_kb` strips stale protocol duplicates from the Meetings.md suffix.**
  A previous render orphaned outside the markers (e.g. a pre-marker apply that
  preserved the old body as the suffix) left a duplicate of the managed
  protocol in the tail, and `_upsert_kb` preserved it on every re-apply. A
  stale duplicate is now detected by the protocol title header (`# … Protocolo
  de Reuniones` / `Meeting Protocol`) and stripped, keeping a trailing
  operator note (`## Estado de validación`).

- **Room authorization is role-based, not room-name based (no prefix/ACL).**
  The room name had become a proxy for the persona's role: the manifest scoped
  by prefix (`scoped: {almaponia: [alma]}`) while the bridge enforced an exact
  room name (`restricted: {almaponia: [alma]}`), so a lead could never operate
  a room whose name didn't literally match the prefix. Now the authorization
  is derived from the persona's role, and room names are free-form:
  - **Bridge** (`agentCanOperateRoom` / `evalRoomPermission`): `full` agents
    (responsible) operate any room *and* room-agnostic actions (recordings);
    every other authenticated agent operates any *named* room (join/leave/
    speak). The per-room `restricted` ACL is removed from the permission
    model and from `config.json`.
  - **PhantomMeet** `access_for` returns `full | participant | none` (no
    prefix); `_persona_context` phrases lead/support by role ("your project",
    not a `prefix-*`); `derive.py` maps org roles via `lead_roles` /
    `support_roles` (no `scoped_responsible_roles` / `restricted_prefixes`);
    the manifest `permissions` block now carries only `full`.
  - `storage.meeting_folders` is keyed by persona id (e.g. `alma`) instead of
    a project prefix (`almaponia`).

- **Add `meeting-join.js` — the join/leave tool personas were missing.**
  The Meeting protocol told personas to “send the DM `join [room] --nick
  <self>`”, but no tool could actually do it: `sala-send` wraps text as
  `[room] text` (speak, not join), and the raw-DM crypto lived only in the
  bridge. Personas improvised and misfired (e.g. a `[--help]` DM denied by
  the bridge). Now:
  - New template `templates/tools/meeting-join.js.j2` → per-persona
    `tools/meeting-join.js` (chmod 0o755), a self-contained Node tool that
    resolves nostr-tools from phantombot's shared tools dir (`createRequire`
    on `~/.local/share/phantombot/tools`, overridable via
    `PHANTOMBOT_TOOLS_DIR`), reads the persona `nsec` from its own
    `identity.json`, and sends a **raw** NIP-17 gift-wrap DM:
    `join [room] --nick <self> [--password X] [--timeout N]` / `leave [room]`.
  - The `--nick` is **always the persona's own identity** — it is never read
    from an invitation or overridable by the caller (no impersonation).
  - `_persona_context` now exposes `bridge` so the template can render the
    bridge `npub` (decoded to hex at runtime with `nip19.decode`).
  - Deployed via `manifest.tools` (all personas), not gated by `invite.roles`:
    every persona gets it, while `meeting-invite.sh` stays scheduling-only.
  - Protocol templates (es/en) updated to reference `tools/meeting-join.js`
    for join/leave instead of hand-built DMs.

- **PR #21 re-review round 6 (atomic manifest persistence)** — closes
  robertclawson's Major on the manifest-persistence path and Kai's open Major
  (manifest mutation outside `_commit_writes`):
  - **Manifest is never truncated in place**: `_persist_invite_roles` /
    `_persist_invite_card` are replaced by `_render_manifest_invite`, which
    renders the mutated manifest to a string buffer FIRST (ruamel to an
    in-memory `io.StringIO`, PyYAML as the fallback) and returns it without
    touching the file. The caller commits it with `_atomic_write` (temp +
    `os.replace`), the same durability guarantee as every persona file.
  - **Fallback re-parses `original`, never a clobbered file**: the PyYAML
    fallback reads the in-memory `original` text — a failed ruamel dump can no
    longer truncate the manifest and then persist the fragment.
  - **Narrowed the except**: only `ImportError` (ruamel absent) triggers the
    PyYAML fallback; a ruamel representer/parse error surfaces as a preflight
    error instead of being silently downgraded.
  - **Folded into the rollback batch**: the manifest write is a `_PendingWrite`
    in the same `_commit_writes` batch as the persona writes, so a failed
    manifest render aborts before any write and a failed manifest write rolls
    back with everything else (no more "personas committed, invite.roles
    absent" divergence).
  - **`parse_tool_mode` docstring/code agree**: `chmod` strings are octal —
    `"0o755"`, `"0755"` and `"755"` now all parse to the same `0o755` (the
    docstring advertised `"0755"`/`"755"` while `int(mode, 0)` parsed `"755"`
    as decimal 755 = 0o1363 and rejected `"0755"` outright).

- **PR #21 re-review round 5 (transactional apply + dead-config cleanup +
  durability + content-verified health + full password coverage)** — resolves
  the two remaining CHANGES_REQUESTED threads and robertclawson's residual:
  - **Banner idempotence**: `_supersede_legacy_kb` / `_has_supersede_banner`
    now detect the superseded banner *after* any leading OKF frontmatter, so a
    frontmatter-backed legacy note no longer gets a duplicate banner on re-apply.
  - **Dead `coordinator_chat` removed**: it was documented in manifest/SPEC/
    examples/KB templates but never used by delivery (phantombot notify is
    untargeted). Removed everywhere; KB protocol now says "the org's
    coordination group" without a phantom field.
  - **Transactional apply with rollback**: `_commit_writes` snapshots every
    destination before writing and rolls back on any failure (no partial
    deployment), and the owned delta is committed *before* the phantomchat.json
    it describes.
  - **Relay-delta durability**: the owned delta is now merged with any existing
    delta instead of being overwritten/deleted, so an operator moving the relay
    down the list no longer erases the record `pm unapply` relies on.
  - **`check-infra` compares tool content**: a tool that exists but whose
    bytes differ from the rendered template is now a FAIL (shared
    `render_tool_content`), not a presence-only OK.
  - **Password regression covers all 4 render paths**: the
    `test_meeting_invite_never_broadcasts_password` test is parametrised over
    (card | built-in) × (es | en).

- **PR #21 re-review round 4 (password out of the notify broadcast)** — the
  room password was still travelling in the body of the untargeted
  `phantombot notify` (broadcast to every authorized owner on every channel):
  - **`meeting-invite.sh` never reads nor broadcasts the password**: the
    `--password-vault` / `--password-file` flags only *declare* that the room
    is password-locked; the secret is never fetched and never rendered into
    the card. `%PASSWORD_LINE%` renders a "shared separately" notice instead,
    and the stdout redaction is gone (no secret to redact).
  - **Delivery is out-of-band**: docs state the password reaches recipients
    through a separate, targeted channel — not the broadcast.

- **PR #21 re-review round 3 (relay_npubs path)** — phantombot's untrusted
  relay tier landed (phantomyard/phantombot#400 closed by #423), so PhantomMeet
  now registers the bridge npub there instead of failing closed:
  - **Bridge npub → `relay_npubs`, never `allowed_npubs`**: `_patch_phantomchat`
    adds the bridge npub to `relay_npubs` (the untrusted relay tier — a relay
    sender is threat-screened, treated as untrusted, never arms TOFU, and
    replies as `shared` even in a 1:1 DM) and never touches `allowed_npubs`
    (a trust grant that skips the threat judge).
  - **Reversible delta extended**: `.phantommeet-phantomchat.delta.json` now
    records both the relay added and the bridge npub added; `pm unapply`
    removes both.
  - **`check-infra` validates the real path**: a bridge npub in `allowed_npubs`
    is still a FAIL, and (for personas with meeting access) a bridge npub
    missing from `relay_npubs` is also a FAIL.
  - **Docs aligned**: README + SPEC describe the `relay_npubs` configuration
    path (not the allowlist) and the corrected delta filename.

- **PR #21 re-review hardening round 2** — see
  `docs/pr-reviews/pr21-phantommeet-review-response.md`:
  - **Bridge npub never in `allowed_npubs`** (fail-closed): `allowed_npubs` is
    a trust grant (allowlisted senders skip the threat judge), so PhantomMeet
    never adds the bridge there — it only moves the private relay first. Bridge
    DMs stay gated until phantombot's `relay_npubs` tier (phantomyard/phantombot#400).
  - **Tool destinations fully contained**: `install_tools` resolves every
    manifest `dest` with the same path-traversal + symlink refusal as
    `legacy_kb_files` (previously a `dest: ../escaped.sh` wrote outside the
    persona directory).
  - **Shell-quoting serializer**: every manifest scalar rendered into
    `meeting-invite.sh` is `shlex.quote`-d (single-quoted), so a manifest
    value can no longer break out of an assignment and execute; the card is
    shell-quoted instead of a terminable heredoc.
  - **Password redacted from stdout**: dry-run never reads the real secret
    (vault *or* file), and the real run's summary redacts the password — it
    never reaches logs or terminal history.
  - **Preflight + atomic writes**: `pm apply` preflights every persona
    (rendered content, JSON validity, contained destinations, tool specs)
    before the first write; a single error aborts with no partial deployment.
    Writes are temp + `os.replace`.
  - **Reversible patch via owned delta**: `.phantommeet-phantomchat.orig.json`
    (a frozen snapshot) is replaced by `.phantommeet-phantomchat.delta.json`
    recording only the relay PhantomMeet added; a new `pm unapply` reverses it
    without touching unrelated operator config.
  - **`notify` claim corrected**: `phantombot notify` broadcasts to every
    authorized owner; the script no longer claims it delivered to a specific
    coordination group.
  - **Docs/SPEC sanitized**: the human workflow no longer teaches trusting the
    recipients line or creating a free-text join task from an invitation — the
    invitation is informational, and joining is driven by the persona's own
    scheduled task.
  - **`_upsert_kb` preserves prefix + suffix** (operator content on both sides
    of the managed block survives a re-apply); the superseded banner is
    inserted *after* any leading OKF frontmatter so it keeps parsing.
  - **`check-infra` compares content**, not just presence: a stale Meetings.md
    body, a missing tool, a bridge npub still in `allowed_npubs`, or a bridge
    npub missing from `relay_npubs` is a FAIL.

- **PR #21 hardening (security + additive-contract fixes)** — see
  `docs/pr-reviews/pr21-phantommeet-review-response.md` for the full audit map:
  - **Inbound text is never authorization**: removed the "check the recipients
    line" and "schedule it yourself" steps from the meeting protocol; the
    invitation is informational, and a persona joins only from its own
    scheduled task.
  - **MEMORY.md carries no procedural content**: the managed section is a
    one-line pointer to `kb/procedures/Meetings.md`.
  - **Cross-persona escalation removed**: `meeting-invite.sh` no longer runs
    `phantombot persona <x> --yes` and no longer plants tasks in another
    persona's runtime — it sends the invitation as a notification only.
  - **Secrets never ride argv**: room password via `--password-vault <name>`
    or `--password-file <path>`.
  - **Strict `--datetime` validation**: ISO-8601 enforced before any substring
    is used, and the room name is built with bash parameter expansion instead
    of a generated `sed` program.
  - **Meetings.md is additive**: OKF frontmatter + marker-delimited managed
    body; content outside the markers is preserved.
  - **Legacy KB files are deprecated, not deleted**: a superseded banner is
    prepended, keeping the wikilink graph intact.
  - **Path containment**: manifest-controlled paths (e.g. `legacy_kb_files`)
    are resolved and refused if they escape the persona directory.
  - **Reversible `phantomchat.json` patch**: the pre-patch state is recorded
    once to `.phantommeet-phantomchat.orig.json`.
  - **`check-infra` covers scoped personas** and verifies legacy files are
    superseded (not absent).

- **Room naming convention**: ISO-first `{YYYY-MM-DD}-{HH-MM}_{topic}`
  (e.g. `2026-08-10-18-06_asamblea_general`) — `YYYY` is now the first
  component of the code, replacing `{DD-MM-YYYY}`. `meeting-invite.sh`
  still accepts the legacy `{DD-MM-YYYY}` token (backwards compatible);
  default manifest and docs use the new format.

## v0.3.0 — 2026-08-10

**Unified recording naming + configurable invitation card + deployment
prerequisites check.**

- **Deployment prerequisites check**: `pm check-infra` now appends its
  report to a **log file** (`--log FILE`, default
  `~/.local/state/phantommeet/check-infra.log`, disable with `--no-log`) in
  addition to the screen — the persistent evidence that a host was verified
  at deploy time. The reference manifest declares the two generic
  PhantomMeet runtime prerequisites (`python3`, `bash`) plus the
  org-specific stack probes (Jitsi, relay, bridge, Whisper venv, summary
  key, finalize hook, phantombot). SPEC §11.5 documents the flow: run
  `pm check-infra` on each host at deployment; `[FAIL]` = missing
  component (reported on screen + log, exit 1), `[SKIP]` = check belongs
  to another host. §11.2 now notes the summary LLM is **user-chosen**
  (provider not fixed; reference: DeepSeek).

- **Unified naming convention**: meeting room name == recording file name,
  `{DD-MM-YYYY}-{HH-MM}_{topic}` (e.g. `10-08-2026-18-06_asamblea_general`).
  Machine-generated date/time uses hyphens; user-entered spaces/symbols are
  slugified to underscores (`slug()` → `s/[^a-z0-9]\+/_/g`). `--type` is no
  longer part of the name; the meaningful part is `--topic`.
- **Recording storage convention**: folder from `storage.drive_folder`
  (default `Grabaciones`; replaces the org-specific
  an org-specific `Reuniones/{YYYY-MM}/` layout). File naming
  `{DD-MM-YYYY} - {HH-MM} - {Nombre de sala}`.
- **Configurable invitation card** (`invite.card`): multi-line template in
  `meeting-invite.sh.j2` via heredoc `CARD_TEMPLATE`, conditional on non-empty
  (else built-in ES/EN format). Tokens: `%TITLE%`, `%RECIPIENTS%`,
  `%DATETIME%`, `%LINK%`, `%ROOM%`, `%PASSWORD_LINE%` (empty when no
  password). Blank lines stripped at send via `sed`.
- **Mandatory card tokens**: `%TITLE%`, `%DATETIME%`, `%LINK%` required;
  validation at load (`pm validate` / `pm apply`) with a clear error listing
  missing tokens.
- **Interactive card config**: `pm apply --ask-card` (empty=keep,
  `base`=restore template, `clear`=built-in, or paste multi-line card ending
  with `.` on its own line), `pm apply --card-file FILE` (one-shot).
  `_persist_invite_card` in apply.py persists via ruamel.yaml with PyYAML
  fallback.
- **Org-branded default card**: an org-branded invitation card as default
  (slogan configurable in `invite.card`).
- Backward compatible: no `invite.card` → built-in format. 7 tests green.

## v0.2.0 — 2026-08-07

**Spec v0.2 (agnostic).** PhantomMeet becomes a fully agnostic meeting
capabilities layer for PhantomForge-provisioned personas.

- **Zero hardcoded organization data**: everything organization-specific
  lives in a single YAML manifest (org, language, bridge, rooms, roles,
  permissions, storage, infra probes, derive rules).
- **Commands**: `pm derive-manifest`, `pm validate`, `pm apply`,
  `pm check-infra`, `pm discover` (see `pm --help`).
- **Idempotent apply** verified locally; en/es + multi-org proven.
- **Reference deployment**: applied to the reference installation
  (4 personas) and verified.
- **Phase 4 E2E done**: Jibri recording → Whisper transcription → DeepSeek
  summary → token-protected download URLs → Google Drive API upload by the
  responsible persona (`workspace.py drive-upload`).
- Docs: `docs/SPEC.md` (full spec + infrastructure obligations),
  `docs/meeting-workflow.md`.

## v0.1.x — 2026-08-07 (pre-agnostic)

Initial meeting capability update package for the reference
installation (Jitsi bridge participation, recording, transcription,
calendar logistics). Superseded by v0.2.0.
