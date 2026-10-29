"""Reconcile must validate its checkpoint before touching the audit log."""

from __future__ import annotations

import pytest

from phantomdocs import audit


def _append(root):
    audit.append(str(root), "actor", "add", "urn:test", "mac", None)


def test_reconcile_accepts_empty_checkpoint(tmp_path):
    assert audit.reconcile(str(tmp_path), 0, None) == 0


def test_reconcile_accepts_matching_head(tmp_path):
    _append(tmp_path)
    count, head = audit.head(str(tmp_path))
    assert audit.reconcile(str(tmp_path), count, head) == 0


@pytest.mark.parametrize("bad_count", [True, False, -1, 1.0, "1", None])
def test_reconcile_rejects_invalid_count_without_touching_log(tmp_path, bad_count):
    _append(tmp_path)
    before = (tmp_path / "audit.log").read_bytes()
    with pytest.raises(ValueError, match="non-negative integer"):
        audit.reconcile(str(tmp_path), bad_count, None)
    assert (tmp_path / "audit.log").read_bytes() == before


@pytest.mark.parametrize("bad_head", [None, "", "0" * 63, "g" * 64, "A" * 64])
def test_reconcile_rejects_invalid_nonempty_head_without_truncating(tmp_path, bad_head):
    _append(tmp_path)
    _append(tmp_path)
    before = (tmp_path / "audit.log").read_bytes()
    with pytest.raises(ValueError, match="valid head hash"):
        audit.reconcile(str(tmp_path), 1, bad_head)
    assert (tmp_path / "audit.log").read_bytes() == before


def test_reconcile_rejects_non_null_head_for_empty_checkpoint(tmp_path):
    _append(tmp_path)
    before = (tmp_path / "audit.log").read_bytes()
    with pytest.raises(ValueError, match="null head"):
        audit.reconcile(str(tmp_path), 0, "0" * 64)
    assert (tmp_path / "audit.log").read_bytes() == before
