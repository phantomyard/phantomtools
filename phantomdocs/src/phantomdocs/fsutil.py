"""Filesystem durability helpers (audit finding #3).

After ``os.replace`` renames a temp file into place, the *directory entry*
change must itself be durably synced for the strongest power-loss guarantee:
``fsync`` on the file persists its contents, but the rename is a directory
metadata change that a crash can otherwise lose (the file reappears under the
old name, or vanishes).

``fsync_dir`` performs that directory sync. It is deliberately best-effort:
directory fsync is unsupported on some platforms/filesystems (Windows, some
network mounts), and a namespace must not fail a commit because the platform
cannot honor the extra durability step. On POSIX + local disks — the
supported single-writer deployment (Model A) — it is a real no-op that turns
"probably durable" into "durable".

The durability assumption is documented in SPEC §12 (Integrity) and
``docs/SECURITY-TRUST-ANCHOR.md``: PhantomDocs targets single-writer local
storage (or GDrive via the persona's OAuth2), where the directory sync closes
the final crash window left by ``fsync(file) + os.replace``.
"""

from __future__ import annotations

import os
import stat


def fsync_dir(directory: str) -> None:
    """Durably sync the directory entry for ``directory`` (best-effort).

    Opens the directory read-only and ``fsync``s it so a preceding rename is
    persisted. No-op (silently) on non-POSIX, on directories that cannot be
    opened, or when the platform refuses to fsync a directory — the caller's
    commit must never fail over this durability step.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def preserve_mode(fd: int, target: str) -> None:
    """Give the open file ``fd`` the permission bits ``target`` already has.

    ``tempfile.mkstemp`` always creates its temporary file 0600, so renaming
    that temp over an existing file — the atomic-replace idiom — silently
    resets the file's permissions. On a store shared through a group (a
    multi-persona PhantomDocs deployment) that means the first writer leaves
    the manifest and the audit log unreadable/unwritable for every other
    account. Copying the destination's mode onto the temp first makes the
    replace change the *contents* and nothing else.

    A first write (no ``target`` yet) is left untouched: the private 0600
    default stands, and a shared deployment grants the group bits on the store
    once. Best-effort — a platform without ``fchmod``, or a ``stat`` that
    fails, leaves the temp's default mode rather than failing the commit.
    """
    try:
        st = os.stat(target)
    except OSError:
        return
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
    except OSError:
        pass
