"""Adversarial tests for issue #74: transactional manifest+audit commit and
atomic blob writes.

The invariant: a mutation commits its audit entry and its manifest update as
one ordered transaction (audit first, manifest second, both under the
inter-process lock), so a crash leaves a *detectable* divergence — never a
committed manifest mutation with no matching audit entry (lost evidence).
"""

import os
import subprocess
import sys

import pytest
import yaml
from click.testing import CliRunner

from phantomdocs import audit as audit_mod
from phantomdocs import identity
from phantomdocs.cli import main

ORG_YAML = """\
version: 1
policies:
  access_levels:
    level-2: { label: Operative, categories: [1, 2] }
  security_categories:
    category-1: { label: Public }
roles:
  - id: cfo
    access_level: level-2
    security_exceptions: []
actors:
  - id: roberto
    role: cfo
    actor_exceptions: []
"""


def _run(args, env=None, actor="roberto"):
    from unittest import mock

    runner = CliRunner()
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    with mock.patch("phantomdocs.cli._os_actor", return_value=actor):
        return runner.invoke(main, args, env=full_env)


def _org(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG_YAML, encoding="utf-8")
    return str(p)


def _init(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    return root, org


def test_audit_head_anchor_recorded(tmp_path):
    """Every mutation advances auditSeq/auditHead in the manifest header."""
    root, org = _init(tmp_path)
    for i in range(3):
        doc = tmp_path / f"d{i}.txt"
        doc.write_text(f"content {i}", encoding="utf-8")
        assert (
            _run(
                [
                    "add",
                    str(doc),
                    "--slug",
                    f"d{i}.txt",
                    "--owners",
                    "cfo",
                    "--org-yaml",
                    org,
                    "--root",
                    root,
                ]
            ).exit_code
            == 0
        )
    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    header = data["manifest"]
    # init (1) + 3 mutations = 4 audit entries.
    assert header["auditSeq"] == 4
    assert header["headSeq"] == 3
    count, head_hash = audit_mod.head(root)
    assert count == 4
    assert head_hash == header["auditHead"]


def test_truncated_audit_tail_detected(tmp_path):
    """Deleting the last N audit lines leaves a valid `prev` chain but is
    detected by the count + head anchor (issue #71 tail truncation)."""
    root, org = _init(tmp_path)
    for i in range(4):
        doc = tmp_path / f"d{i}.txt"
        doc.write_text(f"c{i}", encoding="utf-8")
        assert (
            _run(
                [
                    "add",
                    str(doc),
                    "--slug",
                    f"d{i}.txt",
                    "--owners",
                    "cfo",
                    "--org-yaml",
                    org,
                    "--root",
                    root,
                ]
            ).exit_code
            == 0
        )
    # Baseline verify passes.
    assert _run(["verify", "--root", root]).exit_code == 0

    # Truncate the last 2 lines (5 -> 3 entries): the surviving `prev` chain
    # is intact, but the count/head no longer match the manifest record.
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    audit_path.write_text("".join(lines[:-2]), encoding="utf-8")

    r = _run(["verify", "--root", root])
    assert r.exit_code != 0
    assert "entries found" in r.output or "auditHead" in r.output


def test_audit_ahead_of_manifest_detected(tmp_path):
    """Simulate a crash between audit append and manifest commit: an extra
    audit entry with no manifest mutation must be flagged (issue #74)."""
    root, org = _init(tmp_path)
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "a.txt",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

    # Simulate the orphan audit entry: append one directly (no manifest update).
    audit_mod.append(root, "roberto", "add", "urn:demo:doc:ghost", "ab" * 32, None)

    r = _run(["verify", "--root", root])
    assert r.exit_code != 0
    assert "entries found" in r.output


def test_concurrent_adds_keep_audit_chain_intact(tmp_path):
    """Concurrent `pd add` must not break the audit `prev` chain (issue #74:
    audit append was previously outside the manifest lock)."""
    import pwd

    root = str(tmp_path)
    user = pwd.getpwuid(os.getuid()).pw_name
    org_p = tmp_path / "org.yaml"
    org_p.write_text(
        "version: 1\n"
        "policies:\n"
        "  access_levels:\n"
        "    level-2: { label: Operative, categories: [1, 2] }\n"
        "  security_categories:\n"
        "    category-1: { label: Public }\n"
        "roles:\n"
        "  - id: cfo\n"
        "    access_level: level-2\n"
        "    security_exceptions: []\n"
        "actors:\n"
        f"  - id: {user}\n"
        "    role: cfo\n"
        "    actor_exceptions: []\n",
        encoding="utf-8",
    )
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    procs = []
    for i in range(6):
        doc = tmp_path / f"doc{i}.txt"
        doc.write_text(f"c{i}", encoding="utf-8")
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "phantomdocs.cli",
                    "add",
                    str(doc),
                    "--slug",
                    f"doc{i}.txt",
                    "--owners",
                    "cfo",
                    "--org-yaml",
                    str(org_p),
                    "--root",
                    root,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        )
    for p in procs:
        _, err = p.communicate()
        assert p.returncode == 0, err

    # The audit chain must be intact (no broken `prev` links).
    assert audit_mod.verify_chain(root) == []
    # And the head anchor must match the actual log.
    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    count, head_hash = audit_mod.head(root)
    assert count == data["manifest"]["auditSeq"]
    assert head_hash == data["manifest"]["auditHead"]


def test_local_put_is_atomic_no_partial_blob(tmp_path):
    """LocalBackend.put writes via temp + rename: a successful put leaves no
    temp files and the content-addressable path holds the full bytes."""
    from phantomdocs.storage import LocalBackend

    b = LocalBackend(str(tmp_path))
    h = identity.content_hash(b"hello world")
    b.put(h, b"hello world")
    # No leftover temp files in the blob shard directory.
    shard = os.path.join(str(tmp_path), "blobs", h[:2])
    leftovers = [f for f in os.listdir(shard) if f.startswith(".blob-")]
    assert leftovers == []
    assert b.get(h) == b"hello world"


def test_local_put_crash_leaves_no_partial_blob(tmp_path, monkeypatch):
    """If the write fails mid-way, the content-addressable path must not
    contain a partial blob (temp file is cleaned up)."""
    from phantomdocs.storage import LocalBackend

    b = LocalBackend(str(tmp_path))
    h = identity.content_hash(b"hello world")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fdopen", boom)
    with pytest.raises(OSError):
        b.put(h, b"hello world")

    # The blob path must not exist, and no temp file may remain.
    shard = os.path.join(str(tmp_path), "blobs", h[:2])
    assert not os.path.exists(b.blob_path(h))
    if os.path.isdir(shard):
        leftovers = [f for f in os.listdir(shard) if f.startswith(".blob-")]
        assert leftovers == []
