"""
Deploy session manifest and transactional rollback (`po rollback`).

Every deploy (or deploy-all) appends a *session* record to a manifest
kept inside the archive root:

    personas-archive/.phantomorg-manifest.json

The session records exactly what the deploy changed:

- ``target``: the personas directory it wrote to;
- ``created``: personas that did NOT exist before (fresh creates);
- ``archived``: (name, archive_dir) pairs — personas that existed
  before and were moved to ``personas-archive/`` before being
  overwritten or pruned;
- whether ``personas-archive/`` and the target itself existed before
  the deploy.

``po rollback`` then undoes the LAST session and returns the system to
exactly the state it was in before that deploy:

- archived personas are moved back (the backup is consumed, not left
  behind);
- personas the deploy created are removed;
- if ``personas-archive/`` did not exist before that deploy, it is
  deleted;
- if the target itself did not exist before and is now empty, it is
  deleted too.

Rollback is stack-based: run it once per session you want to undo. The
manifest is what makes the undo transactional — without it, "restore
the backup" would leave the created personas and the archive directory
behind, and the system would not be as it was before.

The manifest is a dotfile, so phantombot's archive listing (which only
reads directories) ignores it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

fcntl: ModuleType | None
try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .norms import NORMS_STATE_FILENAME
from .target import (
    PER_FILE_MARKER,
    DeployError,
    DeployResult,
    _archive_stamp,
    _assert_real_directory,
)

MANIFEST_NAME = ".phantomorg-manifest.json"
LOCK_NAME = ".phantomorg-manifest.lock"
# Transaction lock: serializes whole deploy/rollback transactions against
# each other (the manifest lock alone only protects load/save of the
# manifest, not the target/archive filesystem mutations). It lives NEXT TO
# the archive root, in the runtime dir, because the archive root may not
# exist yet on the first deploy. A dotfile, so phantombot ignores it; never
# deleted (same rationale as the manifest lockfile).
TRANSACTION_LOCK_NAME = ".phantomorg.lock"
# Trash dir used by execute_rollback to keep (not delete) whatever the
# rollback discards, until the rollback has fully succeeded. A dotfile
# directory so phantombot's archive listing ignores it.
TRASH_PREFIX = "._pf_trash_"
# Backup files for data-dir derived artifacts (scopes.json / HUMANS.md),
# snapshotted into the archive root at deploy time so `po rollback` can
# restore them. Named ._pf_data_<filename>-<stamp>; plain FILES (never
# directories), so the archive scan (which only touches directories)
# ignores them entirely. They are disposable in-house artifacts: they
# only matter while their owning session exists, and are swept when that
# session is rolled back or the root is removed.
DATA_BACKUP_PREFIX = "._pf_data_"

# Archive dirs are named <name>-<stamp> (or <name>-<stamp>-<N> for
# same-millisecond collisions), where <stamp> is phantombot's archive
# format: 2026-08-09T16-23-53-854Z. The regex extracts both parts so the
# in_progress reconcile can scan the WHOLE archive root (not just
# planned names) and classify every archive this session created.
_ARCHIVE_NAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)(?:-(?P<suffix>\d+))?$"
)

# Stamp embedded in a data-file backup name (._pf_data_<filename>-<stamp>
# or ...-<stamp>-<N>). The filename part never contains the stamp, so we
# just find every timestamp-shaped token in the tail to compare against
# the session base id.
_DATA_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z")

# Internal dotfiles that live inside the archive root but are not
# phantombot archives; the "is the archive root empty?" check must
# ignore them.
_INTERNAL_NAMES = {MANIFEST_NAME, LOCK_NAME, NORMS_STATE_FILENAME}


def _acquire_lock_file(lock_path: Path):
    """Open (creating if needed) and exclusively lock ``lock_path``.

    Returns the open file object; the caller holds the lock until it is
    closed via ``_release_lock_file``. On POSIX this is an advisory
    flock. On Windows (no fcntl) it falls back to ``msvcrt.locking``
    over a one-byte range with a retry loop — a REAL lock instead of
    the historical no-op (two concurrent deploys on Windows used to
    clobber each other's manifest).

    The lockfile is opened in append mode (never truncated): truncating
    a file another process holds a region lock on would be racy. The
    lockfile is deliberately never deleted while the root exists; it
    goes away with the archive root.
    """
    f = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - handle must stay open for the lock's lifetime
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f
    # pragma: no cover - Windows only
    import msvcrt

    # msvcrt.locking locks a byte RANGE, so the file must already
    # contain at least one byte.
    if f.seek(0, os.SEEK_END) == 0:
        f.write("\0")
        f.flush()
    f.seek(0)
    while True:
        try:
            # LK_LOCK retries only a fixed number of times (10x1s) and
            # then raises; loop manually so a long-held lock never
            # spuriously fails.
            # msvcrt is Windows-only; mypy on Linux cannot see its attrs.
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            return f
        except OSError:
            f.seek(0)
            time.sleep(0.05)


def _release_lock_file(f) -> None:
    """Release a lock acquired by ``_acquire_lock_file`` and close it.
    Best-effort: release failures must never mask the operation that
    held the lock."""
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows only
            import msvcrt

            f.seek(0)
            # msvcrt is Windows-only; mypy on Linux cannot see its attrs.
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    finally:
        f.close()


@contextmanager
def _manifest_lock(archive_root: Path):
    """Serialize load-modify-save cycles on the manifest across
    processes (two concurrent deploys must not lose each other's
    session record). Uses an advisory flock on a sibling lockfile
    (``_acquire_lock_file``; a real msvcrt lock on Windows).

    The lockfile itself is a dotfile inside the archive root; it is
    deliberately never deleted while the root exists (deleting a
    lockfile that another process may hold is racy), and the empty-root
    checks ignore it. When the whole archive root is removed at the end
    of a rollback, the lockfile goes with it.
    """
    # Audit v0.5.7 #2: the archive root must be a real directory, never
    # a symlink (a planted link would redirect all backups and manifest
    # writes outside the tree). Checked here because every
    # load-modify-save cycle funnels through this lock.
    _assert_real_directory(archive_root, "archive root")
    archive_root.mkdir(parents=True, exist_ok=True)
    lock_path = archive_root / LOCK_NAME
    f = _acquire_lock_file(lock_path)
    try:
        yield
    finally:
        _release_lock_file(f)


# Reentrancy registry for ``_transaction_lock``: lock_path -> nesting
# depth held by THIS THREAD. Lets library functions acquire the
# transaction lock internally without deadlocking against the CLI's
# outer acquisition (flock treats separate fds as independent locks).
# Thread-local on purpose: two threads must still serialize against
# each other via the real flock, not share the reentrancy registry.
_LOCK_REGISTRY = threading.local()


def _lock_depth() -> dict[Path, int]:
    reg = getattr(_LOCK_REGISTRY, "paths", None)
    if reg is None:
        reg = {}
        _LOCK_REGISTRY.paths = reg
    return reg


@contextmanager
def _transaction_lock(target: Path):
    """Serialize whole deploy/deploy-all/rollback transactions against
    each other, covering the target/archive filesystem mutations — not
    just the manifest load/save cycle. Lock file lives in
    ``target.parent`` (the runtime dir), a dotfile phantombot ignores.

    Lock ORDER is always: transaction lock (outer) → manifest lock
    (inner). Never invert it: that would deadlock.

    Reentrant per thread: ``execute_rollback``/``plan_rollback`` acquire
    this lock internally (F12, library-level protection), and the CLI
    wraps them in the same lock — a nested acquisition on the same
    lockfile must not deadlock (flock treats two fds of the same file
    as independent). The thread-local registry below tracks locks held
    by the current thread; a nested acquisition is a depth increment,
    not a new flock. Two different threads still serialize via the real
    flock.

    On platforms without fcntl the lock is a real msvcrt lock (see
    ``_acquire_lock_file``) instead of a no-op.
    """
    lock_dir = target.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / TRANSACTION_LOCK_NAME
    reg = _lock_depth()
    if lock_path in reg:
        reg[lock_path] += 1
        try:
            yield
        finally:
            reg[lock_path] -= 1
            if reg[lock_path] <= 0:
                del reg[lock_path]
        return
    f = _acquire_lock_file(lock_path)
    reg[lock_path] = 1
    try:
        yield
    finally:
        _release_lock_file(f)
        del reg[lock_path]


def manifest_path(archive_root: Path) -> Path:
    """Path of the session manifest inside the archive root."""
    return archive_root / MANIFEST_NAME


class ManifestError(Exception):
    """Raised when the session manifest exists but is unreadable or
    structurally corrupt.

    Callers must NEVER treat a corrupt manifest as "no sessions":
    overwriting it (a deploy appends and re-saves) would silently destroy
    the rollback history of every earlier deploy. The file is preserved
    (quarantined) and the operation is refused.
    """


def _empty_after_internals(archive_root: Path) -> bool:
    """True when the archive root holds nothing that must survive: no
    phantombot archives, no trash, no leftover temp files, and no OTHER
    recorded sessions (the manifest may exist but only if it is empty).
    Used before removing the root at the end of a rollback."""
    for p in archive_root.iterdir():
        if p.name in _INTERNAL_NAMES:
            continue
        if p.name.startswith(TRASH_PREFIX):
            continue
        if p.name.endswith(".tmp") or p.name.startswith(DATA_BACKUP_PREFIX):
            # Leftover from a crashed _save_sessions (*.tmp) or a
            # deploy-time data-file backup (._pf_data_*): never a persona,
            # never a reason to keep the root alive. Data backups only
            # matter while their owning session exists, and load_sessions
            # below is the guard against removing a live session's backup.
            continue
        return False
    # Only internal dotfiles (or nothing) left — but a manifest that
    # still lists sessions must keep the root alive. A CORRUPT manifest
    # also keeps the root alive: we cannot know whether it records
    # sessions, and deleting the root would destroy them.
    try:
        return len(load_sessions(archive_root)) == 0
    except ManifestError:
        return False


def remove_abandoned_archive_root(archive_root: Path) -> bool:
    """Best-effort removal of an empty, unneeded archive root.

    A rollback whose session was already dropped can crash in its final
    best-effort root removal, leaving an empty ``personas-archive/``
    behind. The CLI calls this when there is nothing left to roll back,
    so the system returns to exactly its pre-deploy state. Only a root
    without sessions (for ANY target), without archives and without
    trash content is removed; anything else is left untouched. Returns
    True when the root was removed.
    """
    if not archive_root.is_dir():
        return False
    if not _empty_after_internals(archive_root):
        return False
    try:
        for p in archive_root.iterdir():
            if (
                p.is_dir()
                and not p.is_symlink()
                and p.name.startswith(TRASH_PREFIX)
                and any(p.iterdir())
            ):
                # Trash holding content may be the only recovery evidence
                # of an interrupted rollback: keep the root.
                return False
        shutil.rmtree(archive_root)
        return True
    except OSError:
        return False


def load_sessions(archive_root: Path) -> list[dict]:
    """All recorded sessions, oldest first. Returns [] only when there is
    genuinely no manifest (no archive root, or no manifest file).

    Raises ManifestError when the manifest EXISTS but cannot be read or
    parsed. The old behavior (return [] and let the next deploy overwrite
    the corrupt file) silently destroyed the rollback history; refusing
    is the only safe response."""
    if not archive_root.is_dir():
        return []
    mp = manifest_path(archive_root)
    if not mp.is_file():
        return []
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ManifestError(
            f"session manifest is unreadable or corrupt: {mp} ({e})"
        ) from e
    if not isinstance(data, dict):
        raise ManifestError(f"session manifest is corrupt (not an object): {mp}")
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        raise ManifestError(
            f"session manifest is corrupt ('sessions' is not a list): {mp}"
        )
    return sessions


def _quarantine_corrupt_manifest(archive_root: Path) -> str:
    """Move a corrupt manifest aside so it is preserved as evidence and
    can never be overwritten by a blind re-save. Returns the preserved
    path. Best-effort: if the move fails the manifest is left in place."""
    mp = manifest_path(archive_root)
    if not mp.is_file():
        return str(mp)
    stamp = _archive_stamp()
    dest = mp.with_name(f"{MANIFEST_NAME}.corrupt-{stamp}")
    n = 1
    while dest.exists():
        dest = mp.with_name(f"{MANIFEST_NAME}.corrupt-{stamp}-{n}")
        n += 1
        if n > 1000:
            raise ManifestError(
                f"cannot quarantine corrupt manifest {mp}: 1000 candidate "
                "names already exist"
            )
    try:
        os.replace(mp, dest)
    except OSError as e:
        raise ManifestError(f"cannot quarantine corrupt manifest {mp}: {e}") from e
    return str(dest)


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a rename/unlink is durable
    (F13). No-op on platforms where directories cannot be opened."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_STALE_INTERNALS_MAX_AGE = timedelta(days=1)


def _cleanup_stale_internals(archive_root: Path, *, include_trash: bool = True) -> None:
    """Remove leftover temp manifest files (*.tmp) and rollback trash
    dirs (._pf_trash_*) older than a day (F10).

    Two very different kinds of leftovers share this routine:

    - ``*.tmp`` files are a crashed ``_save_sessions`` residue: never a
      persona, never evidence of anything, always safe to collect.

    - ``._pf_trash_*`` dirs hold data a rollback discarded but promised
      to keep until the transaction fully completes. They are ONLY
      garbage-collected when the manifest is absent or holds NO
      sessions (audit v0.5.8 #1: a stale trash dir is the sole recovery
      evidence of an interrupted rollback — even a ``committed`` journal
      entry can be that interruption, when the rollback crashed before
      the rollback_in_progress transition was introduced (pre-v0.5.8)
      or before the transition could be written; see plan_rollback's
      missing-archives logic). So ANY recorded session — committed,
      in_progress or rollback_in_progress — keeps every trash dir
      untouched, even past the age threshold, and so does a corrupt
      manifest (we cannot know whether a session exists).

      ``include_trash=False`` skips the trash check entirely: used by
      plan_rollback, which must never destroy recovery evidence while
      merely planning (the CLI plans twice before executing, and the
      user may cancel between the two).

    ``*.tmp`` files are always collectible regardless: they are a
    crashed ``_save_sessions`` residue, never a persona, never evidence
    of anything.
    """
    if not archive_root.is_dir():
        return
    try:
        sessions = load_sessions(archive_root)
    except ManifestError:
        # Corrupt manifest: cannot know whether a session exists —
        # refuse to collect trash (conservative).
        sessions = None
    # Trash is only collected when the manifest is genuinely empty
    # (absent archive root / no manifest file → load_sessions returns
    # []). None (corrupt) and any non-empty session list both protect
    # every trash dir: committed included, see docstring.
    trash_guard = sessions is None or bool(sessions)
    now = datetime.now(timezone.utc)
    for p in archive_root.iterdir():
        if p.name.endswith(".tmp") or (
            include_trash and p.name.startswith(TRASH_PREFIX) and not trash_guard
        ):
            _stale_remove(p, now)


def _stale_remove(p: Path, now: datetime) -> None:
    """Remove ``p`` when its mtime is older than the staleness cutoff.

    ``p`` is a temp manifest or (only when no session is in_progress) a
    stale rollback trash dir."""
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return
    if now - mtime > _STALE_INTERNALS_MAX_AGE:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _save_sessions(archive_root: Path, sessions: list[dict]) -> None:
    """Persist the session list. An empty list removes the manifest file
    but leaves the archive root itself alone (it may pre-exist with
    phantombot archives). The write is atomic AND durable: the manifest
    is written to a per-process temp file (two writers can never share a
    temp name), fsync'd, then renamed over — a crash mid-write can never
    leave a truncated manifest, and the rename survives a power loss
    (best-effort directory fsync)."""
    if sessions:
        archive_root.mkdir(parents=True, exist_ok=True)
        mp = manifest_path(archive_root)
        payload = (
            json.dumps({"sessions": sessions}, indent=2, ensure_ascii=False) + "\n"
        )
        # Per-process temp name (F7): with a fixed name, two writers
        # could interleave on the same temp file and corrupt each
        # other's write (the advisory lock is not enforced by a
        # bypassing writer). The pid guarantees distinct names.
        tmp = mp.with_name(f"{mp.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, mp)
        _fsync_dir(archive_root)
    else:
        mp = manifest_path(archive_root)
        if mp.is_file():
            mp.unlink()
            _fsync_dir(archive_root)


def _mark_session_state(archive_root: Path, session_id: str, new_state: str) -> None:
    """Transition one session's ``state`` field in the manifest (under
    the manifest lock).

    Used by rollback to mark the journal entry ``rollback_in_progress``
    BEFORE touching the filesystem, so a crash mid-rollback leaves a
    session whose trash is protected from GC (audit v0.5.7, finding #1:
    a committed session whose rollback crashed would otherwise lose its
    trash to ``_cleanup_stale_internals`` after 24h, destroying the only
    evidence that an interrupted rollback consumed the archives —
    turning the retry into a permanent refusal).

    Idempotent: if the session is no longer in the manifest (already
    dropped by a completed rollback), nothing is done.
    """
    with _manifest_lock(archive_root):
        try:
            sessions = load_sessions(archive_root)
        except ManifestError:
            # Corrupt manifest: cannot safely rewrite it here; the
            # rollback will fail loudly when it next reads it.
            return
        for i, s in enumerate(sessions):
            if s.get("id") == session_id:
                merged = dict(s)
                merged["state"] = new_state
                sessions[i] = merged
                _save_sessions(archive_root, sessions)
                return


def record_session(
    target: Path,
    result: DeployResult,
    *,
    command: str,
    orgs: list[str],
    org_yamls: list[tuple[Path, str]] | None = None,
    archive_root_pre_existed: bool,
    target_pre_existed: bool,
) -> dict:
    """Append a session record describing one deploy invocation.

    ``org_yamls`` is an optional list of (org.yaml path, sha256) so
    ``po rollback`` can warn when the spec has drifted since the deploy.

    The load-modify-save cycle runs under an advisory lock so two
    concurrent deploys cannot lose each other's session record.
    """
    archive_root = target.parent / "personas-archive"
    session_id = _archive_stamp()
    with _manifest_lock(archive_root):
        try:
            existing = load_sessions(archive_root)
        except ManifestError as e:
            preserved = _quarantine_corrupt_manifest(archive_root)
            raise DeployError(
                f"refusing to record the deploy session: the session "
                f"manifest is corrupt and would be overwritten. Preserved "
                f"at {preserved}. Resolve it (or delete it if you accept "
                f"losing the rollback history) before deploying again. "
                f"({e})"
            ) from e
        if any(s.get("id") == session_id for s in existing):
            # Two deploys within the same millisecond: give the id a
            # numeric suffix so sessions never share an id (a shared id
            # would make a rollback remove BOTH sessions from the
            # manifest).
            base = session_id
            for n in range(1, 1000):
                session_id = f"{base}-{n}"
                if not any(s.get("id") == session_id for s in existing):
                    break
            else:
                # All 1000 candidate ids are taken (physically requires
                # 1000 deploys in one millisecond): refuse rather than
                # reuse an id and let a rollback drop two sessions.
                raise RuntimeError(
                    "unable to allocate a unique session id: 1000 sessions "
                    "in the same millisecond"
                )
        session = {
            "id": session_id,
            "command": command,
            "target": str(target.resolve()),
            "archive_root_pre_existed": archive_root_pre_existed,
            "target_pre_existed": target_pre_existed,
            "orgs": sorted(set(orgs)),
            "org_yamls": [
                {"path": str(p.resolve()), "sha256": digest}
                for p, digest in (org_yamls or [])
            ],
            "deployed": sorted(result.deployed),
            "created": sorted(result.created),
            "pruned": sorted(result.pruned),
            "archived": [
                # Resolve archive paths at record time: a deploy invoked
                # with a relative --target would otherwise record
                # relative paths that break when pf rollback runs from
                # another directory.
                {"name": name, "dir": str(Path(path).resolve())}
                for name, path in result.archived
            ],
            "file_archives": sorted(
                str(Path(p).resolve()) for p in result.file_archives
            ),
            "scopes_written": result.scopes_written,
            "humans_written": result.humans_written,
            "scopes_backup": result.scopes_backup,
            "scopes_created": result.scopes_created,
            "humans_backup": result.humans_backup,
            "humans_created": result.humans_created,
        }
        sessions = existing
        sessions.append(session)
        _save_sessions(archive_root, sessions)
    return session


def _is_safe_name(name: object) -> bool:
    """True when ``name`` is a single safe directory component: a
    non-empty string equal to its own basename, not ``.``/``..``, with no
    separators and no absolute path. Used to confine manifest entries so
    a corrupt or tampered manifest cannot make rollback touch arbitrary
    filesystem paths."""
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    return name == Path(name).name


def _validate_data_backup(
    backup_raw: object,
    archive_root_resolved: Path,
    label: str,
) -> Path:
    """Validate a recorded data-file backup path with the same safety
    rules as persona archives: it must be an absolute path to a regular
    file (not a symlink) that lives inside the archive root. Returns the
    resolved path, or raises RollbackError on any violation (nothing
    changed)."""
    if not isinstance(backup_raw, str) or not backup_raw:
        raise RollbackError(
            f"refusing to roll back: corrupt manifest ({label} backup is "
            "not a path). Nothing was changed."
        )
    backup = Path(backup_raw)
    if not backup.is_absolute():
        raise RollbackError(
            f"refusing to roll back: {label} backup path is not absolute "
            f"({backup_raw}). Nothing was changed."
        )
    if backup.is_symlink():
        raise RollbackError(
            f"refusing to roll back: {label} backup is a symlink "
            f"({backup}). Nothing was changed."
        )
    if backup.resolve().parent != archive_root_resolved:
        raise RollbackError(
            f"refusing to roll back: {label} backup escapes the archive "
            f"root ({backup_raw}). Nothing was changed."
        )
    return backup


def begin_session(
    target: Path,
    *,
    command: str,
    orgs: list[str],
    planned_archived: list[str],
    planned_created: list[str],
    planned_pruned: list[str],
    archive_root_pre_existed: bool,
    target_pre_existed: bool,
    org_yamls: list[tuple[Path, str]] | None = None,
) -> dict:
    """Write a durable journal entry BEFORE the target is mutated.

    The session is recorded with ``state: "in_progress"`` and the list of
    personas the deploy plans to archive / create / prune. If the process
    crashes or the deploy fails mid-way, this entry is the trace that the
    target/archive were modified, so ``po rollback`` can reconcile the
    interrupted attempt instead of pretending it never happened.

    ``commit_session`` must be called after a successful deploy to
    transition the entry to ``committed`` with the real results.
    """
    archive_root = target.parent / "personas-archive"
    session_id = _archive_stamp()
    with _manifest_lock(archive_root):
        try:
            existing = load_sessions(archive_root)
        except ManifestError as e:
            preserved = _quarantine_corrupt_manifest(archive_root)
            raise DeployError(
                f"refusing to deploy: the session manifest is corrupt and "
                f"would be overwritten. Preserved at {preserved}. Resolve "
                f"it (or delete it if you accept losing the rollback "
                f"history) before deploying again. ({e})"
            ) from e
        if any(s.get("id") == session_id for s in existing):
            base = session_id
            for n in range(1, 1000):
                session_id = f"{base}-{n}"
                if not any(s.get("id") == session_id for s in existing):
                    break
            else:
                raise RuntimeError(
                    "unable to allocate a unique session id: 1000 sessions "
                    "in the same millisecond"
                )
        session = {
            "id": session_id,
            "state": "in_progress",
            "command": command,
            "target": str(target.resolve()),
            "archive_root_pre_existed": archive_root_pre_existed,
            "target_pre_existed": target_pre_existed,
            "orgs": sorted(set(orgs)),
            "org_yamls": [
                {"path": str(p.resolve()), "sha256": digest}
                for p, digest in (org_yamls or [])
            ],
            "planned_archived": sorted(set(planned_archived)),
            "planned_created": sorted(set(planned_created)),
            "planned_pruned": sorted(set(planned_pruned)),
        }
        _save_sessions(archive_root, existing + [session])
    return session


def commit_session(target: Path, session_id: str, result: DeployResult) -> dict:
    """Transition an ``in_progress`` journal entry to ``committed`` with
    the deploy's real results (archive dirs, created/deployed/pruned
    names). If the entry no longer exists in the manifest (e.g. the
    manifest was removed by hand), it is re-registered as committed so a
    successful deploy never loses its rollback record.
    """
    archive_root = target.parent / "personas-archive"
    committed = {
        "state": "committed",
        "deployed": sorted(result.deployed),
        "created": sorted(result.created),
        "pruned": sorted(result.pruned),
        "archived": [
            {"name": name, "dir": str(Path(path).resolve())}
            for name, path in result.archived
        ],
        "file_archives": sorted(str(Path(p).resolve()) for p in result.file_archives),
        "scopes_written": result.scopes_written,
        "humans_written": result.humans_written,
        "scopes_backup": result.scopes_backup,
        "scopes_created": result.scopes_created,
        "humans_backup": result.humans_backup,
        "humans_created": result.humans_created,
    }
    with _manifest_lock(archive_root):
        try:
            sessions = load_sessions(archive_root)
        except ManifestError as e:
            preserved = _quarantine_corrupt_manifest(archive_root)
            raise DeployError(
                f"refusing to commit the deploy session: the session "
                f"manifest is corrupt and would be overwritten. Preserved "
                f"at {preserved}. Resolve it before deploying again. ({e})"
            ) from e
        for i, s in enumerate(sessions):
            if s.get("id") == session_id:
                merged = dict(s)
                merged.update(committed)
                sessions[i] = merged
                _save_sessions(archive_root, sessions)
                return merged
        # Entry lost (manifest removed by hand): re-register the whole
        # session as committed, keeping the journal contract. The
        # pre-existed flags are unknowable now — infer them from the
        # filesystem (conservative: an existing archive root/target is
        # treated as pre-existing, so a rollback never deletes content
        # that was there before this deploy).
        session = {
            "id": session_id,
            "command": "deploy",
            "target": str(target.resolve()),
            "archive_root_pre_existed": archive_root.is_dir(),
            "target_pre_existed": target.is_dir(),
            "orgs": [],
            "org_yamls": [],
        }
        session.update(committed)
        _save_sessions(archive_root, sessions + [session])
        return session


def discard_session(
    target: Path, session_id: str, archive_root_pre_existed: bool | None = None
) -> None:
    """Remove a journal entry without executing any filesystem work.

    Used when a deploy recorded its in_progress journal entry but in the
    end mutated nothing (e.g. every organization of a deploy-all was
    skipped, or a collision was rejected in preflight): the entry has
    nothing to reconcile, so it is simply dropped.

    When ``archive_root_pre_existed`` is False (the deploy created
    ``personas-archive/`` for the first time) and the archive root now
    holds nothing but the manifest lock, the empty directory is removed
    as well — a rejected deploy must leave the filesystem exactly as it
    found it.
    """
    archive_root = target.parent / "personas-archive"
    with _manifest_lock(archive_root):
        try:
            sessions = load_sessions(archive_root)
        except ManifestError as e:
            preserved = _quarantine_corrupt_manifest(archive_root)
            raise DeployError(
                f"refusing to drop the deploy session: the session manifest "
                f"is corrupt and would be overwritten. Preserved at "
                f"{preserved}. Resolve it before deploying again. ({e})"
            ) from e
        sessions = [s for s in sessions if s.get("id") != session_id]
        _save_sessions(archive_root, sessions)
        if archive_root_pre_existed is False:
            # Remove the manifest file if the last session was dropped,
            # then the root dir if nothing else remains (manifest lock is
            # expected to stay — it is only removed when the root itself
            # goes away). F10: do this INSIDE the lock — a concurrent
            # library-level record_session between save and unlink would
            # otherwise be destroyed.
            try:
                manifest_path = archive_root / MANIFEST_NAME
                if manifest_path.exists():
                    manifest_path.unlink()
                remaining = [
                    p
                    for p in archive_root.iterdir()
                    if p.name != LOCK_NAME and not p.name.endswith(".tmp")
                ]
                if not remaining:
                    (archive_root / LOCK_NAME).unlink(missing_ok=True)
                    archive_root.rmdir()
            except OSError:
                # Cleanup is best-effort: a leftover empty dir does not
                # affect correctness (no session, no personas mutated).
                pass


def sessions_for_target(archive_root: Path, target: Path) -> list[dict]:
    """Sessions recorded for a specific target directory."""
    resolved = str(target.resolve())
    out: list[dict] = []
    for s in load_sessions(archive_root):
        if not isinstance(s, dict):
            # F6: a corrupt manifest must never crash with an
            # AttributeError later (``s.get`` on a string) — refuse
            # loudly instead.
            raise ManifestError(
                "session manifest is corrupt (a session entry is not an object)"
            )
        if s.get("target") == resolved:
            out.append(s)
    return out


def latest_session_for(archive_root: Path, target: Path) -> dict | None:
    """The most recent session for the target, or None."""
    mine = sessions_for_target(archive_root, target)
    return mine[-1] if mine else None


class RollbackError(Exception):
    """Raised when a rollback cannot be planned or executed safely."""


@dataclass
class RollbackPlan:
    """Validated plan to undo one deploy session. Nothing is executed
    until ``execute_rollback`` is called."""

    session: dict
    restore: list[tuple[str, Path]] = field(default_factory=list)
    """(persona_name, archive_dir) to move back into the target."""
    remove_created: list[str] = field(default_factory=list)
    """Persona names the deploy created; their target dirs are removed."""
    discard: list[str] = field(default_factory=list)
    """Persona names whose CURRENT target version will be replaced by the
    archived one. These versions are NOT deleted outright: they are moved
    to a trash dir and only removed once the rollback fully succeeds."""
    discard_archives: list[Path] = field(default_factory=list)
    """Archive dirs this session created that must NOT be restored.

    Only populated for in_progress (interrupted) sessions: archives whose
    persona name is in the session's ``planned_created`` were created
    IN this session (e.g. org B archived a persona that org A had just
    created in the same deploy-all). Restoring them would resurrect an
    in-session artifact; they are instead moved to the trash and removed
    once the rollback succeeds."""
    unexpected: list[tuple[str, Path]] = field(default_factory=list)
    """Archive dirs found in the archive root that this session did NOT
    create (in_progress reconcile only): valid <name>-<stamp> names with
    stamp >= the session id, but the name appears in NO planned list.

    The deploy never archives names outside planned_archived/
    planned_pruned, so such dirs are foreign (phantombot import-persona,
    manual restore, an older PhantomOrg version...). They are left
    EXACTLY as found — never restored, never discarded — and the user is
    warned. """
    spec_drift: list[str] = field(default_factory=list)
    """Paths of org.yaml files that changed since the deploy was recorded."""
    restore_data: list[tuple[str, str]] = field(default_factory=list)
    """(dest_abs, backup_abs) data-dir files to restore to their exact
    pre-deploy state: the pre-overwrite backup (snapshotted at deploy
    into personas-archive/) is copied back over the current version.

    Only populated for committed sessions: the backup path is recorded
    in the manifest at commit time. An interrupted (in_progress) deploy
    has no recorded backup path, so its data files are left untouched
    (see ``data_skipped``)."""
    remove_data: list[str] = field(default_factory=list)
    """Absolute data-dir file paths the deploy created (they did not
    exist pre-deploy). On rollback they are removed to return to the
    exact pre-deploy state (absent)."""
    data_skipped: bool = False
    """True when this session's data files (scopes.json / HUMANS.md)
    were NOT restored because their pre-deploy state is unknown (an
    interrupted in_progress deploy, or an old session recorded before
    data-file backup support). The user is warned that the data dir may
    not be byte-for-byte restored."""
    cleanup_only: bool = False
    """True when every archive was already consumed by a previous (partial)
    rollback: nothing to restore, only created-persona removal, trash
    cleanup and directory cleanup remain."""

    @property
    def session_id(self) -> str:
        return str(self.session.get("id", "?"))


@dataclass
class RollbackResult:
    """What a rollback actually did."""

    session_id: str
    restored: list[str] = field(default_factory=list)
    """Persona names moved back from the archive."""
    discarded: list[str] = field(default_factory=list)
    """Persona names whose post-deploy version was removed first."""
    discarded_archives: list[str] = field(default_factory=list)
    """Archive dirs that were discarded instead of restored (in-session
    artifacts of an interrupted deploy)."""
    removed_created: list[str] = field(default_factory=list)
    """Persona names the deploy created, now removed."""
    restored_data: list[str] = field(default_factory=list)
    """Data-dir file paths (scopes.json / HUMANS.md) restored to their
    pre-overwrite bytes from the deploy-time backup."""
    removed_data: list[str] = field(default_factory=list)
    """Data-dir file paths the deploy created, now removed (they did not
    pre-exist)."""
    data_skipped: bool = False
    """True when the session's data files were not restored (unknown
    pre-deploy state: interrupted deploy or pre-backup session)."""
    archive_root_deleted: bool = False
    """True if personas-archive/ was removed (it did not pre-exist)."""
    target_deleted: bool = False
    """True if the target dir was removed (did not pre-exist, now empty)."""


def plan_rollback(archive_root: Path, target: Path) -> RollbackPlan:
    """Validate the latest session for the target and build the plan.

    Raises RollbackError when there is nothing to roll back or when an
    archived persona directory is missing (the rollback would be
    incomplete — better to refuse than to half-restore).

    If the session's archives were ALL already consumed (a previous
    rollback restored them but did not finish its cleanup — e.g. the
    trash could not be removed), the plan becomes a *cleanup-only* plan:
    nothing left to restore, only the remaining created personas, the
    trash, and the directory cleanup to finish.
    """
    try:
        # F10: purge stale TEMP MANIFESTS left by crashed processes
        # before reading the manifest, so they can never be mistaken for
        # live sessions or distort the plan. Trash dirs are NOT touched
        # here (include_trash=False): they are recovery evidence (audit
        # v0.5.8 #1) — a previous interrupted rollback may have left its
        # journal entry committed with a trash dir as the only proof
        # that the archives were consumed. The CLI plans twice before
        # executing (and the user may cancel in between); destroying
        # that evidence while merely planning would turn the retry into
        # a permanent refusal.
        _cleanup_stale_internals(archive_root, include_trash=False)
        session = latest_session_for(archive_root, target)
    except ManifestError as e:
        raise RollbackError(
            f"cannot roll back: the session manifest is unreadable or "
            f"corrupt ({e}). The archived personas in {archive_root} are "
            "still there and can be restored manually (move them back "
            "into the target), but the rollback history is unavailable."
        ) from e
    if session is None:
        raise RollbackError("nothing to roll back — no deploy session recorded")

    # F6: validate the manifest's list fields BEFORE consuming them. A
    # corrupt/tampered manifest must never make rollback iterate a
    # non-list (``created: "abc"`` would iterate to characters,
    # ``org_yamls: null`` would crash with a TypeError mid-plan). Refuse
    # loudly; nothing has been changed yet.
    for field_name in (
        "planned_archived",
        "planned_created",
        "planned_pruned",
        "created",
        "deployed",
        "pruned",
    ):
        value = session.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise RollbackError(
                "refusing to roll back: corrupt manifest "
                f"({field_name} is not a list of names). Nothing was changed."
            )
    for field_name in ("archived", "org_yamls"):
        value = session.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            raise RollbackError(
                "refusing to roll back: corrupt manifest "
                f"({field_name} is not a list of objects). Nothing was changed."
            )

    # Confine every manifest-supplied path (a corrupt or tampered
    # manifest must never make rollback touch arbitrary filesystem
    # content). The recorded target must be the canonical one we were
    # invoked with.
    recorded_target = session.get("target")
    if (
        not isinstance(recorded_target, str)
        or not recorded_target
        or Path(recorded_target).resolve() != target.resolve()
    ):
        raise RollbackError(
            "refusing to roll back: the session's recorded target does not "
            "match the requested target. Nothing was changed."
        )
    archive_root_resolved = archive_root.resolve()

    plan = RollbackPlan(session=session)
    missing: list[tuple[str, str]] = []
    present: list[tuple[str, Path]] = []

    if session.get("state") == "in_progress":
        # An interrupted deploy: no committed results yet. Reconcile the
        # attempt from the filesystem — the filesystem is the truth, not
        # the pre-computed plan. Every archive this session created
        # (stamp >= the journal entry's own stamp) is either restored or
        # discarded:
        #
        #   - archives whose persona name was in ``planned_created`` are
        #     in-session artifacts (an earlier org CREATED the persona in
        #     this same deploy-all and a later org archived it).
        #     Restoring them would resurrect something that did not exist
        #     before the session; they are discarded instead.
        #   - archives whose name is in planned_archived/planned_pruned
        #     are restored — EXCEPT that if the same persona name was
        #     archived more than once within the session (two orgs
        #     sharing an actor id, both with --force), only the OLDEST
        #     archive holds the pre-session version: it is restored,
        #     every later archive of that name is an in-session artifact
        #     and is discarded. Restoring both would clobber the
        #     pre-session version with an in-session one (the second
        #     restore would trash the freshly restored pre-session
        #     version).
        #   - archives whose name appears in NO planned list were never
        #     created by this deploy (the deploy only archives names in
        #     planned_archived/planned_pruned): they are foreign — left
        #     EXACTLY as found and reported as ``unexpected``, never
        #     restored, never discarded.
        #
        # Personas the attempt created (planned_created that now exist in
        # the target) are removed. Planned-but-never-touched entries
        # leave no trace and are simply ignored.
        planned_created = set(session.get("planned_created") or [])
        planned_names = planned_created | set(
            (session.get("planned_archived") or [])
            + (session.get("planned_pruned") or [])
        )
        session_id = str(session.get("id", ""))
        # F6: session ids may carry a numeric suffix (-1, -2...) from a
        # same-millisecond collision. The suffix sorts AFTER the base
        # stamp lexicographically, so an archive stamped at exactly the
        # session's own start millisecond would be misclassified as
        # "archived before this session started" and skipped. Compare
        # the base stamp only (strip a trailing -N suffix).
        base_session_id = re.sub(r"-\d+$", "", session_id)
        for hit in sorted(archive_root.iterdir()):
            if not hit.is_dir() or hit.is_symlink():
                continue
            if hit.name.startswith(TRASH_PREFIX):
                # Our own rollback trash dir (possibly left by a
                # previous interrupted rollback): never a persona, never
                # restored, never discarded — leave it alone.
                continue
            m = _ARCHIVE_NAME_RE.match(hit.name)
            if m is None:
                # Not an archive dir (e.g. a hand-made directory, or a
                # phantombot archive with a non-standard name). Never
                # touch it.
                continue
            name, stamp = m.group("name"), m.group("stamp")
            if not _is_safe_name(name):
                raise RollbackError(
                    f"refusing to roll back: unsafe persona name in "
                    f"manifest ({name!r}). Nothing was changed."
                )
            if stamp < base_session_id:
                # Archived before this session started: not ours.
                continue
            if name in planned_created:
                # Created AND archived within this same session: an
                # in-session artifact. Discard it instead of restoring.
                plan.discard_archives.append(hit)
            elif name in planned_names:
                present.append((name, hit))
            else:
                # Name is in NO planned list: this deploy never archives
                # names outside planned_archived/planned_pruned, so this
                # dir is foreign (phantombot import-persona, manual
                # restore, an older PhantomOrg version...). Leave it
                # EXACTLY as found — restoring or discarding it would
                # consume/move an archive this session never created.
                plan.unexpected.append((name, hit))
        # Only the oldest archive per name is the pre-session version.
        # ``sorted()`` yields the oldest first (stamps are chronological
        # and a suffix-free name sorts before suffixed ones), so the
        # first hit per name is the one to restore; the rest are
        # in-session artifacts of the interrupted deploy-all.
        seen_names: set[str] = set()
        deduped_present: list[tuple[str, Path]] = []
        for name, hit in present:
            if name in seen_names:
                plan.discard_archives.append(hit)
            else:
                seen_names.add(name)
                deduped_present.append((name, hit))
        present = deduped_present
        for name in session.get("planned_created") or []:
            if not _is_safe_name(name):
                raise RollbackError(
                    f"refusing to roll back: unsafe persona name in "
                    f"manifest ({name!r}). Nothing was changed."
                )
            if (target / name).exists():
                plan.remove_created.append(name)
        plan.restore = present
        plan.cleanup_only = (
            not present and not plan.remove_created and not plan.discard_archives
        )
        # An interrupted (in_progress) deploy has no recorded data-file
        # backup paths (they are only persisted at commit time): the
        # pre-deploy state of scopes.json / HUMANS.md is unknowable, so
        # they are left untouched and the user is warned.
        plan.data_skipped = True
        return plan

    # Committed session: the manifest records exactly what the deploy
    # archived, in creation order. Dedupe by persona name FIRST: a
    # deploy-all --force where two orgs share an actor id archives the
    # same name twice in one session (S1 = the pre-session version,
    # S2 = org A's freshly deployed version). Only the FIRST archive per
    # name holds the pre-session version — restoring both in recorded
    # order would clobber the freshly restored pre-session version with
    # the in-session one (the second restore trashes the first, and the
    # trash is then deleted). This is the same rule the in_progress
    # branch applies when it scans the archive root; the committed
    # branch must apply it to the recorded list.
    first_per_name: dict[str, Path] = {}
    duplicate_archives: list[Path] = []
    for entry in session.get("archived", []):
        if not isinstance(entry, dict):
            raise RollbackError(
                "refusing to roll back: corrupt manifest (archived entry is "
                "not an object). Nothing was changed."
            )
        name = entry.get("name")
        if not isinstance(name, str) or not _is_safe_name(name):
            raise RollbackError(
                f"refusing to roll back: unsafe persona name in manifest "
                f"({name!r}). Nothing was changed."
            )
        dir_raw = entry.get("dir")
        if not isinstance(dir_raw, str) or not dir_raw:
            raise RollbackError(
                "refusing to roll back: corrupt manifest (archived entry has "
                "no dir). Nothing was changed."
            )
        archive = Path(dir_raw)
        if not archive.is_absolute():
            raise RollbackError(
                f"refusing to roll back: archive path is not absolute "
                f"({dir_raw}). Nothing was changed."
            )
        if archive.is_symlink():
            raise RollbackError(
                f"refusing to roll back: archived persona is a symlink "
                f"({archive}). Nothing was changed."
            )
        if archive.resolve().parent != archive_root_resolved:
            raise RollbackError(
                f"refusing to roll back: archive path escapes the archive root "
                f"({dir_raw}). Nothing was changed."
            )
        if name not in first_per_name:
            first_per_name[name] = archive
        else:
            # Same name archived more than once in this session: only
            # the FIRST (oldest, pre-session) archive is restored; the
            # later ones are in-session artifacts and are discarded.
            duplicate_archives.append(archive)
    plan.discard_archives.extend(duplicate_archives)

    # Split the first-per-name archives into present (still in the
    # archive root) and missing (already consumed). `missing` keeps the
    # RECORDED name (not one derived from the path): the recorded name
    # is what the rollback would restore the persona as.
    for name, archive in first_per_name.items():
        if archive.is_dir():
            present.append((name, archive))
        else:
            missing.append((name, str(archive)))

    if missing:
        # Some first-per-name archives are gone. Only a previous rollback
        # attempt (interrupted mid-restore) may legitimately have
        # consumed them — and execute_rollback ALWAYS leaves a trash dir
        # behind when it discards the version it replaces (it discards
        # before every restore of an existing persona), so a lingering
        # trash dir is the evidence that such an attempt ran.
        #
        # Cases:
        # 1. Mixed missing/present AND no trash dir: the missing
        #    archives were removed OUTSIDE PhantomOrg (manual
        #    deletion). Refuse — continuing would silently succeed on an
        #    incomplete rollback. This is the historical behavior.
        # 2. A trash dir exists AND every missing persona is back in the
        #    target: an interrupted rollback consumed them (the restore
        #    move completed). Continue and finish the job.
        # 3. A trash dir exists AND some missing persona is NOT in the
        #    target: that restore never completed and the pre-deploy
        #    version is lost. Refuse with a manual-recovery message.
        trash_evidence = any(
            p.name.startswith(TRASH_PREFIX) for p in archive_root.iterdir()
        )
        if present and not trash_evidence:
            raise RollbackError(
                "cannot roll back: some archived personas are missing:"
                + "\n".join(f"  - {m}" for _, m in missing)
                + "\nNo interrupted rollback could have consumed them — they "
                "were removed outside PhantomOrg. The rollback would be "
                "incomplete, so nothing was changed."
            )
        lost = [m for name, m in missing if not (target / name).is_dir()]
        if lost:
            raise RollbackError(
                "cannot roll back: some archived personas are missing and "
                "not present in the target (manual recovery required):"
                + "\n".join(f"  - {m}" for m in lost)
                + "\nThe rollback would be incomplete, so nothing was changed."
            )
        # Every missing archive was already restored by an interrupted
        # attempt: the plan below only finishes the job (restoring what
        # remains, discarding in-session artifacts, cleaning up).
    if present:
        plan.restore = present
    else:
        plan.cleanup_only = True

    plan.remove_created = [str(n) for n in (session.get("created") or [])]
    for n in plan.remove_created:
        if not _is_safe_name(n):
            raise RollbackError(
                f"refusing to roll back: unsafe persona name in manifest "
                f"({n!r}). Nothing was changed."
            )

    # Personas whose CURRENT version will be replaced: warn the user in
    # the confirmation. The current version is never lost outright — it
    # goes to a trash dir until the rollback succeeds.
    plan.discard = [name for name, _ in plan.restore if (target / name).exists()]

    for entry in session.get("org_yamls") or []:
        path_raw = entry.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise RollbackError(
                "refusing to roll back: corrupt manifest (org_yamls entry "
                "has no path). Nothing was changed."
            )
        org_yaml = Path(path_raw)
        if not org_yaml.is_file():
            continue
        import hashlib

        digest = hashlib.sha256(org_yaml.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            plan.spec_drift.append(str(org_yaml))

    # Data-dir derived files (scopes.json / HUMANS.md): recorded only on
    # committed sessions. If a pre-existing file was overwritten, its
    # backup lives in personas-archive/ (restore it); if the file was
    # CREATED by this deploy (did not pre-exist), remove it. Old sessions
    # recorded before data-file backup support have none of these keys
    # and are handled as a back-compat skip (data_skipped warning).
    for backup_key, created_key, label in (
        ("scopes_backup", "scopes_created", "scopes.json"),
        ("humans_backup", "humans_created", "HUMANS.md"),
    ):
        created = session.get(created_key) is True
        if created:
            # The deploy CREATED the file (it did not exist at the start
            # of the session — e.g. the first org of a deploy-all wrote
            # it fresh even if a later org backed it up): the exact
            # pre-session state is ABSENT, so remove it on rollback.
            plan.remove_data.append(str((target.parent / label).resolve()))
            continue
        backup_raw = session.get(backup_key)
        if isinstance(backup_raw, str) and backup_raw:
            backup = _validate_data_backup(backup_raw, archive_root_resolved, label)
            if backup.is_file():
                plan.restore_data.append(
                    (str((target.parent / label).resolve()), str(backup))
                )
            else:
                # Backup missing (consumed by a previous interrupted
                # rollback or removed by hand). Not fatal: the derived
                # file still exists; the user is warned via data_skipped.
                plan.data_skipped = True
        elif not isinstance(backup_raw, str):
            # Old session (pre-backup support) or an interrupted entry:
            # pre-deploy state unknowable.
            plan.data_skipped = True

    return plan


def execute_rollback(plan: RollbackPlan) -> RollbackResult:
    """Undo the session described by the plan, transactionally.

    F12: acquires the transaction lock internally, so a direct library
    caller is serialized against concurrent deploys/rollbacks just like
    the CLI is (the CLI also holds the lock while re-planning and
    calling this — the lock is reentrant, so the nested acquisition is
    a no-op there).

    Order matters — every irreversible step happens AFTER the recoverable
    ones, and the manifest entry is dropped LAST:

    1. archived personas are moved back (their backups are consumed);
    2. data-dir derived files (scopes.json / HUMANS.md) are restored to
       their pre-deploy state (backup copied back) or removed (if the
       deploy created them);
    3. personas the deploy created are removed (to the trash);
    4. the trash (our own temporary dir) is deleted;
    5. personas-archive/ and the target are deleted if they did not
       pre-exist and are now empty;
    6. ONLY THEN the session entry is removed from the manifest.

    Because the manifest is dropped last, a failure in any step leaves
    the session recorded: if the failure happened after the archives
    were consumed, the next `po rollback` sees a cleanup-only plan and
    finishes the job instead of getting stuck.

    Safety: nothing that existed in the target is ever deleted outright.
    Whatever the rollback replaces or removes (post-deploy versions of
    restored personas, personas the deploy created) is moved to a trash
    dir inside personas-archive/ first and is only deleted after every
    other step has succeeded. If anything fails mid-way, the trash keeps
    every discarded item recoverable and the manifest entry is kept, so
    the state is never silently lost.
    """
    session = plan.session
    target_raw = session.get("target")
    if not isinstance(target_raw, str) or not Path(target_raw).is_absolute():
        raise RollbackError(
            "refusing to roll back: session target is missing or not absolute."
        )
    target = Path(target_raw)
    try:
        return _begin_rollback(plan, target)
    except OSError as e:
        # The journal transition (_mark_session_state) is the only thing
        # that can raise here before any mutation happens. Wrap it in the
        # same retryable contract as the mutation phase so the CLI reports
        # it uniformly instead of a raw traceback: nothing was changed and
        # a plain retry is always safe.
        raise RollbackError(
            f"rollback interrupted by an error: {e}\n"
            + "The session entry is still recorded and nothing was "
            "changed, so you can retry `po rollback` after fixing the "
            "cause."
        ) from e


def _begin_rollback(plan: RollbackPlan, target: Path) -> RollbackResult:
    """Start the rollback: transition the journal entry to
    ``rollback_in_progress`` FIRST, immediately after the transaction
    lock is acquired and BEFORE any cleanup or filesystem mutation, then
    run the mutation phase.

    Audit v0.5.8 #1 (HIGH, ChatGPT re-verification): the rollback journal
    transition must be the first thing that happens under the lock. If
    it happened after _cleanup_stale_internals (as in v0.5.7), a stale
    trash dir — the only recovery evidence of a previous interrupted
    rollback whose entry is still committed — would be garbage-collected
    while the manifest still said ``committed`` (trash_guard off), and
    the retry would then refuse permanently. Writing the state first
    means the GC always sees a protected session from the very first
    moment of the rollback.

    ``_execute_rollback_locked`` does NOT change the state again: the
    transition happens exactly once, here.
    """
    archive_root = target.parent / "personas-archive"
    with _transaction_lock(target):
        _mark_session_state(archive_root, plan.session_id, "rollback_in_progress")
        return _execute_rollback_locked(plan, target)


def _execute_rollback_locked(plan: RollbackPlan, target: Path) -> RollbackResult:
    """The mutation phase of the rollback; the caller holds the
    transaction lock."""
    session = plan.session
    archive_root = target.parent / "personas-archive"
    # F10: a previous crashed deploy/rollback may have left stale temp
    # manifests. Clean them before mutating. Trash dirs are safe here:
    # _begin_rollback already transitioned this session to
    # rollback_in_progress, so the GC protects every trash dir (audit
    # v0.5.8 #1).
    _cleanup_stale_internals(archive_root)
    result = RollbackResult(session_id=plan.session_id)
    result.data_skipped = plan.data_skipped

    # Trash dir: created lazily, inside the archive root (a dotfile dir
    # phantombot ignores).
    trash: Path | None = None

    def _trash() -> Path:
        nonlocal trash
        if trash is None:
            trash = archive_root / f"{TRASH_PREFIX}{_archive_stamp()}"
            trash.mkdir(parents=True, exist_ok=True)
        return trash

    def _discard(path: Path) -> Path:
        """Move path into the trash (never delete it directly)."""
        dest = _trash() / path.name
        for n in range(1, 1000):
            if not dest.exists():
                break
            dest = _trash() / f"{path.name}-{n}"
        else:
            # All 1000 candidate names are taken: raise instead of
            # falling through — with a taken ``dest``, shutil.move would
            # place the item INSIDE the existing directory instead of at
            # the intended trash path.
            raise RollbackError(
                f"unable to allocate a unique trash destination for "
                f"{path.name!r}: 1000 same-name trash entries already exist"
            )
        shutil.move(str(path), str(dest))
        return dest

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name, archive in plan.restore:
            dest = target / name
            if (archive / PER_FILE_MARKER).is_file():
                # Per-file (additive) archive: restore each owned file back
                # into the LIVE persona directory, never moving the
                # directory itself. Runtime-owned files stay untouched; the
                # current version of each restored file is trashed first,
                # preserving the <persona>/<rel> structure so a discarded
                # file is always attributable.
                discarded_any = False
                for p in sorted(archive.rglob("*")):
                    if not p.is_file() or p.name == PER_FILE_MARKER:
                        continue
                    rel = p.relative_to(archive)
                    live = dest / rel
                    if live.exists():
                        trash_dest = _trash() / name / rel
                        trash_dest.parent.mkdir(parents=True, exist_ok=True)
                        for n in range(1, 1000):
                            if not trash_dest.exists():
                                break
                            trash_dest = _trash() / name / f"{rel.as_posix()}-{n}"
                        shutil.move(str(live), str(trash_dest))
                        discarded_any = True
                    live.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, live)
                # The archive dir is consumed: remove it once restored.
                shutil.rmtree(archive, ignore_errors=True)
                result.restored.append(name)
                if discarded_any:
                    result.discarded.append(name)
            else:
                if dest.exists():
                    _discard(dest)
                    result.discarded.append(name)
                shutil.move(str(archive), str(dest))
                result.restored.append(name)

        for name in plan.remove_created:
            dest = target / name
            if dest.exists():
                _discard(dest)
                result.removed_created.append(name)

        for archive in plan.discard_archives:
            if archive.is_dir():
                _discard(archive)
                result.discarded_archives.append(archive.name)

        # Data-dir derived files (scopes.json / HUMANS.md): restore the
        # pre-deploy backup over the current version, or remove a file
        # the deploy created (it did not pre-exist). These are derived,
        # regenerable files — not user data — so they are overwritten /
        # removed directly (no trash needed). The consumed backup file
        # is deleted so the archive root can be removed when it did not
        # pre-exist. A failure here is recoverable (the manifest is still
        # recorded) and lands in the same RollbackError contract.
        for dest_raw, backup_raw in plan.restore_data:
            dest = Path(dest_raw)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_raw, dest)
            result.restored_data.append(dest_raw)
            Path(backup_raw).unlink(missing_ok=True)
        for dest_raw in plan.remove_data:
            dest = Path(dest_raw)
            if dest.exists():
                dest.unlink(missing_ok=True)
                result.removed_data.append(dest_raw)
        # Sweep every deploy-time data-file backup that belongs to THIS
        # session (stamp >= its base id). This includes the recorded
        # backups restored above and any ORPHAN backups the deploy made
        # but the session did not record (a deploy-all where multiple
        # orgs each rewrite scopes.json/HUMANS.md backs the file up once
        # per org; only the FIRST — the pre-session state — is recorded
        # in the manifest, the rest are in-session duplicates). Leaving
        # them would accumulate dead weight in the archive root (and,
        # if the root pre-existed, keep it from ever cleaning up).
        sid_base = re.sub(r"-\d+$", "", str(session.get("id", "")))
        for _pf in list(archive_root.iterdir()):
            if not (
                _pf.is_file()
                and not _pf.is_symlink()
                and _pf.name.startswith(DATA_BACKUP_PREFIX)
            ):
                continue
            _stamp = _pf.name[len(DATA_BACKUP_PREFIX) :]
            # Strip the leading "<filename>-" to reach the stamp, then
            # compare against the session base id.
            for _tail in _DATA_STAMP_RE.findall(_stamp):
                if _tail >= sid_base:
                    # Not a local try/except: a failure here aborts the
                    # rollback (RollbackError) and the session stays
                    # recorded, so a retry re-restores/removes idempotently
                    # and sweeps again.
                    _pf.unlink(missing_ok=True)
                    break

        # Recoverable work is done. Now the irreversible cleanup: our
        # own trash first (it is removed only once everything above
        # succeeded). The archive-root / target deletion happens in the
        # final lock block, where we know the manifest state AFTER this
        # session is dropped.
        #
        # Remove every trash dir that belongs to THIS session's rollback
        # (its own attempts, plus any left behind by an earlier
        # interrupted attempt of the same session): everything the
        # rollback — or its crashed predecessor — discarded is confirmed
        # garbage now, since every restore/removal above succeeded.
        # Trash dirs OLDER than this session are kept: they may be the
        # only recovery evidence of a previous session's interrupted
        # rollback (plan_rollback refuses to finish that session without
        # it). Empty trash dirs are swept regardless (no content, no
        # evidence value).
        sid_base = re.sub(r"-\d+$", "", str(session.get("id", "")))
        for _p in list(archive_root.iterdir()):
            if not (
                _p.is_dir() and not _p.is_symlink() and _p.name.startswith(TRASH_PREFIX)
            ):
                continue
            _stamp = _p.name[len(TRASH_PREFIX) :]
            if _stamp >= sid_base or not any(_p.iterdir()):
                shutil.rmtree(_p)

        if not session.get("target_pre_existed", False) and target.is_dir():
            leftovers = list(target.iterdir())
            if not leftovers:
                target.rmdir()
                result.target_deleted = True

        # Everything succeeded — only now drop the session from the
        # manifest (under the lock, so a concurrent deploy cannot be
        # overwritten between our load and save).
        root_pre_existed = session.get("archive_root_pre_existed", False)
        remove_root = False
        with _manifest_lock(archive_root):
            try:
                sessions = [
                    s
                    for s in load_sessions(archive_root)
                    if s.get("id") != session.get("id")
                ]
            except ManifestError as e:
                raise RollbackError(
                    f"the session manifest became unreadable during the "
                    f"rollback: {e}. The session entry was NOT dropped; "
                    "resolve the manifest and retry."
                ) from e
            _save_sessions(archive_root, sessions)
            # Sweep leftover manifest temp files while holding the lock:
            # every writer creates its temp file inside this lock, so no
            # live writer's temp can exist here — each *.tmp is the
            # residue of a crashed writer (most likely this session's own
            # interrupted rollback, whose temp would otherwise survive
            # into the final state).
            for _p in list(archive_root.iterdir()):
                if _p.name.endswith(".tmp"):
                    try:
                        _p.unlink(missing_ok=True)
                    except OSError:
                        pass
            # With the session dropped, is the archive root now truly
            # disposable? Only if it did not pre-exist before this
            # deploy AND nothing else lives in it (no other sessions, no
            # phantombot archives). Compute the decision UNDER the lock
            # (no concurrent writer can change the manifest between our
            # save and the check)...
            if (
                not root_pre_existed
                and archive_root.is_dir()
                and _empty_after_internals(archive_root)
            ):
                remove_root = True
        # F8: actually removing the root is a best-effort cleanup, NOT a
        # transactional step. Do it OUTSIDE the lock (an rmtree inside
        # the lock would block concurrent deploys for its whole runtime,
        # and a failure there was previously misreported as a failed
        # rollback). If it fails, the root simply remains — harmless,
        # and removable by hand.
        if remove_root:
            shutil.rmtree(archive_root, ignore_errors=True)
            result.archive_root_deleted = not archive_root.exists()
    except OSError as e:
        raise RollbackError(
            f"rollback interrupted by an error: {e}\n"
            + "The session entry is still recorded, so you can retry "
            "`po rollback` after fixing the cause — if the archived "
            "personas were already restored, it will only finish the "
            "cleanup. Anything the rollback had to discard was moved to "
            f"{trash or archive_root} (a ._pf_trash_* directory) — check "
            "there before retrying."
        ) from e

    return result
