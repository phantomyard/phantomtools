# Changelog

## Unreleased

- **PR #21 hardening (security + additive-contract fixes)** — see
  `docs/SOURCES.md` (or the review response) for the full audit map:
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
