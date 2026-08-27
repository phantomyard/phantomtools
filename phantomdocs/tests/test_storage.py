from unittest import mock

import pytest

from phantomdocs.identity import content_hash as _content_hash
from phantomdocs.storage import (
    GdriveBackend,
    LocalBackend,
    SshBackend,
    StorageError,
    read_reference,
    resolve_backend,
)


def test_local_backend_put_get_has(tmp_path):
    b = LocalBackend(str(tmp_path))
    data = b"hello"
    h = _content_hash(data)
    b.put(h, data)
    assert b.has(h) is True
    assert b.get(h) == data


def test_local_backend_get_rejects_mismatched_content(tmp_path):
    """A mutated blob (content no longer matches its hash) is refused on
    read — integrity holds on the read path, not only under `pd verify`."""
    b = LocalBackend(str(tmp_path))
    data = b"hello"
    h = _content_hash(data)
    b.put(h, data)
    # Corrupt the stored bytes.
    blob = b.blob_path(h)
    with open(blob, "wb") as f:
        f.write(b"tampered")
    with pytest.raises(StorageError):
        b.get(h)


def test_local_backend_rejects_bad_hash(tmp_path):
    b = LocalBackend(str(tmp_path))
    with pytest.raises(StorageError):
        b.put("nothex", b"x")


def test_resolve_backend_local():
    b = resolve_backend("local:///tmp/x")
    assert isinstance(b, LocalBackend)
    assert b.root == "/tmp/x"


def test_resolve_backend_local_two_slash():
    """local://<root> (two slashes) resolves to <root>, not the cwd."""
    b = resolve_backend("local://mystore")
    assert isinstance(b, LocalBackend)
    assert b.root.endswith("mystore")


def test_resolve_backend_bare_path():
    b = resolve_backend("/some/dir")
    assert isinstance(b, LocalBackend)
    assert b.root.endswith("/some/dir")


def test_resolve_backend_ssh():
    b = resolve_backend("ssh://user@host:2222/var/phantomdocs")
    assert isinstance(b, SshBackend)
    assert b.host == "host"
    assert b.user == "user"
    assert b.port == 2222
    assert b.base == "/var/phantomdocs"


def test_resolve_backend_gdrive():
    assert isinstance(resolve_backend("gdrive://"), GdriveBackend)


def test_gdrive_backend_put_returns_file_id():
    """`put` returns the Drive file id printed by the persona's tooling."""
    b = GdriveBackend()
    h = _content_hash(b"x")
    with (
        mock.patch.object(GdriveBackend, "_require_tool"),
        mock.patch(
            "phantomdocs.storage._run_checked",
            return_value=mock.Mock(returncode=0, stdout="file-abc\n", stderr=""),
        ),
    ):
        assert b.put(h, b"x") == "file-abc"


def test_gdrive_backend_put_fails_closed_without_file_id():
    """An upload that yields no file id cannot round-trip, so `put` must
    fail closed rather than fabricate an unrecoverable URI."""
    b = GdriveBackend()
    h = _content_hash(b"x")
    with (
        mock.patch.object(GdriveBackend, "_require_tool"),
        mock.patch(
            "phantomdocs.storage._run_checked",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ),
        pytest.raises(StorageError),
    ):
        b.put(h, b"x")


def test_resolve_backend_unknown():
    with pytest.raises(StorageError):
        resolve_backend("ftp://host/x")


def test_ssh_remote_path_validates_hash():
    b = SshBackend(host="h", base="/var/phantomdocs")
    h = "b" * 64
    assert b.remote_path(h) == f"/var/phantomdocs/blobs/{h[:2]}/{h}"
    with pytest.raises(StorageError):
        b.remote_path("short")


def test_ssh_put_is_atomic_and_quotes_base(monkeypatch):
    """`put` writes to a temp file then renames atomically, and shell-quotes
    the (possibly space-containing) base path (issue #57)."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("phantomdocs.storage._run_checked", fake_run)
    b = SshBackend(host="h", base="/var/phantomdocs with space")
    b.put("b" * 64, b"data")
    cmd = captured["args"][-1]
    # atomic: write to a temp file then rename into place
    assert "cat >" in cmd and " && mv " in cmd
    # the base path (with a space) is shell-quoted
    assert "'/var/phantomdocs with space" in cmd


def test_read_reference_missing_file_raises_storage_error(tmp_path):
    """A missing `file://` reference raises StorageError, not a raw
    FileNotFoundError (issue #58)."""
    with pytest.raises(StorageError):
        read_reference(f"file://{tmp_path}/does-not-exist.txt")


def test_ssh_get_quotes_remote_path(monkeypatch):
    """`get` must shell-quote the remote path like `put` does — a `base` with
    shell metacharacters must not reach the remote shell (issue #72)."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("phantomdocs.storage._run_checked", fake_run)
    b = SshBackend(host="h", base="/var/x; id")
    with pytest.raises(StorageError):
        b.get("b" * 64)  # empty stdout -> hash mismatch; the command is what we assert
    cmd = captured["args"][-1]
    assert cmd == "cat '/var/x; id/blobs/bb/" + "b" * 64 + "'"


def test_ssh_has_quotes_remote_path(monkeypatch):
    """`has` must shell-quote the remote path (issue #72)."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("phantomdocs.storage._run_checked", fake_run)
    b = SshBackend(host="h", base="/var/x; id")
    b.has("b" * 64)
    cmd = captured["args"][-1]
    assert cmd == "test -f '/var/x; id/blobs/bb/" + "b" * 64 + "'"

def test_local_backend_rejects_symlink_escape(tmp_path):
    """A symlinked shard dir that escapes the storage root is refused on
    write (and read) — `realpath` confinement (issue #75)."""
    root = tmp_path / "root"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    b = LocalBackend(str(root))
    data = b"secret-bytes"
    h = _content_hash(data)
    shard = root / "blobs" / h[:2]
    shard.parent.mkdir(parents=True)
    shard.symlink_to(attacker)  # root/blobs/<aa> -> outside the storage root

    with pytest.raises(StorageError):
        b.put(h, data)
