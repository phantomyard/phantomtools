# PhantomMeet

**Agnostic meeting capabilities layer for PhantomForge-provisioned personas.**

PhantomMeet gives AI personas of any PhantomForge-provisioned organization
everything they need to run, join, and follow up on meetings:

- **Participate** in meeting rooms as text-only attendees via a bridge
- **Record** meetings and **list** recordings
- **Transcribe** and **store** each meeting's artifacts where its responsible decides
- **Upload** artifacts to Google Drive with the **Google Drive API** via
  `workspace.py drive-upload` (a persona whose workspace tooling already has
  Drive access — service account with domain-wide delegation or OAuth2).
  See [docs/SPEC.md §6.1](docs/SPEC.md#61-storing-artifacts--google-drive-api-upload-reference-case)
- **Logistics**: create/invite/open meetings from Google Calendar
- **Configurable invitation card**: multi-line template with tokens
  (`%TITLE%`, `%RECIPIENTS%`, `%DATETIME%`, `%LINK%`, `%ROOM%`,
  `%PASSWORD_LINE%`), validated at load — `%TITLE%`, `%DATETIME%`, `%LINK%`
  mandatory, the rest optional. Set via `pm apply --ask-card` (interactive)
  or `--card-file FILE` (one-shot); `base` restores the packaged template,
  `clear` falls back to the built-in ES/EN format.
- **Unified recording naming**: meeting room name == recording file name
  (`{YYYY-MM-DD}-{HH-MM}_{topic}`); recording storage in the org's
  `storage.drive_folder` (e.g. `Grabaciones`).

It is an **update package applied on top of a PhantomForge installation**: it
adds knowledge, tools and configuration to the provisioned personas without
breaking the existing installation.

## Agnostic by design

PhantomMeet contains **zero hardcoded organization data**. Everything
organization-specific lives in a single YAML **manifest**:

```yaml
org: my-org
language: en            # en | es  (language of generated persona content)
bridge:
  relay: ws://host:7777
  npub: npub-bridge
rooms:
  suffix: "@conference.domain"
  naming: "{YYYY-MM-DD}-{HH-MM}_{topic}"
roles:                  # persona → directive role
  maria: responsible
  juan: support
permissions:
  full: [maria]
  restricted:
    projectx: [juan]
storage:
  decided_by: responsible   # the tool never imposes a location
  cleanup_after_confirm: true
```

See `examples/example-org.yaml` for a complete reference manifest.

## What it applies (idempotent)

Per persona, PhantomMeet manages:

- `kb/procedures/Meetings.md` — role-aware meeting protocol
  (responsible vs support; full vs restricted access)
- `MEMORY.md` — compact "Meetings" section between
  `<!-- phantommeet:start -->` / `<!-- phantommeet:end -->` markers
- `phantomchat.json` — private relay moved first; bridge npub added to
  `allowed_npubs`

Re-running the apply is safe: unchanged files are skipped.

## Usage

> **New to PhantomMeet?** Read the [Meeting Workflow — Human User Guide](docs/meeting-workflow.md) first: it explains how a real meeting works end to end, with no technical knowledge required.

```bash
# Derive the manifest from a PhantomForge org model + base manifest
pm derive-manifest --org organizations/<org>/org.yaml \
                  --base examples/<org>.base.yaml \
                  --out examples/<org>.yaml

# Validate a manifest
pm validate --manifest examples/example-org.yaml

# Preview every change without writing
pm apply --manifest examples/example-org.yaml \
         --target ~/.local/share/phantombot/personas --dry-run

# Apply
pm apply --manifest examples/example-org.yaml \
         --target ~/.local/share/phantombot/personas

# Apply, with optional interactive card setup
pm apply --manifest examples/example-org.yaml \
         --target ~/.local/share/phantombot/personas --ask-card

# Verify infrastructure is healthy (read-only) + personas fully applied.
# Report goes to the screen AND to a log file (default
# ~/.local/state/phantommeet/check-infra.log; --log FILE to choose, --no-log to
# disable). Run this on every host at deployment time — it is the
# prerequisites check for the third-party software PhantomMeet needs.
pm check-infra --manifest examples/example-org.yaml \
               --target ~/.local/share/phantombot/personas
```

The manifest is **derived, not edited**: `derive-manifest` reads the
organization hierarchy from a PhantomForge `org.yaml` (directive org roles
→ `responsible`, support org roles → `support` with a restricted room
prefix). See [`docs/SPEC.md` §5.1](docs/SPEC.md#51-deriving-the-manifest-from-a-phantomforge-org-model).

`check-infra` probes the endpoints declared in the manifest's `infra` section
(HTTP, WebSocket NIP-01 round-trip, read-only commands, file/env checks for
local tooling like whisper + summary keys) and verifies every persona is fully
applied. Checks may declare `host:` to run only on a given machine
(`--host vps`); non-matching ones are SKIPped, not failed. Run it on the
meeting host for a complete check. Exit code 0 = healthy. The report is
printed to the screen **and** appended to a log file (`--log FILE`, default
`~/.local/state/phantommeet/check-infra.log`, disable with `--no-log`) — this
is the deployment prerequisites check: PhantomMeet documents its
third-party needs but never installs them; run `check-infra` on each host at
deploy time to verify availability and catch anything missing. See
[`docs/SPEC.md` §5.2](docs/SPEC.md#52-infrastructure-verification-pm-check-infra)
and [`§11.5`](docs/SPEC.md#115-deployment-prerequisites-check-pm-check-infra---log).

## Repository Layout

```
phantommeet/
├── README.md
├── LICENSE                # MIT
├── CHANGELOG.md
├── pyproject.toml         # Package: v0.3.0, Python ≥3.10, deps PyYAML/Jinja2/click
├── install.sh             # Portable install (symlinks bin/ to PATH)
├── bin/                   # CLI wrappers: pm, phantommeet (+ .cmd for Windows)
├── docs/                  # SPEC.md, meeting-workflow.md, GITHUB-READINESS.md
├── examples/              # Base + derived reference manifests (org-agnostic, placeholders)
├── src/phantommeet/       # Package: manifest, derive, infra, apply, discovery, CLI
│   └── templates/
│       ├── kb/            # Meeting protocol templates (en/es)
│       ├── memory/        # MEMORY section templates (en/es)
│       └── tools/         # meeting-invite.sh.j2 (invitation card template)
├── tests/                 # test_smoke.py + fixtures (7 tests)
└── .github/workflows/ci.yml  # Standalone CI: lint + bandit + tests + smoke
```

## Dependencies & maintenance

PhantomMeet is a thin layer on top of a substantial stack: **self-hosted
Jitsi** (Prosody, Jicofo, Jitsi Videobridge, Jibri), a **private Nostr relay**
(locally patched), **Phantombot** personas and **Google Workspace**
(Calendar/Drive). Transcription is done locally with **Whisper**
(faster-whisper, no API costs).

All of these require ongoing maintenance (security updates, relay patch
re-basing, recording cleanup, TLS renewal, OAuth refresh). Read
[`docs/SPEC.md` §11](docs/SPEC.md#11-infrastructure-third-party-software--maintenance)
for the full infrastructure, dependency and maintenance obligations before
deploying. This is **not an install-and-forget project**.

## Status

- **2026-08-07** — Spec v0.2 (agnostic). Update package v0.2.0 implemented and
  tested locally (idempotency verified; en/es + multi-org proven). Applied to
  the reference installation and verified.
- **2026-08-07** — Phase 4 E2E done in the reference deployment: Jibri
  recording → Whisper transcription → DeepSeek summary → token-protected
  download URLs → **Google Drive API upload** by the responsible persona
  (`workspace.py drive-upload`) — fully autonomous, no
  technician needed in day-to-day operation.
- **2026-08-10** — v0.3.0: unified recording naming (`{YYYY-MM-DD}-{HH-MM}_{topic}`, room == file), `storage.drive_folder` convention, and a fully configurable invitation card (`invite.card`, mandatory-token validation, `--ask-card` / `--card-file` interactive setup, org-branded default).

Development happens in the **PhantomTools monorepo** (private):
`salvaalba-dev/phantomtools` — PhantomMeet lives in the `phantommeet/` tool
subdirectory, same pattern as PhantomForge. The subtree carries its own
standalone CI (lint + bandit + tests + smoke).

## License

MIT — see [LICENSE](LICENSE).
