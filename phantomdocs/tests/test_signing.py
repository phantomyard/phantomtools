"""Mutation-signing tests (issue #30 v2): Schnorr signatures bind authorship."""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from phantomdocs import signing
from phantomdocs.cli import main


def _gen_keypair():
    """Return (nsec_bech32, pubkey_hex) for a fresh key."""
    import coincurve

    secret = coincurve.PrivateKey().secret.hex()
    return secret, signing.pubkey_from_nsec(secret)


@pytest.fixture
def nsec_file(tmp_path):
    """A nsec file + its pubkey hex + the raw secret hex."""
    import coincurve

    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    f = tmp_path / "nsec.txt"
    f.write_text(secret)
    return str(f), pubkey, secret


def test_npub_nsec_roundtrip():
    import coincurve

    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    # bech32 encode our own nsec/npub to exercise the decoders
    nsec = _bech32_encode("nsec", bytes.fromhex(secret))
    npub = _bech32_encode("npub", bytes.fromhex(pubkey))
    assert signing.nsec_to_secret_hex(nsec) == secret
    assert signing.npub_to_pubkey_hex(npub) == pubkey


def _bech32_encode(hrp: str, data: bytes) -> str:
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


def test_sign_and_verify_roundtrip():
    mac = "ab" * 32
    secret = "cd" * 32
    sig = signing.sign_mutation(secret, mac)
    pubkey = signing.pubkey_from_nsec(secret)
    assert len(sig) == 128
    assert signing.verify_mutation(pubkey, sig, mac) is True
    assert signing.verify_mutation(pubkey, sig, "ef" * 32) is False


def test_verify_bad_key_fails():
    mac = "ab" * 32
    sig = signing.sign_mutation("cd" * 32, mac)
    other_pubkey = signing.pubkey_from_nsec("11" * 32)
    assert signing.verify_mutation(other_pubkey, sig, mac) is False


ORG = """\
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-1:
      categories: [1]
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


def test_add_signs_node_and_verify_accepts(tmp_path, nsec_file):

    nsec_path, pubkey, _secret = nsec_file
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace("NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(pubkey)))
    )

    doc = tmp_path / "report.txt"
    doc.write_text("quarterly report")

    runner = CliRunner()
    # init
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
    # add (signed)
    r = runner.invoke(
        main,
        [
            "add",
            str(doc),
            "--slug",
            "report",
            "--category",
            "category-2",
            "--owners",
            "ceo",
            "--org-yaml",
            str(org),
            "--actor",
            "paco",
            "--nsec-file",
            nsec_path,
            "--root",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 0, r.output

    manifest = (tmp_path / "manifest.yaml").read_text()
    assert "sig:" in manifest and "sigPubkey:" in manifest

    # verify (no org-yaml) -> signature still checked cryptographically
    r = runner.invoke(main, ["verify", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    # verify with org-yaml -> declared-key check passes
    r = runner.invoke(main, ["verify", "--org-yaml", str(org), "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output


def test_verify_flags_undeclared_signing_key(tmp_path, nsec_file):
    import coincurve

    nsec_path, _pubkey, _secret = nsec_file
    # org.yaml declares a DIFFERENT actor npub than the signing key
    other_pubkey = coincurve.PrivateKey().public_key.format()[1:33].hex()
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace(
            "NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(other_pubkey))
        )
    )

    doc = tmp_path / "report.txt"
    doc.write_text("quarterly report")

    runner = CliRunner()
    assert (
        runner.invoke(
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
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            main,
            [
                "add",
                str(doc),
                "--slug",
                "report",
                "--category",
                "category-2",
                "--owners",
                "ceo",
                "--org-yaml",
                str(org),
                "--actor",
                "paco",
                "--nsec-file",
                nsec_path,
                "--root",
                str(tmp_path),
            ],
        ).exit_code
        == 0
    )

    # verify without org-yaml: signature is cryptographically valid -> passes
    assert runner.invoke(main, ["verify", "--root", str(tmp_path)]).exit_code == 0
    # verify with org-yaml: signing key is NOT a declared actor -> fails
    r = runner.invoke(main, ["verify", "--org-yaml", str(org), "--root", str(tmp_path)])
    assert r.exit_code != 0
    assert "undeclared key" in r.output


def test_unsigned_nodes_still_verify(tmp_path):
    """Without a nsec, mutations are unsigned and verify still passes (v1)."""
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace(
            "NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex("ab" * 32))
        )
    )

    doc = tmp_path / "report.txt"
    doc.write_text("quarterly report")

    runner = CliRunner()
    assert (
        runner.invoke(
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
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            main,
            [
                "add",
                str(doc),
                "--slug",
                "report",
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
        ).exit_code
        == 0
    )
    r = runner.invoke(main, ["verify", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output


def test_sign_mutation_from_env(tmp_path):
    """PHANTOMDOCS_NSEC env is honored when --nsec-file is absent."""
    import coincurve

    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace("NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(pubkey)))
    )
    doc = tmp_path / "report.txt"
    doc.write_text("quarterly report")

    runner = CliRunner()
    assert (
        runner.invoke(
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
        ).exit_code
        == 0
    )
    env = dict(os.environ, PHANTOMDOCS_NSEC=secret)
    r = runner.invoke(
        main,
        [
            "add",
            str(doc),
            "--slug",
            "report",
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
        env=env,
    )
    assert r.exit_code == 0, r.output
    assert "sig:" in (tmp_path / "manifest.yaml").read_text()
