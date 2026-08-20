"""
Simple mutations on an existing org.yaml: add-department, add-role,
add-actor, remove-*, rename-*. Each one loads the raw YAML (not the typed
model, so it can write back preserving the rest of the document), mutates
the entry, and rewrites the file. The underlying business validation is
left to `po validate`; the remove/rename mutations do make their own
structural checks (see below) because without them broken references
would be created silently.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import yaml

fcntl: ModuleType | None
try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


from ..spec.shape_validator import is_valid_identifier


class RemovalBlockedError(Exception):
    """
    Raised when removing something would leave broken references in the
    rest of the org.yaml (a role with assigned actors, a department with
    sub-departments, a role others report to or that appears in
    escalation_matrix). The message lists exactly what blocks it.
    """

    def __init__(self, blockers: list[str]):
        self.blockers = blockers
        super().__init__("Cannot remove: " + "; ".join(blockers))


class DuplicateIdError(Exception):
    """Raised when trying to add a department/role/actor with an id that already exists."""


def _backup_stamp() -> str:
    """Timestamp for backup filenames, unique within a second:
    20260809-150400-123456 (microseconds make same-second mutations
    impossible to collide)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _fsync_dir(path: Path) -> None:
    """Fsync a directory so a completed rename is durable across a crash.

    ``os.replace`` atomically swaps the file, but the rename itself lives
    in the parent directory's metadata — without fsyncing it, a power
    loss can revert the file to its old name/content on some filesystems.
    Best-effort: directories are not fsync-able on every platform.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform-dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform-dependent
        pass
    finally:
        os.close(fd)


def backup_org_file(org_path: Path) -> Path | None:
    """Copy org.yaml to org.yaml.bak-<timestamp> before it is modified.

    Returns the backup path, or None when the org file does not exist
    yet (nothing to back up). Restoring is a simple
    `cp org.yaml.bak-<ts> org.yaml`. Announces the backup on stderr so
    the user always knows a rollback point was created.

    The copy is atomic and durable: content is written to a temp file,
    flushed, fsynced, then ``os.replace``d into place, and the parent
    directory is fsynced so the rename itself survives a crash — a
    crash cannot leave a truncated backup that would silently corrupt
    a restore.
    """
    if not org_path.exists():
        return None
    backup_path = org_path.with_name(f"{org_path.name}.bak-{_backup_stamp()}")
    tmp_path = backup_path.with_name(f"{backup_path.name}.tmp-{uuid4().hex}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(org_path.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, backup_path)
        _fsync_dir(backup_path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    print(f"Backup of {org_path.name} written to {backup_path}", file=sys.stderr)
    return backup_path


@contextmanager
def _mutation_lock(org_path: Path):
    """Serialize load-mutate-save cycles on an org.yaml across processes.

    Two concurrent `po add-*` / `po setup` invocations (flag mode is
    explicitly for scripting/CI) would otherwise both load the same doc,
    both append, both save — the second ``os.replace`` silently discards
    the first mutation. Advisory flock on a sibling lockfile; on Windows
    (no fcntl) it falls back to a real ``msvcrt.locking`` byte-range
    lock with a retry loop (audit v0.5.7 #3: the historical no-op let
    two Windows writers clobber each other's mutation — the same bug the
    deploy layer's manifest lock already fixed).

    The lockfile is deliberately never deleted while the org file exists
    (deleting a lockfile another process may hold is racy).
    """
    lock_path = org_path.with_name(f"{org_path.name}.lock")
    f = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - handle must stay open for the lock's lifetime
    if fcntl is not None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return
    # pragma: no cover - Windows only
    import msvcrt

    # msvcrt.locking locks a byte RANGE, so the file must already
    # contain at least one byte.
    if f.seek(0, os.SEEK_END) == 0:
        f.write("\0")
        f.flush()
    f.seek(0)
    try:
        while True:
            try:
                # LK_LOCK retries only a fixed number of times (10x1s)
                # and then raises; loop manually so a long-held lock
                # never spuriously fails.
                # msvcrt is Windows-only; mypy on Linux cannot see its attrs.
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                break
            except OSError:
                f.seek(0)
                time.sleep(0.05)
        yield
    finally:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        f.close()


def _load(org_path: Path) -> dict:
    """Loads org.yaml as a dict, with clear errors instead of raw
    tracebacks: an empty file yields an empty dict (a fresh org with no
    entries yet is valid input for add-*), a non-mapping document or an
    unreadable file raises ``ValueError`` with a message the CLI already
    knows how to present."""
    try:
        with open(org_path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"Cannot read {org_path}: {e}") from e
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        # ValueError on purpose (not TypeError): the CLI catches
        # (KeyError, ValueError) and presents the message; TypeError
        # would escape as a raw traceback.
        raise ValueError(  # noqa: TRY004
            f"{org_path} does not contain a YAML mapping (found {type(doc).__name__})"
        )
    return doc


def _save(org_path: Path, doc: dict) -> None:
    """Atomically replace org.yaml with ``doc``.

    Sequence (the backup remains the recovery point):

    1. backup original  -> org.yaml.bak-<ts> (previous contents are
       always recoverable)
    2. write the complete document to org.yaml.tmp-<uuid> (a partial
       write can never be seen as the live spec: a crash after this
       step leaves only an orphan .tmp file, never a truncated
       org.yaml)
    3. fsync the temp file so the data is durable before it becomes
       visible
    4. os.replace(tmp, org_path) — atomic rename on POSIX/Windows: the
       reader sees either the old complete file or the new complete
       file, never an empty or half-written one.

    A crash between (1) and (4) leaves org.yaml untouched and a
    recoverable backup: pf validate / pf build / automation can never
    observe a truncated active spec. The parent directory is fsynced
    after the rename so the swap itself is durable (not just the data).
    """
    backup_org_file(org_path)
    tmp_path = org_path.with_name(f"{org_path.name}.tmp-{uuid4().hex}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, org_path)
        _fsync_dir(org_path.parent)
    except BaseException:
        # Never leave a half-written temp pretending to be a pending
        # save; the live org.yaml is still the previous complete one.
        tmp_path.unlink(missing_ok=True)
        raise


def add_department(
    org_path: Path, dept_id: str, name: str, parent: str | None, access_policy: str
) -> None:
    with _mutation_lock(org_path):
        doc = _load(org_path)
        existing = doc.setdefault("departments", [])
        if any(d["id"] == dept_id for d in existing):
            raise DuplicateIdError(f"A department with id '{dept_id}' already exists")
        existing.append(
            {
                "id": dept_id,
                "name": name,
                "parent": parent,
                "access_policy": access_policy,
            }
        )
        _save(org_path, doc)


def add_role(
    org_path: Path,
    role_id: str,
    name: str,
    department: str,
    reports_to: str | None,
    access_level: str,
    functions: list[str] | None = None,
    reports_to_human: str | None = None,
    security_exceptions: list[str] | None = None,
) -> None:
    with _mutation_lock(org_path):
        doc = _load(org_path)
        existing = doc.setdefault("roles", [])
        if any(r["id"] == role_id for r in existing):
            raise DuplicateIdError(f"A role with id '{role_id}' already exists")
        existing.append(
            {
                "id": role_id,
                "name": name,
                "department": department,
                "reports_to": reports_to,
                "reports_to_human": reports_to_human,
                "functions": functions or [],
                "access_level": access_level,
                "security_exceptions": security_exceptions or [],
            }
        )
        _save(org_path, doc)


def add_actor(
    org_path: Path,
    actor_id: str,
    role: str,
    tools: list[str],
    telegram_bot: str | None = None,
    tools_excluded: list[str] | None = None,
    actor_exceptions: list[str] | None = None,
    tone: str | None = None,
) -> None:
    with _mutation_lock(org_path):
        doc = _load(org_path)
        existing = doc.setdefault("actors", [])
        if any(a["id"] == actor_id for a in existing):
            raise DuplicateIdError(f"An actor with id '{actor_id}' already exists")
        existing.append(
            {
                "id": actor_id,
                "role": role,
                "telegram_bot": telegram_bot,
                "tools": tools,
                "tools_excluded": tools_excluded or [],
                "actor_exceptions": actor_exceptions or [],
                "tone": tone,
            }
        )
        _save(org_path, doc)


def add_role_and_actor(
    org_path: Path,
    *,
    role_id: str,
    role_name: str,
    department: str,
    reports_to: str | None,
    access_level: str,
    reports_to_human: str | None = None,
    functions: list[str] | None = None,
    security_exceptions: list[str] | None = None,
    actor_id: str,
    tools: list[str],
    telegram_bot: str | None = None,
    tools_excluded: list[str] | None = None,
    actor_exceptions: list[str] | None = None,
    tone: str | None = None,
) -> None:
    """Adds a role AND its actor in ONE atomic load-mutate-save cycle.

    ``import-audit --apply`` needs both-or-nothing: applying the role
    first and then the actor (two separate save cycles) leaves a
    half-applied import when the actor id collides — the role is already
    persisted and the org.yaml is silently changed despite the "Not
    applied" error. This pre-checks BOTH ids (existence and identifier
    grammar) before mutating anything, then writes both in a single
    ``_save`` under a single lock, so a rejected apply can never leave a
    partial org.yaml.

    Grammar check: add-role/add-actor are interactive and leave final
    validation to `po validate`, but this is a batch import path that
    promises a validate-clean org.yaml — writing an id that the validator
    immediately rejects would break that promise.
    """
    with _mutation_lock(org_path):
        doc = _load(org_path)
        roles = doc.setdefault("roles", [])
        actors = doc.setdefault("actors", [])
        if any(r["id"] == role_id for r in roles):
            raise DuplicateIdError(f"A role with id '{role_id}' already exists")
        if any(a["id"] == actor_id for a in actors):
            raise DuplicateIdError(f"An actor with id '{actor_id}' already exists")
        for label, value in (("role id", role_id), ("actor id", actor_id)):
            if not is_valid_identifier(value):
                raise ValueError(
                    f"Invalid {label} {value!r} — ids must match "
                    r"[a-z0-9][a-z0-9_-]* (lowercase letter/digit first, "
                    "then lowercase letters, digits, '-' or '_'; max 64 "
                    "chars; no Windows-reserved device name)"
                )
        roles.append(
            {
                "id": role_id,
                "name": role_name,
                "department": department,
                "reports_to": reports_to,
                "reports_to_human": reports_to_human,
                "functions": functions or [],
                "access_level": access_level,
                "security_exceptions": security_exceptions or [],
            }
        )
        actors.append(
            {
                "id": actor_id,
                "role": role_id,
                "telegram_bot": telegram_bot,
                "tools": tools,
                "tools_excluded": tools_excluded or [],
                "actor_exceptions": actor_exceptions or [],
                "tone": tone,
            }
        )
        _save(org_path, doc)


# --------------------------------------------------------------------
# remove-*
# --------------------------------------------------------------------


def remove_department(org_path: Path, dept_id: str, cascade: bool = False) -> list[str]:
    """
    Removes a department. Blocks if it has assigned roles (roles are NEVER
    touched automatically: you must reassign or remove them by hand
    first). If it has child departments, blocks unless --cascade, which
    promotes them to root (parent: null) instead of removing them.

    Returns the list of cascade actions performed (empty if none).
    """
    with _mutation_lock(org_path):
        doc = _load(org_path)
        departments = doc.get("departments", [])
        if not any(d["id"] == dept_id for d in departments):
            raise KeyError(f"Department '{dept_id}' does not exist")

        roles_using_it = [
            r["id"] for r in doc.get("roles", []) if r.get("department") == dept_id
        ]
        if roles_using_it:
            raise RemovalBlockedError(
                [
                    (
                        f"{len(roles_using_it)} role(s) still assigned to this department "
                        f"({roles_using_it}) — reassign or remove them first; this is never "
                        f"resolved in cascade"
                    )
                ]
            )

        children = [d["id"] for d in departments if d.get("parent") == dept_id]
        if children and not cascade:
            raise RemovalBlockedError(
                [
                    (
                        f"{len(children)} child department(s) point to this one as parent "
                        f"({children}) — use cascade=True to promote them to root, or "
                        f"reassign them by hand"
                    )
                ]
            )

        cascade_actions: list[str] = []
        if children and cascade:
            for d in departments:
                if d.get("parent") == dept_id:
                    d["parent"] = None
                    cascade_actions.append(
                        f"departments.{d['id']}.parent -> null (promoted to root)"
                    )

        doc["departments"] = [d for d in departments if d["id"] != dept_id]
        _save(org_path, doc)
        return cascade_actions


def remove_role(org_path: Path, role_id: str, cascade: bool = False) -> list[str]:
    """
    Removes a role. ALWAYS blocks (even with cascade) if actors are
    assigned to this role — deleting a real actor without it being
    explicitly requested would be too destructive to do in cascade; you
    must reassign or remove them with remove_actor first.

    If there are subordinate roles (reports_to pointing to this one) or
    escalation_matrix entries that reference it, blocks unless
    cascade=True, which:
    - promotes the subordinates to root (reports_to -> null)
    - removes the escalation_matrix entries that mentioned it

    Returns the list of cascade actions performed.
    """
    with _mutation_lock(org_path):
        doc = _load(org_path)
        roles = doc.get("roles", [])
        if not any(r["id"] == role_id for r in roles):
            raise KeyError(f"Role '{role_id}' does not exist")

        actors_using_it = [
            a["id"] for a in doc.get("actors", []) if a.get("role") == role_id
        ]
        if actors_using_it:
            raise RemovalBlockedError(
                [
                    (
                        f"{len(actors_using_it)} actor(s) still assigned to this role "
                        f"({actors_using_it}) — reassign or remove them with remove_actor "
                        f"first; this is NEVER resolved in cascade"
                    )
                ]
            )

        subordinates = [r["id"] for r in roles if r.get("reports_to") == role_id]
        escalation = doc.get("escalation_matrix", [])
        escalation_refs = [
            e for e in escalation if e.get("from") == role_id or e.get("to") == role_id
        ]

        if (subordinates or escalation_refs) and not cascade:
            blockers = []
            if subordinates:
                blockers.append(
                    f"{len(subordinates)} subordinate role(s) ({subordinates})"
                )
            if escalation_refs:
                blockers.append(
                    f"{len(escalation_refs)} entry(ies) in escalation_matrix"
                )
            raise RemovalBlockedError(
                [
                    "Referenced by "
                    + " and ".join(blockers)
                    + " — use cascade=True to fix it automatically"
                ]
            )

        cascade_actions: list[str] = []
        if cascade:
            for r in roles:
                if r.get("reports_to") == role_id:
                    r["reports_to"] = None
                    cascade_actions.append(
                        f"roles.{r['id']}.reports_to -> null (promoted to root)"
                    )

            remaining_escalation = []
            for e in escalation:
                if e.get("from") == role_id or e.get("to") == role_id:
                    cascade_actions.append(
                        f"escalation_matrix: removed entry {e.get('from')} -> {e.get('to')}"
                    )
                else:
                    remaining_escalation.append(e)
            doc["escalation_matrix"] = remaining_escalation

        doc["roles"] = [r for r in roles if r["id"] != role_id]
        _save(org_path, doc)
        return cascade_actions


def remove_actor(org_path: Path, actor_id: str) -> None:
    """
    Removes an actor. No other element of the spec references actors by
    id (escalation_matrix references role_id, not actor_id), so there are
    no structural blocks to check — but this does NOT delete the already
    compiled/deployed directory of that actor; that is the responsibility
    of whoever operates the runtime (see the notice in the CLI).
    """
    with _mutation_lock(org_path):
        doc = _load(org_path)
        actors = doc.get("actors", [])
        if not any(a["id"] == actor_id for a in actors):
            raise KeyError(f"Actor '{actor_id}' does not exist")
        doc["actors"] = [a for a in actors if a["id"] != actor_id]
        _save(org_path, doc)


# --------------------------------------------------------------------
# rename-*
# --------------------------------------------------------------------


def rename_department(org_path: Path, old_id: str, new_id: str) -> list[str]:
    """Renames a department and updates every cross-reference (parent, roles.department)."""
    with _mutation_lock(org_path):
        doc = _load(org_path)
        departments = doc.get("departments", [])
        if not any(d["id"] == old_id for d in departments):
            raise KeyError(f"Department '{old_id}' does not exist")
        if any(d["id"] == new_id for d in departments):
            raise ValueError(f"A department with id '{new_id}' already exists")

        updated_refs: list[str] = []
        for d in departments:
            if d["id"] == old_id:
                d["id"] = new_id
            elif d.get("parent") == old_id:
                d["parent"] = new_id
                updated_refs.append(f"departments.{d['id']}.parent")

        for r in doc.get("roles", []):
            if r.get("department") == old_id:
                r["department"] = new_id
                updated_refs.append(f"roles.{r['id']}.department")

        _save(org_path, doc)
        return updated_refs


def rename_role(org_path: Path, old_id: str, new_id: str) -> list[str]:
    """Renames a role and updates every cross-reference (reports_to, actors.role, escalation_matrix)."""
    with _mutation_lock(org_path):
        doc = _load(org_path)
        roles = doc.get("roles", [])
        if not any(r["id"] == old_id for r in roles):
            raise KeyError(f"Role '{old_id}' does not exist")
        if any(r["id"] == new_id for r in roles):
            raise ValueError(f"A role with id '{new_id}' already exists")

        updated_refs: list[str] = []
        for r in roles:
            if r["id"] == old_id:
                r["id"] = new_id
            elif r.get("reports_to") == old_id:
                r["reports_to"] = new_id
                updated_refs.append(f"roles.{r['id']}.reports_to")

        for a in doc.get("actors", []):
            if a.get("role") == old_id:
                a["role"] = new_id
                updated_refs.append(f"actors.{a['id']}.role")

        for e in doc.get("escalation_matrix", []):
            if e.get("from") == old_id:
                e["from"] = new_id
                updated_refs.append(f"escalation_matrix: from {old_id} -> {new_id}")
            if e.get("to") == old_id:
                e["to"] = new_id
                updated_refs.append(f"escalation_matrix: to {old_id} -> {new_id}")

        _save(org_path, doc)
        return updated_refs


def rename_actor(org_path: Path, old_id: str, new_id: str) -> None:
    """
    Renames an actor. Nothing else in the spec references actors by id,
    so there are no cross-references to update. IMPORTANT (also warned in
    the CLI): the compiled/deployed directory keeps the old id until the
    next `po build` + `po deploy` — this only changes the org.yaml, it
    doesn't move anything on disk on the runtime side.
    """
    with _mutation_lock(org_path):
        doc = _load(org_path)
        actors = doc.get("actors", [])
        if not any(a["id"] == old_id for a in actors):
            raise KeyError(f"Actor '{old_id}' does not exist")
        if any(a["id"] == new_id for a in actors):
            raise ValueError(f"An actor with id '{new_id}' already exists")
        for a in actors:
            if a["id"] == old_id:
                a["id"] = new_id
        _save(org_path, doc)
