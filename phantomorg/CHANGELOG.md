# Changelog

## v0.5.11 — 2026-08-19

**Additive deploy + phantomyard PR #25 review hardening.**

- **Additive deploy** (`deploy/target.py`): `po deploy` writes only the files
  PhantomOrg owns, in place, atomically per file — it never moves, replaces,
  or archives the live persona directory. `identity.json`, `vault.sqlite`,
  `memory/`, `kb/` notes, and all runtime-owned files survive a redeploy.
  Overwritten owned files are backed up per-file into `personas-archive/` and
  `po rollback` restores them. There is no whole-directory replacement mode:
  a fresh persona is a runtime-owned lifecycle operation, never a compiler
  deploy.
- **Prune reverts only owned regions** (`deploy/target.py`): pruning an actor
  no longer in the spec archives and removes only PhantomOrg-owned content —
  "plain" files are removed, "merge" files keep everything outside the
  ORG:BEGIN/END markers, and "seed"/runtime files are left byte-for-byte.
  The persona directory itself is never removed.
- **Principal-only trust** (`phantomchat_gen.py`): `allowed_npubs` now
  contains only the explicit `principal_npubs` (empty by default, fail-closed).
  The shared human group identity (`human_npubs`), the bridge and relays are
  delivery endpoints, never promoted to principal trust. `greeted` stays empty.
- **memory/norms.md is seed-only**: the drawer is owned by the
  capture/heartbeat/nightly pipeline; the compiler seeds it once (a pointer)
  and never overwrites it. The communication norm lives in the KB as an
  OKF-frontmatter procedure (`kb/procedures/comunicacion-agentes.md`).
- **SOUL.md**: platform security/trust/prompt-injection content plus voice,
  communication, and working-memory guidance seeded outside ORG blocks;
  access levels labeled non-enforcing.
- **Stale-output reconciliation** (`compiler/build.py`): removed actors and
  obsolete derived artifacts are cleaned when reusing an output directory.
- **Collision-safe data-file backups** (UUID suffix) and **non-zero exit**
  for partial `deploy-all` / `build-all`.
- **CI** wired at the repository root: ruff check + format, bandit, mypy, and
  the full test suite.
- **Synthetic fixtures** throughout organizations/, docs/, CHANGELOG, and
  tests — fictional org/person/project names AND newly generated synthetic
  npubs (no real keys carried over).

## v0.5.10 — 2026-08-12

**Humans registry + Telegram drift protection + i18n.** Release covering
all phantomorg work since v0.5.9 (Aug 11–12): per-actor memory scopes,
npub schema + phantomchat-check, communication norms v1.2–v1.5,
phantomchat.json compilation, full English translation, the first-class
`humans:` block, and the new `po telegram-check` anti-drift command.

- **`humans:` first-class registry** (`spec/model.py`, `spec/
  shape_validator.py`, `schema.json`, wizard, `compiler/humans.py`,
  `deploy/target.py`): org.yaml can now declare the org's humans
  (id/name/role/telegram_user_id/npub). `po build` generates an
  org-wide `HUMANS.md` registry (`__humans__` output); `po deploy`
  ships it to the data dir. `humans` is optional and an empty array
  is valid (unlike roles/actors). Deployed for Verdant Aquaponics Co-op:
  mar, julia, leo, mirta.
- **`po telegram-check`** (new command, `compiler/telegram.py`):
  contrasts every declared `actors[].telegram_bot` handle against the
  LIVE bot via Telegram's public `getMe`. Token resolution mirrors the
  phantombot runtime: sub-persona token wins; otherwise the main token
  applies to the default persona — and `state.json`'s
  `default_persona` overrides `config.toml`'s (a stale state override
  is detectable, e.g. the Aug 11 CEO→diego drift). Statuses: ok /
  mismatch / no-token / not-declared / error. Non-invasive, works from
  any host with internet, exit 0/1 for CI, `--json` for automation.
- **`telegram_bot` shape validation** (`shape_validator.py`): handle
  must be `@` + 5..32 chars of `[A-Za-z0-9_]` at `po validate` — the
  Aug 12 drift (@COS_AU_bot/@PL_AU_bot/@Training_AU_bot aspirational
  names) is now caught at validate time AND live-check time.
- **Aligned AU org.yaml telegram_bot handles** with the deployed bots
  (getMe-verified): @PA_AU_bot (lucia), @Dana_AU_bot (dana),
  @Elias_AU_bot (elias).
- **i18n:** remaining Spanish prose translated to English (forge, meet,
  docs; commits 29b629a, 9db188e).
- **Norms v1.2–v1.5** (`compiler/norma*`): compile-time-agnostic
  agent-communication v1.2 (separate channels), v1.3 anti-loop
  envelope, v1.4 rid rule, v1.5 loop lifecycle
  (24h timeout, 2-retry backoff, unique rid).
- **`phantomchat.json` compiled from org.yaml** (bridge integration,
  96ff3c2).
- **npub schema + `po phantomchat-check` + build warnings** (89f5ad7).
- **Per-actor memory scopes** derived from the org model
  (chain/department, edfb1a0).

**Verification:** 519 passed + 45 subtests, 0 failures. Live-verified
against the Verdant Aquaponics Co-op runtime (5/5 telegram handles match;
stale-handle drift detected).

## v0.5.9 — 2026-08-10

**Exhaustive rollback fault-injection audit (Board President's methodology) —
crash after every FS operation, verify final state == exact pre-deploy
state.**

- **New test suite `tests/test_rollback_fault_injection.py` (698 lines):**
  - **Probe (2 tests):** records the REAL filesystem operation sequence
    of a rollback (via injected faults on mkdir/rmdir/move/rmtree/
    unlink/replace) and compares it against a canonical enumeration.
    Any code drift breaks the probe before the crash matrix can
    silently lose coverage.
  - **Crash matrix (36 subtests, 2 scenarios):** scenario A = fresh
    deploy (16 ops); scenario B = pre-existing target/root with foreign
    content + second deploy (20 ops). Every operation is crashed in
    turn, then retried through the real CLI (`po rollback --yes`); the
    final state must equal the pre-deploy state byte-for-byte,
    including file permissions (mode bits) and the manifest bytes. The
    only crash that drops the session (final archive-root rmtree) must
    exit 1 with "Nothing to roll back".
  - **Residue handling (3 tests):** foreign-pid `*.tmp` swept by retry;
    half-removed trash (interrupted rmtree) swept by retry; older
    session's evidence trash preserved when rolling back a newer
    session (only the owning session consumes its trash).
- **`session.py`:**
  - `remove_abandoned_archive_root()`: when a rollback's session was
    already dropped but the final best-effort root removal crashed,
    the CLI now removes the genuinely-empty leftover archive root so
    the system returns to exactly its pre-deploy state (never touches a
    root with sessions, archives, or evidence-bearing trash).
  - `execute_rollback` wraps the pre-mutation OSError path in a
    retryable `RollbackError` (uniform CLI reporting, no raw
    traceback; nothing changed, retry always safe).
  - Trash sweep scoped to the rolling-back session: removes the
    session's own trash dirs plus any left by interrupted predecessors
    of the SAME session; preserves older sessions' evidence trash;
    empty trash dirs swept regardless (no evidence value).
  - Orphan `*.tmp` sweep under the manifest lock (residue of crashed
    writers, incl. this session's own interrupted rollback).
- **`cli.py`:** "not mine" branch calls `remove_abandoned_archive_root`
  and reports it; `RollbackError` caught in the execution path.

**Verification:** 426 passed + 45 subtests, 0 failures (42.5s). Full
suite green.

## v0.5.8 — 2026-08-10

**Second adversarial re-verification round (ChatGPT) — rollback/GC
operation order (HIGH) + two additional GC findings.**

- **#1 (HIGH, blocking): `_cleanup_stale_internals` ran BEFORE the
  journal transition to `rollback_in_progress`, so a stale trash dir
  (the sole recovery evidence of a previous interrupted rollback whose
  entry was still `committed`) was garbage-collected at the start of
  every rollback, turning the retry into a permanent refusal.** The
  rollback state transition now happens first thing under the
  transaction lock (`_begin_rollback`), before any cleanup or
  filesystem mutation, and `_execute_rollback_locked` never changes
  state again. Additionally, `plan_rollback` no longer touches trash
  dirs at all (`include_trash=False` — the CLI plans twice before
  executing and the user may cancel in between), and the GC now
  protects trash whenever the manifest holds ANY session — `committed`
  included, because committed + trash is exactly what a pre-v0.5.8
  interrupted rollback leaves behind (a finished rollback deletes its
  own trash).
- **#2 (MEDIUM, found during verification): the GC docstring promised a
  corrupt manifest keeps trash, but the code collected it.** With
  `load_sessions` raising, `sessions` was `None` and `trash_guard`
  defaulted to `False` — the trash was deleted despite the contract.
  `trash_guard` is now `sessions is None or bool(sessions)`: corrupt
  manifest and any non-empty session list both protect every trash dir.
  Trash is only collected when the manifest is genuinely absent/empty.

Regression tests: committed-session trash is now KEPT (the v0.5.7 test
that expected collection was inverted — it encoded the bug), corrupt-
manifest trash kept, empty-manifest trash still collected,
`plan_rollback` preserves trash evidence (finish-the-cleanup plan
instead of refusal), and a failed `execute_rollback` leaves
`rollback_in_progress` (state written before the mutation).

## v0.5.7 — 2026-08-10

**Adversarial re-verification round (ChatGPT) — rollback durability,
archive-root symlinks, wizard mutation lock.**

A re-verification of the v0.5.5/v0.5.6 fixes against the current code
found 3 new issues, all verified real and fixed:

- **#1 (HIGH, blocking): stale trash from an interrupted rollback could
  be GC'd after 24h, permanently blocking the retry.** A crash mid-
  rollback (some archives restored, some discarded into trash, session
  never dropped) left the journal entry as `committed` — the GC only
  protected `in_progress` sessions, so after 24h the trash (the only
  evidence the archives were consumed) was deleted and the retry became
  a permanent `RollbackError`. Rollback now marks the session
  `rollback_in_progress` BEFORE any filesystem mutation
  (`_mark_session_state`, idempotent under the manifest lock), and the
  GC protects both states (`PROTECTED_STATES`). On success the session
  is dropped as before.
- **#2 (MEDIUM): a pre-planted symlink at the archive root
  (`personas-archive`) redirected every backup outside the expected
  tree.** `mkdir(parents=True, exist_ok=True)` happily follows a symlink
  to an existing directory. New `_assert_real_directory()` rejects a
  symlink or non-directory before use, applied at `archive_persona()`
  and the manifest lock choke point.
- **#3 (MEDIUM): the wizard mutation lock was a no-op on Windows.** The
  `fcntl is None` branch yielded immediately, so two concurrent
  `po add-*` / `po setup` writers could lose each other's mutations (the
  deploy layer's manifest lock already fixed this). Replaced with a real
  portable lock: `fcntl.flock` on POSIX, `msvcrt.locking` byte-range
  with a retry loop on Windows. Deliberately replicated (not extracted
  to a shared util) to avoid touching the well-tested deploy lock zone
  pre-publication.

Regression tests for all three: `rollback_in_progress` trash GC
protection, committed-session trash still collectible, archive-root
symlink refusal, mutation lock serialization.

## v0.5.6 — 2026-08-10

**Crash-point / fault-injection audit (v0.5.6).**

A second audit round (fault-injection tests + crash-window analysis) mapped
non-atomic operations and checked every crash window against the recovery
invariants. Two real findings fixed:

- **G (MEDIUM): venv refresh could be lost between `git merge` and
  `pip install -e .`.** A crash (SIGKILL, power loss) in that window left
  the venv stale with no way to notice — a later `po update` saw
  "Already on X" and never refreshed. `apply_update` now writes a
  `.pf-update-pending` marker BEFORE the merge; `run_update` self-heals:
  the next `po update` (even when already on the latest version) detects
  the marker, refreshes the venv and clears it. `--check` never mutates —
  it only warns. A failed merge clears the marker (no HEAD change).
- **C (LOW): mkstemp leftovers from a killed build were never cleaned.**
  SIGKILL between `mkstemp` and `os.replace` in `_atomic_write` left
  `.SOUL.md.XXXXXX` garbage in the output tree forever. `build()` now
  sweeps stale temps (matching the `.{name}.{6 alnum}` mkstemp shape,
  older than 1h, never directories/symlinks/fresh files).

Crash-window analysis confirmed the remaining invariants hold: deploy
archive→replace and multi-actor deploy crashes are recoverable via
`plan_rollback` (filesystem-based for `in_progress`); manifest writes are
atomic via tmp+fsync+`os.replace` with a 1-day GC cutoff for leftover temps;
`apply_update` fetch→merge is idempotent on retry; rollback is idempotent
across the trash-rmtree→manifest-drop window.

New tests: pending-marker lifecycle (5), stale-tmp sweep (1), plus the
fault-injection battery (6) added during the audit. 410 tests + 9 subtests,
mypy 0 errors (27 files), ruff/format/bandit clean.

## v0.5.5 — 2026-08-10

**Security & correctness hardening (dual adversarial review, v0.5.5).**

Two independent adversarial reviews (ChatGPT + Copilot) audited v0.5.4 and
found 6 issues, all fixed here — cotejo document in `docs/`:

- **H1 (HIGH): rollback trash GC could destroy recovery evidence.**
  `_cleanup_stale_internals` deleted `._pf_trash_*` dirs older than a day
  even when a session was still `in_progress`. Trash is now only
  garbage-collected when no session is in_progress (a corrupt manifest also
  keeps trash untouched). `*.tmp` files are still always collected.
- **H2 (MEDIUM): symlinked actor output dir.** `build_actor` used
  `resolve()` which follows links; a link pointing INSIDE the output tree
  (e.g. `out/dana -> out/lucia`) passed the containment check. New
  `_assert_no_symlink_components` rejects any symlink among the path
  components below the output root.
- **H3 (HIGH): `po update` supply-chain guard.** The update repository
  (env/`--repo` override) is now validated against the checkout's git
  origin — a mismatch is refused. The ff-only merge also uses `--` so a
  tag can never be parsed as a git option.
- **C1 (MEDIUM-HIGH): deploy target symlink.** `deploy` now refuses a
  target root that is itself a symlink (parents may still be symlinked
  legitimately, e.g. a moved home).
- **C2 (MEDIUM-HIGH): `refresh_venv` now runs with `cwd=root`.** Running
  `po update` from /tmp or another directory no longer pip-installs the
  wrong directory.
- **C3 (MEDIUM): POSIX wrappers no longer require a global python3.**
  `bin/po` / `bin/phantomorg` prefer the repo `.venv` first; a portable
  readlink loop resolves the repo root when no global python3 exists.

New tests: stale-trash GC semantics (3), updater origin-mismatch / `--` /
refresh cwd (4), deploy symlink target (2), build symlink components (2),
wrapper venv-without-python3 (2). 397 tests + 9 subtests, mypy 0 errors,
ruff/bandit clean.

## v0.5.4 — 2026-08-10

**OS-agnostic wrappers (Linux / macOS / Windows).**

- `bin/po` and `bin/phantomorg` used GNU-only `readlink -f` to resolve the
  repo root — fatal on macOS (BSD readlink has no `-f`). They now resolve
  through `python3` (already a hard dependency), portable across GNU and BSD.
- `install.sh` got the same portable resolution for its symlink checks.
- New `bin/po.cmd` and `bin/phantomorg.cmd` for Windows: resolve the repo
  from their own location, prefer `.venv\Scripts\python.exe`, else `python`,
  and delegate to `python -m phantomorg.cli`.
- README: manual install instructions per OS (install.sh on Linux/macOS;
  add `bin` to PATH on Windows).
- New wrapper smoke tests: POSIX wrappers run via bash (help exits 0, symlink
  resolution still works); `.cmd` presence and delegation contract checked.

## v0.5.3 — 2026-08-10

**`po update` venv layout: Windows support.**

- `refresh_venv` used to hardcode `.venv/bin/python` (POSIX layout). On
  Windows a venv lives at `.venv/Scripts/python.exe`, so `po update` on
  Windows skipped the dependency refresh silently.
- New `_venv_python()` helper picks the interpreter by OS
  (`os.name == "nt"` → `Scripts/python.exe`, else `bin/python`), and
  `refresh_venv` uses it. Windows-style venvs are ignored on POSIX and
  vice versa.
- 3 new tests (POSIX layout, Windows layout, Windows-ignores-POSIX-venv):
  380 total + 9 subtests. mypy 0, ruff/format/bandit clean.

## v0.5.2 — 2026-08-10

**CI fix: bandit `# nosec` comments in `po update`.**

- Bandit 1.9.4 does not accept explanatory text after the `# nosec` test
  ids on the same line (`# nosec B603 B607 — fixed arg list`). The text
  made bandit treat every word as a test name, the nosec was not applied,
  and the CI `bandit -r phantomorg -q` step failed (exit 1) on
  `phantomorg/updater/__init__.py`.
- All `# nosec` comments now carry only the test ids (`# nosec B603 B607`,
  `# nosec B310`, `# nosec B608`); explanations moved to the preceding
  comment line. The `import subprocess` line gained `# nosec B404`.
- Local bandit (same 1.9.4) now reports 0 issues with the exact CI
  command; the CI failure on v0.5.0/v0.5.1 was purely the comment format.

## v0.5.1 — 2026-08-10

**`po update` hardening: remotes with embedded credentials.**

- `remote_origin_repo` now normalizes `https://user:token@github.com/owner/repo.git`
  URLs (credentials embedded) in addition to plain `git@github.com:` and
  `https://github.com/` forms. Previously such a remote was not recognized
  as GitHub and `po update` failed with "no GitHub remote found".
- New test case covering the credentials-embedded URL form.

## v0.5.0 — 2026-08-10

**`po update` — self-update from GitHub Releases (phantombot-style update
cycle, Fase 5).**

### New command

- **`po update [--check] [--force] [--repo owner/name]`** fetches the
  latest PhantomOrg release from GitHub and fast-forwards the local
  checkout to it. PhantomOrg ships as a git checkout (install.sh
  symlinks `bin/po`), so an update is a git fast-forward to the released
  tag plus a venv dependency refresh when the repo has a `.venv`.
  - `--check`: reports without installing; exit 0 = up to date, 2 =
    update available, 1 = error (cron-alertable).
  - `--force`: skips the confirmation (cron-friendly).
  - Repo discovery: `remote.origin` → `$PHANTOMORG_UPDATE_REPO` →
    `--repo` (highest priority wins). `GITHUB_TOKEN` is honored for the
    private repo / higher rate caps; a rejected token (401) or
    rate-limit (403) retries once without auth, phantombot-style.
  - Safety: refuses to update when the working tree has tracked
    modifications (local edits are never clobbered); refuses non-git
    checkouts and missing remotes with clear errors.

### Implementation

- New `phantomorg/updater/` module: GitHub `/releases/latest` client,
  dotted-version comparison (0.4.10 > 0.4.9), git fast-forward apply,
  venv refresh. Fully seam-injected (http_get, subprocess) and tested.
- 24 new tests (377 total + 8 subtests), ruff/format/bandit clean, mypy
  0 errors across 27 source files.

## v0.4.19 — 2026-08-10

**mypy debt cleared: zero errors across 26 source files.**

### Type hardening

- **fcntl fallback annotated canonically** (`fcntl = None  # type:
  ignore[assignment]`) in `wizard/mutations.py` and `deploy/session.py`,
  matching the existing project pattern.
- **`deploy/session.py` rollback: non-str archived names rejected.**
  `entry.get("name")` now guarded with an `isinstance(name, str)` check
  before indexing `first_per_name`; a malformed archive entry with a
  non-string name raises `RollbackError` instead of a `TypeError` deep
  in the rollback loop.
- **`msvcrt` lock calls annotated** with `# type: ignore[attr-defined]`
  (Windows-only branch, stubs don't cover `locking`/`LK_LOCK`/`LK_UNLCK`).
- **`_ExpandUserPath` type-var fix:** the custom click ParamType now
  overrides `__init__` with `*args: Any, path_type: type | None` so
  `types-click`'s strict `_PathType` TypeVar no longer rejects
  `path_type=Path` (22 call sites cleaned with one ignore site).
- **Stub types added to dev deps:** `types-PyYAML`, `types-click`,
  `types-Jinja2` (types-tomli does not exist on PyPI — tomli is a
  backport covered by typeshed's tomllib).

### Quality

- mypy: **Success: no issues found in 26 source files** (was 13 errors).
- 353 tests + 5 subtests pass; ruff/format/bandit clean.
- New test: rollback rejects archive entries with non-string names.

## v0.4.18 — 2026-08-10

**Deploy LOW findings F6/F7/F10/F12 fixed** (adversarial review
deploy.md).

### Deploy

- **Suffixed session ids no longer misclassify same-timestamp archives
  (F6):** an in_progress session id carrying a collision suffix (`-1`,
  `-2`…) is now compared by its base stamp when deciding whether an
  archive predates the session. Previously the suffix sorted after the
  base stamp lexicographically, so an archive stamped at exactly the
  session's own start millisecond was skipped instead of restored.
- **Non-directory targets are refused, not silently lost (F7):**
  `archive_persona` now raises `DeployError` when the entry to archive
  is a plain file instead of a directory. A file used to pass
  `src.exists()`, got archived as a file, and was never restored by
  rollback (the restore branch requires a directory) — the pre-deploy
  file was permanently lost under the compiled dir.
- **Manifest unlink moved inside the lock (F10):** `discard_session`
  now removes the manifest while holding the manifest lock, closing a
  library-level race where a concurrent `record_session` between the
  save and the unlink could be destroyed. (CLI callers already
  serialized; the gap was library-level only.)
- **`execute_rollback` acquires the transaction lock internally (F12):**
  a direct library caller is now serialized against concurrent
  deploys/rollbacks just like the CLI is. The transaction lock is now
  reentrant per thread, so the CLI's outer acquisition plus the
  internal one cannot deadlock, and two threads still serialize via
  the real flock.

### Quality

- 4 new tests (352 total), ruff/format/bandit clean, mypy no new debt
  (13 pre-existing stub errors).

## v0.4.17 — 2026-08-10

**Compiler merge robustness F4-F6 fixed** (adversarial review
compiler.md).

### Compiler

- **Ambiguous ORG markers are never destroyed (F4):** if an existing
  file contains duplicate or unbalanced ORG:BEGIN/END markers (for
  example a manual note quoting a well-formed marker pair), the merge
  now preserves the whole file and warns instead of replacing every
  match — the quoted annotation is no longer silently overwritten with
  freshly rendered content. Previously `merge_content` replaced every
  marker match, so a manual annotation containing a well-formed fake
  pair lost its body on the next build.
- **CRLF files merge correctly (F5):** the markers are now tolerant to
  `\r\n` line endings, so a file edited on Windows (or rewritten by
  another tool) no longer matches zero blocks and silently freezes —
  spec changes now propagate into CRLF files (written back as LF).
  Additionally, when an existing file has no ORG blocks but differs
  from the fresh render, the build warns that spec changes are NOT
  being applied (deliberate opt-out is preserved, but no longer
  silent).
- **Literal END markers in generated bodies no longer truncate
  blocks (F6):** block extraction now uses the *last* END marker for a
  name, so a generated body containing the literal text
  `<!-- ORG:END <name> -->` is kept whole instead of being cut at
  the fake marker on every rebuild.

## v0.4.16 — 2026-08-10

**Wizard LOW findings L2-L5 fixed** (adversarial review wizard.md).

### Wizard

- **`po setup` validates the org.yaml structure (L2):** reusing a
  malformed org.yaml (missing `organization.id`, or a
  department/role entry without `id`) now fails with a friendly
  "not a valid org.yaml for `po setup`" message instead of a raw
  `KeyError` traceback mid-wizard.
- **Wizard mode stops leaking tracebacks (L3):** `_run_wizard` now
  catches `DuplicateIdError` and `FileExistsError` (in addition to
  `click.Abort`) and prints a clean message with exit code 1 —
  duplicate ids typed in the add-department/role/actor wizards and
  `po new-org` over an existing org dir no longer dump a traceback.
- **Durable renames (L4):** `backup_org_file` and `_save` fsync the
  parent directory after `os.replace`, so the rename itself survives
  a power loss (previously only the file data was fsynced; the
  docstrings over-claimed durability). Best-effort on platforms
  where directories cannot be fsynced.

### Compiler

- **Strict template variables (L5):** the Jinja2 environment now uses
  `StrictUndefined` — a typo in a template variable (e.g.
  `t.memory_hint` vs `t.memory_hint_1`) fails the build loudly
  instead of rendering silently empty and dropping content.

## v0.4.15 — 2026-08-10

**cli-tests hardening round: F5-F12 fixed** (CLI review cli-tests.md;
F1-F4 landed earlier in the session).

### CLI

- **Import audit atomicity (F5):** `po import-audit --apply` applies
  all mutations in a single batch (`add_role_and_actor` runs once per
  org with one `_save`) instead of role-first-then-actor with two
  saves — an interruption between the two can no longer leave a
  half-applied org.
- **`--from`/`--target` paths expand `~` (F7):** all click `Path`
  options now use an `_ExpandUserPath` subclass (click 8.3.1 lacks
  `expand_user`) with `file_okay=False`, so `po deploy --from ~/out
  --target ~/target` works as expected.
- **Interrupted deploys are honest (F8):** `po deploy` interrupted by
  Ctrl-C now says "run `po rollback`" (the session is committed so a
  rollback is available); `po rollback` interrupted warns the target
  "may be partially applied".
- **`po rename` asks for confirmation (F9):** destructive rename now
  requires `--yes`/confirmation like the other destructive commands.
- **Splitter never proposes empty fragments (F10):** candidates whose
  text lacks alphabetic characters are no longer offered as split
  points.
- **`import-audit` hardening (F6/F11):** unreadable/missing files are
  skipped with a warning instead of a raw traceback, and
  `reports_to_human` is emitted in the REVIEW fragment and passed
  through on `--apply`.
- **Deploy-all exit code (F3):** `po deploy-all` now exits 1 when a
  mutation failed (previously only reported failures per-org).
- **Deploy-all planned prune uses metadata org ids (F12):** the
  rollback journal's `planned_pruned` now matches the prune criterion
  (metadata `organization.id`) instead of the `organizations/` folder
  name — an org.yaml whose id differs from its folder no longer
  records a prune plan that diverges from what actually gets pruned.
- **Wizard mutation loader (F1):** `mutations._load` tolerates a
  missing `pf.setup.mutations` key (`or {}`) instead of crashing on
  brand-new configs.

### Tests

- 16 new tests: F3 exit code, F5 batch import-audit, F6 missing-file
  skip, F7 tilde expansion, F8 Ctrl-C messaging, F9 rename
  confirmation, F10 splitter filtering, F11 reports_to_human
  pass-through, F12 metadata-based planned_pruned. 329 tests total.

## v0.4.14 — 2026-08-09

**Adversarial review follow-up: remaining deploy findings fixed**
(deploy-2.md F4-F13; F1-F3 landed in v0.4.11).

### Deploy robustness

- **Malformed metadata never crashes a deploy (F4):** `_read_meta` is
  now best-effort — absent, unreadable, invalid-YAML or non-mapping
  `.phantomorg.yaml` files return `None` ("unknown origin") instead
  of raising. A single persona with a broken meta file can no longer
  abort a whole deploy or prune scan with a raw traceback, and the
  deploy-all prune loop skips unreadable target entries.
- **Symmetric metadata check (deploy.md F4):** a compiled actor WITHOUT
  metadata that would overwrite an existing target entry is now refused
  unless `--force`, mirroring the existing-without-metadata case.
- **Session commit/discard failures are friendly (F5):** if the
  manifest write fails (disk full, permissions) AFTER a successful
  deploy, the CLI says "Deployment SUCCEEDED but the session could not
  be committed" and exits 1 — no raw traceback, no false claim that
  the deploy failed.
- **Missing directories (F9):** `po deploy-all` with a missing `--base`
  (or `--dist-base`) prints a friendly error and exits 1 instead of a
  raw `FileNotFoundError` traceback.
- **Windows locking (F7):** the manifest and transaction locks now use
  a real `msvcrt.locking` range lock (with retry loop) instead of
  degrading to a no-op on Windows, and `_save_sessions` writes to a
  per-process temp name so two writers can never interleave on the
  same temp file.
- **Archive-root removal outside the lock (F8):** the final
  `rmtree(archive_root)` now runs AFTER the session is dropped and the
  manifest lock is released, best-effort — a failure there no longer
  produces the false "retry" message (the session entry is already
  gone).
- **Stale internal cleanup (F10):** leftover `*.tmp` manifests and
  `._pf_trash_*` dirs older than a day are purged before planning or
  executing a rollback (and at the start of every manifest-locked
  operation), so they can never be mistaken for live sessions or
  distort the archive-root emptiness check.
- **Dead `skipped_collisions` removed (F11):** the field was never
  populated; its CLI reporting branch was unreachable. Removed with
  the test assertion.
- **`commit_session` re-registration infers pre-existed flags (F12):**
  when the manifest entry vanished mid-deploy, the re-registered
  session now infers `archive_root_pre_existed`/`target_pre_existed`
  from the filesystem instead of hardcoding `False` (a rollback can no
  longer delete a directory that existed before the deploy).
- **fsync before rename (F13):** `_save_sessions` fsyncs the temp file
  before `os.replace` and best-effort fsyncs the directory, closing
  the rename-without-fsync durability window.
- **`po rollback` with nothing to roll back now exits 1** (was 0),
  consistent with the other failure paths.

### Tests

- 6 new tests: malformed/non-mapping meta in target (deploy + prune),
  compiled actor without meta (collision + `--force`), missing
  base/dist dirs, commit-failure messaging. 313 tests total.

## v0.4.13 — 2026-08-09

**Adversarial review follow-up: MEDIUM findings fixed** across the
compiler, the wizard and the spec/validator layer (spec F1-F7 =
validator F1-F7; the two reviews cover the same `phantomorg/spec/`
module).

### Compiler

- **Atomic writes (F2/F3):** all three write helpers
  (`write_if_changed`, `write_plain_if_changed`, `write_if_missing`)
  now go through `_atomic_write` (mkstemp in the same dir → write →
  fsync → `os.replace`), so a crash or ENOSPC mid-write can no longer
  destroy a hand-edited merged file, and `os.replace` also stops
  write-through on file-level symlinks. Symlinked paths are refused on
  read with a clear `ValueError`.
- **Silent language fallback (F7):** `build_actor` now warns when
  `resolve_lang` picks a language that is not in
  `available_languages()` (previously fell back to English silently).
- **`po build --only <unknown>` (F8):** friendly error listing the
  available actors instead of an unhandled `KeyError`.
- **build-all dir vs id (F9):** warns when an org directory name
  differs from `organization.id` (breaks `deploy-all --prune` ownership
  matching / cross-org overwrite detection).

### Wizard

- **Transactional batch apply (M1):** the setup wizard's apply loop now
  builds the fully-mutated document in memory, pre-validates duplicate
  ids, and commits with a single atomic `_save()` — a mid-batch
  `DuplicateIdError` no longer leaves a partial org.yaml.
- **Flock around mutations (M2):** all nine `add_*`/`remove_*`/`rename_*`
  functions run under `_mutation_lock` (fcntl.flock; no-op on Windows),
  so concurrent mutations can no longer silently lose updates.
- **org_id path traversal (M3):** `new_org` and the wizard create-new
  path validate org ids with `is_valid_identifier` before creating
  directories (`../../x` can no longer escape `base_dir`).

### Spec / validator

- **Duplicate YAML keys rejected (F1):** the loader now uses a
  `_UniqueKeyLoader` (SafeLoader subclass) that raises on duplicated
  mapping keys instead of silently keeping the last value.
- **Loader error contract (F1):** malformed YAML, invalid UTF-8, deep
  nesting, directories, missing files and I/O errors all surface as
  `OrgSpecError`; `po build` shows a friendly error and `po build-all`
  skips the broken org instead of aborting the batch.
- **Identifier grammar (F2/F5):** `fullmatch` anchoring rejects ids
  with trailing newlines; Windows-reserved device names (`con`, `aux`,
  `nul`, `prn`, `com1-9`, `lpt1-9`) and ids over 64 chars are rejected.
- **bool ≠ int (F5/F6):** `version: true`, `max_hops: true`,
  `categories: [true]`, `soul_line_budget: true` now fail validation.
- **Explicit nulls (F2/F3/F6):** `soul_line_budget: null` and
  `cross_department: null` are rejected; the model normalizes
  `soul_line_budget` null → 300 so a valid spec can never crash
  `validate_compiled_output` with a TypeError. `default_language` /
  `owner` / `scope` remain nullable (schema.json now says so).
- **Unknown keys rejected (F3/F4):** every object level enforces
  `additionalProperties: false` — typo'd optional fields
  (`security_excpetions`, `telegram_bott`) fail validation instead of
  silently deploying without the intended configuration.

### Tests

307 tests pass (was 270); new regression files
`tests/test_spec_media.py` (37 tests), `tests/test_compiler_media.py`
(11) and `tests/test_wizard_media.py` (7). ruff, format and bandit all
clean.

## v0.4.12 — 2026-08-09

**Adversarial review follow-up: remaining HIGH findings fixed** —
Python 3.10 compatibility restored in the compiler and `po setup`
create-new no longer overwrites an existing org.yaml without a backup.

### Fixed (HIGH): `datetime.UTC` broke Python 3.10

`phantomorg/compiler/build.py` used `datetime.UTC` (added in Python
3.11) while `requires-python = ">=3.10"` — on 3.10 the scaffold seed
stamping crashed with AttributeError. Replaced with
`datetime.timezone.utc` (available since 3.2). New source-level
compatibility guard (`tests/test_python310_compat.py`) pins the declared
3.10 floor and forbids known 3.11+-only stdlib names in production code,
since CI only exercises 3.12.

### Fixed (HIGH): `po setup` create-new silently overwrote an existing org.yaml

When the create-new branch of the setup wizard wrote to an org id that
already had an `org.yaml` on disk (e.g. an id reused from a previous run,
or a file present under `base_dir`), the existing spec was truncated and
replaced with no backup — unlike `po new-org`, which refuses. The
create-new write now warns when the file exists, backs it up
(`org.yaml.bak-<ts>`, same atomic+durable path as `_save`), and writes
atomically via `mutations._save`. Regression test
`test_setup_create_new_over_existing_org_backs_up_first`.

## v0.4.11 — 2026-08-09

**Adversarial review (6 areas, 7 reports) — 1 CRITICAL + 5 HIGH findings
fixed.** The deploy→rollback data-loss contract is now guaranteed in the
multi-org duplicate-name and race paths.

### Fixed (CRITICAL): committed-session rollback destroyed the pre-session
version on duplicate archives

`deploy-all --force` with two orgs sharing an actor id records the same
persona name TWICE in one committed session's `archived` list
(`dana-S1` = the pre-session version archived by org A; `dana-S2` = the
in-session version archived by org B). The in_progress reconcile branch
deduped oldest-wins, but the committed branch had NO dedupe: rollback
restored both in recorded order, the second restore trashed the freshly
restored pre-session version, and the trash was then deleted — the
pre-session version was permanently lost, the target was left with the
wrong intermediate version, and rollback reported success.

The committed branch now applies the same rule as the in_progress
branch: only the FIRST recorded archive per persona name is restored;
later same-name archives are in-session artifacts and go to the discard
list. Pinned by `test_double_archive_same_name_committed_restores_oldest`
(verified to fail on the pre-fix code).

### Fixed (HIGH): interrupted rollback was permanently stuck

A rollback that crashed after restoring ≥1 archive refused every retry:
`plan_rollback` saw the consumed archives as "missing" and raised, even
though the persona was back in the target and the rollback only needed
to finish. The session stayed in the manifest forever; only manual
filesystem surgery worked.

`plan_rollback` now uses trash-dir evidence (execute_rollback ALWAYS
leaves a `._pf_trash_*` dir when it discards a replaced version):
missing archives + trash dir + persona back in target = the interrupted
rollback consumed them, continue; missing + no trash = removed outside
PhantomOrg, refuse (historical behavior kept); missing + trash +
persona NOT in target = pre-deploy version genuinely lost, refuse with
a manual-recovery message. Pinned by
`test_plan_rollback_continues_after_interrupted_restore`,
`test_plan_rollback_refuses_missing_persona_not_in_target` and
`test_plan_rollback_refuses_without_trash_evidence`.

### Fixed (HIGH): corrupt manifest silently destroyed rollback history

`load_sessions` returned `[]` on an unreadable/corrupt
`.phantomorg-manifest.json`; the next deploy's `begin_session` loaded
`[]` and re-saved, overwriting the corrupt file and destroying the
entire rollback history (archives orphaned forever).

`load_sessions` now RAISES `ManifestError` when the manifest exists but
cannot be read/parsed (returns `[]` only when genuinely absent). Every
writer (`begin_session`, `commit_session`, `discard_session`, legacy
`record_session`) quarantines the corrupt file as
`.phantomorg-manifest.json.corrupt-<stamp>` (never overwritten) and
refuses with `DeployError`; read-only paths (`plan_rollback`,
`execute_rollback`, `_empty_after_internals`) treat it as "unknown",
never as "no sessions", and the CLI exits 1 with a clear message
instead of 0. A corrupt manifest also keeps the archive root alive.
Pinned by the new `TestManifestCorruption` class (5 tests).

## v0.4.10 — 2026-08-09

**Copilot verification round (third review): 2 HIGH findings on the
current code — both confirmed and fixed.**

### Fixed: journal plan computed OUTSIDE the transaction lock (race)

`deploy` and `deploy-all` computed `planned_archived`/
`planned_created`/`planned_pruned` and the pre_existed flags BEFORE
acquiring `_transaction_lock`. Two concurrent PhantomOrg processes
could both observe the same pre-state; the second deploy (its own
preflight sees the real state) would archive a persona its stale plan
lists as `planned_created`. If that deploy then crashed, the
interrupted rollback would treat the persona as "created by this
deploy" and DISCARD its archive — the persona's pre-deploy version
would be gone, violating the exact pre-deploy-state contract.

Both commands now compute the entire journal plan INSIDE the
`_transaction_lock` block, so the plan always reflects the target
exactly as the deploy is about to mutate it. (The pre-confirmation
plan banner is informational and stays outside; the journal is what
matters for rollback.)

Pinned by `test_deploy_plan_state_is_computed_under_transaction_lock`:
structural regression test asserting the first `archives_dir`
inspection (only used by the plan; `deploy_target` never calls it)
happens between lock-enter and lock-exit. Verified to fail on the
pre-fix code.

### Fixed: interrupted-session reconcile consumed FOREIGN archive dirs

Confirmed E2E: with an `in_progress` session pending, any dir in
`personas-archive/` matching `<name>-<stamp>` with stamp >= the session
id was treated as belonging to the interrupted deploy. A foreign
archive (phantombot `import-persona`, a manual restore, an older
PhantomOrg version) was "restored" into the target — or, if its
name collided with a `planned_created` name, discarded into the trash
and deleted. The rollback consumed archives it never created.

The in_progress reconcile now only touches archives whose name appears
in the session's planned lists (archived/created/pruned). A dir whose
name is in NO planned list is foreign: it is left EXACTLY as found,
reported as "left untouched: <name> (foreign archives, kept as-is)",
and keeps the archive root alive (nothing foreign is ever deleted).

Pinned by `test_rollback_leaves_foreign_archives_untouched` (E2E:
interrupted deploy + planted foreign archive → rollback restores only
the real archive, leaves the foreign one intact, keeps the archive
root, target exact).

241 tests total; ruff/bandit/format clean.

## v0.4.9 — 2026-08-09

**Logic-inspection review (ChatGPT, second pass on current code): 3
remaining risks — 1 real bug fixed, 1 hardening, 1 portability note.**

### Fixed: interrupted deploy-all with the same persona name archived twice

Concrete failure mode behind the review's ``begin_session`` dedup note:
in a deploy-all where two orgs share an actor id (``--force``) and that
name pre-existed in the target, the name is archived twice within one
session — org A archives the PRE-SESSION version, org B later archives
org A's freshly deployed version. The ``in_progress`` reconcile put BOTH
archives on the restore list; ``execute_rollback`` then restored the
pre-session version, and the second restore TRASHED that freshly
restored pre-session version and replaced it with the in-session one —
the rollback ended with the wrong version in the target.

``plan_rollback`` now restores only the OLDEST archive per persona name
(the pre-session version; ``sorted()`` yields it first) and discards
every later archive of that name as an in-session artifact. The
``planned_created`` rule (all archives of a name created in-session are
discarded) is unchanged.

Pinned by ``test_double_archive_same_name_restores_oldest_not_in_session``
(E2E: two orgs sharing ``dana``, pre-existing third-org dana, failure
injected after both archives; asserts the restored dana is the
pre-session third-org version and the archive root is fully removed).

### Hardened: wizard org.yaml backup is now atomic

``backup_org_file`` previously did a plain ``write_bytes`` — a crash
mid-copy left a truncated ``.bak`` that would silently corrupt a future
restore. It now writes to a temp file, fsyncs, and ``os.replace``s into
place (same pattern as ``_save``), cleaning up the temp file on failure.

2 new tests: backup lands via atomic replace with no temp leftovers;
backup failure leaves no temp file behind.

### Portability note (no code change): ``_manifest_lock`` requires fcntl

On non-POSIX platforms (e.g. Windows) the flock-based manifest lock
degrades to a no-op, so concurrent deploy/rollback manifest writes would
not be serialized there. PhantomOrg targets Linux/phantombot runtimes;
this is documented as a portability caveat, not a supported platform.

239 tests total; ruff/bandit/format clean.

## v0.4.8 — 2026-08-09

**Review's recommended-priority follow-up: the 'most concrete
reproducible bug' (collision case) verified not reproducible; one
leftover-artifact cleanup.**

The review's top-priority item was a collision case claimed to leave a
modified runtime with no rollback transaction:

```
actor A -> successfully archived/deployed
actor B -> collision
deploy exits with error
record_session() never runs
```

Verified against the current code (and pinned with 3 new tests in
`TestReviewCollisionBug`): this is **not reproducible** — preflight
(v0.4.4) checks every actor's collision/symlink state before any
mutation, so a rejected deploy never touches the target and never
leaves a session behind; with `--force` the collision is deliberate and
the deploy records a committed session, so `po rollback` can restore
the overwritten persona.

Each item of the review's recommended priority maps to already-shipped
work: (1) journaled transaction = v0.4.3 durable journal; (2) preflight
= v0.4.4 H2; (3) serialize deploy/deploy-all/rollback = the transaction
lock held by all three commands; (4) staging ownership = v0.4.4 H1
(UUID dirs + mtime>1h stale cleanup); (5) constrain IDs = v0.4.7
identifier grammar + path containment; (6) atomic `_save()` = v0.4.5.

New fix: a deploy rejected in preflight left an empty `personas-archive/`
behind (created by the journal before the preflight ran inside
`deploy_target`). `discard_session` now removes the archive root when it
was created by that deploy and nothing but the manifest lock remains —
a rejected deploy leaves the filesystem exactly as it found it.

3 new tests (collision leaves no partial state/session; discarded
session does not block the next deploy; `--force` overwrite records a
rollbackable session). 236 tests total; ruff/bandit/format clean.

## v0.4.7 — 2026-08-09

**Finding #5: identifier grammar (path-traversal fix) + complete shape
validation (High + Medium).**

- **High — actor IDs can no longer traverse the filesystem.** The shape
  validator previously required ids to be strings but imposed no safe
  identifier grammar, and the compiler built `out_dir / actor.id`
  directly — so an id like `../outside` could write compiled output
  outside the requested build directory. Now every id in org.yaml
  (`organization.id`, `department.id`, `role.id`, `actor.id`, and the
  keys of `access_levels` / `security_categories`) must match the
  unified grammar `^[a-z0-9][a-z0-9_-]*$`, enforced by a single
  `is_valid_identifier()` used throughout `validate_shape`. The
  compiler adds defense-in-depth: `build_actor` resolves the actor
  output path and refuses any actor whose path escapes the requested
  `out_dir` (a `ValueError`), so even a spec constructed outside
  `load_org_yaml` cannot write out of bounds. `schema.json` and the
  spec §5.2 now document the unified grammar (previously only
  `organization.id` had a pattern).
- **Medium — malformed YAML can no longer crash with raw Python
  exceptions.** `validate_shape` now mirrors the schema completely:
  mapping checks for `policies`, `access_levels`,
  `security_categories` and every nested entry; list + item-type checks
  for `languages`, `functions`, `tools`, `tools_excluded`,
  `actor_exceptions`, `security_exceptions`, `categories`,
  `message_types` and `escalation_matrix`; scalar checks for `version`,
  `max_hops`, `soul_line_budget` (>= 50), `cross_department`,
  `scope` (enum), plus optional-field type checks (`parent`,
  `reports_to`, `reports_to_human`, `telegram_bot`, `tone`, `owner`,
  `default_language`). Malformed input raises `ShapeError` (surfaced as
  a clean `Load error:` by the CLI), never an AttributeError/TypeError
  traceback.

43 new tests: identifier-grammar rejection (traversal ids, hidden
files, separators, absolute paths, uppercase, leading dash), systematic
malformed-type checks, and a compiler path-containment test with a
hand-built spec. 233 tests total; ruff/bandit/format clean.

## v0.4.6 — 2026-08-09

**deploy-all interrupted-session reconcile: full filesystem truth +
in-session archive disposal (finding #4 residual).**

The ChatGPT review's finding #4 claimed a failed deploy-all left a
partial deploy "manually recoverable" only. That scenario was already
fixed by v0.4.3 (durable in_progress sessions) + v0.4.4 (preflight): an
org failing MID-mutation after other orgs succeeded leaves an
in_progress session that `po rollback` reconciles completely — proven
by a new E2E regression test. This release closes the last residual
gap the review hinted at:

- **The in_progress reconcile now scans the whole archive root, not
  just the pre-planned names.** Previously it only looked for archives
  of personas in the session's `planned_archived`/`planned_pruned`
  (estimated from the PRE-deploy state). In a deploy-all where org A
  CREATES a persona and org B (sharing the actor id, `--force`)
  archives it before failing, that archive was never planned (the
  persona did not exist pre-deploy) — it stayed orphaned in
  `personas-archive/` forever, keeping the archive root alive.
- **In-session archives are discarded, not restored.** Archives whose
  persona name is in the session's `planned_created` were created
  inside this very session; restoring them would resurrect an
  in-session artifact. They are now moved to the rollback trash and
  removed once the rollback succeeds (`discarded:` line in the
  rollback output).
- The scan only ever considers real archive dirs (regex-validated
  `<name>-<stamp>` names), skips trash/dot-dirs, and keeps the
  existing stamp >= session-id window, symlink and name-safety checks.

Two new tests: `test_deploy_all_mid_mutation_failure_is_recoverable`
(the review's literal scenario) and
`test_deploy_all_interrupted_org_archives_in_session_created_persona`
(the orphan regression). 190 tests total.

## v0.4.5 — 2026-08-09

**Atomic org.yaml saves in the wizard/mutation layer.**

- `_save()` no longer opens `org.yaml` for a truncating write. It now
  writes the complete document to `org.yaml.tmp-<uuid>`, fsyncs it,
  and atomically renames it over `org.yaml` with `os.replace`. A
  process crash or disk failure mid-write can no longer leave the live
  spec empty or half-written (which would break subsequent
  `po validate` / `po build` / automation); readers see either the old
  complete file or the new complete file. The `.bak-<ts>` backup
  remains the recovery point, written first as before. Any failure
  during the temp write cleans the temp file up and leaves `org.yaml`
  untouched.

## v0.4.4 — 2026-08-09

**ChatGPT review round 3 — staging races, preflight collisions, and
suffix exhaustion (3 findings, all fixed).**

- **Staging dirs are collision-proof and age-gated (H1).** Staging
  directories are now named with a UUID (`uuid4`), so two deploys
  started in the same millisecond can never share a staging dir (a
  timestamp-only name could collide and let one deploy clobber the
  other's staging copy). Leftover staging dirs are only cleaned when
  demonstrably stale: `_cleanup_stale_staging` now removes a
  `.pf-staging-*` dir only when its mtime is older than 1 hour — a
  fresh dir is assumed to belong to a deploy that is still running.
- **Collisions are detected in preflight, before any mutation (H2).**
  All collision, symlink and prune decisions are computed from the
  PRE-deploy target state, before the first staging/archive/swap. A
  rejected deploy now leaves the target and the archive exactly as they
  were — no partial deploys, no consumed backups, no phantom
  `in_progress` session entries. In `deploy-all`, a colliding org is
  skipped in preflight (it mutates nothing) while the other orgs still
  deploy and are recorded in the session; if every org collides, the
  empty session is discarded.
- **Suffix exhaustion raises instead of nesting (H3).** Every site
  that allocates a `-1..-999` numeric suffix now raises on exhaustion
  instead of falling through: with a taken destination, `shutil.move`
  would have placed the source INSIDE the existing directory
  (`<dest>/<name>/`), silently corrupting the archive/trash layout.
  Covered: `archive_persona` (archive dirs), rollback `_discard`
  (trash entries), and session-id allocation in both `record_session`
  and `begin_session`.

## v0.4.3 — 2026-08-09

**ChatGPT review round 2 — durability, concurrency, and manifest
confinement (3 findings, all fixed).**

- **Deploy sessions are now durable (H1).** `po deploy`/`deploy-all`
  write a journal entry (`state: in_progress`) with the planned
  archived/created/pruned personas BEFORE mutating the target, and only
  transition it to `committed` after success. A crash, SIGKILL, power
  failure, or failed `record` no longer leaves a modified target/archive
  with no trace: `po rollback` detects the interrupted session and
  *reconciles* it — restoring whatever the attempt already archived and
  discarding whatever it created. Deploying while an interrupted session
  is unresolved is refused until you roll it back.
- **Whole transactions are serialized (H2).** A new transaction lock
  (`.phantomorg.lock` in the runtime dir) is held by the entire
  deploy / deploy-all / rollback, not just the manifest load/save — a
  rollback can no longer race a concurrent deploy on the same target.
  Rollback re-plans under the lock after the user's confirmation and
  refuses if the session changed meanwhile (TOCTOU). Lock order is
  always transaction → manifest, so no deadlock.
- **Manifest paths are confined (H3).** `plan_rollback` now validates
  every manifest-supplied value before planning: persona names must be a
  single safe directory component (no separators, no `.`/`..`, no
  absolute paths), archive dirs must be absolute and direct children of
  `personas-archive/`, and the recorded target must match the invoked
  target. A corrupt or tampered manifest can no longer make rollback
  move arbitrary filesystem content.

## v0.4.2 — 2026-08-09

**Post-review hardening (5 findings from an external code review).**

- **Rollback failure is now truly retryable (R1).** The session entry is
  dropped from the manifest LAST — after every filesystem step
  (restore, discard, trash removal, directory cleanup) has succeeded.
  A failure mid-rollback leaves the session recorded; if the archived
  personas were already restored, the next `po rollback` becomes a
  *cleanup-only* plan that finishes the job instead of refusing.
  `_empty_after_internals` only considers the archive root empty when
  no other session entries survive (the manifest counts).
- **Concurrent deploys cannot lose sessions (R2).** `record_session`
  and the rollback's manifest update run under an advisory `flock` on
  `.phantomorg-manifest.lock` (a dotfile, ignored by phantombot),
  serializing the load-modify-save cycle across processes. Platforms
  without `fcntl` degrade to no locking.
- **deploy-all preserves archive order and duplicates (R3).** The
  aggregated session no longer collapses `archived` through
  `set()`/`sorted()` — the same actor archived twice (e.g. two orgs
  sharing an actor id) keeps both entries, and they are restored in
  the exact order they were recorded.
- **Deploys are atomic per persona (R4).** Each compiled actor is first
  copied to a staging dir inside the target (`.pf-staging-<stamp>/`)
  and only swapped into place with an atomic `os.replace` AFTER the
  previous version has been archived. A copy failure can no longer
  leave a half-written persona in the runtime or consume a backup
  without a replacement. Stale staging dirs from crashed deploys are
  cleaned up on the next deploy.
- **Symlinks are refused, never followed (R5).** `archive_persona`
  refuses to move a symlink; compiled actors containing symlinks are
  rejected; `po rollback` refuses archived personas that are symlinks.
- New tests: trash-failure retry (cleanup-only), concurrent
  record_session (8 threads, no lost sessions), symlink refusal
  (compiled actor, target entry, archive), staging copy-failure
  leaves target and backup intact, no staging leftovers after deploy.

## v0.4.1 — 2026-08-09

**Hardening audit of the rollback path — no data is ever deleted
outright.**

- **Rollback never `rmtree`s live data.** When a rollback replaces a
  persona whose current version changed after the deploy (or removes a
  persona the deploy created), the current version is moved to a trash
  dir (`personas-archive/._pf_trash_<stamp>/`, a dotfile dir phantombot
  ignores) instead of being deleted. The trash is only removed after
  every rollback step has succeeded; if the rollback fails mid-way, the
  discarded content survives there, the manifest entry is kept, and the
  error message points to the trash for manual recovery.
- **Rollback failures are contained.** `execute_rollback` now wraps its
  steps: any `OSError` raises a `RollbackError` that says what happened
  and where the preserved data is — no silent half-restore.
- **The confirmation now shows what will be replaced.** `po rollback`
  prints a `replace:` line listing personas whose current version will
  be swapped for the archived one (with the trash guarantee).
- **Manifest writes are atomic** (temp file + rename), so a crash can
  never truncate the manifest.
- **Session ids are collision-proof** (numeric suffix if two deploys
  land in the same millisecond — previously a shared id would have made
  a rollback drop BOTH sessions).
- **Archive paths are resolved at record time**, so a deploy invoked
  with a relative `--target` no longer records relative paths that
  break `po rollback` from another directory.
- **Corrupt manifest is reported.** `po rollback` now tells the user
  when the manifest exists but is unreadable, and that the archived
  personas can still be restored manually.
- **Mid-deploy filesystem errors are caught** in `deploy`/`deploy-all`:
  if a copy fails after some personas were already archived, the CLI
  explains that those backups are intact in `personas-archive/` and how
  to restore them manually (no session is recorded for a partial
  deploy).
- **Python 3.10 support fixed**: `tomllib` (3.11+) is imported with a
  `tomli` fallback, matching the declared `requires-python >=3.10`
  (previously the CLI would not even import on 3.10).
- New tests: rollback failure keeps discarded data in the trash with
  the manifest intact; trash is removed on full success.

## v0.4.0 — 2026-08-09

**Transactional rollback — one command restores the system to exactly the
state it was in before a deploy.**

- **Deploy session manifest** (new): every `po deploy` / `po deploy-all`
  records a *session* in `personas-archive/.phantomorg-manifest.json`
  (a dotfile, ignored by phantombot): what was deployed, what was
  created, what was archived (with exact paths), and whether
  `personas-archive/` and the target existed before the deploy.
- **`po rollback`** (new): undoes the last deploy — archived personas are
  moved back (the backup is consumed, not left behind), personas the
  deploy created are removed, and if `personas-archive/` (or the target
  itself) did not exist before that deploy it is deleted too. The system
  ends up exactly as it was before the process started.
  - Stack-based: run it once per deploy you want to undo.
  - `po rollback --list` shows recorded sessions.
  - `--yes` skips the confirmation for scripting/CI.
  - Refuses to roll back when an archived persona is missing (an
    incomplete rollback is worse than none).
  - Warns when an org.yaml has drifted (spec changed) since the deploy.
- Deploy output now ends with a hint: `Rollback available: pf rollback`.
- New tests: full rollback cycle (v1 → v2 → rollback restores v1 and
  deletes the backups), fresh-deploy rollback (created personas removed,
  empty target deleted), prune rollback, pre-existing archive dir kept,
  missing archive refusal, stacked sessions, CLI list/rollback.

## v0.3.0 — 2026-08-09

**Rollback safety — nothing is written without a final confirmation, and
everything PhantomOrg modifies is backed up first.**

- **Final confirmation before applying** (new): `po deploy`, `po deploy-all`
  and `po setup` now show a summary of what they are about to do and ask
  for an explicit `[y/N]` confirmation before writing anything. Answering
  no prints `Cancelled — no changes were made.` and exits 1. New `--yes`
  flag on `deploy`/`deploy-all` skips the prompt for scripting/CI.
- **Personas are archived, never silently overwritten** (new): before
  `po deploy` overwrites an existing persona (same organization, or
  `--force`), the whole directory is moved to
  `personas-archive/<name>-<timestamp>/` — phantombot's own backup
  convention (same name format, so `phantombot import-persona` can list
  and restore it). Prune (`--prune`) archives actors instead of deleting
  them, so even removing an actor from the spec is reversible.
- **The archive directory is announced** (new): the first time
  `personas-archive/` is created, the CLI tells the user where backups
  live; every archived persona is listed with its exact archive path.
- **org.yaml is backed up before every mutation** (new): `add-*`,
  `remove-*`, `rename-*` and `po setup` on an existing org write
  `org.yaml.bak-<timestamp>` (microsecond precision) next to the file
  before modifying it, and announce it on stderr. Restoring is a simple
  `cp org.yaml.bak-<ts> org.yaml`.
- New tests: overwrite archives first (phantombot-compatible names),
  prune archives instead of deleting, `--force` archives hand-written
  personas, archive-dir creation notice, org.yaml backup before mutation
  (2 new test classes; 138 tests total).

**`po setup` — guided installation over a phantombot installation.**

- New command: `po setup [--phantombot-dir PATH] [--org org.yaml] [--base DIR]`.
  One-pass wizard that:
  1. locates the phantombot personas directory (detected at
     `~/.local/share/phantombot/personas/` or asked),
  2. reuses an existing org.yaml or creates a fresh one (departments
     defined interactively, access policy defaults to `level-2`),
  3. reassigns every existing persona to a department + role —
     `import-audit` suggests both, the user confirms or overrides,
  4. optionally adds brand-new personas,
  5. writes/mutates the org.yaml so `po validate` accepts it immediately.
- Roles are shared: several personas accepting the same suggested role
  reuse one role entry instead of creating duplicates.
- Role id default is the slugified suggestion (`"Project Lead"` →
  `project_lead`); without one, `<actor_id>_role` — never the bare actor
  id, which would collide with the org-wide id uniqueness rule.
- `_slugify` now strips accents (`"café"` → `cafe`).
- `po setup` only writes the org.yaml — it never touches MEMORY.md or any
  persona content. Manual content outside ORG blocks stays intact on
  later deploys.
- Spec section 7.1 documents the flow; README shows usage. 131 tests,
  ruff + mypy + bandit clean.

## v0.1.3 — 2026-08-09

**Scaffold aligned with phantombot's persona layout.**

- The memory scaffold now matches `phantombot`'s own `personaScaffold.ts`:
  the four structured drawers are created as FILES (`memory/people.md`,
  `memory/decisions.md`, `memory/lessons.md`, `memory/commitments.md`) plus
  `memory/archive/`, instead of the previous OpenClaw-style category
  directories (`people/`, `decisions/`, ...).
- Seed files are stamped idempotently, exactly like phantombot:
  `kb/Home.md` and `kb/templates/{atomic-note,runbook,decision,postmortem}.md`.
  An existing seed is never overwritten.
- Net effect: a freshly deployed actor now boots with the drawers the
  nightly memory cycle promotes into, and the KB home/templates the memory
  system expects — previously missing.

## v0.1.2 — 2026-08-09

**Validator hardening (findings from a Copilot code review).**

- **Ids must be strings**: `shape_validator` now enforces `str` for
  organization/department/role/actor ids and key reference fields
  (`role`, `department`, `access_level`, `access_policy`). A numeric or
  list id previously passed shape validation and could break the
  duplicate check with a `TypeError`.
- **Org-wide id uniqueness**: an actor, role or department may no longer
  share the same id. Per-group uniqueness was already enforced; ids are
  now treated as org-wide identifiers so hand-edited YAML stays
  unambiguous.
- **Duplicate escalation pairs flagged**: the same `from -> to` appearing
  twice would silently emit two identical escalation paths in the
  compiled SOUL; the validator now reports it.

## v0.1.1 — 2026-08-09

**Packaging / installation.**

- **`install.sh` (phantomyard tool convention)**: symlinks `bin/po` and
  `bin/phantomorg` into `~/.local/bin` (or `$PREFIX/bin`) so the CLI is
  on your PATH without a pip install. The repo stays the single source of
  truth — edits take effect on the next run.
- **`bin/` wrappers**: resolve their own real path through the symlink and
  run the CLI from the repo; use a repo `.venv` when present, else system
  `python3`. Dependencies (PyYAML, Jinja2, click) are checked up front with
  a clear install hint.
- **`po --version` fix**: now reads the version from the repo's
  `pyproject.toml` (single source of truth) with a fallback to installed
  metadata, so it reports the right version when run from a symlinked
  checkout without a pip install.
- **Safety**: the installer never clobbers — it refuses to overwrite a
  foreign symlink or a regular file that isn't its own, and replaces only
  its own (or dangling) symlinks. Idempotent: safe to re-run.
- **README**: new Install section (symlink install recommended, pip install
  as alternative).

## v0.1.0 — 2026-08-09 (first public release)

First public release of the English, runtime-agnostic project. It
supersedes the internal Spanish development line (see Pre-release
history below).

**What's in this release:**
- **English everywhere**: full translation of the spec, changelog,
  README, code strings, docstrings, comments and tests.
- **Runtime-agnostic deploy**: `PHANTOMORG_TARGET_DIR` env var to
  override the target personas directory; deploy module renamed to
  `deploy/target.py`.
- **Packaging fix**: the Jinja2 templates are now shipped in the wheel
  (`[tool.setuptools.package-data]`), so `pip install .` works and
  `po build` finds `identity.j2`/`soul.j2`/`tools.j2`/`memory.j2`.
- **CLI UX**: `po --version`; `new-org` only requires `--id` and
  `--name` (sector defaults to `general`, language to `en`); wizard
  aborts (Ctrl+C/EOF) print "Cancelled — no changes were made." and
  exit 1.
- **CI**: monorepo-root GitHub Actions workflow (`pipeline` job) that
  installs non-editable and runs the full test suite plus an
  end-to-end smoke build.

---

## Pre-release history (internal development, Spanish)

Versions below are the internal pre-publication line (0.1.0 → 0.9.0).
The public release restarts at v0.1.0 above; the history is preserved
for transparency.

---

## v0.9.0 — default_language now actually wires the templates

**Gap found:** `organization.languages` / `default_language` existed
in the model and the schema from the beginning, but no template read
them. The headers and fixed phrases of `SOUL.md`/`IDENTITY.md`/
`tools.md`/`MEMORY.md` were hardcoded in Spanish directly in the
`.j2` files — the field was decorative, the output was always in
Spanish no matter what the spec said.

**Fix:** new `compiler/i18n.py` with a dictionary of the ~30 fixed
strings per template (headers, labels, notices), in `es` and `en`.
`build.py` resolves the organization's real language
(`resolve_lang()`: explicit `default_language` → first language of
`languages` → `es` by default) and passes it to the 4 templates as
`t`. The templates use `{{ t.clave }}` for everything fixed; the
dynamic content (role names, departments, functions, escalation
conditions, `policies` labels...) still comes out exactly as it is in
`org.yaml`, untranslated — there's no way to know what language the
user wrote those values in, and it's not PhantomOrg's
responsibility to decide.

Tested in a real terminal: Verdant Aquaponics Co-op (`default_language: es`)
still generates in Spanish exactly as before; a synthetic
organization with `default_language: en` generates `SOUL.md`/`IDENTITY.md`/
`tools.md` entirely in English, with the real spec values
(`Headquarters`, `Executive`) untranslated.

**Decision I made and why:** I'm leaving the CLI (`po ...`, help
messages, errors) in Spanish. It's not that switching to English was
technically hard, but it was expensive to verify properly: dozens of
tests (`test_cli.py`, `test_remove_rename.py`, etc.) do `assertIn`
on concrete Spanish error strings, and changing the CLI language
would have meant touching all those tests at the same time as the
CLI, with more surface area for introducing a silent mismatch than
the benefit (purely cosmetic, it doesn't affect any generated agent)
justifies right now. If the CLI is ever needed in English, it's an
isolated, mechanical change — it doesn't block anything else.

**On point 3 of the original proposal** ("update the ~3 tests that
assume Spanish"): I reviewed the repo before touching anything — no
existing test depended on the Spanish template strings (`grep`
found no real matches). None needed updating; I did add 8 new tests
(`tests/test_i18n.py`) that explicitly test language resolution and
the real content in both languages.

## v0.8.1 — deploy no longer silently overwrites a hand-written SOUL

**Critical gap found while trying to migrate a real 5-agent
infrastructure with existing SOULs, never generated by PhantomOrg:**
`po deploy` only detected collisions by comparing `organization_id`
in `.phantomorg.yaml`. A hand-written SOUL doesn't have that
file — so `existing_org` returned `None`, the collision condition
was never met, and `shutil.copytree(dirs_exist_ok=True)` overwrote
the real file **without any warning**. `import-audit` only captures
structural facts (role, department, reports_to, tools) — never the
decision principles, business rules, or style that someone wrote by
hand, so this silent overwrite would have been real, unrecoverable
information loss.

**Fix:** a destination that exists but has no `.phantomorg.yaml`
is now treated as a collision — it requires explicit `--force`, with
a message that explicitly says "it may be a hand-written persona".
`--force` remains the intentional escape hatch for when you really
want to overwrite.

Tested in a real terminal reproducing the exact scenario: 5 folders
with hand-written `SOUL.md` (no metadata) in the destination → `po deploy`
without `--force` stops for all 5 at once, listing each one, and the
hand-written content remains intact after the attempt.

**Correct flow for migrating existing SOULs (it's not automatic, and
it shouldn't be):**
1. `po import-audit --persona-dir <persona> --role-id <id> --against-org <org.yaml>`
   for each agent — captures only the structure (role, department,
   reports_to, tools), never the domain content.
2. `po build --out <new-dir>` (never directly to the real destination) —
   generates a new SOUL.md, correct in structure, generic in
   everything else.
3. **Manual step, irreplaceable:** copy by hand what's valuable from
   the original SOUL (principles, business rules, style) into the
   "Notes (manual editing)" section of the newly generated SOUL —
   outside the `ORG:BEGIN/END` blocks, so it survives future
   regenerations.
4. Only then `po deploy` from that already merged directory.

## v0.8.0 — import-audit --apply, build-all/deploy-all, deploy --prune, CLI tests

Closes the four gaps explicitly flagged in the previous audit.

**1. `import-audit --apply`:** applies the proposed fragment directly
on `--against-org` (add-role + add-actor), asking for confirmation
unless `--yes`. Never picks a random candidate if `reports_to` is
left ambiguous: it applies with `reports_to: null`, same as in the
read-only fragment. Requires `--against-org` explicitly (it's the
file that gets written to).

**Related gap found while implementing this:** `add_department` /
`add_role` / `add_actor` didn't check whether the id already existed —
a repeated `po add-role --id ceo` silently created a second entry
with the same id, and the validator didn't catch it either
(`check_references` only checked references, not uniqueness). Fix:
new `DuplicateIdError` in the three creation functions, plus a
uniqueness check in the validator as defense in depth (in case
someone edits `org.yaml` by hand).

**2. `build-all` / `deploy-all`:** operate on all organizations under
`--base` at once. `build-all` compiles each one to
`--out/<org_id>/`, without aborting the batch if an organization is
not valid (it skips it and continues). `deploy-all` deploys from
`--dist-base/<org_id>/`, reporting which organization had no build
or had a collision without stopping the others.

**3. `deploy --prune`:** deletes from the destination the actors that
belong to the SAME organization but are no longer in the current
build (removed with `remove-actor`). Never touches actors from
another organization, even if they're not in this build — the
criterion is always "same organization, no longer in the spec",
never "not in this build". If the compiled build is empty or has no
metadata, it prunes nothing (better not to prune than to over-prune).

**4. CLI tests with `click.testing.CliRunner`** (`tests/test_cli.py`):
cover build-all/deploy-all, import-audit --apply (including
cancellation and duplicate-id rejection), deploy --prune, and the
basic commands — exercising the real CLI wiring, not just the
underlying Python functions.

Tested in a real terminal (not just in the test runner):
`build-all` + `deploy-all` on Verdant Aquaponics Co-op and United Capital
Group at the same time, and a duplicate `add-role --id ceo` attempt
cleanly rejected.

## v0.7.0 — remove-department/role/actor and rename-department/role/actor

**Gap found:** there was no way to remove or rename anything once
created, except by editing `org.yaml` by hand — with the real risk of
leaving broken references (a deleted role that another one still
reported to, a removed department with roles still assigned, an
actor pointing to a half-renamed role).

**Fix — `remove-*`:**
- `remove-department`: blocks if it has roles assigned (they are
  never reassigned on their own). If it has child departments, it
  blocks unless `--cascade`, which promotes them to root
  (`parent: null`).
- `remove-role`: blocks **always** (even with `--cascade`) if there
  are actors assigned — deleting a real actor without explicitly
  asking for it is too destructive to automate; you have to use
  `remove-actor` first. With `--cascade`, it promotes subordinate
  roles to root and removes the `escalation_matrix` entries that
  mentioned it.
- `remove-actor`: no structural blocks (nothing else references
  actors by id), but it explicitly warns that it doesn't delete the
  already compiled/deployed directory.

All ask for confirmation (`click.confirm`) unless `--yes`/`-y`.

**Fix — `rename-*`:** update every cross-reference automatically:
`rename-department` fixes the `parent` of children and the
`department` of roles; `rename-role` fixes the `reports_to` of
subordinates, the `role` of actors, and the `from`/`to` of
`escalation_matrix`; `rename-actor` only changes the id (nothing
else references it), with a note that the directory on disk doesn't
move on its own — you need `po build` + `po deploy` afterwards.

Tested on the real CLI against a copy of Verdant Aquaponics Co-op: a real
block when trying to delete `ceo` with Marco assigned (even with
`--cascade`); after removing Marco, `--cascade` correctly promoted
`chief_of_staff` and `cfo` to root and cleaned up the 3
`escalation_matrix` entries that mentioned `ceo`;
`rename-role chief_of_staff -> cos` updated the 5 correct
cross-references and the result still passed `po validate`.

## v0.6.0 — The interactive wizard now suggests from the real organization

**Before:** `add-role`/`add-actor` in interactive mode asked for
`department`, `reports_to` and `role` as free text — a typo
(`operacioness` instead of `operaciones`) was saved as-is and only
detected later with `po validate`.

**Now:** the wizard reads the real `org.yaml` before asking and
offers the existing departments/roles as `click.Choice`:
- `add-department` suggests the existing departments for the
  `parent` (+ "none" for root).
- `add-role` suggests the existing departments for `department`, and
  the existing roles for `reports_to` (+ "none" for root).
- `add-actor` suggests the existing roles for `role`.

If the organization doesn't have departments yet (for `add-role`) or
roles (for `add-actor`), the wizard stops with an explicit message
instead of offering an empty list of options. Tested on the real
CLI: a typo in the department (`operacioness`) is now rejected on
the spot (`Error: 'operacioness' is not one of ...`), instead of
being saved and discovered later with `po validate`.

## v0.5.0 — Fuzzy matching and department suggestion in import-audit

Closes the two gaps explicitly left open in v0.4.0.

**1. Fuzzy matching (typos, non-literal abbreviations):**
`_match_candidate()` now tries, in order of confidence, exact →
substring → fuzzy (`difflib.get_close_matches`, stdlib, cutoff 0.8).
Fuzzy matches are recorded separately in
`ResolvedImportFindings.fuzzy_matches` and always carry an explicit
verification note — never presented with the same confidence as an
exact or substring match. Tested with "Robrto" → resolves to
`diego`/`cfo` with the corresponding warning.

**2. Department suggestion without a resolved superior:** new
three-level priority (`_resolve_department`):
  1. Department of the role already resolved via `reports_to` (the
     most reliable).
  2. An explicit `**Departamento**:` detected in the text, resolved
     against the real departments (exact → substring → fuzzy).
     Tested with "Formacion" (no accent) → resolves to `formacion`
     by fuzzy match.
  3. Department name match as a substring inside the detected role
     (low confidence, declared as such in the note), e.g.
     "Director de Operaciones" → suggests `operaciones`. If the role
     text mentions more than one department name, it's declared
     ambiguous and nothing is suggested.
  4. If no path resolves anything, it explains exactly why not (so
     that `--department` doesn't feel like an arbitrary fallback).

## v0.4.0 — import-audit resolves "reports to" against a real organization

**Gap found:** `reports_to_guess` was free text extracted by regex
("CEO", "Lucia", "Marco, Board President o Tomás"...) that was never
translated into a real `role_id` — the proposed fragment always
left `reports_to: null` with the raw text as a comment, regardless
of whether the destination was resolvable.

**Fix:** new `--against-org <org.yaml>` flag and
`resolve_against_org()` function. With it, the detected text is
split into candidates (by commas/"o"/"y"/"or"/"and" — the real
pattern found in the Verdant Aquaponics Co-op audit, "Diego escala a
Marco, Board President o Tomás") and matched against the roles and actors of
the destination organization:

- If all candidates that match something point to the same role, it
  resolves without ambiguity (even if some candidates remain
  unmatched, probably external humans — they're listed separately,
  not silently discarded).
- If they match different roles, it's explicitly marked as ambiguous
  in the fragment (`# AMBIGUO: ... resuelve a mano`) — one is never
  chosen at random.
- When it resolves, `department_id` is also suggested from the
  department of the resolved role, so `--department` becomes
  optional when `--against-org` is used.

Tested with the real Verdant Aquaponics Co-op case: "Marco, Board President o Tomás"
resolves to `role_id='ceo'` (via the actor `marco`), with
`Board President`/`Tomás` listed as not found (candidates for
`reports_to_human`, not for role).

**Still unresolved** (scope explicitly outside this iteration):
fuzzy matching beyond substring/exact (e.g. tolerating typos or
non-literal abbreviations), and department resolution when the text
doesn't mention anyone already in the spec.

## v0.3.1 — The interactive wizard now asks for the template

**Gap found:** `po new-org` in flags mode (`--template ngo`) did
apply the sector template, but interactive mode (no flags) didn't
ask for it at all — the "sector" question only fed
`organization.sector` (descriptive metadata), it didn't trigger any
template. Answering "ngo" there had no effect on the generated
departments.

**Fix:** `run_new_org_wizard()` adds an explicit template question
(`click.Choice`, with "none" + the templates of `po templates`) and
passes the result to `new_org(..., template=...)`. Both paths (flags
and wizard) are now consistent.

## v0.3.0 — Epic 3: multi-organization, sector templates, import audit

**Safe multi-organization in `deploy`:** every compiled actor now
includes a metadata file (`.phantomorg.yaml`) with its
`organization_id`. `po deploy` compares that id against the one
already in the destination: if two different organizations share an
actor id, the deploy stops with `DeployCollisionError` unless
`--force` is passed. Before, deploying two organizations to the same
persona directory could silently overwrite one agent with the
other's. New `po list-orgs` command to list and validate at a glance
all organizations under `organizations/`.

**Sector templates (`po new-org --template <sector>`):**
`ngo` (modeled on the real structure of Verdant Aquaponics Co-op),
`pyme`, `consultora`, `finance`. Each template only provides the
default departments — roles and actors remain 100% specific to each
real organization, deliberately. New `po templates` command to list
them.

**Import audit (`po import-audit`):** analyzes an already existing
persona folder (not generated by PhantomOrg — by hand, from a
generic `create-persona`, or imported from another format) and
proposes a `roles:`/`actors:` fragment to review and paste by hand.
It never writes directly to any `org.yaml`: it's a heuristic over
free text (regex over Markdown without guaranteed structure), so
every field not detected with confidence stays as an explicit
warning instead of being filled with an invented value.

## v0.2.1 — {org_id} resolution in request_id_format

**Gap found:** `po new-org` resolved `{org_id}` with an f-string at
the moment of creating the file, but any `org.yaml` written or
edited by hand (like those of Verdant Aquaponics Co-op and United Capital
Group) left the placeholder literal. The generated SOUL.md ended
with
`` `{org_id}-{yyyymmdd}-{seq4}` `` unresolved — confusing for the
agent itself, which doesn't know what `{org_id}` is.

**Fix:** resolution is centralized in the compiler
(`compiler/request_id.py::resolve_request_id_format`), not in the
wizard. It doesn't matter whether the `org.yaml` came from
`po new-org` or was written by hand: the generated SOUL.md always
carries the organization's real id substituted, and
`{yyyymmdd}`/`{seq4}` are deliberately left literal (the
agent/runtime resolves them when generating a real Request-ID, not
PhantomOrg at build time). `new_org.py` no longer resolves
`{org_id}` early, so that both paths (wizard and manual editing)
produce the same input format and depend on a single resolution
point.

## v0.2.0 — Block merge (fix of a real gap reported after the VPS pilot)

**Gap found:** `write_if_changed` froze the entire file if it
contained the `[ORG:manual]` marker, including the sections
derived from `org.yaml` (security/escalation/comms). Any manual
note left the whole file out of regeneration forever.

**Fix:** the templates now delimit each spec-derived section with
`<!-- ORG:BEGIN <section> -->` / `<!-- ORG:END <section> -->`
markers. On each `po build`:

- Everything INSIDE a block is always regenerated.
- Everything OUTSIDE (manual notes, before/between/after the
  blocks) is preserved as-is.
- If the file has no recognizable blocks, it's preserved in full
  (a deliberate opt-out, for whoever decides to leave automatic
  generation entirely).

See `phantomorg/compiler/blocks.py` and the tests in
`tests/test_blocks.py` and `tests/test_compiler_au.py`
(`test_manual_note_outside_blocks_is_preserved_but_blocks_regenerate`).

**Related fix (found in the same review):** `MEMORY.md` was
regenerated on every build like the other files. Since it
accumulates facts written by the runtime during the agent's real
operation, that could erase real memory. Now it uses
`write_if_missing`: it's created once and never touched again in
later builds, whether or not it has blocks.

**Migration if you're coming from v0.1.0:** any file you marked with
the old `[ORG:manual]` (whole file) will keep being preserved
unchanged — it simply has no recognizable `ORG:BEGIN/END` blocks,
so it falls into the "full opt-out" case. To benefit from the
per-section merge again in that agent, delete the file and
recompile it from scratch with `po build`, and move your manual
notes to the "Notes (manual editing)" section at the end.

## v0.1.0 — Initial MVP

Wizard (`new-org`, `add-department`, `add-role`, `add-actor`),
validator (schema + escalation DAG + cross-references + budgets),
compiler (IDENTITY/SOUL/tools/MEMORY + scaffold memory/kb), deploy.
Pilot with the real `org.yaml` of Verdant Aquaponics Co-op.
