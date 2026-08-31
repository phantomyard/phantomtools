"""Adversarial tests for issue #69: the DocumentService security boundary.

The service must establish its own security context — load the org.yaml from
a trusted path, require the actor to be declared, and bind the signing key to
the actor at mutation time. These tests try to bypass the boundary and assert
it holds.
"""

import os

import coincurve
import pytest

from phantomdocs import identity, manifest, signing
from phantomdocs.documents import DocumentError, DocumentService


def _write_org(tmp_path, text):
    p = tmp_path / "org.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _init_root(tmp_path):
    root = str(tmp_path)
    mac = identity.root_mac("org", "", "docs")
    manifest.save(root + "/manifest.yaml", manifest.empty_manifest("org", "docs", mac))
    return root


GOOD_ORG = """\
version: 1
organization:
  id: org
policies:
  access_levels:
    level-2: { label: Operative, categories: [1, 2] }
  security_categories:
    category-1: { label: Public }
    category-2: { label: Confidential }
roles:
  - id: cfo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: cfo
    npub: NPUB_PLACEHOLDER
    actor_exceptions: []
"""


def _npub(pubkey_hex):
    # reuse signing's private bech32 encoder through the test module
    return _bech32_encode("npub", bytes.fromhex(pubkey_hex))


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


def test_forged_org_schema_version_rejected(tmp_path):
    """A forged org.yaml with version 999 (which load_org rejects) must not
    authorize anything — the service loads the org itself and refuses it."""
    root = _init_root(tmp_path)
    org_path = _write_org(
        tmp_path,
        """\
version: 999
organization:
  id: org
policies:
  access_levels:
    level-9: { label: Everything, categories: [1, 2, 3, 4] }
  security_categories:
    category-4: { label: Secret }
roles:
  - id: god
    access_level: level-9
    security_exceptions: []
actors:
  - id: paco
    role: god
    actor_exceptions: []
""",
    )
    with pytest.raises(DocumentError, match="cannot load org model|schema version"):
        DocumentService(root, org_path, "paco")


def test_forged_org_granting_self_access_rejected(tmp_path):
    """A valid-version org that grants the caller category-4 access but does
    not declare the actor's key binding is still not a trust boundary: the
    service uses the *trusted* org.yaml path, so a caller cannot substitute a
    forged dict. This proves the caller can no longer pass an arbitrary org
    dict at all (constructor takes a path, and authorization comes from it)."""
    root = _init_root(tmp_path)
    # A "forged" org that would let a fresh actor write category-4.
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org_path = _write_org(
        tmp_path,
        GOOD_ORG.replace("NPUB_PLACEHOLDER", _npub(pubkey)),
    )
    # The actor is declared and key-bound, so this succeeds for category-1/2.
    svc = DocumentService(root, org_path, "paco", None)
    # But there is no path to write category-4: it is not in the org's
    # categories, so even a key-bound actor is denied (fail-closed).
    with pytest.raises(DocumentError, match="denied"):
        svc.create_folder(
            name="secret",
            parent=None,
            category="category-4",
            owners=["cfo"],
        )


def test_undeclared_actor_rejected_at_boundary(tmp_path):
    """An actor not present in the trusted org model is refused before any
    mutation (fail-closed), even without signing."""
    root = _init_root(tmp_path)
    org_path = _write_org(
        tmp_path, GOOD_ORG.replace("NPUB_PLACEHOLDER", _npub("ab" * 32))
    )
    with pytest.raises(DocumentError, match="not an actor"):
        DocumentService(root, org_path, "intruder")


def test_random_nsec_cannot_sign_as_declared_actor(tmp_path):
    """A random nsec cannot authenticate a mutation asserted as a declared
    actor — the key must map to the actor's npub (issue #69 PoC)."""
    root = _init_root(tmp_path)
    secret = coincurve.PrivateKey().secret.hex()
    pubkey = signing.pubkey_from_nsec(secret)
    org_path = _write_org(tmp_path, GOOD_ORG.replace("NPUB_PLACEHOLDER", _npub(pubkey)))
    nsec_path = tmp_path / "nsec.txt"
    nsec_path.write_text(secret, encoding="utf-8")
    os.chmod(nsec_path, 0o600)

    # The honest actor's own key binds and signs fine.
    svc = DocumentService(root, org_path, "paco", str(nsec_path))
    svc.add_document(
        content=b"ok",
        ref_location=None,
        slug="a.txt",
        category="category-1",
        folder=None,
        owners=["cfo"],
        backend=None,
    )

    # A DIFFERENT random key, still claiming actor=paco, is refused.
    other_secret = coincurve.PrivateKey().secret.hex()
    other_nsec = tmp_path / "other.nsec"
    other_nsec.write_text(other_secret, encoding="utf-8")
    os.chmod(other_nsec, 0o600)
    with pytest.raises(DocumentError, match="declared npub"):
        DocumentService(root, org_path, "paco", str(other_nsec))


def test_actor_without_npub_cannot_sign(tmp_path):
    """An actor with no declared npub cannot sign at all (fail-closed)."""
    root = _init_root(tmp_path)
    org_path = _write_org(
        tmp_path,
        """\
version: 1
organization:
  id: org
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
  - id: paco
    role: cfo
    actor_exceptions: []
""",
    )
    secret = coincurve.PrivateKey().secret.hex()
    nsec_path = tmp_path / "nsec.txt"
    nsec_path.write_text(secret, encoding="utf-8")
    os.chmod(nsec_path, 0o600)
    with pytest.raises(DocumentError, match="no declared npub"):
        DocumentService(root, org_path, "paco", str(nsec_path))
