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
