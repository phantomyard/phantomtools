"""Structural manifest schema validation (issue #46).

`manifest.validate()` must catch shape/integrity problems at load time —
before a command fails deep inside its workflow. These tests cover the
structural checks (required fields, formats, kind/category, MAC uniqueness,
known parents/predecessors, duplicate versions, ref targets). Cryptographic
integrity (recomputing MACs and content hashes) is `pd verify`'s job and is
tested separately.
"""

from phantomdocs import identity, manifest


def _valid_manifest():
    """A structurally-valid single-namespace manifest: one folder, one doc."""
    root = identity.root_mac("org", "", "docs")
    folder_mac = identity.node_mac(root, identity.component_for_folder("reports"))
    doc_mac = identity.node_mac(folder_mac, identity.component_for_doc("r.md", b"data"))
    return {
        "manifest": {
            "version": 1,
            "org": "org",
            "namespace": "docs",
            "tenant": "single",
            "rootMac": root,
            "signedRootMac": None,
        },
        "refs": {},
        "nodes": [
            {
                "urn": "urn:org:folder:reports",
                "mac": folder_mac,
                "parentMac": root,
                "kind": "folder",
                "slug": "reports",
                "category": "category-1",
                "owners": ["cfo"],
                "meta": {},
                "relations": {},
            },
            {
                "urn": "urn:org:doc:reports/r.md",
                "mac": doc_mac,
                "parentMac": folder_mac,
                "kind": "doc",
                "slug": "r.md",
                "category": "category-1",
                "contentHash": identity.content_hash(b"data"),
                "size": 4,
                "owners": ["cfo"],
                "locations": [{"backend": "local", "path": "/tmp/blobs/aa/hash"}],
                "meta": {"title": "r.md"},
                "relations": {},
                "previous": None,
            },
        ],
    }


def test_valid_manifest_passes():
    assert manifest.validate(_valid_manifest()) == []


def test_empty_manifest_passes():
    data = manifest.empty_manifest("org", "docs", identity.root_mac("org", "", "docs"))
    assert manifest.validate(data) == []


def test_missing_required_field():
    data = _valid_manifest()
    del data["nodes"][0]["mac"]
    errors = manifest.validate(data)
    assert any("missing required field 'mac'" in e for e in errors)


def test_invalid_mac_format():
    data = _valid_manifest()
    data["nodes"][0]["mac"] = "zz" * 32
    errors = manifest.validate(data)
    assert any("mac must be a 64-hex string" in e for e in errors)


def test_duplicate_mac():
    data = _valid_manifest()
    data["nodes"][1]["mac"] = data["nodes"][0]["mac"]
    errors = manifest.validate(data)
    assert any("duplicate mac" in e for e in errors)


def test_unknown_parent():
    data = _valid_manifest()
    data["nodes"][1]["parentMac"] = "f" * 64
    errors = manifest.validate(data)
    assert any("parentMac" in e and "unknown" in e for e in errors)


def test_unknown_previous():
    data = _valid_manifest()
    data["nodes"][1]["previous"] = "e" * 64
    errors = manifest.validate(data)
    assert any("previous" in e and "unknown" in e for e in errors)


def test_invalid_kind():
    data = _valid_manifest()
    data["nodes"][0]["kind"] = "link"
    errors = manifest.validate(data)
    assert any("kind must be one of" in e for e in errors)


def test_invalid_category():
    data = _valid_manifest()
    data["nodes"][0]["category"] = "top-secret"
    errors = manifest.validate(data)
    assert any("not a 'category-...' id" in e for e in errors)


def test_hierarchical_category_accepted():
    data = _valid_manifest()
    data["nodes"][0]["category"] = "category-4-almaponia"
    assert manifest.validate(data) == []


def test_doc_missing_content_hash():
    data = _valid_manifest()
    del data["nodes"][1]["contentHash"]
    errors = manifest.validate(data)
    assert any("missing contentHash" in e for e in errors)


def test_duplicate_version():
    """The same (urn, contentHash) appearing twice is a duplicate version."""
    data = _valid_manifest()
    duplicate = dict(data["nodes"][1])
    # Distinct MAC (so we isolate the duplicate-version check from the
    # duplicate-mac check); same urn + contentHash.
    duplicate["mac"] = identity.node_mac(
        duplicate["parentMac"], identity.component_for_doc(duplicate["slug"], b"x")
    )
    data["nodes"].append(duplicate)
    errors = manifest.validate(data)
    assert any("duplicate version" in e for e in errors)
    assert not any("duplicate mac" in e for e in errors)


def test_ref_invalid_target():
    data = _valid_manifest()
    data["refs"]["latest"] = {"mac": "nothex"}
    errors = manifest.validate(data)
    assert any("target MAC must be 64-hex" in e for e in errors)


def test_ref_missing_target():
    data = _valid_manifest()
    data["refs"]["latest"] = {"actor": "roberto"}
    errors = manifest.validate(data)
    assert any("missing or invalid target MAC" in e for e in errors)


def test_ref_bare_mac_accepted():
    data = _valid_manifest()
    data["refs"]["latest"] = data["nodes"][1]["mac"]
    assert manifest.validate(data) == []
