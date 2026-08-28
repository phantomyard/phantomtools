"""DocumentService — the mutating document workflows (issue #46).

The service owns the domain workflow (authorize, resolve parent, compute the
MAC chain, store the blob, build/sign the node, mutate the manifest, audit).
These tests drive it directly; the CLI tests (test_smoke.py) cover the same
paths end-to-end through `pd`.

Since issue #69 the service establishes its own security context: it loads the
org.yaml from a trusted path, requires the actor to be declared, and binds the
signing key to the actor. Tests therefore construct it with
``DocumentService(root, org_yaml_path, actor_id, nsec_file)``.
"""

import os

import pytest
import yaml

from phantomdocs import identity, manifest
from phantomdocs.documents import DocumentError, DocumentService

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


def _svc(tmp_path):
    root = str(tmp_path)
    mac = identity.root_mac("org", "", "docs")
    manifest.save(
        os.path.join(root, "manifest.yaml"),
        manifest.empty_manifest("org", "docs", mac),
    )
    org_path = tmp_path / "org.yaml"
    org_path.write_text(ORG_YAML, encoding="utf-8")
    return root, str(org_path), DocumentService(root, str(org_path), "roberto")


def _repo(root):
    return manifest.ManifestRepository(
        manifest.load(os.path.join(root, "manifest.yaml"))
    )


def test_create_folder(tmp_path):
    root, _org, svc = _svc(tmp_path)
    result = svc.create_folder(
        name="reports",
        parent=None,
        category="category-1",
        owners=["cfo"],
    )
    assert result["path"] == "reports"
    assert result["urn"] == "urn:org:folder:reports"
    assert _repo(root).node_by_urn("urn:org:folder:reports") is not None


def test_create_folder_denied_without_owners(tmp_path):
    _root, _org, svc = _svc(tmp_path)
    with pytest.raises(DocumentError, match="denied"):
        svc.create_folder(
            name="x",
            parent=None,
            category="category-1",
            owners=[],
        )


def test_add_document_version_and_unchanged(tmp_path):
    root, _org, svc = _svc(tmp_path)
    added = svc.add_document(
        content=b"v1",
        ref_location=None,
        slug="a.txt",
        category=None,
        folder=None,
        owners=["cfo"],
        backend=None,
    )
    assert added["verb"] == "added"
    assert added["logical"] == "a.txt"

    unchanged = svc.add_document(
        content=b"v1",
        ref_location=None,
        slug="a.txt",
        category=None,
        folder=None,
        owners=["cfo"],
        backend=None,
    )
    assert unchanged["unchanged"] is True

    versioned = svc.add_document(
        content=b"v2",
        ref_location=None,
        slug="a.txt",
        category=None,
        folder=None,
        owners=["cfo"],
        backend=None,
    )
    assert versioned["verb"] == "versioned"

    # Two versions registered under one URN.
    assert len(_repo(root).versions_of("urn:org:doc:a.txt")) == 2


def test_set_ref(tmp_path):
    root, _org, svc = _svc(tmp_path)
    svc.add_document(
        content=b"v1",
        ref_location=None,
        slug="a.txt",
        category=None,
        folder=None,
        owners=["cfo"],
        backend=None,
    )
    result = svc.set_ref(name="latest", ref="a.txt")
    assert result["name"] == "latest"
    assert result["urn"] == "urn:org:doc:a.txt"
    assert "latest" in _repo(root).refs
