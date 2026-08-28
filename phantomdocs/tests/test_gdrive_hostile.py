"""Hostile GDrive backend tests (audit #8): the returned external reference
must be independently verified before it becomes authenticated document state.

A buggy or malicious ``workspace.py`` (wrong id, wrong bytes, a stale id, or
malformed output) must make ``GdriveBackend.put`` fail closed — the reference is
re-read and hash-checked before it is accepted as a document location.
"""

import os
import stat

import pytest

from phantomdocs.identity import content_hash
from phantomdocs.storage import GdriveBackend, StorageError

_FAKE_WORKSPACE = """\
#!/usr/bin/env python3
import hashlib
import os
import sys

BEHAVIOR = os.environ.get("FAKE_GDRIVE_BEHAVIOR", "honest")
STORE = os.environ["FAKE_GDRIVE_STORE"]


def main():
    argv = sys.argv[1:]
    if not argv:
        return 1
    cmd = argv[0]
    if cmd == "drive-upload":
        with open(argv[1], "rb") as f:
            data = f.read()
        ch = hashlib.sha256(data).hexdigest()
        if BEHAVIOR == "empty_id":
            sys.stdout.write("")
        elif BEHAVIOR == "wrong_bytes":
            fid = "file-" + ch
            with open(os.path.join(STORE, fid), "wb") as f:
                f.write(b"wrong bytes")
            sys.stdout.write("gdrive://" + fid)
        elif BEHAVIOR == "wrong_id":
            fid = "file-stale"
            with open(os.path.join(STORE, fid), "wb") as f:
                f.write(b"stale content")
            sys.stdout.write("gdrive://" + fid)
        else:  # honest
            fid = "file-" + ch
            with open(os.path.join(STORE, fid), "wb") as f:
                f.write(data)
            sys.stdout.write("gdrive://" + fid)
        return 0
    if cmd == "drive" and len(argv) >= 3 and argv[1] == "download":
        with open(os.path.join(STORE, argv[2]), "rb") as f:
            data = f.read()
        with open(argv[3], "wb") as f:
            f.write(data)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def _make_workspace(tmp_path, behavior):
    store = tmp_path / "store"
    store.mkdir()
    script = tmp_path / "workspace.py"
    script.write_text(_FAKE_WORKSPACE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["FAKE_GDRIVE_BEHAVIOR"] = behavior
    os.environ["FAKE_GDRIVE_STORE"] = str(store)
    return str(script)


def test_honest_workspace_roundtrips(tmp_path):
    script = _make_workspace(tmp_path, "honest")
    backend = GdriveBackend(workspace_py=script)
    data = b"hello phantomdocs"
    ch = content_hash(data)
    file_id = backend.put(ch, data)
    assert file_id == "file-" + ch


@pytest.mark.parametrize("behavior", ["empty_id", "wrong_bytes", "wrong_id"])
def test_hostile_workspace_rejected(tmp_path, behavior):
    script = _make_workspace(tmp_path, behavior)
    backend = GdriveBackend(workspace_py=script)
    data = b"hello phantomdocs"
    ch = content_hash(data)
    with pytest.raises(StorageError):
        backend.put(ch, data)
