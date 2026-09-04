"""Directory durability tests (audit finding #3).

After ``os.replace``, the parent directory must be fsynced so a power loss
cannot lose the rename. ``fsync_dir`` is best-effort: a commit must never fail
because the platform cannot honor the directory sync. These tests pin that
contract — the directory is synced on POSIX, and a fsync failure (or a
non-POSIX platform) does not break the atomic write path.
"""

from __future__ import annotations

import os
from unittest import mock

from phantomdocs import identity, manifest
from phantomdocs.fsutil import fsync_dir
from phantomdocs.identity import content_hash
from phantomdocs.storage import LocalBackend


def test_fsync_dir_is_noop_when_directory_open_fails(tmp_path, monkeypatch):
    """If the directory cannot be opened, fsync_dir returns without raising."""
    monkeypatch.setattr(os, "name", "posix")
    real_open = os.open

    def _open(path, *args, **kwargs):
        if path == str(tmp_path):
            raise OSError("cannot open directory")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _open)
    fsync_dir(str(tmp_path))  # must not raise


def test_fsync_dir_swallows_fsync_failure(tmp_path, monkeypatch):
    """A fsync failure on the directory is swallowed (best-effort)."""
    monkeypatch.setattr(os, "name", "posix")

    def _fsync(fd):
        raise OSError("EIO on directory fsync")

    monkeypatch.setattr(os, "fsync", _fsync)
    fsync_dir(str(tmp_path))  # must not raise


def test_fsync_dir_noop_on_non_posix(tmp_path, monkeypatch):
    """On non-POSIX, fsync_dir does nothing (and must not open the dir)."""
    monkeypatch.setattr(os, "name", "nt")
    with mock.patch("os.open") as opened:
        fsync_dir(str(tmp_path))
    opened.assert_not_called()


def test_manifest_save_fsyncs_directory(tmp_path, monkeypatch):
    """manifest.save() syncs the parent directory after the atomic replace."""
    monkeypatch.setattr(os, "name", "posix")
    calls: list[str] = []

    real_open = os.open

    def _open(path, *args, **kwargs):
        if path == str(tmp_path):
            calls.append("open-dir")
        return real_open(path, *args, **kwargs)

    def _fsync(fd):
        calls.append("fsync")

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr(os, "fsync", _fsync)

    mac = identity.root_mac("org", "", "docs")
    manifest.save(
        os.path.join(str(tmp_path), "manifest.yaml"),
        manifest.empty_manifest("org", "docs", mac),
    )
    # The file fsync plus the directory open+fsync both happened.
    assert calls.count("fsync") >= 2
    assert "open-dir" in calls


def test_blob_put_fsyncs_shard_directory(tmp_path, monkeypatch):
    """LocalBackend.put() syncs the shard directory after the atomic rename."""
    monkeypatch.setattr(os, "name", "posix")
    backend = LocalBackend(str(tmp_path))
    h = content_hash(b"payload")
    calls: list[int] = []

    real_fsync = os.fsync

    def _fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fsync)
    backend.put(h, b"payload")
    # At least the blob file and the shard directory were fsynced.
    assert len(calls) >= 2
    assert backend.get(h) == b"payload"
