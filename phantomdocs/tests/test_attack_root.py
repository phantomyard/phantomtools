"""Adversarial tests for issues #70 (root anchor) and #71 (audit head anchor).

The org seals the namespace head (rootMac + headSeq + headMac + auditSeq +
auditHead) with the org key; `verify --org-pubkey` recomputes the root MAC from
the org identity and verifies the seal. A forged root, a deleted version, or a
rolled-back/truncated audit head all change the sealed envelope and must fail.
"""

from pathlib import Path

import coincurve
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


def _setup(tmp_path, n_docs=3):
    """init + add N docs + seal, returning (root, org_path, org_pubkey_hex,
    org_npub, org_nsec_path, runner)."""
    org_secret = coincurve.PrivateKey().secret.hex()
    org_pubkey = signing.pubkey_from_nsec(org_secret)
    org_npub = _bech32_encode("npub", bytes.fromhex(org_pubkey))
    org_nsec = tmp_path / "org.nsec"
    org_nsec.write_text(org_secret, encoding="utf-8")
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

    r = runner.invoke(
        main, ["seal", "--nsec-file", str(org_nsec), "--root", str(tmp_path)]
    )
    assert r.exit_code == 0, r.output
    return str(tmp_path), str(org), org_pubkey, org_npub, str(org_nsec), runner


def _verify(runner, root, org_pubkey, extra=None):
    args = ["verify", "--org-pubkey", org_pubkey, "--root", root]
    if extra:
        args += extra
    return runner.invoke(main, args)


def test_sealed_namespace_verifies(tmp_path):
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 3)
    r = _verify(runner, root, pubkey)
    assert r.exit_code == 0, r.output


def test_forged_root_detected(tmp_path):
    """Replace rootMac and recompute descendants → verify must fail (#70)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 1)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))

    # Forge: swap in a different root and recompute each node's MAC chain off
    # the forged root (an internally coherent graph with a different root).
    forged_root = "ab" * 32
    data["manifest"]["rootMac"] = forged_root

    from phantomdocs import identity

    parent_mac = forged_root
    for node in data["nodes"]:
        if node.get("kind") == "folder":
            node["parentMac"] = parent_mac
            node["mac"] = identity.node_mac(
                parent_mac, identity.component_for_folder(node["slug"])
            )
        else:
            node["parentMac"] = parent_mac
            node["mac"] = identity.doc_version_mac(
                parent_mac, node.get("previous"), node["slug"], b""
            )
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "rootMac does not match" in r.output or "seal" in r.output


def test_delete_latest_version_detected(tmp_path):
    """Delete the latest version → head advances/changes → seal fails (#70)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 3)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    data["nodes"] = data["nodes"][:-1]
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "seal" in r.output or "headMac" in r.output or "headSeq" in r.output


def test_delete_intermediate_version_detected(tmp_path):
    """Delete an intermediate version → version lineage breaks (#70)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 3)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    # Remove the middle node; the version chain/head anchors must catch it.
    data["nodes"].pop(1)
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0


def test_rollback_to_older_copy_detected(tmp_path):
    """Replace the manifest with an older valid copy → --expected-head-seq
    (the external notion of current head) must catch the rollback (#70)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 4)
    mp = tmp_path / "manifest.yaml"
    full = mp.read_text(encoding="utf-8")

    # Simulate an attacker rolling back to an older (still-valid) state: take
    # the full manifest, drop the last node, and fix the head fields so the
    # in-band anchors are internally consistent. The operator knows the head
    # was at 4, so --expected-head-seq 4 catches it.
    data = yaml.safe_load(full)
    data["nodes"] = data["nodes"][:-1]
    data["manifest"]["headSeq"] = len(data["nodes"])
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # Without the external expectation, the seal may still verify (older state
    # is internally coherent) — but with it, the rollback is caught.
    r = _verify(runner, root, pubkey, extra=["--expected-head-seq", "4"])
    assert r.exit_code != 0
    assert "rolled back" in r.output


def test_audit_tail_truncation_detected(tmp_path):
    """Delete the last N audit lines → count/head mismatch (#71)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 4)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    audit_path.write_text("".join(lines[:-3]), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_audit_whole_log_rollback_detected(tmp_path):
    """Replace the audit log with an older copy → count mismatch (#71)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 5)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    # An "older copy": keep only the init + first mutation.
    audit_path.write_text("".join(lines[:2]), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_audit_middle_line_modified_detected(tmp_path):
    """Modify a middle audit line → prev chain breaks (#71)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 4)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    import json as _json

    mid = len(lines) // 2
    entry = _json.loads(lines[mid])
    entry["action"] = "tampered"
    lines[mid] = _json.dumps(entry, sort_keys=True) + "\n"
    audit_path.write_text("".join(lines), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_audit_historical_line_inserted_detected(tmp_path):
    """Insert a valid historical line → prev chain + count mismatch (#71)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 4)
    audit_path = tmp_path / "audit.log"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Insert a duplicate of an earlier (valid) line in the middle.
    lines.insert(len(lines) // 2, lines[1])
    audit_path.write_text("".join(lines), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "audit" in r.output


def test_seal_wrong_key_refused_without_org_authorization(tmp_path):
    """Sealing with a key the org never authorized is refused (#70/#104)."""
    root, _org, _pubkey, _npub, _nsec, runner = _setup(tmp_path, 1)
    attacker_secret = coincurve.PrivateKey().secret.hex()
    attacker_nsec = tmp_path / "attacker.nsec"
    attacker_nsec.write_text(attacker_secret, encoding="utf-8")
    r = runner.invoke(main, ["seal", "--nsec-file", str(attacker_nsec), "--root", root])
    assert r.exit_code != 0
    assert "--org-nsec-file" in r.output


def test_forged_reseal_by_unknown_key_detected(tmp_path):
    """A head seal rewritten by an unauthorized key must not verify (#70)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 1)
    manifest_path = tmp_path / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    m = data["manifest"]

    attacker_secret = coincurve.PrivateKey().secret.hex()
    attacker_pubkey = signing.pubkey_from_nsec(attacker_secret)
    envelope = signing.seal_envelope(
        root_mac=m["rootMac"],
        head_seq=int(m.get("headSeq") or 0),
        head_mac=m.get("headMac") or m["rootMac"],
        audit_seq=int(m.get("auditSeq") or 0),
        audit_head=m.get("auditHead"),
        require_signatures=m.get("requireSignatures"),
        crypto_version=m.get("cryptoVersion"),
    )
    m["signedRootMac"] = signing.sign_seal(attacker_secret, envelope)
    m["sealPubkey"] = attacker_pubkey
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "seal" in r.output


def test_unsealed_manifest_rejected_with_org_pubkey(tmp_path):
    """With --org-pubkey, a namespace that was never sealed is refused (#70)."""
    runner = CliRunner()
    root = str(tmp_path)
    r = runner.invoke(
        main,
        [
            "init",
            "--org",
            "example-org",
            "--namespace",
            "docs",
            "--org-pubkey",
            "ab" * 32,
            "--root",
            root,
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["verify", "--org-pubkey", "ab" * 32, "--root", root])
    assert r.exit_code != 0
    assert "seal" in r.output


# --- seal-key lifecycle: history, rotation, revocation (issue #104) ---


def _mutate(root, org, runner, name="extra.txt"):
    """Advance the namespace head by adding one more document."""
    doc = Path(root) / name
    doc.write_text(f"content {name}", encoding="utf-8")
    r = runner.invoke(
        main,
        [
            "add",
            str(doc),
            "--slug",
            name,
            "--category",
            "category-2",
            "--owners",
            "ceo",
            "--org-yaml",
            org,
            "--actor",
            "paco",
            "--root",
            root,
        ],
    )
    assert r.exit_code == 0, r.output


def _keypair(tmp_path, name):
    """A fresh keypair, its hex pubkey/npub and an nsec file path."""
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    nsec_path = tmp_path / f"{name}.nsec"
    nsec_path.write_text(secret, encoding="utf-8")
    return pubkey, signing.npub_encode(pubkey), str(nsec_path)


def _manifest(tmp_path):
    return yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))


def test_seal_records_identity_and_history(tmp_path):
    """The first seal records the identity key and one history event (#104)."""
    _root, _org, _pubkey, npub, _nsec, _runner = _setup(tmp_path, 1)
    m = _manifest(tmp_path)["manifest"]
    assert m["sealIdentityNpub"] == npub
    events = m["seals"]
    assert len(events) == 1
    assert events[0]["npub"] == npub
    assert events[0]["cs"] == m["sealedHeadSeq"]
    assert events[0]["sig"] == m["signedRootMac"]
    assert m["sealKeys"] == []


def test_rotation_with_org_authorization_verifies(tmp_path):
    """The identity key authorizes a second seal key; verify accepts it."""
    root, org, pubkey, _npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, b_npub, b_nsec = _keypair(tmp_path, "seal-b")

    r = runner.invoke(
        main,
        [
            "seal",
            "--nsec-file",
            b_nsec,
            "--org-nsec-file",
            org_nsec,
            "--root",
            root,
        ],
    )
    assert r.exit_code == 0, r.output
    assert "rotated" in r.output

    m = _manifest(tmp_path)["manifest"]
    assert m["sealPubkey"] == _b_pubkey
    assert m["sealIdentityNpub"] == _npub
    assert [rec["npub"] for rec in m["sealKeys"]] == [b_npub]
    assert len(m["seals"]) == 2

    r = _verify(runner, root, pubkey)
    assert r.exit_code == 0, r.output
    assert "org-authorized seal key" in r.output


def test_rotation_without_org_key_refused(tmp_path):
    """A new seal key is refused unless the identity key authorizes it."""
    root, org, _pubkey, _npub, _org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, _b_npub, b_nsec = _keypair(tmp_path, "seal-b")

    r = runner.invoke(main, ["seal", "--nsec-file", b_nsec, "--root", root])
    assert r.exit_code != 0
    assert "--org-nsec-file" in r.output


def test_rotation_with_wrong_org_key_refused(tmp_path):
    """Only the namespace's own identity key can authorize a seal key."""
    root, org, _pubkey, _npub, _org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, _b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    _c_pubkey, _c_npub, c_nsec = _keypair(tmp_path, "not-the-org")

    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", c_nsec, "--root", root],
    )
    assert r.exit_code != 0
    assert "org identity key" in r.output


def test_authorized_seal_key_keeps_sealing(tmp_path):
    """Once authorized, a seal key re-seals on its own after mutations."""
    root, org, pubkey, _npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner, "one.txt")
    _b_pubkey, _b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", org_nsec, "--root", root],
    )
    assert r.exit_code == 0, r.output

    _mutate(root, org, runner, "two.txt")
    r = runner.invoke(main, ["seal", "--nsec-file", b_nsec, "--root", root])
    assert r.exit_code == 0, r.output
    r = _verify(runner, root, pubkey)
    assert r.exit_code == 0, r.output


def test_unauthorized_seal_key_history_rejected(tmp_path):
    """A seal by a key with no org delegation must not verify (#104)."""
    root, org, pubkey, _npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", org_nsec, "--root", root],
    )
    assert r.exit_code == 0, r.output

    # Drop the org delegation: the seal is now self-attested.
    path = tmp_path / "manifest.yaml"
    data = _manifest(tmp_path)
    data["manifest"]["sealKeys"] = [
        {"npub": b_npub, "valid_from": "2020-01-01T00:00:00Z", "delegation": "00" * 64}
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "seal" in r.output


def test_revoked_seal_key_fails_closed(tmp_path):
    """A seal made at/after the key's revocation is rejected (#104)."""
    root, org, pubkey, _npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", org_nsec, "--root", root],
    )
    assert r.exit_code == 0, r.output
    sealed_ts = _manifest(tmp_path)["manifest"]["seals"][-1]["ts"]

    r = runner.invoke(
        main,
        [
            "revoke-seal-key",
            b_npub,
            "--org-nsec-file",
            org_nsec,
            "--revoked-at",
            sealed_ts,
            "--root",
            root,
        ],
    )
    assert r.exit_code == 0, r.output
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "revoked" in r.output or "validity window" in r.output


def test_identity_key_cannot_be_revoked(tmp_path):
    """The org identity key anchors the namespace; it cannot be revoked (#104)."""
    root, _org, _pubkey, npub, org_nsec, runner = _setup(tmp_path, 1)
    r = runner.invoke(
        main, ["revoke-seal-key", npub, "--org-nsec-file", org_nsec, "--root", root]
    )
    assert r.exit_code != 0
    assert "cannot be revoked" in r.output


def test_revocation_requires_the_org_identity_key(tmp_path):
    """A revocation the org identity key did not sign is refused (#104)."""
    root, org, pubkey, _npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", org_nsec, "--root", root],
    )
    assert r.exit_code == 0, r.output
    _x_pubkey, _x_npub, x_nsec = _keypair(tmp_path, "not-the-org")

    r = runner.invoke(
        main, ["revoke-seal-key", b_npub, "--org-nsec-file", x_nsec, "--root", root]
    )
    assert r.exit_code != 0
    assert "org identity key" in r.output
    # The forged revocation was not applied: the namespace still verifies.
    r = _verify(runner, root, pubkey)
    assert r.exit_code == 0, r.output


def test_identity_mismatch_rejected(tmp_path):
    """A manifest declaring another seal identity must not verify (#104)."""
    root, _org, pubkey, _npub, _nsec, runner = _setup(tmp_path, 1)
    path = tmp_path / "manifest.yaml"
    data = _manifest(tmp_path)
    _other_pubkey, other_npub, _other_nsec = _keypair(tmp_path, "other")
    data["manifest"]["sealIdentityNpub"] = other_npub
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = _verify(runner, root, pubkey)
    assert r.exit_code != 0
    assert "seal identity" in r.output


def test_seal_keys_lists_history(tmp_path):
    """``pd seal-keys`` shows the identity, authorized keys and seals."""
    root, org, _pubkey, npub, org_nsec, runner = _setup(tmp_path, 1)
    _mutate(root, org, runner)
    _b_pubkey, b_npub, b_nsec = _keypair(tmp_path, "seal-b")
    r = runner.invoke(
        main,
        ["seal", "--nsec-file", b_nsec, "--org-nsec-file", org_nsec, "--root", root],
    )
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["seal-keys", "--root", root])
    assert r.exit_code == 0, r.output
    assert npub in r.output
    assert b_npub in r.output
    assert "seals" in r.output
