"""Mutation-signing tests (issue #30 v2): Schnorr signatures bind authorship."""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from phantomdocs import signing
from phantomdocs.cli import main
from phantomdocs.signing import mutation_envelope


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
    os.chmod(f, 0o600)  # issue #76: signing keys must be private
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
    env = mutation_envelope(
        mac=mac,
        actor="paco",
        action="add",
        category="category-2",
        owners=["ceo"],
        locations=[{"backend": "local", "path": "/store"}],
        urn="urn:ex:doc:x",
    )
    sig = signing.sign_mutation(secret, env)
    pubkey = signing.pubkey_from_nsec(secret)
    assert len(sig) == 128
    assert signing.verify_mutation(pubkey, sig, env) is True
    # A different MAC (or any different field) invalidates the signature.
    env_other_mac = mutation_envelope(
        mac="ef" * 32,
        actor="paco",
        action="add",
        category="category-2",
        owners=["ceo"],
        locations=[{"backend": "local", "path": "/store"}],
        urn="urn:ex:doc:x",
    )
    assert signing.verify_mutation(pubkey, sig, env_other_mac) is False


def test_signature_binds_authorization_fields():
    """Changing category, owners, actor, or locations invalidates the
    signature — a signature over the bare MAC would stay valid (the reviewer
    reproduction)."""
    mac = "ab" * 32
    secret = "cd" * 32

    def env(**overrides):
        defaults = {
            "mac": mac,
            "actor": "paco",
            "action": "add",
            "category": "category-2",
            "owners": ["ceo"],
            "locations": [{"backend": "local", "path": "/store"}],
            "urn": "urn:ex:doc:x",
        }
        return mutation_envelope(**{**defaults, **overrides})

    sig = signing.sign_mutation(secret, env())
    pubkey = signing.pubkey_from_nsec(secret)
    assert signing.verify_mutation(pubkey, sig, env()) is True
    assert signing.verify_mutation(pubkey, sig, env(category="category-1")) is False
    assert signing.verify_mutation(pubkey, sig, env(owners=["intern"])) is False
    assert signing.verify_mutation(pubkey, sig, env(actor="pepa")) is False
    assert signing.verify_mutation(pubkey, sig, env(action="mkdir")) is False
    assert (
        signing.verify_mutation(
            pubkey, sig, env(locations=[{"backend": "ssh", "ref": "ssh://h/x"}])
        )
        is False
    )


def test_verify_bad_key_fails():
    mac = "ab" * 32
    env = mutation_envelope(
        mac=mac,
        actor="paco",
        action="add",
        category="category-2",
        owners=["ceo"],
        locations=None,
        urn="urn:ex:doc:x",
    )
    sig = signing.sign_mutation("cd" * 32, env)
    other_pubkey = signing.pubkey_from_nsec("11" * 32)
    assert signing.verify_mutation(other_pubkey, sig, env) is False


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


def test_add_rejects_undeclared_signing_key(tmp_path, nsec_file):
    """Issue #69: a signing key that is not the actor's declared npub is
    rejected at mutation time (not merely flagged by `verify --org-yaml`)."""
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
    # The mutation must be refused: the key does not belong to actor paco.
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
    assert r.exit_code != 0
    assert "declared npub" in r.output


def test_add_rejects_actor_impersonation(tmp_path):
    """One declared actor's key must not authenticate a mutation asserted as
    a DIFFERENT actor (issue #69). paco signs with his nsec but claims
    actor=pepa; the mutation is refused at the service boundary."""
    import coincurve

    paco_secret = coincurve.PrivateKey().secret.hex()
    paco_pubkey = signing.pubkey_from_nsec(paco_secret)
    pepa_pubkey = coincurve.PrivateKey().public_key.format()[1:33].hex()

    org = tmp_path / "org.yaml"
    org.write_text(
        """\
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
    npub: NPUB_PACO
    actor_exceptions: []
  - id: pepa
    role: ceo
    npub: NPUB_PEPA
    actor_exceptions: []
""".replace("NPUB_PACO", _bech32_encode("npub", bytes.fromhex(paco_pubkey))).replace(
            "NPUB_PEPA", _bech32_encode("npub", bytes.fromhex(pepa_pubkey))
        )
    )

    nsec_file = tmp_path / "paco.nsec"
    nsec_file.write_text(paco_secret)
    os.chmod(nsec_file, 0o600)

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
    # Add the doc claiming actor=pepa, but signed with paco's nsec: refused.
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
            "pepa",
            "--nsec-file",
            str(nsec_file),
            "--root",
            str(tmp_path),
        ],
    )
    assert r.exit_code != 0
    assert "declared npub" in r.output

    # Sanity: the honest signing (paco signs as paco) succeeds and verifies.
    root2 = tmp_path / "honest"
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
                str(root2),
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
                str(nsec_file),
                "--root",
                str(root2),
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            main, ["verify", "--org-yaml", str(org), "--root", str(root2)]
        ).exit_code
        == 0
    )


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


def test_signed_tag_binds_ref_name_and_target(tmp_path, nsec_file):
    """Repointing or renaming ``manifest.refs`` after a signed tag must fail
    verify — the ref name and target MAC are bound into the signature
    (PR #38 cli.py:824)."""
    import yaml

    nsec_path, pubkey, _secret = nsec_file
    org = tmp_path / "org.yaml"
    org.write_text(
        ORG.replace("NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(pubkey)))
    )
    runner = CliRunner()

    def setup(root):
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
                    str(root),
                ],
            ).exit_code
            == 0
        )
        for slug in ("one", "two"):
            d = root / f"{slug}.txt"
            d.write_text(f"content of {slug}")
            assert (
                runner.invoke(
                    main,
                    [
                        "add",
                        str(d),
                        "--slug",
                        slug,
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
                        str(root),
                    ],
                ).exit_code
                == 0
            )
        assert (
            runner.invoke(
                main,
                [
                    "tag",
                    "latest",
                    "one",
                    "--org-yaml",
                    str(org),
                    "--actor",
                    "paco",
                    "--nsec-file",
                    nsec_path,
                    "--root",
                    str(root),
                ],
            ).exit_code
            == 0
        )

    # Baseline: signed tag verifies.
    root = tmp_path / "ok"
    setup(root)
    r = runner.invoke(main, ["verify", "--org-yaml", str(org), "--root", str(root)])
    assert r.exit_code == 0, r.output

    # Repoint `latest` to the other node's MAC.
    repointed = tmp_path / "repointed"
    setup(repointed)
    mp = repointed / "manifest.yaml"
    data = yaml.safe_load(mp.read_text())
    other_mac = next(n["mac"] for n in data["nodes"] if n["slug"] == "two")
    data["refs"]["latest"]["mac"] = other_mac
    with mp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    r = runner.invoke(
        main, ["verify", "--org-yaml", str(org), "--root", str(repointed)]
    )
    assert r.exit_code != 0
    assert "ref signature invalid" in r.output

    # Rename the ref key after tagging.
    renamed = tmp_path / "renamed"
    setup(renamed)
    mp = renamed / "manifest.yaml"
    data = yaml.safe_load(mp.read_text())
    data["refs"]["production"] = data["refs"].pop("latest")
    with mp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    r = runner.invoke(main, ["verify", "--org-yaml", str(org), "--root", str(renamed)])
    assert r.exit_code != 0
    assert "ref signature invalid" in r.output
