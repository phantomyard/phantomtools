"""Short audit writes must fail closed before the mutation commits."""

from __future__ import annotations

import builtins

import pytest

from phantomdocs import audit


def test_append_rejects_short_write(tmp_path, monkeypatch):
    real_open = builtins.open

    class ShortWriter:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._handle.close()
            return False

        def write(self, data):
            self._handle.write(data[:-1])
            return len(data) - 1

        def flush(self):
            self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

    def fake_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if "ab" in mode:
            return ShortWriter(handle)
        return handle

    monkeypatch.setattr(audit, "open", fake_open, raising=False)

    with pytest.raises(OSError, match="incomplete audit append"):
        audit.append(str(tmp_path), "actor", "add", "urn", "mac", None)

    # The append failed before flush/fsync; the partial bytes are not evidence.
    assert not (tmp_path / "audit.log").read_bytes().endswith(b"\n")
