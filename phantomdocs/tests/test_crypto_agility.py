"""Crypto-agility tests (audit decision 3): the crypto-suite version is part of
the authenticated state, so a future v2 can verify v1 while v1 refuses v2.

- every signed mutation envelope binds ``crypto_version``;
- every head seal binds ``crypto_version`` (and the manifest declares it);
- a node / manifest declaring an unsupported crypto version is refused
  fail-closed (verify cannot interpret the signatures).
"""

import json
import os

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


def _setup(tmp_path, seal=True):
    actor_secret = coincurve.PrivateKey().secret.hex()
    actor_pubkey = signing.pubkey_from_nsec(actor_secret)
    org_secret = coincurve.PrivateKey().secret.hex()
    org_pubkey = signing.pubkey_from_nsec(org_secret)

    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace(
            "NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(actor_pubkey))
        ),
        encoding="utf-8",
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

    doc = tmp_path / "a.txt"
    doc.write_text("hello", encoding="utf-8")
    r = runner.invoke(
        main,
        [
            "add",
            str(doc),
            "--slug",
            "a.txt",
            "--category",
            "category-2",
            "--owners",
            "ceo",
            "--org-yaml",
            str(org),
            "--actor",
            "paco",
            "--nsec-file",
            str(actor_nsec),
            "--root",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 0, r.output

    if seal:
        r = runner.invoke(
            main, ["seal", "--nsec-file", str(org_nsec), "--root", str(tmp_path)]
        )
        assert r.exit_code == 0, r.output

    return {
        "root": str(tmp_path),
        "org": str(org),
        "org_pubkey": org_pubkey,
        "runner": runner,
        "tmp": tmp_path,
    }


def test_mutation_envelope_binds_crypto_version():
    env = signing.mutation_envelope(
        mac="ab" * 32,
        actor="paco",
        action="add",
        category="category-1",
        owners=["ceo"],
        locations=None,
        urn="urn:ex:doc:x",
    )
    payload = json.loads(env.decode("utf-8"))
    assert payload["crypto_version"] == signing.CRYPTO_VERSION


def test_seal_envelope_binds_crypto_version():
    env = signing.seal_envelope(
        root_mac="ab" * 32,
        head_seq=1,
        head_mac="cd" * 32,
        audit_seq=2,
        audit_head="ef" * 32,
    )
    payload = json.loads(env.decode("utf-8"))
    assert payload["crypto_version"] == signing.CRYPTO_VERSION


def test_manifest_declares_crypto_version(tmp_path):
    _setup(tmp_path, seal=False)
    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    assert data["manifest"]["cryptoVersion"] == signing.CRYPTO_VERSION


def test_unsupported_node_crypto_version_rejected(tmp_path):
    ctx = _setup(tmp_path, seal=False)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    node = next(n for n in data["nodes"] if n.get("kind") == "doc")
    node["cryptoVersion"] = 2
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = ctx["runner"].invoke(
        main, ["verify", "--org-yaml", ctx["org"], "--root", ctx["root"]]
    )
    assert r.exit_code != 0
    assert "unsupported crypto version" in r.output


def test_unsupported_manifest_crypto_version_rejected(tmp_path):
    ctx = _setup(tmp_path, seal=False)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    data["manifest"]["cryptoVersion"] = 2
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = ctx["runner"].invoke(main, ["verify", "--root", ctx["root"]])
    assert r.exit_code != 0
    assert "unsupported crypto version" in r.output


def _legacy_v1_envelope(node):
    """Rebuild the mutation envelope the way pre-crypto-agility v1 did:
    identical to ``mutation_envelope`` but with no ``crypto_version`` field
    (and no ``cryptoVersion`` stored on the node)."""
    return signing.mutation_envelope(
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
        crypto_version=None,
    )


def test_legacy_v1_signature_and_seal_still_verify(tmp_path):
    """Cross-version regression: artifacts signed + sealed by the pre-PR v1
    implementation (no ``crypto_version`` in the envelope, no ``cryptoVersion``
    stored anywhere) must remain verifiable after the crypto-agility upgrade.

    We rebuild a fresh namespace with the current code, then re-sign every
    mutation and re-seal the head using the *legacy* envelope shape and drop
    the ``cryptoVersion`` keys, exactly as the old implementation wrote them.
    """
    ctx = _setup(tmp_path, seal=True)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))

    actor_nsec = (tmp_path / "actor.nsec").read_text(encoding="utf-8").strip()
    org_nsec = (tmp_path / "org.nsec").read_text(encoding="utf-8").strip()

    # Re-sign each mutation over the legacy envelope and drop cryptoVersion.
    for node in data.get("nodes", []):
        if node.get("sig") and node.get("sigPubkey"):
            node["sig"] = signing.sign_mutation(actor_nsec, _legacy_v1_envelope(node))
        node.pop("cryptoVersion", None)

    # Re-seal the head over the legacy seal envelope and drop the header version.
    m = data["manifest"]
    legacy_seal = signing.seal_envelope(
        root_mac=m["rootMac"],
        head_seq=int(m.get("headSeq") or 0),
        head_mac=m.get("headMac") or m["rootMac"],
        audit_seq=int(m.get("auditSeq") or 0),
        audit_head=m.get("auditHead"),
        crypto_version=None,
    )
    m["signedRootMac"] = signing.sign_seal(org_nsec, legacy_seal)
    m.pop("cryptoVersion", None)

    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = ctx["runner"].invoke(
        main,
        [
            "verify",
            "--org-yaml",
            ctx["org"],
            "--org-pubkey",
            ctx["org_pubkey"],
            "--root",
            ctx["root"],
        ],
    )
    assert r.exit_code == 0, r.output


def test_legacy_v1_ref_still_verifies(tmp_path):
    """A legacy signed ref (no ``cryptoVersion``, signature over the
    pre-crypto-agility envelope) must still verify after the upgrade."""
    ctx = _setup(tmp_path, seal=False)
    mp = tmp_path / "manifest.yaml"

    # Create a signed tag with the current implementation.
    r = ctx["runner"].invoke(
        main,
        [
            "tag",
            "latest",
            "a.txt",
            "--org-yaml",
            ctx["org"],
            "--actor",
            "paco",
            "--nsec-file",
            str(tmp_path / "actor.nsec"),
            "--root",
            ctx["root"],
        ],
    )
    assert r.exit_code == 0, r.output

    actor_nsec = (tmp_path / "actor.nsec").read_text(encoding="utf-8").strip()
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    ref = data["refs"]["latest"]
    # ``verify`` rebuilds the ref envelope from the *target node's*
    # category/owners/locations/urn, so the legacy signature must be made over
    # exactly those fields (see the ref path in ``cli.verify``).
    target = next(n for n in data["nodes"] if n["mac"] == ref["mac"])
    legacy_env = signing.mutation_envelope(
        mac=ref["mac"],
        actor=ref.get("actor", ""),
        action=ref.get("action", "tag"),
        category=target.get("category", ""),
        owners=target.get("owners"),
        locations=target.get("locations"),
        urn=target.get("urn", ""),
        ref="latest",
        seq=ref.get("seq"),
        prev_head=ref.get("prevHead"),
        ts=ref.get("ts"),
        crypto_version=None,
    )
    ref["sig"] = signing.sign_mutation(actor_nsec, legacy_env)
    ref.pop("cryptoVersion", None)
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = ctx["runner"].invoke(
        main, ["verify", "--org-yaml", ctx["org"], "--root", ctx["root"]]
    )
    assert r.exit_code == 0, r.output


def test_unsigned_node_unsupported_version_rejected(tmp_path):
    """An *unsigned* node declaring an unsupported crypto version must fail
    closed. Previously the version check lived inside the signature branch, so
    stripping ``sig``/``sigPubkey`` and setting ``cryptoVersion: 2`` printed OK.
    """
    ctx = _setup(tmp_path, seal=False)
    mp = tmp_path / "manifest.yaml"
    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    node = next(n for n in data["nodes"] if n.get("kind") == "doc")
    node.pop("sig", None)
    node.pop("sigPubkey", None)
    node["cryptoVersion"] = 2
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = ctx["runner"].invoke(main, ["verify", "--root", ctx["root"]])
    assert r.exit_code != 0
    assert "unsupported crypto version" in r.output


def test_unsigned_ref_unsupported_version_rejected(tmp_path):
    """An *unsigned* ref declaring an unsupported crypto version must fail
    closed (the ref path had the same conditional gap as the node path)."""
    ctx = _setup(tmp_path, seal=False)
    mp = tmp_path / "manifest.yaml"

    r = ctx["runner"].invoke(
        main,
        [
            "tag",
            "latest",
            "a.txt",
            "--org-yaml",
            ctx["org"],
            "--actor",
            "paco",
            "--nsec-file",
            str(tmp_path / "actor.nsec"),
            "--root",
            ctx["root"],
        ],
    )
    assert r.exit_code == 0, r.output

    data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    ref = data["refs"]["latest"]
    ref.pop("sig", None)
    ref.pop("sigPubkey", None)
    ref["cryptoVersion"] = 2
    mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    r = ctx["runner"].invoke(main, ["verify", "--root", ctx["root"]])
    assert r.exit_code != 0
    assert "unsupported crypto version" in r.output
