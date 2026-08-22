"""
Copies the compiled output (out_dir/<actor_id>/...) to the runtime's
personas directory, with three safety checks:

- Cross-organization collision: if the target already has an actor with
  that id belonging to ANOTHER organization (according to the
  `.phantomorg.yaml` the compiler leaves in each compiled actor), the
  deployment is rejected unless `force=True`.
- Unmanaged persona (hand-written SOUL): if the target already exists but
  has NO `.phantomorg.yaml` at all, it is treated as a collision — it
  may be a persona that never went through PhantomOrg, and overwriting
  it without notice would destroy hand-written content (decision
  principles, business rules, style) that no import-audit heuristic
  captures. It requires explicit `force=True`.
- Optional prune (`prune=True`): removes from the target the actors that
  belong to the SAME organization being deployed but are no longer in the
  current build (they were removed from org.yaml). It never touches
  actors of another organization, even if they are not in this build —
  the criterion is always "same organization, no longer present", not
  "not in this build".
- Rollback safety: before any existing persona directory is overwritten
  (same organization, or `force=True`), the whole directory is MOVED to
  the sibling `personas-archive/<name>-<timestamp>/` (phantombot's own
  archive convention, so `phantombot import-persona` can restore it).
  Pruned actors are archived the same way instead of being deleted.
  The first time the archive directory is created, an explicit notice is
  returned so the CLI can tell the user where backups live.

Without the first check, deploying two different organizations to the
same personas directory could silently overwrite one organization's agent
with another's if they share an actor id. Without the second, `po deploy`
would silently overwrite any hand-written SOUL sharing an actor id with
something freshly compiled — the real gap exposed by the first attempt to
migrate an existing 5-agent infrastructure. Without the prune, removing
an actor from the spec (`remove-actor`) left its orphan folder in the
target forever. Without the archive step, none of those overwrites would
be reversible — the archive is the pre-modification backup the runtime
never makes on its own.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml

from ..compiler.blocks import has_blocks, merge_content, strip_blocks
from ..compiler.humans import HUMANS_FILENAME, merge_humans_markdown
from ..compiler.scopes import SCOPES_FILENAME, merge_scopes_json

# Default target when neither --target nor PHANTOMORG_TARGET_DIR is set.
# Phantombot's personas directory is used as the practical default, but
# PhantomOrg itself is runtime-agnostic: any directory works.
DEFAULT_PERSONAS_DIR = Path.home() / ".local/share/phantombot/personas"

# Environment variable to override the deploy target without flags.
ENV_TARGET_DIR = "PHANTOMORG_TARGET_DIR"


def default_personas_dir() -> Path:
    """Resolve the default personas directory.

    Priority: $PHANTOMORG_TARGET_DIR (if set and non-empty) →
    DEFAULT_PERSONAS_DIR.
    """
    env_dir = os.environ.get(ENV_TARGET_DIR, "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return DEFAULT_PERSONAS_DIR


_META_FILENAME = ".phantomorg.yaml"


# phantombot keeps overwritten personas in a sibling "personas-archive/"
# directory, named "<name>-<ISO-timestamp>", and restores them with
# `phantombot import-persona`. PhantomOrg reuses that exact convention
# so the backup it makes before overwriting is restorable with the
# runtime's own tooling (no PhantomOrg-specific restore needed).
#
# The timestamp format matters: phantombot parses
# "<name>-<YYYY-MM-DDTHH-MM-SS-mmmZ>" (ISO-8601 with ':' and '.' replaced
# by '-') with an optional "-N" suffix for same-millisecond collisions.
def archives_dir(personas_dir: Path) -> Path:
    """Sibling directory where phantombot (and PhantomOrg) keep
    archived personas: <personas_dir>/../personas-archive."""
    return personas_dir.parent / "personas-archive"


def _archive_stamp() -> str:
    """ISO-8601 timestamp in phantombot's archive format:
    2026-08-09T15-04-00-000Z (':' and '.' replaced by '-', UTC offset
    rendered as 'Z' exactly like JS Date.toISOString())."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        .replace(":", "-")
        .replace(".", "-")
    )


def _assert_real_directory(path: Path, label: str) -> None:
    """Refuse a path that is a symlink or a non-directory (audit v0.5.7
    #2): ``personas-archive/`` could be pre-planted as a symlink to an
    external location, redirecting every backup outside the expected
    tree (``mkdir(exist_ok=True)`` happily follows a symlink to an
    existing dir). Call before creating or using a directory PhantomOrg
    treats as its own."""
    if path.is_symlink():
        raise DeployError(
            f"refusing to use {label}: {path} is a symlink. Remove it or "
            f"point it at a real directory, then retry."
        )
    if path.exists() and not path.is_dir():
        raise DeployError(
            f"refusing to use {label}: {path} is not a directory. Remove it "
            f"or turn it into a directory, then retry."
        )


def archive_persona(personas_dir: Path, name: str) -> tuple[Path, bool]:
    """Move personas_dir/<name>/ into personas-archive/<name>-<stamp>/.

    Returns (archive_dir, created_archive_root): the destination, and
    whether the archive root directory was created by this call (so the
    CLI can announce it). Same-millisecond collisions get a numeric
    suffix, exactly like phantombot's personaScaffold.

    Refuses to archive a symlink: moving a link that points outside the
    tree would relocate (or later restore) content from an unexpected
    location.
    """
    src = personas_dir / name
    if not src.exists():
        raise FileNotFoundError(f"persona '{name}' does not exist at {src}")
    # F7: only directories are personas. A plain FILE at the target
    # passes ``src.exists()``, gets archived as a file, and is then
    # never restored by rollback (the restore branch requires a
    # directory archive) — the pre-deploy file would be permanently
    # lost under the compiled dir. Refuse loudly instead.
    if not src.is_dir():
        raise DeployError(
            f"refusing to archive '{name}': {src} is not a directory (it is "
            f"a file). A persona must be a directory; remove the file or "
            f"turn it into a directory, then retry."
        )
    if src.is_symlink():
        raise DeployCollisionError(
            f"refusing to archive '{name}': the target entry {src} is a "
            "symlink (possibly pointing outside the personas tree). Remove "
            "it manually, then retry."
        )
    archive_root = archives_dir(personas_dir)
    # Audit v0.5.7 #2: the archive root itself could be a pre-planted
    # symlink redirecting backups outside the tree. Reject before mkdir.
    _assert_real_directory(archive_root, "archive root")
    created_root = not archive_root.exists()
    archive_root.mkdir(parents=True, exist_ok=True)

    base = f"{name}-{_archive_stamp()}"
    dst = archive_root / base
    for suffix in range(1, 1000):
        if not dst.exists():
            break
        dst = archive_root / f"{base}-{suffix}"
    else:
        # All 1000 candidate names exist: the loop never found a free
        # one. Raising instead of falling through is important — with a
        # taken ``dst``, shutil.move would place the source INSIDE the
        # existing directory (``<dst>/<name>/``) instead of at the
        # intended archive path, silently corrupting the layout.
        raise DeployError(
            f"unable to allocate a unique archive destination for '{name}': "
            "1000 same-millisecond archives already exist"
        )
    shutil.move(str(src), str(dst))
    return dst, created_root


def _move_to_archive(personas_dir: Path, name: str, created_flags: set[Path]) -> Path:
    """Archive one persona directory, recording whether the archive root
    was newly created (so the CLI announces it once per deploy). Returns
    the archive directory."""
    dst, created_root = archive_persona(personas_dir, name)
    if created_root:
        created_flags.add(archives_dir(personas_dir))
    return dst


class DeployError(Exception):
    """Base class for deployment failures (collisions, unsafe targets,
    exhausted names). The CLI catches it to report a friendly message."""


class DeployCollisionError(DeployError):
    """Raised when a compiled actor would collide with one of another organization."""


@dataclass
class DeployResult:
    target: Path
    deployed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    """Personas written that did NOT exist in the target before this deploy
    (fresh creates, not overwrites). Needed for rollback: these must be
    removed to return the system to its pre-deploy state."""
    pruned: list[str] = field(default_factory=list)
    archived: list[tuple[str, str]] = field(default_factory=list)
    """(persona_name, archive_dir) for each pre-overwrite backup made.

    Deploy is strictly additive, so these archive dirs always hold
    PER-FILE backups (the owned files at their relative paths).
    ``file_archives`` marks their resolved paths for rollback."""
    file_archives: set[str] = field(default_factory=set)
    """Resolved archive-dir paths that contain per-file (additive)
    backups. Rollback copies these files back in place."""
    created_archive_dirs: set[Path] = field(default_factory=set)
    """Archive roots created during this deploy (to announce once)."""
    scopes_written: bool = False
    """True when compiled scopes.json was transported to the data dir
    (target.parent — e.g. ~/.local/share/phantombot/scopes.json)."""
    scopes_backup: str | None = None
    """Absolute path (inside personas-archive/) of the pre-overwrite
    copy of scopes.json, or None when the file did not exist before it
    was written (the deploy created it fresh) or when it was not
    written at all. ``po rollback`` restores this backup to return the
    data dir to its exact pre-deploy state."""
    scopes_created: bool = False
    """True when scopes.json did NOT exist in the data dir before this
    deploy and the deploy created it. On rollback it is removed to
    return to the exact pre-deploy state (absent)."""

    humans_written: bool = False
    """True when compiled HUMANS.md was transported to the data dir
    (target.parent). Same semantics as scopes_written."""
    humans_backup: str | None = None
    """Absolute path (inside personas-archive/) of the pre-overwrite
    copy of HUMANS.md, or None when the file did not exist before it
    was written or was not written at all. Restored by rollback."""
    humans_created: bool = False
    """True when HUMANS.md did NOT exist in the data dir before this
    deploy and the deploy created it. Removed by rollback."""


def _write_data_file(
    filename: str,
    label: str,
    compiled_dir: Path,
    target: Path,
    staging: Path,
    merge_fn: Callable[[str, str], str] | None = None,
) -> tuple[bool, str | None, bool]:
    """Transport a compiled data-dir artifact (scopes.json / HUMANS.md)
    into the data dir (``target.parent / filename``), returning
    ``(written, backup, created)``.

    - ``written``: True when the file was transported (the build had it).
    - ``backup``: absolute path (inside personas-archive/) of the
      pre-overwrite copy, or None when the destination did not exist
      before the write (the deploy created it fresh).
    - ``created``: True when the destination did NOT exist before this
      write and the deploy created it.

    The destination deliberately lives OUTSIDE the target tree
    (personas/): the data dir is where phantombot reads it from (next
    to memory.sqlite / memory-index/). The pre-overwrite version is
    backed up into personas-archive/ so ``po rollback`` can restore the
    data dir to its exact pre-deploy state.

    Backward compatible: an old build without the file deploys exactly
    as before (returns (False, None, False)). Symlink guards on both
    sides mirror the persona policy (refuse a planted link instead of
    following it); the copy goes through staging (same filesystem as
    the destination) so the final os.replace is atomic.

    Multi-org note: deploy-all calls this once per org. With ``merge_fn``
    set (scopes.json unions the per-org ``scopes`` maps; HUMANS.md upserts
    the org's registry), the LAST org no longer clobbers the earlier orgs'
    global artifacts — the data dir accumulates every org instead. The
    backup records the pre-overwrite state of each write; the deploy-all
    merge keeps the FIRST org's backup (the true pre-session state),
    matching the archive-dedup semantics.
    """
    src = compiled_dir / filename
    if not src.exists():
        return False, None, False
    if src.is_symlink():
        raise DeployCollisionError(
            f"refusing to deploy: compiled {filename} is a symlink"
        )
    dest = target.parent / filename
    if dest.is_symlink():
        raise DeployCollisionError(
            f"refusing to deploy: data dir {filename} is a symlink"
        )

    dest_pre_existed = dest.exists()
    backup: str | None = None
    if dest_pre_existed:
        # Snapshot the pre-overwrite version into personas-archive/ so
        # rollback can restore the exact pre-deploy state. The backup is
        # a plain file (ignored by the archive scan, which only touches
        # directories) with the ._pf_data_ prefix so it is clearly ours
        # and never mistaken for a persona archive.
        archive_root = archives_dir(target)
        _assert_real_directory(archive_root, "personas-archive")
        archive_root.mkdir(parents=True, exist_ok=True)
        # Collision-safe backup name (adversarial review P1): timestamp-only
        # names collide at millisecond precision and ``copy2`` would then
        # silently overwrite a sibling backup. Use exclusive creation with a
        # UUID suffix so two deploy-all writes landing on the same stamp can
        # never share (or clobber) a backup path.
        backup_path = archive_root / (
            f"._pf_data_{filename}-{_archive_stamp()}-{uuid4().hex}"
        )
        shutil.copy2(dest, backup_path)
        backup = str(backup_path.resolve())

    staging_copy = staging / filename
    if merge_fn is not None and dest_pre_existed:
        # Multi-org deploy-all: merge the incoming artifact into what is
        # already in the data dir instead of overwriting it (last-wins).
        existing = dest.read_text(encoding="utf-8")
        incoming = src.read_text(encoding="utf-8")
        staging_copy.write_text(merge_fn(existing, incoming), encoding="utf-8")
    else:
        shutil.copy2(src, staging_copy)
    os.replace(staging_copy, dest)
    return True, backup, not dest_pre_existed


def _write_scopes_file(
    compiled_dir: Path,
    target: Path,
    staging: Path,
    merge: bool = False,
) -> tuple[bool, str | None, bool]:
    """Transport compiled scopes.json (if any) into the data dir.

    Returns ``(written, backup, created)`` — see ``_write_data_file``.
    ``merge`` (multi-org deploy-all) unions the incoming scopes map into
    an existing data-dir scopes.json instead of overwriting it.
    """
    return _write_data_file(
        SCOPES_FILENAME,
        "scopes.json",
        compiled_dir,
        target,
        staging,
        merge_fn=merge_scopes_json if merge else None,
    )


def _write_humans_file(
    compiled_dir: Path,
    target: Path,
    staging: Path,
    merge: bool = False,
) -> tuple[bool, str | None, bool]:
    """Transport compiled HUMANS.md (if any) into the data dir.

    Same semantics as ``_write_scopes_file``: destination is
    ``target.parent / HUMANS.md`` (the data dir), NOT the target tree.
    An org without a ``humans:`` block builds no HUMANS.md and deploys
    exactly as before (returns (False, None, False)). ``merge`` upserts
    the org's registry into an existing data-dir HUMANS.md.
    """
    return _write_data_file(
        HUMANS_FILENAME,
        "HUMANS.md",
        compiled_dir,
        target,
        staging,
        merge_fn=merge_humans_markdown if merge else None,
    )


def _read_meta(actor_dir: Path) -> dict | None:
    """Best-effort read of a compiled/target actor's metadata.

    Returns None when the file is absent, unreadable, invalid YAML, or
    not a mapping — NEVER raises. A malformed meta file in one persona
    (target or build) must not be able to crash a whole deploy or prune
    scan with a raw traceback (adversarial review F4); callers treat
    None exactly like "no metadata": unknown origin, requires --force.
    """
    meta_path = actor_dir / _META_FILENAME
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _org_id_of(actor_dir: Path) -> str | None:
    meta = _read_meta(actor_dir)
    return meta.get("organization_id") if meta else None


def _assert_no_symlinks(actor_dir: Path) -> None:
    """Reject compiled actors that contain symlinks. PhantomOrg
    compiles plain text files; a symlink in the build could point
    anywhere and copying through it would pull unexpected content into
    the runtime (or worse). Refuse loudly instead."""
    for p in actor_dir.rglob("*"):
        if p.is_symlink():
            raise DeployCollisionError(
                f"refusing to deploy '{actor_dir.name}': the compiled actor "
                f"contains a symlink ({p}). PhantomOrg outputs plain "
                "text files; remove the link from the build and retry."
            )


def _staging_dir(target: Path) -> Path:
    """A fresh staging directory inside the target (same filesystem, so
    the final os.replace is an atomic rename). Dotfile prefix so
    phantombot ignores it if a crash leaves it behind.

    UUID-based name: two deploys started in the same millisecond can
    never obtain the same staging directory (a timestamp-only name could
    collide), and a name never implies ownership — stale cleanup is done
    by age, not by name."""
    return target / f".pf-staging-{uuid4().hex}"


# Staging dirs are cleaned only when demonstrably stale: older than this.
# A live deploy in another process (e.g. library-level use without the
# transaction lock) must never have its staging dir deleted merely
# because its name matches the pattern.
_STALE_STAGING_MAX_AGE = timedelta(hours=1)


def _cleanup_stale_staging(target: Path) -> None:
    """Remove leftover staging dirs from previous (interrupted) deploys.
    Only our own dotfile-prefixed dirs, never anything else, and only
    when they are older than ``_STALE_STAGING_MAX_AGE`` — a fresh dir is
    assumed to belong to a deploy that is still running."""
    if not target.is_dir():
        return
    now = datetime.now(timezone.utc)
    for p in target.iterdir():
        if not (p.name.startswith(".pf-staging-") and p.is_dir()):
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if now - mtime > _STALE_STAGING_MAX_AGE:
            shutil.rmtree(p, ignore_errors=True)


def _preflight(
    compiled_actor_dirs: list[Path],
    target: Path,
    force: bool,
    prune: bool,
    compiled_org_id: str | None,
    compiled_actor_ids: set[str],
) -> tuple[list[str], list[str]]:
    """Check everything BEFORE any mutation happens.

    Returns ``(collisions, prune_list)``:

    - ``collisions``: messages for every actor that would collide with an
      existing target entry (cross-organization, or an unmanaged
      hand-written persona). Empty when ``force`` (which overwrites).
    - ``prune_list``: names of target personas that belong to the
      deployed organization but are no longer in the build, computed
      from the PRE-deploy state.

    Also refuses symlinks before any write: symlinks inside a compiled
    actor (``_assert_no_symlinks``) and target entries that are symlinks
    (checked before ``_org_id_of`` would read THROUGH the link).

    The caller raises ``DeployCollisionError`` when ``collisions`` is
    non-empty. Because this runs before the first staging/archive/swap,
    a rejected deploy can never leave a partial state (some actors
    deployed, others not) — the gap that used to let a collision produce
    a partial deploy with no rollback session.
    """
    collisions: list[str] = []
    for actor_dir in compiled_actor_dirs:
        _assert_no_symlinks(actor_dir)
        dest = target / actor_dir.name
        if dest.is_symlink():
            raise DeployCollisionError(
                f"refusing to deploy '{actor_dir.name}': the target entry "
                f"{dest} is a symlink (possibly pointing outside the personas "
                "tree). Remove it manually, then retry."
            )
        if not dest.exists():
            continue
        existing_org = _org_id_of(dest)
        incoming_org = _org_id_of(actor_dir)
        if existing_org is None:
            # Target without PhantomOrg metadata: it may be a
            # hand-written SOUL that never went through here. There is no
            # way to know its origin, so it is treated the same as a
            # cross-organization collision — requires explicit --force
            # instead of silently overwriting.
            if not force:
                collisions.append(
                    f"{actor_dir.name}: the target already exists but has NO "
                    f"PhantomOrg metadata (.phantomorg.yaml) — it may "
                    f"be a hand-written persona; use --force if you really "
                    f"want to overwrite it"
                )
        elif incoming_org is None:
            # Compiled actor WITHOUT metadata (tampered/corrupt build, or
            # a hand-assembled dir passed to `po deploy --from`). Its
            # origin is unknown, so it is treated the same as the
            # symmetric case above: refuse unless --force. The compiler
            # always writes metadata, so this only fires for non-standard
            # builds — but the check must be symmetric to be a real
            # defense (adversarial review deploy.md F4).
            if not force:
                collisions.append(
                    f"{actor_dir.name}: the build has NO PhantomOrg metadata "
                    f"(.phantomorg.yaml) and the target already has this "
                    f"name — its origin is unknown; use --force if you "
                    f"really want to overwrite it"
                )
        elif incoming_org is not None and existing_org != incoming_org and not force:
            collisions.append(
                f"{actor_dir.name}: already exists in the target and belongs to "
                f"organization '{existing_org}', not '{incoming_org}'"
            )

    prune_list: list[str] = []
    if prune and compiled_org_id is not None:
        # Same criterion as before: same organization, no longer in the
        # build. Never another org, never an unmanaged persona, never a
        # symlink (archiving a symlink is refused by archive_persona; a
        # symlink entry is anomalous and is left alone).
        for existing_actor_dir in sorted(target.iterdir()):
            if not existing_actor_dir.is_dir() or existing_actor_dir.is_symlink():
                continue
            if existing_actor_dir.name in compiled_actor_ids:
                continue
            if _org_id_of(existing_actor_dir) == compiled_org_id:
                prune_list.append(existing_actor_dir.name)

    return collisions, prune_list


# ---------------------------------------------------------------------------
# Additive deploy: PhantomOrg owns FILES (and marker-delimited sections),
# never DIRECTORY trees. See CONTRIBUTING.md §1.3. The compiled output is a
# source of "what PhantomOrg wants"; deploy writes only the paths below,
# in place, atomically per file, preserving every runtime-owned file
# (identity.json, vault.sqlite, memory.sqlite, daily files, kb notes, ...).
# ---------------------------------------------------------------------------

# Relative paths PhantomOrg owns inside a persona directory, mapped to the
# strategy used to write each into a LIVE persona:
#   "merge"  -> block-merge (ORG:BEGIN/END) against the live file: the
#                compiled ORG blocks replace the live ones, everything
#                OUTSIDE the blocks is preserved byte-for-byte.
#   "plain"  -> fully derived file, plain atomic overwrite (archived first).
#   "seed"   -> write only if missing (accumulating runtime state; the
#                live content always outranks the compiled template).
_OWNED_FILES: dict[str, str] = {
    "IDENTITY.md": "merge",
    "SOUL.md": "merge",
    "tools.md": "merge",
    ".phantomorg.yaml": "plain",
    "kb/procedures/comunicacion-agentes.md": "plain",
    "MEMORY.md": "seed",
    "memory/people.md": "seed",
    "memory/decisions.md": "seed",
    "memory/lessons.md": "seed",
    "memory/commitments.md": "seed",
    "kb/Home.md": "seed",
    "kb/templates/concept.md": "seed",
    "kb/templates/runbook.md": "seed",
    "kb/templates/decision.md": "seed",
    "kb/templates/postmortem.md": "seed",
    # The allowlist is runtime state (TOFU / persistTrust write to it);
    # PhantomOrg seeds it once and never overwrites it.
    "phantomchat.json": "seed",
}

# Scaffold directories PhantomOrg creates (mkdir exist_ok, never deletes).
_OWNED_DIRS: tuple[str, ...] = (
    "memory/archive",
    "kb/inbox",
    "kb/concepts",
    "kb/runbooks",
    "kb/procedures",
    "kb/decisions",
    "kb/infra",
    "kb/people",
    "kb/projects",
    "kb/postmortems",
    "kb/templates",
)

PER_FILE_MARKER = ".pf-file-archive"


def _apply_owned_file(
    strategy: str, live_path: Path, compiled_path: Path
) -> str | None:
    """Compute the new content for ``live_path`` given the compiled file,
    or None when nothing should be written (seed on an existing file, or
    no change). Never writes; the caller archives and writes atomically."""
    compiled_content = compiled_path.read_text(encoding="utf-8")
    if strategy == "seed":
        if live_path.exists():
            return None
        return compiled_content
    if strategy == "plain":
        if (
            live_path.exists()
            and live_path.read_text(encoding="utf-8") == compiled_content
        ):
            return None
        return compiled_content
    if strategy == "merge":
        if not live_path.exists():
            return compiled_content
        # A compiled file with no ORG markers provides no owned sections to
        # merge (tampered/partial build); leave the live file untouched
        # rather than stripping its blocks.
        if not has_blocks(compiled_content):
            return None
        live = live_path.read_text(encoding="utf-8")
        merged = merge_content(live, compiled_content)
        return merged if merged != live else None
    raise DeployError(f"internal error: unknown owned-file strategy {strategy!r}")


def _archive_file(
    personas_dir: Path,
    name: str,
    rel: str,
    archive_dirs: dict[str, Path],
    created_flags: set[Path],
) -> Path | None:
    """Back up the current content of ``personas_dir/<name>/<rel>`` into
    ``personas-archive/<name>-<stamp>/<rel>`` before it is overwritten.

    Returns the backup file path, or None when there was nothing to back
    up. One archive directory is reused for all files of the same persona
    within a single deploy (so a persona's overwritten files are grouped
    under one restorable directory)."""
    src = personas_dir / name / rel
    if not src.exists() or src.is_symlink():
        return None
    if name not in archive_dirs:
        archive_root = archives_dir(personas_dir)
        _assert_real_directory(archive_root, "archive root")
        created_root = not archive_root.exists()
        archive_root.mkdir(parents=True, exist_ok=True)
        base = f"{name}-{_archive_stamp()}"
        dst = archive_root / base
        for suffix in range(1, 1000):
            if not dst.exists():
                break
            dst = archive_root / f"{base}-{suffix}"
        else:
            raise DeployError(
                f"unable to allocate a unique archive destination for "
                f"'{name}': 1000 same-millisecond archives already exist"
            )
        dst.mkdir(parents=True, exist_ok=False)
        (dst / PER_FILE_MARKER).write_text("", encoding="utf-8")
        archive_dirs[name] = dst
        if created_root:
            created_flags.add(archive_root)
    backup = archive_dirs[name] / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, backup)
    return backup


def _write_owned_file(live_path: Path, content: str) -> None:
    """Atomically write ``content`` to ``live_path`` (tmp + os.replace on
    the FILE, never on a directory)."""
    live_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = live_path.with_name(f".{live_path.name}.pf-tmp-{uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, live_path)


def deploy(
    compiled_dir: Path,
    target_dir: Path | None = None,
    force: bool = False,
    prune: bool = False,
    merge_data_files: bool = False,
) -> DeployResult:
    """Deploy compiled personas into ``target``.

    This is strictly ADDITIVE (CONTRIBUTING.md §1): only the files
    PhantomOrg owns are written, in place, atomically per file. The live
    persona directory is never moved, replaced or archived, and every
    runtime-owned file (identity.json, vault.sqlite, memory/, kb/ notes,
    daily files, ...) is left exactly as it was. There is no whole-directory
    replacement mode: a fresh persona is a runtime-owned lifecycle operation
    (stop/backup/restore), never a compiler deploy.
    """
    target = target_dir or default_personas_dir()
    # C1 (adversarial review, v0.5.5): the deploy tree must not be reached
    # through a symlink — the archive/restore machinery moves real
    # directories around, and writing through a planted link would redirect
    # the whole deployment to an arbitrary directory (or make a later
    # archive/restore act on the wrong tree). The tree itself may live on
    # another filesystem via a symlinked PARENT (e.g. ~ moved to another
    # disk); only the final component is refused, mirroring the existing
    # per-entry symlink policy (_assert_no_symlinks, dest.is_symlink).
    if target.is_symlink():
        raise DeployError(
            f"refusing to deploy: target {target} is a symlink (possibly "
            "pointing outside the personas tree). Remove it or point "
            "--target at the real directory."
        )
    target.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_staging(target)

    result = DeployResult(target=target)
    created_flags: set[Path] = set()

    compiled_actor_dirs = [d for d in sorted(compiled_dir.iterdir()) if d.is_dir()]
    compiled_actor_ids = {d.name for d in compiled_actor_dirs}
    has_scopes = (compiled_dir / SCOPES_FILENAME).exists()
    has_humans = (compiled_dir / HUMANS_FILENAME).exists()

    # The org_id of this build is the same for all its actors (they all
    # come from the same compile); reading it from the first one that has
    # metadata is enough, to know which target actors "belong to this
    # organization" for --prune purposes.
    compiled_org_id = None
    for d in compiled_actor_dirs:
        org_id = _org_id_of(d)
        if org_id is not None:
            compiled_org_id = org_id
            break

    # Preflight: every collision, symlink and prune decision is computed
    # from the CURRENT target BEFORE anything is written. Raising here
    # (below, when collisions is non-empty) guarantees a rejected deploy
    # never leaves a partial state.
    collisions, prune_list = _preflight(
        compiled_actor_dirs,
        target,
        force,
        prune,
        compiled_org_id,
        compiled_actor_ids,
    )
    if collisions and not force:
        raise DeployCollisionError(
            "Deployment stopped due to cross-organization collision(s):\n"
            + "\n".join(f"  - {c}" for c in collisions)
            + "\nUse --force if you really want to overwrite."
        )

    # ------------------------------------------------------------------
    # ADDITIVE deploy (the only path for `po deploy`): write the
    # files PhantomOrg owns into the LIVE persona directory, in place,
    # atomically per file. The persona directory itself is never moved,
    # replaced or archived, and every runtime-owned file (identity.json,
    # vault.sqlite, memory/, kb/ notes, daily files, ...) is left exactly
    # as it was. Files being overwritten are backed up per-file into
    # personas-archive/ so `po rollback` can restore them.
    # ------------------------------------------------------------------
    archive_dirs: dict[str, Path] = {}
    for actor_dir in compiled_actor_dirs:
        dest = target / actor_dir.name
        dest_existed = dest.exists()

        # Create the scaffold directories (mkdir exist_ok, never delete).
        for _d in _OWNED_DIRS:
            (dest / _d).mkdir(parents=True, exist_ok=True)

        touched = False
        for rel, strategy in _OWNED_FILES.items():
            compiled_path = actor_dir / rel
            if not compiled_path.exists():
                # The build did not produce this file (e.g. phantomchat.json
                # when the actor has no npub). Do NOT touch a live file of
                # that name: it is runtime state.
                continue
            live_path = dest / rel
            new_content = _apply_owned_file(strategy, live_path, compiled_path)
            if new_content is None:
                continue
            # Back up the live file before overwriting it (per-file).
            if live_path.exists():
                _archive_file(target, actor_dir.name, rel, archive_dirs, created_flags)
            _write_owned_file(live_path, new_content)
            touched = True

        if touched or not dest_existed:
            result.deployed.append(actor_dir.name)
        if not dest_existed:
            result.created.append(actor_dir.name)

    # Org-level derived artifact: transport scopes.json (if the build
    # produced one) to the DATA DIR (target.parent), not the target tree.
    staging = None
    if has_scopes or has_humans:
        staging = _staging_dir(target)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            result.scopes_written, result.scopes_backup, result.scopes_created = (
                _write_scopes_file(
                    compiled_dir, target, staging, merge=merge_data_files
                )
            )
            result.humans_written, result.humans_backup, result.humans_created = (
                _write_humans_file(
                    compiled_dir, target, staging, merge=merge_data_files
                )
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # Prune AFTER the deploy loop, using the preflight-computed list. A
    # pruned persona's PhantomOrg-OWNED content is archived per-file and
    # reverted; its accumulated mind (identity, vault, memory, KB notes,
    # seed files) is left untouched and the persona directory is NEVER
    # removed (a tool may never own a persona directory):
    #   - "plain"  -> the whole file is PhantomOrg-owned: archive + unlink.
    #   - "merge"  -> only the ORG:BEGIN/END blocks are owned: archive, then
    #                 write back the file with the blocks stripped (manual
    #                 content outside the markers survives in place).
    #   - "seed"   -> seed-only runtime files are never owned: leave them.
    for name in prune_list:
        persona_dir = target / name
        for rel, strategy in _OWNED_FILES.items():
            live_path = persona_dir / rel
            if not live_path.exists() or live_path.is_symlink():
                continue
            if strategy == "seed":
                continue
            backup = _archive_file(target, name, rel, archive_dirs, created_flags)
            if backup is None:
                continue
            if strategy == "merge":
                # Remove ONLY the owned blocks; keep everything outside them.
                content = live_path.read_text(encoding="utf-8")
                stripped = strip_blocks(content)
                if stripped == content:
                    continue  # no owned blocks to remove
                _write_owned_file(live_path, stripped)
            else:
                # "plain": fully derived, fully owned — remove the file.
                live_path.unlink()
        result.pruned.append(name)

    for name, archive_dir in archive_dirs.items():
        result.archived.append((name, str(archive_dir)))
        result.file_archives.add(str(archive_dir.resolve()))

    if created_flags:
        result.created_archive_dirs.update(created_flags)

    return result
