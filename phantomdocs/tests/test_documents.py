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


def test_add_document_version_preserves_parent(tmp_path):
    """Versioning under a folder keeps the same parentMac across versions."""
    root, _org, svc = _svc(tmp_path)
    svc.create_folder(
        name="reports", parent=None, category="category-1", owners=["cfo"]
    )
    svc.add_document(
        content=b"v1",
        ref_location=None,
        slug="r.md",
        category=None,
        folder="reports",
        owners=["cfo"],
        backend=None,
    )
    v2 = svc.add_document(
        content=b"v2",
        ref_location=None,
        slug="r.md",
        category=None,
        folder="reports",
        owners=["cfo"],
        backend=None,
    )
    assert v2["verb"] == "versioned"
    versions = _repo(root).versions_of("urn:org:doc:reports/r.md")
    assert len(versions) == 2
    assert versions[0]["parentMac"] == versions[1]["parentMac"]


def test_add_document_rejects_parent_change(tmp_path):
    """Versioning must not silently change a document's tree position.

    A manifest whose current version's ``parentMac`` disagrees with the folder
    named by ``--folder`` is a broken tree-position invariant; the service
    derives the parent from the existing document and refuses to version it
    when ``--folder`` names a different parent (a move is a separate op).
    """
    root, _org, svc = _svc(tmp_path)
    svc.create_folder(name="a", parent=None, category="category-1", owners=["cfo"])
    svc.create_folder(name="b", parent=None, category="category-1", owners=["cfo"])
    svc.add_document(
        content=b"v1",
        ref_location=None,
        slug="x.txt",
        category=None,
        folder="a",
        owners=["cfo"],
        backend=None,
    )

    # Break the parentMac/URN invariant: the doc lives under "a" (URN path
    # "a/x.txt") but its parentMac now points at folder "b".
    folder_b = _repo(root).node_by_urn("urn:org:folder:b")
    path = os.path.join(root, "manifest.yaml")
    data = manifest.load(path)
    data["nodes"][-1]["parentMac"] = folder_b["mac"]
    manifest.save(path, data)

    with pytest.raises(DocumentError, match="cannot move"):
        svc.add_document(
            content=b"v2",
            ref_location=None,
            slug="x.txt",
            category=None,
            folder="a",
            owners=["cfo"],
            backend=None,
        )
