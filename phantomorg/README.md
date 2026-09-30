# PhantomOrg

A compiler that models **any organization** — departments, roles, actors,
access policies, escalation matrix — and compiles it into the persona
files **Phantombot** loads: `IDENTITY.md`, `SOUL.md`, `tools.md`,
`MEMORY.md`, plus a `memory/` and `kb/` scaffold.

PhantomOrg is a tool **for Phantombot**: the output is Phantombot's
persona format by design. The target directory is configurable — see
[Deploy target](#deploy-target).

See the full technical specification in [`docs/PhantomOrg-spec.md`](docs/PhantomOrg-spec.md)
for the design, external references (Google A2A, FIPA-ACL, CrewAI,
RBAC/ABAC/PBAC) and the development backlog.

## Install

Installation is manual on all supported OSes (Linux, macOS, Windows). The
CLI itself and `po update` are OS-agnostic — Python 3.10+ and git are the
only requirements.

### One-line install (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/phantomyard/phantomtools/main/phantomorg/install.sh | bash
```

This fetches the repo and symlinks `po` / `phantomorg` into `~/.local/bin`
— usable straight from a terminal or by an agent with just a shell. When run
piped like this the script clones the repo first (it is safe standalone, it
does not assume it is inside a working tree); run from inside a checkout it
symlinks into that checkout instead. Set `PREFIX` to install elsewhere and
`PHANTOMORG_REPO_DIR` to choose where the clone lives (defaults to
`~/.local/share/phantomorg`).

### Linux / macOS — symlink install (recommended, matches the phantomyard tool convention)

```bash
./install.sh                 # symlinks bin/po and bin/phantomorg into ~/.local/bin
PREFIX=/usr/local ./install.sh   # or another prefix (may need sudo)
```

The repo stays the single source of truth: the symlinks point at `bin/po` and
`bin/phantomorg` in this checkout, so editing the repo takes effect on the
next run — no reinstall needed. Re-run `./install.sh` if you move the repo.

The install script never clobbers: it refuses to overwrite a foreign symlink
or a regular file that isn't its own (it may hold in-place edits).

Dependencies: `python3` with PyYAML, Jinja2 and click (`python3 -m pip install
--user pyyaml jinja2 click`), or a self-contained repo venv (`python3 -m venv
.venv && .venv/bin/pip install .` — the wrappers pick it up automatically).

### Windows — manual install (no installer yet)

Add this repo's `bin` directory to your PATH:

```bat
setx PATH "%PATH%;C:\path\to\phantomorg\bin"
```

(or set it via System Properties > Environment Variables). Then use `po` or
`phantomorg` from any shell — the `.cmd` wrappers resolve the repo root and
prefer `.venv\Scripts\python.exe` when the repo has a venv, else `python`.
Without the wrappers you can always run `python -m phantomorg.cli`.

### pip install (development / virtualenv)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
# Guided installation over a phantombot installation (or a fresh start):
# detects the existing personas, asks you to assign each one a department
# and a role (import-audit suggests both), lets you add new personas, and
# writes the org.yaml. One pass — then validate/build/deploy.
po setup --phantombot-dir ~/.local/share/phantombot/personas
#   -> no existing org.yaml? define the departments interactively
#   -> each detected persona: department + role (suggested, you confirm)
#   -> optionally add brand-new personas
#   -> org.yaml written; run `po validate` + `po build` + `po deploy` next

# Or bring your own org.yaml and only assign personas to it:
po setup --org organizations/my-org/org.yaml --phantombot-dir ~/.local/share/phantombot/personas

# Create an organization (optionally from a sector template)
po new-org --id my-org --name "My Org" --sector ngo --lang en --template ngo
po templates                          # list available templates

# Build the organization by hand (departments, roles, actors)
po add-department --org organizations/my-org/org.yaml --id ops --name Operations --access-policy level-2
po add-role       --org organizations/my-org/org.yaml --id lead --name "Team Lead" --department ops --access-level level-2
po add-actor      --org organizations/my-org/org.yaml --id maria --role lead --tool email --tool drive

# Edit what's already there
po rename-role   --org organizations/my-org/org.yaml --old-id lead --new-id team_lead
po remove-actor  --org organizations/my-org/org.yaml --id maria
po remove-role   --org organizations/my-org/org.yaml --id team_lead --cascade   # promotes subordinates, cleans escalation_matrix
po remove-department --org organizations/my-org/org.yaml --id ops              # blocks while it still has roles

# Validate and compile
po validate --org organizations/verdant-aquaponics/org.yaml
po build --org organizations/verdant-aquaponics/org.yaml --out ./dist
po deploy --from ./dist --target ~/.local/share/phantombot/personas/ --force   # --force only if you accept overwriting another organization

# Multi-organization
po list-orgs --base organizations
po build-all --base organizations --out ./dist            # compiles all to ./dist/<org_id>/
po deploy-all --base organizations --dist-base ./dist --target ~/.local/share/phantombot/personas/ --prune

# Undo a deploy — the system returns to exactly the state before it
po rollback --list        # show recorded deploy sessions
po rollback               # confirm + undo the last deploy
po rollback --yes         # non-interactive

# Import an existing persona (not generated by PhantomOrg)
po import-audit --persona-dir ./personas/marcos --role-id ops_lead \
  --against-org organizations/my-org/org.yaml
# ...or apply the fragment directly instead of just reviewing it:
po import-audit --persona-dir ./personas/marcos --role-id ops_lead \
  --against-org organizations/my-org/org.yaml --apply
```

`po deploy` and `po deploy-all` print a summary of what they are about to
do and ask for a final `[y/N]` confirmation before writing anything
(overwritten personas are archived to `personas-archive/` first — see
[Rollback & safety](#rollback--safety)). Pass `--yes` to skip the prompt
in scripts/CI.

All creation commands (`new-org`, `add-department`, `add-role`, `add-actor`)
also work without flags: they fall back to an interactive wizard that asks
step by step and suggests the departments/roles that already exist as
options (not free text). `po new-org` only needs `--id` and `--name` to run
non-interactively — `--sector`, `--lang` and `--template` are optional
(defaults: sector `general`, language `en`, no template). Pressing Ctrl+C
or hitting EOF in any wizard cancels cleanly without writing anything.

The `remove-*` commands ask for confirmation unless `--yes`/`-y` is passed.
`remove-role` and `remove-department` block by default if deleting would
break references (assigned actors, subordinates, `escalation_matrix`
entries); `--cascade` fixes the *structure* automatically (promotes to
root, cleans the matrix) but **never** deletes an actor in cascade — that
always requires an explicit `remove-actor` first.

### Worked example — from org.yaml to a persona tree

Here is a minimal but real spec, written by hand. Save it as
`organizations/my-org/org.yaml`:

```yaml
version: 1

organization:
  id: my-org
  name: "My Org"
  sector: ngo
  languages: [en]
  default_language: en

departments:
  - { id: management, name: "Management", parent: null, access_policy: level-2 }
  - { id: operations, name: "Operations", parent: management, access_policy: level-2 }

roles:
  - id: ceo
    name: "CEO"
    department: management
    reports_to: null
    reports_to_human: "Board President"
    functions: [vision, strategy]
    access_level: level-2
    security_exceptions: []
    description: "Direction and strategy; resolves cross-department blockers."

  - id: project_lead
    name: "Project Lead"
    department: operations
    reports_to: ceo
    functions: [projects, field_coordination]
    access_level: level-2
    security_exceptions: []
    description: "Runs the pilot project end to end."

actors:
  - id: marco
    role: ceo
    npub: npub16fg8f93njtj7nervk94w6kgtdp4vtze8dzfer2qjc394mx6luzgqavqwgg
    telegram_bot: "@marco_bot"
    tools: [email, drive, calendar]
    tools_excluded: []
    actor_exceptions: []
    tone: formal

  - id: dana
    role: project_lead
    npub: npub1ax0ysc0rz74p3j3mreylczfc658setut8g4thqv80qk0y6td3ursy8jhvm
    telegram_bot: "@dana_bot"
    tools: [email, drive, notebooklm]
    tools_excluded: []
    actor_exceptions: []

humans:
  - id: board_president
    name: "Board President"
    role: "Board President"
    telegram_user_id: 123456789
    npub: null

policies:
  access_levels:
    level-2: { label: "Operative", categories: [1, 2] }
    level-1: { label: "Restricted", categories: [1] }
  security_categories:
    category-1: { label: "Public / internal-low" }
    category-2: { label: "Confidential" }

escalation_matrix:
  - { from: project_lead, to: ceo, condition: "field blocker", cross_department: false }
  - { from: "*", to: ceo, condition: "Category 0 exception requested", cross_department: true }

communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
  norm_version: "1.5"
  envelope:
    marker: "[env]"
    ttl_hours: 6
  channels:
    human:
      platform: telegram
      group: "My Org Coordination"
      chat_id: "-1000000000001"
    agent:
      platform: phantomchat
      relay: "ws://relay.example.invalid:7777"
      bridge_npub: "npub1w6huqqg6v56jpzu757j8d6gywxndmfl2fa28neqqzwnjzxete7psswsyx9"
      human_npubs: []       # delivery identity — screened by the threat judge
      principal_npubs: []   # explicit principals only (trust); empty = fail-closed
      public_relays: ["wss://relay.damus.io"]
```

Validate it, then compile:

```bash
po validate --org organizations/my-org/org.yaml
po build --org organizations/my-org/org.yaml --out ./dist
```

The compile writes, for **each actor**, a complete Phantombot persona
under `./dist/<actor_id>/`:

```
dist/
├── marco/
│   ├── IDENTITY.md        # name, role, department, reports-to, channel, tone
│   ├── SOUL.md            # personality + functions, derived from role/access level
│   ├── tools.md           # the actor's tool list (email, drive, calendar)
│   ├── MEMORY.md          # created once, then never regenerated
│   ├── phantomchat.json   # the actor's npub for the PhantomChat channel
│   ├── norms.json         # scaffold norms — one plain-text line per norm
│   ├── .phantomorg.yaml # provenance — which org/role generated this persona
│   ├── memory/            # commitments, decisions, people, lessons scaffolds
│   └── kb/                # Home.md + procedures + templates (decision, runbook, …)
└── dana/
    └── …                  # same tree, built from project_lead instead
```

Plus two organization-wide files: `HUMANS.md` (the human registry) and
`scopes.json` (per-actor access scope derived from `policies`).

Then ship it to Phantombot's personas directory:

```bash
po deploy --from ./dist --target ~/.local/share/phantombot/personas/
# summary printed, then [y/N] before anything is written
```

**Norms are filed as drawer rows, not a markdown file.** On phantombot
≥ 1.1.282 the five memory drawers live as rows in `memory.sqlite`
(`drawer_entries`), ranked by weight + exponential decay — `memory/norms.md`
is a deprecated read path. So `po deploy` files each line of the compiled
`norms.json` as a row via `phantombot memory drawers --kind norms --file
"<line>" --persona <id>` (requires the phantombot binary ≥ 1.1.282 on the
target host; `--no-file-norms` skips it, `--phantombot-bin` points at a
non-default binary).

The generated files are **block-based**: everything PhantomOrg owns lives
inside `ORG:BEGIN … ORG:END` markers. Any notes you add outside those
blocks are preserved across re-builds. `MEMORY.md` is the exception — it is
created once and never regenerated, so a persona's accumulated memory
survives every rebuild.

### Language of generated files

`SOUL.md`/`IDENTITY.md`/`tools.md`/`MEMORY.md` are generated in the
language set by `organization.default_language` in `org.yaml` (`en` and
`es` supported today; any other value falls back to `en`). PhantomOrg is
bilingual by design and defaults to English: the language is resolved
each build with this priority — **explicit `default_language` > first
entry in `languages` > `en`**. So an organization that declares
explicitly `default_language: es` (or lists `es` first) is generated in
Spanish, while any English- or unset-language organization is generated
in English.

Only the fixed labels and phrases from the templates are translated —
the real values of the spec (role names, departments, functions,
`policies` labels) come out exactly as you wrote them, in whatever
language the organization keeps its data in. The CLI (`po ...`, messages
and errors) is always in English regardless of the organization's
language.

The repo ships two real example organizations that demonstrate the
two directions:

- `organizations/verdant-aquaponics/org.yaml` — Spanish-speaking org,
  `default_language: es`. Its generated personas come out in Spanish.
- `organizations/harbor-capital/org.yaml` — English-speaking org,
  `default_language: en`. Its generated personas come out in English.

Add a new language by adding a translated block to
`phantomorg/compiler/i18n.py` (see its docstring).

### Deploy target

The deploy command copies compiled personas into Phantombot's personas
directory. The default is `~/.local/share/phantombot/personas/`, but you
can stage the output anywhere:

- `--target <dir>` on `po deploy` / `po deploy-all`, or
- the `PHANTOMORG_TARGET_DIR` environment variable.

PhantomOrg writes the persona files Phantombot expects and checks for
collisions before overwriting — it never overwrites a persona it didn't
generate unless you pass `--force`.

### Rollback & safety

Every operation that writes something asks first, and everything it
modifies is backed up before it is touched:

- **Final confirmation.** `po deploy`, `po deploy-all` and `po setup`
  print a summary of what they are about to do and ask `[y/N]` before
  writing anything. Answering no writes nothing (`Cancelled — no changes
  were made.`). Pass `--yes` to skip the prompt in scripts/CI.
- **Personas are archived before overwrite.** When `po deploy` overwrites
  an existing persona (same organization, or `--force`), the whole
  directory is moved to `personas-archive/<name>-<timestamp>/` — the
  sibling directory Phantombot itself uses for backups, with the same
  name format. Restore with Phantombot's own command:
  `phantombot import-persona` → *Restore an archived persona*.
- **Prune archives instead of deleting.** `po deploy --prune` moves
  orphaned actors of the same organization to `personas-archive/` rather
  than removing them, so removing an actor from the spec stays
  reversible.
- **Transactional rollback — `po rollback`.** Every deploy records a
  *session* in `personas-archive/.phantomorg-manifest.json` (a dotfile
  Phantombot ignores). `po rollback` undoes the last deploy and returns
  the system to exactly the state it was in before it: archived personas
  are moved back (the backup is consumed), personas the deploy created
  are removed, and if `personas-archive/` (or the target itself) did not
  exist before that deploy it is deleted too. Stack-based: run it once
  per deploy you want to undo.

  ```
  po rollback --list     # show recorded sessions
  po rollback            # confirm + undo the last deploy
  po rollback --yes      # non-interactive
  ```

  `po rollback` refuses to run when an archived persona is missing (an
  incomplete rollback is worse than none) and warns when an `org.yaml`
  has drifted since the deploy.

  Rollback is designed so a failure never loses data and never blocks
  you permanently:

  - **Replaced content is never deleted outright.** Whatever the
    rollback replaces or removes (post-deploy versions of restored
    personas, personas the deploy created) is moved to a trash dir
    (`personas-archive/._pf_trash_<stamp>/`, a dotfile Phantombot
    ignores) and only deleted after every step has succeeded.
  - **The session entry is dropped last.** If a rollback fails mid-way
    (e.g. the trash could not be removed), the session stays recorded
    and everything that was already restored is in place. Running
    `po rollback` again then becomes a *cleanup-only* pass that finishes
    the job.
  - **Manifest writes are atomic and serialized.** Sessions are stored
    under an advisory file lock, so two concurrent deploys cannot lose
    each other's session record.
  - **Deploys are durable: a crash can never leave an untraceable
    mutation.** Before touching the target, `po deploy`/`deploy-all`
    write a journal entry (`state: in_progress`) with the planned
    archived/created/pruned personas; only after success is it
    transitioned to `committed`. If the process dies mid-deploy,
    `po rollback` sees the interrupted session and *reconciles* it:
    whatever the attempt already archived is restored, whatever it
    created is discarded. Deploying again while an interrupted session
    is unresolved is refused until you roll it back.
  - **Whole transactions are serialized.** A transaction lock
    (`.phantomorg.lock` in the runtime dir, a dotfile Phantombot
    ignores) is held for the entire deploy / deploy-all / rollback, so
    a rollback can never race a concurrent deploy on the same target.
    Rollback re-plans under the lock after you confirm and refuses if a
    new deploy was recorded in the meantime.
  - **Manifest-supplied paths are confined.** `po rollback` validates
    every entry from the manifest before planning: persona names must
    be a single safe directory component and archive dirs must be
    absolute direct children of `personas-archive/`. A corrupt or
    tampered manifest can never make rollback touch content outside
    the personas tree.
- **Deploys are atomic per persona.** Each compiled actor is first
  copied to a staging directory inside the target
  (`.pf-staging-<stamp>/`) and only swapped into place with an atomic
  rename after the previous version has been archived. A failed copy
  never leaves a half-written persona in the runtime and never consumes
  a backup without a replacement in place.
- **Symlinks are refused, never followed.** PhantomOrg never moves or
  copies through symlinks: a symlink in the compiled output, a target
  entry that is a symlink, or an archived persona that is a symlink all
  abort with a clear error instead of being followed outside the tree.
- **The archive directory is announced.** The first time `personas-archive/`
  is created, the CLI prints where backups live; each archived persona is
  listed with its exact path. Deploys end with a
  `Rollback available: po rollback` hint.
- **org.yaml is backed up before every mutation.** `add-*`, `remove-*`,
  `rename-*` and `po setup` on an existing org write
  `org.yaml.bak-<timestamp>` next to the file before modifying it and
  announce it. To undo an edit: `cp org.yaml.bak-<ts> org.yaml`.

> Note: `personas-archive/` is created automatically by PhantomOrg (or
> Phantombot) the first time something is archived. It is a dotfile-free
> sibling of `personas/`; Phantombot never reads it as a persona source.

## What it does NOT do

PhantomOrg is a **spec compiler**, not an agent runtime and not a
workflow engine. The boundaries matter, so here is what is deliberately
out of scope:

- **It does not run, host, or talk to the agents.** It only *generates*
  the persona files. Phantombot (or any loader that reads the same
  format) is what boots the persona; PhantomOrg has no runtime of its
  own.
- **It does not invent a persona's accumulated history.** `MEMORY.md` is
  created empty once and never overwritten; `memory/` and `kb/` are
  scaffolds. The agent's lived experience is the agent's own — PhantomOrg
  never regenerates or resets it.
- **It does not manage secrets or credentials.** The spec carries public
  identifiers (npub, Telegram handle, group chat id). API keys, tokens and
  vault entries belong to Phantombot's configuration, not to `org.yaml`.
- **It does not wire the live channels.** The `communication` block records
  where the organization *intends* to talk (relay, group, bridge npub), but
  PhantomOrg only compiles those values into the output; it does not
  create the Telegram bots, the Nostr keys, or the relay.
- **It does not lock `org.yaml` against concurrent edits.** Two sessions
  editing the same spec at once is a known gap (last-writer-wins); it backs
  up before every mutation but provides no multi-writer coordination.
- **It does not understand arbitrary text.** `import-audit` is a text
  heuristic: it recognizes the patterns PhantomOrg generates and a few
  common layouts, not every possible hand-written persona.
- **It does not ship on PyPI.** Installation is the symlink install (or
  `pip install -e .` for development); there is no published package.

## Structure

```
bin/                        # po / phantomorg wrappers (symlinked by install.sh)
install.sh                  # symlink installer (phantomyard tool convention)
phantomorg/
├── cli.py                  # entrypoint `po` / `phantomorg`
├── spec/                   # model, schema and org.yaml loader
├── validator/              # DAG check, cross-references, budgets
├── compiler/               # IDENTITY.md / SOUL.md / tools.md / MEMORY.md generation
├── deploy/                 # copies output to the personas directory (Phantombot by default)
└── wizard/                 # interactive commands (new-org, add-role, add-actor...)

organizations/
└── verdant-aquaponics/
    └── org.yaml            # real instance used as a test fixture

tests/                      # pytest: schema, escalation cycles, end-to-end compilation
```

## Status

Working MVP, with Epic 3 (multi-organization, sector templates,
import-audit) and the full editing cycle (remove/rename) resolved:

- `new-org` (with `--template`), `add-department`, `add-role`, `add-actor`
  — flags or interactive wizard with dynamic choices; duplicate ids are
  rejected.
- `remove-department`, `remove-role` (with structural `--cascade`, never
  deletes actors), `remove-actor`.
- `rename-department`, `rename-role`, `rename-actor` — every cross
  reference is updated automatically.
- `validate` (schema + escalation DAG + cross-references + id uniqueness
  + budgets).
- `build` / `build-all` (block-based merge with `ORG:BEGIN/END`;
  `MEMORY.md` is created once and never regenerated).
- `deploy` / `deploy-all` (collision detection between organizations,
  `--force`, `--prune` to clean up actors already removed from the spec).
- `list-orgs`, `templates`.
- `import-audit` (text heuristics + exact/substring/fuzzy resolution via
  `--against-org`, and `--apply` to write the result directly).
- `update` (self-update from GitHub Releases: `--check` / `--force`,
  exit codes 0/1/2 cron-alertable — see `CHANGELOG.md` v0.5.0).

Tests: 522 passed + 49 subtests (`unittest`, including
`click.testing.CliRunner` for the CLI in `tests/test_cli.py`). See
`CHANGELOG.md` for the detail of each iteration and which real gap
motivated each change.

Real pending work, not yet resolved: no PyPI packaging (only
`pip install -e .`); `import-audit` is still a text heuristic, it doesn't
understand arbitrary structure beyond what its patterns cover; and there
is no locking mechanism for concurrent edits on the same `org.yaml` from
two sessions at once.

## License

MIT — see [LICENSE](LICENSE).
