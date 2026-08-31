"""Adversarial tests for issue #73: signed mutations must bind a monotonic
sequence + committed head so a replay or an out-of-order re-insertion fails.

A valid signature over (actor, action, category, owners, locations, urn, mac)
is static: re-submitting it later would still verify cryptographically. The
fix binds ``seq`` + ``prevHead`` into the envelope and checks the chain, so a
mutation authorized for an *older* committed state no longer verifies against
the current one.
"""

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


def _setup(tmp_path):
    """init a namespace and return (root, org_path, nsec_path, secret, pubkey)."""
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace("NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(pubkey)))
    )
    nsec = tmp_path / "nsec.txt"
    nsec.write_text(secret)
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "init",
            "--org",
            "example-org",
            "--namespace",
            "docs",
            "--root",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 0, r.output
    return tmp_path, str(org), str(nsec), secret, pubkey, runner


def _add(runner, root, org, nsec, slug, content):
    doc = root / f"{slug}.txt"
    doc.write_text(content, encoding="utf-8")
    return runner.invoke(
        main,
        [
            "add",
            str(doc),
            "--slug",
            slug,
            "--category",
            "category-2",
            "--owners",
            "ceo",
            "--org-yaml",
            org,
            "--actor",
            "paco",
            "--nsec-file",
            nsec,
            "--root",
            str(root),
        ],
    )


def test_signed_mutation_binds_sequence_and_prev_head(tmp_path):
    """New signed nodes carry seq + prevHead and verify cleanly."""
    root, org, nsec, _s, _p, runner = _setup(tmp_path)
    assert _add(runner, root, org, nsec, "a", "v1").exit_code == 0
    assert _add(runner, root, org, nsec, "b", "v1").exit_code == 0

    data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    nodes = [n for n in data["nodes"] if n.get("kind") == "doc"]
    assert len(nodes) == 2
    assert [n["seq"] for n in nodes] == [1, 2]
    assert nodes[0]["prevHead"] == data["manifest"]["rootMac"]
    assert nodes[1]["prevHead"] == nodes[0]["mac"]
    assert data["manifest"]["headSeq"] == 2
    assert data["manifest"]["headMac"] == nodes[1]["mac"]

    r = runner.invoke(main, ["verify", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output


def test_replay_with_stale_seq_detected(tmp_path):
    """A mutation whose ``seq`` is not strictly increasing (a replay/rollback
    artifact) is flagged by the sequence check, and — because ``seq`` is bound
    into the signed envelope — also breaks the signature."""
    root, org, nsec, _s, _p, runner = _setup(tmp_path)
    assert _add(runner, root, org, nsec, "a", "v1").exit_code == 0
    assert _add(runner, root, org, nsec, "b", "v1").exit_code == 0

    mp = root / "manifest.yaml"
    data = yaml.safe_load((mp).read_text(encoding="utf-8"))
    docs = [n for n in data["nodes"] if n.get("kind") == "doc"]
    assert len(docs) == 2
    assert [n["seq"] for n in docs] == [1, 2]
    # Replay artifact: set the last node's seq back to 1 (non-increasing).
    docs[1]["seq"] = 1
    with open(mp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    r = runner.invoke(main, ["verify", "--org-yaml", org, "--root", str(root)])
    assert r.exit_code != 0
    assert "seq" in r.output or "signature" in r.output


def test_replayed_node_with_stale_prev_head_detected(tmp_path):
    """Re-pointing a signed node's prevHead at an older head (after a
    rollback) breaks prevHead chaining and the signature — both are flagged."""
    root, org, nsec, _s, _p, runner = _setup(tmp_path)
    assert _add(runner, root, org, nsec, "a", "v1").exit_code == 0
    assert _add(runner, root, org, nsec, "b", "v1").exit_code == 0

    mp = root / "manifest.yaml"
    data = yaml.safe_load((mp).read_text(encoding="utf-8"))
    docs = [n for n in data["nodes"] if n.get("kind") == "doc"]
    assert len(docs) == 2
    # Corrupt docs[1].prevHead to point at the root instead of docs[0].mac:
    # the signature is now over a different (still-plausible) field value, so
    # the cryptographic check must also fail.
    docs[1]["prevHead"] = data["manifest"]["rootMac"]
    with open(mp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    r = runner.invoke(main, ["verify", "--org-yaml", org, "--root", str(root)])
    assert r.exit_code != 0
    assert "prevHead" in r.output or "signature" in r.output


def test_envelope_without_sequence_still_verifies_for_legacy(tmp_path):
    """An envelope with no seq/prevHead (legacy path) is unchanged: the
    signature is built and checked without those fields, preserving v1
    compatibility."""
    mac = "ab" * 32
    env = signing.mutation_envelope(
        mac=mac,
        actor="paco",
        action="add",
        category="category-2",
        owners=["ceo"],
        locations=None,
        urn="urn:ex:doc:x",
    )
    sig = signing.sign_mutation("cd" * 32, env)
    pub = signing.pubkey_from_nsec("cd" * 32)
    assert signing.verify_mutation(pub, sig, env) is True
