"""Integrated security/chaos suite over the combined phantomdocs tree (#90-#97).

This suite attacks the *composed* protocol — the full pipeline

    authorize -> sign(seq, prevHead, actor, ts) -> write blob -> write audit
              -> commit manifest -> update head -> seal

rather than individual mechanisms, per the August 28 security audit's point 1.
It covers:

  * point 1 — composed crash / rollback / substitution states;
  * point 2 — seq/prevHead protocol semantics;
  * point 3 — external seal / key trust model;
  * point 4 — manifest/audit/seal anchor combination matrix.

Tests marked ``xfail`` document a *known gap*: a property the combined tree
does not yet enforce. They are the concrete output of this audit phase and
must be resolved (or explicitly accepted as a policy decision) before the
gate in the audit's GO/NO-GO section can clear.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import coincurve
import pytest
import yaml
from click.testing import CliRunner

from phantomdocs import signing
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
    npub: NPUB_PLACEHOLDER
    actor_exceptions: []
"""


def _bech32_encode(hrp, data):
    from phantomdocs.signing import (
        _BECH32_CHARSET,
        _bech32_expand,
        _bech32_polymod,
        _convertbits,
    )

    values = _convertbits(list(data), 8, 5)
    checksum = [0] * 6
    poly = _bech32_polymod(_bech32_expand(hrp) + values + checksum) ^ 1
    for i in range(6):
        checksum[i] = (poly >> (5 * (5 - i))) & 31
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in values + checksum)


def _npub(pubkey_hex):
    return _bech32_encode("npub", bytes.fromhex(pubkey_hex))


def _setup(tmp_path, n_docs=2, seal=True, sign=True):
    """init + add N docs (signed) + seal, returning a context dict."""
    actor_secret = coincurve.PrivateKey().secret.hex()
    actor_pubkey = signing.pubkey_from_nsec(actor_secret)
    org_secret = coincurve.PrivateKey().secret.hex()
    org_pubkey = signing.pubkey_from_nsec(org_secret)

    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace("NPUB_PLACEHOLDER", _npub(actor_pubkey)), encoding="utf-8"
    )
    actor_nsec = tmp_path / "actor.nsec"
    actor_nsec.write_text(actor_secret)
    os.chmod(actor_nsec, 0o600)
    org_nsec = tmp_path / "org.nsec"
    org_nsec.write_text(org_secret)

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
        _add_doc(runner, tmp_path, ctx_of(org, actor_nsec), i)

    if seal:
        r = runner.invoke(
            main, ["seal", "--nsec-file", str(org_nsec), "--root", str(tmp_path)]
        )
        assert r.exit_code == 0, r.output

    return {
        "root": str(tmp_path),
        "org": str(org),
        "org_pubkey": org_pubkey,
        "actor_nsec": str(actor_nsec),
        "actor_secret": actor_secret,
        "actor_pubkey": actor_pubkey,
        "org_nsec": str(org_nsec),
        "runner": runner,
        "tmp": tmp_path,
    }


def ctx_of(org, actor_nsec):
    return {"org": str(org), "nsec": str(actor_nsec)}


def _add_doc(runner, tmp_path, cc, i, sign=True):
    doc = tmp_path / f"d{i}.txt"
    doc.write_text(f"content {i}", encoding="utf-8")
    args = [
        "add",
        str(doc),
        "--slug",
        f"d{i}.txt",
        "--category",
        "category-2",
        "--owners",
        "ceo",
        "--org-yaml",
        cc["org"],
        "--actor",
        "paco",
        "--root",
        str(tmp_path),
    ]
    if sign:
        args += ["--nsec-file", cc["nsec"]]
    r = runner.invoke(main, args)
    assert r.exit_code == 0, r.output
    return r


def _add_more(ctx, i):
    doc = ctx["tmp"] / f"d{i}.txt"
    doc.write_text(f"content {i}", encoding="utf-8")
    r = ctx["runner"].invoke(
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
            ctx["org"],
            "--actor",
            "paco",
            "--nsec-file",
            ctx["actor_nsec"],
            "--root",
            ctx["root"],
        ],
    )
    assert r.exit_code == 0, r.output


def _manifest(tmp_path):
    return yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))


def _write_manifest(tmp_path, data):
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


def _doc_nodes(data):
    return [n for n in data["nodes"] if n.get("kind") == "doc"]


def _verify(ctx, extra=None, with_org_yaml=False):
    args = ["verify", "--org-pubkey", ctx["org_pubkey"], "--root", ctx["root"]]
    if with_org_yaml:
        args = [
            "verify",
            "--org-yaml",
            ctx["org"],
            "--org-pubkey",
            ctx["org_pubkey"],
            "--root",
            ctx["root"],
        ]
    if extra:
        args += extra
    return ctx["runner"].invoke(main, args)


def _snapshot(tmp_path):
    """Copy manifest + audit + blobs (the whole mutable state)."""
    snap = Path(tempfile.mkdtemp(prefix="pd-snap-"))
    for name in ("manifest.yaml", "audit.log"):
        src = tmp_path / name
        if src.exists():
            shutil.copy2(src, snap / name)
    blobs = tmp_path / "blobs"
    if blobs.exists():
        shutil.copytree(blobs, snap / "blobs")
    return snap


def _restore(tmp_path, snap):
    for name in ("manifest.yaml", "audit.log"):
        src = snap / name
        if src.exists():
            shutil.copy2(src, tmp_path / name)
    blobs = tmp_path / "blobs"
    shutil.rmtree(blobs, ignore_errors=True)
    if (snap / "blobs").exists():
        shutil.copytree(snap / "blobs", blobs)


def _resign_node(node, actor_secret, actor_pubkey):
    """Re-sign a tampered node with the actor key (a keyed-but-buggy writer or
    migration), so the signature stays valid while the field under test changes."""
    env = signing.mutation_envelope(
        mac=node["mac"],
        actor=node.get("actor", ""),
        action=node.get("action", ""),
        category=node.get("category", ""),
        owners=node.get("owners"),
        locations=node.get("locations"),
        urn=node.get("urn", ""),
        seq=node.get("seq"),
        prev_head=node.get("prevHead"),
        ts=node.get("ts"),
    )
    node["sig"] = signing.sign_mutation(actor_secret, env)
    node["sigPubkey"] = actor_pubkey


# ---------------------------------------------------------------------------
# Point 1 — composed crash / rollback / substitution states
# ---------------------------------------------------------------------------


def test_crash_after_manifest_but_before_seal_detected(tmp_path):
    """A mutation committed but not yet re-sealed is flagged when the org
    pubkey is supplied (head advanced past the last seal)."""
    ctx = _setup(tmp_path, n_docs=2, seal=True)
    _add_more(ctx, 2)  # headSeq advances to 3; no re-seal
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "seal" in r.output


def test_crash_after_audit_but_before_manifest_commit_detected(tmp_path):
    """An audit entry appended without its manifest commit (audit ahead by 1)
    is flagged by the count/head anchor check."""
    ctx = _setup(tmp_path, n_docs=2, seal=False)
    # Simulate: append a mutation audit line without committing the manifest.
    from phantomdocs import audit

    audit.append(
        ctx["root"],
        "paco",
        "add",
        "urn:example-org:doc:ghost",
        "ab" * 32,
        None,
        seq=3,
    )
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_blob_substitution_after_valid_mutation_detected(tmp_path):
    """Replacing the blob bytes behind a valid signed node is detected
    (content hash mismatch)."""
    ctx = _setup(tmp_path, n_docs=1, seal=False)
    data = _manifest(tmp_path)
    node = _doc_nodes(data)[0]
    ch = node["contentHash"]
    blob = tmp_path / "blobs" / ch[:2] / ch
    assert blob.exists()
    blob.write_bytes(b"substituted content of a different identity")
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "content hash" in r.output


def test_corrupted_storage_with_valid_signature_detected(tmp_path):
    """Corrupting stored bytes while the manifest signature stays valid is
    detected via the content-hash / MAC recomputation."""
    ctx = _setup(tmp_path, n_docs=1, seal=False)
    data = _manifest(tmp_path)
    node = _doc_nodes(data)[0]
    blob = tmp_path / "blobs" / node["contentHash"][:2] / node["contentHash"]
    raw = blob.read_bytes()
    blob.write_bytes(raw[: len(raw) // 2] + b"\x00" * (len(raw) - len(raw) // 2))
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "content hash" in r.output


def test_valid_storage_with_invalid_signature_detected(tmp_path):
    """Valid storage but a tampered signature is detected (crypto check)."""
    ctx = _setup(tmp_path, n_docs=1, seal=False)
    data = _manifest(tmp_path)
    node = _doc_nodes(data)[0]
    node["sig"] = "ff" * 64
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "signature" in r.output


# ---------------------------------------------------------------------------
# Point 2 — seq/prevHead semantics
# ---------------------------------------------------------------------------


def test_duplicate_seq_detected(tmp_path):
    ctx = _setup(tmp_path, n_docs=2, seal=False)
    data = _manifest(tmp_path)
    docs = _doc_nodes(data)
    docs[1]["seq"] = docs[0]["seq"]
    _resign_node(docs[1], ctx["actor_secret"], ctx["actor_pubkey"])
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "seq" in r.output


def test_seq_reset_detected(tmp_path):
    ctx = _setup(tmp_path, n_docs=3, seal=False)
    data = _manifest(tmp_path)
    docs = _doc_nodes(data)
    docs[-1]["seq"] = 1  # reset back to 1
    _resign_node(docs[-1], ctx["actor_secret"], ctx["actor_pubkey"])
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "seq" in r.output


def test_prevhead_wrong_seq_correct_detected(tmp_path):
    ctx = _setup(tmp_path, n_docs=2, seal=False)
    data = _manifest(tmp_path)
    docs = _doc_nodes(data)
    docs[1]["prevHead"] = data["manifest"]["rootMac"]  # wrong: should be docs[0].mac
    _resign_node(docs[1], ctx["actor_secret"], ctx["actor_pubkey"])
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "prevHead" in r.output or "sequence" in r.output


def test_seq_drift_from_audit_detected(tmp_path):
    """A node whose ``seq`` (and the manifest ``headSeq``) drift ahead of the
    authoritative audit log — a gap 1 -> 3 with a *valid* signature — is caught
    because ``headSeq`` must agree with the audit log's max mutation seq."""
    ctx = _setup(tmp_path, n_docs=2, seal=False)
    data = _manifest(tmp_path)
    docs = _doc_nodes(data)
    docs[1]["seq"] = 3  # gap: 1 -> 3, seq 2 missing
    _resign_node(docs[1], ctx["actor_secret"], ctx["actor_pubkey"])
    data["manifest"]["headSeq"] = 3
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "headSeq" in r.output or "audit" in r.output


def test_audit_seq_gap_detected(tmp_path):
    """A gap in the audit log's own ``seq`` field (0, 1, 4) is flagged as
    non-contiguous by ``audit.sequence_issues``."""
    from phantomdocs import audit

    ctx = _setup(tmp_path, n_docs=2, seal=False)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Only the tail entry is changed, so no downstream ``prev`` references it;
    # this isolates the seq-contiguity check from the hash-chain check.
    entry = json.loads(lines[-1])
    entry["seq"] = 4  # gap: 0, 1, 4
    lines[-1] = json.dumps(entry, sort_keys=True) + "\n"
    audit_path.write_text("".join(lines), encoding="utf-8")

    problems = audit.sequence_issues(ctx["root"])
    assert any("contiguous" in p for p in problems)


@pytest.mark.xfail(
    reason=(
        "KNOWN GAP: an attacker who can rewrite the whole repository can roll "
        "back to an older *internally consistent* sealed state (old manifest + "
        "old audit + old seal) and verify passes — the seal is self-referential "
        "(sealedHeadSeq lives in the same manifest the attacker rewrites). Only "
        "--expected-head-seq (an external anchor) catches it, and it is optional."
    ),
    strict=False,
)
def test_whole_state_rollback_undetected_without_external_anchor(tmp_path):
    ctx = _setup(tmp_path, n_docs=2, seal=True)
    snap = _snapshot(tmp_path)  # state at headSeq=2, sealed

    # Legitimately advance to headSeq=3, then re-seal.
    _add_more(ctx, 2)
    r = ctx["runner"].invoke(
        main, ["seal", "--nsec-file", ctx["org_nsec"], "--root", ctx["root"]]
    )
    assert r.exit_code == 0, r.output

    # Attack: roll the whole state back to the older snapshot.
    _restore(tmp_path, snap)

    # Desired: verify must fail. Currently it passes (internally consistent).
    r = _verify(ctx)
    assert r.exit_code != 0


def test_whole_state_rollback_detected_with_external_anchor(tmp_path):
    """The same rollback is caught when the operator supplies the externally
    known head sequence."""
    ctx = _setup(tmp_path, n_docs=2, seal=True)
    snap = _snapshot(tmp_path)

    _add_more(ctx, 2)
    r = ctx["runner"].invoke(
        main, ["seal", "--nsec-file", ctx["org_nsec"], "--root", ctx["root"]]
    )
    assert r.exit_code == 0, r.output

    _restore(tmp_path, snap)

    r = _verify(ctx, extra=["--expected-head-seq", "3"])
    assert r.exit_code != 0
    assert "rolled back" in r.output


# ---------------------------------------------------------------------------
# Point 3 — external seal / key trust model
# ---------------------------------------------------------------------------


def test_manifest_seal_pubkey_swapped_detected(tmp_path):
    ctx = _setup(tmp_path, n_docs=1, seal=True)
    data = _manifest(tmp_path)
    data["manifest"]["sealPubkey"] = "ab" * 32
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "seal" in r.output


def test_wrong_org_pubkey_detected(tmp_path):
    ctx = _setup(tmp_path, n_docs=1, seal=True)
    wrong = signing.pubkey_from_nsec(coincurve.PrivateKey().secret.hex())
    r = ctx["runner"].invoke(
        main, ["verify", "--org-pubkey", wrong, "--root", ctx["root"]]
    )
    assert r.exit_code != 0
    assert "seal" in r.output or "rootMac" in r.output


def test_org_yaml_actor_npub_swapped_detected(tmp_path):
    """If the attacker swaps an actor's declared npub in org.yaml, the mutation
    signature no longer maps to a valid key for that actor."""
    ctx = _setup(tmp_path, n_docs=1, seal=True)
    other = signing.pubkey_from_nsec(coincurve.PrivateKey().secret.hex())
    org = tmp_path / "org.yaml"
    org.write_text(ORG.replace("NPUB_PLACEHOLDER", _npub(other)), encoding="utf-8")
    r = _verify(ctx, with_org_yaml=True)
    assert r.exit_code != 0
    assert "signature" in r.output


# ---------------------------------------------------------------------------
# Point 4 — manifest/audit/seal anchor combination matrix
# ---------------------------------------------------------------------------


def test_manifest_new_audit_old_detected(tmp_path):
    """manifest=new, audit=old (audit rolled back behind the manifest)."""
    ctx = _setup(tmp_path, n_docs=3, seal=True)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    audit_path.write_text("".join(lines[:-1]), encoding="utf-8")
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_manifest_old_audit_new_detected(tmp_path):
    """manifest=old, audit=new (audit ahead of the manifest)."""
    ctx = _setup(tmp_path, n_docs=3, seal=True)
    data = _manifest(tmp_path)
    data["nodes"] = data["nodes"][:-1]
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0


def test_manifest_new_seal_old_detected(tmp_path):
    """manifest=new, seal=old (head advanced past the last seal)."""
    ctx = _setup(tmp_path, n_docs=2, seal=True)
    _add_more(ctx, 2)  # head advances, seal stays behind
    r = _verify(ctx)
    assert r.exit_code != 0
    assert "seal" in r.output


def test_manifest_old_seal_new_detected(tmp_path):
    """manifest=old, seal=new (manifest rolled back; seal envelope no longer
    matches the stored signature)."""
    ctx = _setup(tmp_path, n_docs=3, seal=True)
    data = _manifest(tmp_path)
    data["nodes"] = data["nodes"][:-1]
    _write_manifest(tmp_path, data)
    r = _verify(ctx)
    assert r.exit_code != 0
