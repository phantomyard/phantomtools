import pytest

from phantomdocs.storage import (
    GdriveBackend,
    LocalBackend,
    SshBackend,
    StorageError,
    resolve_backend,
)


def test_local_backend_put_get_has(tmp_path):
    b = LocalBackend(str(tmp_path))
    h = "a" * 64
    b.put(h, b"hello")
    assert b.has(h) is True
    assert b.get(h) == b"hello"


def test_local_backend_rejects_bad_hash(tmp_path):
    b = LocalBackend(str(tmp_path))
    with pytest.raises(StorageError):
        b.put("nothex", b"x")


def test_resolve_backend_local():
    b = resolve_backend("local:///tmp/x")
    assert isinstance(b, LocalBackend)
    assert b.root == "/tmp/x"


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


def test_resolve_backend_unknown():
    with pytest.raises(StorageError):
        resolve_backend("ftp://host/x")


def test_ssh_remote_path_validates_hash():
    b = SshBackend(host="h", base="/var/phantomdocs")
    h = "b" * 64
    assert b.remote_path(h) == f"/var/phantomdocs/blobs/{h[:2]}/{h}"
    with pytest.raises(StorageError):
        b.remote_path("short")
