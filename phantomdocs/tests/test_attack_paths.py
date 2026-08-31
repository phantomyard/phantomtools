"""Adversarial tests for issue #75: the local blob store must not escape its
storage root through a symlinked shard directory or blob path."""

import os

import pytest

from phantomdocs.identity import content_hash as _content_hash
from phantomdocs.storage import LocalBackend, StorageError


def _plant_symlinked_shard(root: str, content_hash: str, target: str) -> None:
    """Replace <root>/blobs/<aa> with a symlink to ``target``."""
    shard = os.path.join(root, "blobs", content_hash[:2])
    if os.path.lexists(shard):
        os.unlink(shard)
    os.symlink(target, shard)


def test_put_refuses_symlinked_shard(tmp_path):
    root = str(tmp_path)
    attacker = os.path.join(root, "attacker")
    os.makedirs(attacker)
    os.makedirs(os.path.join(root, "blobs"))

    b = LocalBackend(root)
    h = _content_hash(b"secret payload")
    _plant_symlinked_shard(root, h, attacker)

    with pytest.raises(StorageError, match="symlinked"):
        b.put(h, b"secret payload")

    # The attacker directory must not receive any data.
    assert os.listdir(attacker) == []


def test_get_refuses_symlinked_shard(tmp_path):
    root = str(tmp_path)
    attacker = os.path.join(root, "attacker")
    os.makedirs(attacker)
    os.makedirs(os.path.join(root, "blobs"))

    # A valid blob lives in the attacker location; a symlinked shard would
    # redirect get() to it.
    h = _content_hash(b"leaked bytes")
    target_blob = os.path.join(attacker, h)
    with open(target_blob, "wb") as f:
        f.write(b"leaked bytes")

    b = LocalBackend(root)
    _plant_symlinked_shard(root, h, attacker)

    with pytest.raises(StorageError, match="symlinked"):
        b.get(h)


def test_has_returns_false_for_symlinked_shard(tmp_path):
    root = str(tmp_path)
    attacker = os.path.join(root, "attacker")
    os.makedirs(attacker)
    os.makedirs(os.path.join(root, "blobs"))

    h = _content_hash(b"x")
    b = LocalBackend(root)
    _plant_symlinked_shard(root, h, attacker)
    assert b.has(h) is False


def test_get_refuses_hardlinked_blob(tmp_path):
    """A blob hardlinked to an arbitrary file (nlink > 1) is refused."""
    root = str(tmp_path)
    b = LocalBackend(root)
    h = _content_hash(b"data")
    b.put(h, b"data")

    blob = os.path.join(root, "blobs", h[:2], h)
    outside = os.path.join(root, "outside-link")
    os.link(blob, outside)  # now nlink == 2
    with pytest.raises(StorageError, match="hardlinked"):
        b.get(h)


def test_get_refuses_symlinked_blob_path(tmp_path):
    """A symlink at the blob address itself must not be followed on read."""
    root = str(tmp_path)
    b = LocalBackend(root)
    h = _content_hash(b"real")
    b.put(h, b"real")

    blob = os.path.join(root, "blobs", h[:2], h)
    os.unlink(blob)
    secret = os.path.join(root, "secret.txt")
    with open(secret, "wb") as f:
        f.write(b"not the blob")
    os.symlink(secret, blob)

    with pytest.raises(StorageError):
        b.get(h)


def test_put_normal_path_still_works(tmp_path):
    """Regression: the confinement must not break the happy path."""
    root = str(tmp_path)
    b = LocalBackend(root)
    h = _content_hash(b"hello")
    b.put(h, b"hello")
    assert b.has(h) is True
    assert b.get(h) == b"hello"
