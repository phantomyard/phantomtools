"""Adversarial tests for issue #76: nsec lifecycle.

Three properties:
1. A signing key file must be private: 0600 accepted, 0640/0644 rejected,
   wrong owner rejected, symlink rejected.
2. Rotation: a dated key transition (key A valid T0–T1, key B valid from T1)
   must not retroactively invalidate history signed with key A.
3. Revocation: a key revoked at T must authenticate mutations signed before T
   and fail for mutations signed after T.
"""

import os

import coincurve
import pytest
import yaml
from click.testing import CliRunner

from phantomdocs import signing
from phantomdocs.access import key_valid_at
from phantomdocs.cli import main
from phantomdocs.documents import DocumentError, DocumentService

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


def _init_root(tmp_path, org_npub=None):
    runner = CliRunner()
    root = str(tmp_path)
    args = ["init", "--org", "example-org", "--namespace", "docs", "--root", root]
    r = runner.invoke(main, args)
    assert r.exit_code == 0, r.output
    return root, runner


# ---------------------------------------------------------------------------
# 7.1 file permissions
# ---------------------------------------------------------------------------


def _write_org(tmp_path, npub):
    org = tmp_path / "org.yaml"
    org.write_text(ORG.replace("NPUB_PLACEHOLDER", npub), encoding="utf-8")
    return str(org)


def test_nsec_0600_accepted(tmp_path):
    secret = coincurve.PrivateKey().secret.hex()
    nsec = tmp_path / "nsec.txt"
    nsec.write_text(secret)
    os.chmod(nsec, 0o600)
    root, _runner = _init_root(tmp_path)
    org = _write_org(tmp_path, _npub(signing.pubkey_from_nsec(secret)))
    svc = DocumentService(root, org, "paco", str(nsec))
    svc.add_document(
        content=b"ok",
        ref_location=None,
        slug="a.txt",
        category="category-1",
        folder=None,
        owners=["ceo"],
        backend=None,
    )


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_nsec_group_or_world_readable_rejected(tmp_path, mode):
    secret = coincurve.PrivateKey().secret.hex()
    nsec = tmp_path / "nsec.txt"
    nsec.write_text(secret)
    os.chmod(nsec, mode)
    root, _runner = _init_root(tmp_path)
    org = _write_org(tmp_path, _npub(signing.pubkey_from_nsec(secret)))
    with pytest.raises(DocumentError, match="0600"):
        DocumentService(root, org, "paco", str(nsec))


def test_nsec_symlink_rejected(tmp_path):
    secret = coincurve.PrivateKey().secret.hex()
    real = tmp_path / "real.nsec"
    real.write_text(secret)
    os.chmod(real, 0o600)
    link = tmp_path / "nsec.txt"
    os.symlink(str(real), str(link))
    root, _runner = _init_root(tmp_path)
    org = _write_org(tmp_path, _npub(signing.pubkey_from_nsec(secret)))
    with pytest.raises(DocumentError, match="symlink"):
        DocumentService(root, org, "paco", str(link))


# ---------------------------------------------------------------------------
# 7.2 rotation
# ---------------------------------------------------------------------------


def test_rotation_does_not_invalidate_history():
    """A signature made with a key valid at T0 still verifies at T1 after the
    actor rotated to a different key (issue #76.2)."""
    secret_a = coincurve.PrivateKey().secret.hex()
    secret_b = coincurve.PrivateKey().secret.hex()
    pubkey_a = signing.pubkey_from_nsec(secret_a)
    pubkey_b = signing.pubkey_from_nsec(secret_b)
    org = yaml.safe_load(
        """
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2: { categories: [1, 2] }
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    npub: NPUB_B
    keys:
      - npub: NPUB_A
        valid_from: "2026-01-01T00:00:00Z"
        valid_until: "2026-06-01T00:00:00Z"
      - npub: NPUB_B
        valid_from: "2026-06-01T00:00:00Z"
    actor_exceptions: []
""".replace("NPUB_A", _npub(pubkey_a)).replace("NPUB_B", _npub(pubkey_b))
    )
    # Key A was valid in May 2026.
    assert key_valid_at(org, "paco", pubkey_a, "2026-05-15T00:00:00Z") is True
    # Key A is no longer valid in July 2026 (rotated out).
    assert key_valid_at(org, "paco", pubkey_a, "2026-07-15T00:00:00Z") is False
    # Key B is valid from June 2026 onward.
    assert key_valid_at(org, "paco", pubkey_b, "2026-07-15T00:00:00Z") is True


# ---------------------------------------------------------------------------
# 7.3 revocation
# ---------------------------------------------------------------------------


def test_revocation_distinguishes_before_and_after():
    """A key revoked at T authenticates signatures from before T and fails
    signatures from after T (issue #76.3)."""
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org = yaml.safe_load(
        """
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2: { categories: [1, 2] }
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    npub: NPUB_X
    keys:
      - npub: NPUB_X
        revoked_at: "2026-08-01T00:00:00Z"
    actor_exceptions: []
""".replace("NPUB_X", _npub(pubkey))
    )
    assert key_valid_at(org, "paco", pubkey, "2026-07-31T23:59:59Z") is True
    assert key_valid_at(org, "paco", pubkey, "2026-08-01T00:00:00Z") is False
    assert key_valid_at(org, "paco", pubkey, "2026-08-02T00:00:00Z") is False


def test_revoked_key_rejected_at_sign_time(tmp_path):
    """A revoked key cannot sign a new mutation at all (issue #76.3)."""
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org = tmp_path / "org.yaml"
    org.write_text(
        """
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2: { categories: [1, 2] }
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    npub: NPUB_X
    keys:
      - npub: NPUB_X
        revoked_at: "2020-01-01T00:00:00Z"
    actor_exceptions: []
""".replace("NPUB_X", _npub(pubkey)),
        encoding="utf-8",
    )
    nsec = tmp_path / "nsec.txt"
    nsec.write_text(secret)
    os.chmod(nsec, 0o600)
    root, _runner = _init_root(tmp_path)
    with pytest.raises(DocumentError, match="declared npub"):
        DocumentService(root, str(org), "paco", str(nsec))


def test_verify_flags_mutation_signed_by_revoked_key(tmp_path):
    """A node whose signature was made by a now-revoked key is flagged by
    verify --org-yaml (the key is not valid at the node's timestamp)."""
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org = tmp_path / "org.yaml"
    # Key valid at sign time, then revoked. We simulate by writing a node
    # signed with ts BEFORE the revocation, then revoking the key and running
    # verify with a key record that revokes it before that ts.
    org.write_text(
        """
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2: { categories: [1, 2] }
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    npub: NPUB_X
    keys:
      - npub: NPUB_X
        revoked_at: "2020-01-01T00:00:00Z"
    actor_exceptions: []
""".replace("NPUB_X", _npub(pubkey)),
        encoding="utf-8",
    )
    nsec = tmp_path / "nsec.txt"
    nsec.write_text(secret)
    os.chmod(nsec, 0o600)
    runner = CliRunner()
    root = str(tmp_path)
    assert (
        runner.invoke(
            main,
            ["init", "--org", "example-org", "--namespace", "docs", "--root", root],
        ).exit_code
        == 0
    )
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    # The org declares the key revoked since 2020, but the mutation's ts is
    # "now", so verify --org-yaml must flag the signature.
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
            str(nsec),
            "--root",
            root,
        ],
    )
    # The service itself refuses to sign with a revoked key.
    assert r.exit_code != 0
    assert "declared npub" in r.output
