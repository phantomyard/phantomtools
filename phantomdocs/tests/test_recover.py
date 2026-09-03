"""Recovery protocol tests (audit #7): `pd recover` re-aligns the audit log with
the manifest after an interrupted audit-first transaction.

The only recoverable crash state is *audit ahead of manifest* — orphaned audit
entries whose manifest commit never landed. Every other divergence (manifest
ahead, a broken hash chain, a head-hash mismatch) is evidence of tampering and
is refused fail-closed.
"""

import json
import os

import coincurve
import pytest
import yaml
from click.testing import CliRunner

from phantomdocs import audit, signing
from phantomdocs.cli import main

ORG = """\
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2:
      categories: [1, 2]
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    actor_exceptions: []
"""


def _setup(tmp_path, n_docs=2):
    org_secret = coincurve.PrivateKey().secret.hex()
    org_pubkey = signing.pubkey_from_nsec(org_secret)
    org = tmp_path / "org.yaml"
    org.write_text(ORG, encoding="utf-8")

    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "init",
            "--org",
            "example-org",
            "--namespace",
            "docs",
            "--org-pubkey",
            org_pubkey,
            "--root",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 0, r.output

    for i in range(n_docs):
        doc = tmp_path / f"d{i}.txt"
        doc.write_text(f"content {i}", encoding="utf-8")
        r = runner.invoke(
            main,
            [
                "add",
                str(doc),
                "--slug",
                f"d{i}.txt",
                "--category",
                "category-2",
                "--owners",
                "ceo",
                "--org-yaml",
                str(org),
                "--actor",
                "paco",
                "--root",
                str(tmp_path),
            ],
        )
        assert r.exit_code == 0, r.output

    return str(tmp_path), str(org), runner


def _orphan(root, seq):
    """Simulate a crash: append an audit entry whose manifest commit was lost."""
    audit.append(
        root, "paco", "add", "urn:example-org:doc:ghost", "ab" * 32, None, seq=seq
    )


def _manifest(tmp_path):
    return yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))


def test_recover_audit_ahead_by_one(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    _orphan(root, seq=3)  # crash between audit append and manifest commit
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code == 0, r.output
    assert "discarded 1 orphaned" in r.output

    data = _manifest(tmp_path)
    count, _h = audit.head(root)
    assert count == data["manifest"]["auditSeq"]
    # The namespace now verifies cleanly.
    r = runner.invoke(main, ["verify", "--root", root])
    assert r.exit_code == 0, r.output


def test_recover_audit_ahead_by_n(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    _orphan(root, seq=3)
    _orphan(root, seq=4)
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code == 0, r.output
    assert "discarded 2 orphaned" in r.output


def test_recover_nothing_to_do(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code == 0, r.output
    assert "nothing to recover" in r.output


def test_recover_refuses_manifest_ahead(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    # Truncate the audit log: the manifest is now ahead (tampering, not crash).
    audit.truncate(root, len(audit.raw_lines(root)) - 1)
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code != 0
    assert "missing" in r.output


def test_recover_refuses_broken_chain(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    entry = json.loads(lines[1])
    entry["action"] = "tampered"
    lines[1] = json.dumps(entry, sort_keys=True) + "\n"
    audit_path.write_text("".join(lines), encoding="utf-8")
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code != 0
    assert "hash chain is broken" in r.output


def test_recover_refuses_head_mismatch(tmp_path):
    root, _org, runner = _setup(tmp_path, n_docs=2)
    _orphan(root, seq=3)
    # Corrupt the manifest's recorded head so the orphan tail does not chain
    # cleanly off it.
    mp = tmp_path / "manifest.yaml"
    data = _manifest(tmp_path)
    data["manifest"]["auditHead"] = "ff" * 32
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = runner.invoke(main, ["recover", "--root", root])
    assert r.exit_code != 0
    assert "refusing to recover" in r.output


def test_truncate_is_atomic_replace(tmp_path, monkeypatch):
    """truncate rewrites via temp-file → fsync → atomic replace (audit #2).

    A crash mid-rewrite must not corrupt or shorten the audit log beyond the
    intended ``keep`` prefix: the original file stays intact until the whole
    replacement is written and fsynced, and no stray temp file is left behind.
    """
    root, _org, _runner = _setup(tmp_path, n_docs=2)
    audit_path = tmp_path / "audit.log"
    before = audit_path.read_bytes()
    keep = len(audit.raw_lines(root)) - 1  # drop the last line

    # Simulate a crash *inside* the atomic replace: make os.replace raise, so
    # the temp file is written but never swaps in. The original must survive
    # byte-for-byte and no .audit-*.tmp leftover may remain.
    real_replace = os.replace

    def _boom(src, dst):
        raise OSError("simulated power loss during replace")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        audit.truncate(root, keep)
    monkeypatch.setattr(os, "replace", real_replace)

    assert audit_path.read_bytes() == before
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".audit-")]
    assert leftovers == []

    # A successful truncate keeps exactly `keep` lines and leaves no temp file.
    audit.truncate(root, keep)
    assert len(audit.raw_lines(root)) == keep
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".audit-")]
    assert leftovers == []
