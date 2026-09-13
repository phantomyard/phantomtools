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


def test_structural_issues_flags_parent_mac_disagreement():
    """Versions of one URN must share a single tree position (parentMac)."""
    data = _valid_manifest()
    root = data["manifest"]["rootMac"]
    v1 = data["nodes"][1]
    v2 = dict(v1)
    v2["mac"] = "b" * 64
    v2["parentMac"] = root  # different tree position than v1's folder
    v2["contentHash"] = identity.content_hash(b"other")
    v2["previous"] = v1["mac"]
    data["nodes"].append(v2)
    issues = manifest.structural_issues(data)
    assert any("disagree on parentMac" in i for i in issues)


def test_structural_issues_parent_mac_agreement_ok():
    """Two versions sharing a parentMac produce no tree-position issue."""
    data = _valid_manifest()
    v1 = data["nodes"][1]
    v2 = dict(v1)
    v2["mac"] = "c" * 64
    v2["contentHash"] = identity.content_hash(b"other")
    v2["previous"] = v1["mac"]
    data["nodes"].append(v2)
    issues = manifest.structural_issues(data)
    assert not any("disagree on parentMac" in i for i in issues)


# --- current version is an explicit field, not array position (issue #99) ---


def _two_version_manifest():
    """One URN with two versions; v2 chains off v1 and is the explicit head."""
    data = _valid_manifest()
    v1 = data["nodes"][1]
    v2 = dict(v1)
    v2["mac"] = "d" * 64
    v2["contentHash"] = identity.content_hash(b"other")
    v2["previous"] = v1["mac"]
    data["nodes"].append(v2)
    data["currentVersions"] = {v1["urn"]: v2["mac"]}
    return data


def test_current_version_is_not_array_position():
    """Reordering nodes must not change which version resolves as current."""
    data = _two_version_manifest()
    urn = data["nodes"][1]["urn"]
    expected = data["nodes"][2]["mac"]
    assert manifest.node_by_urn(data, urn)["mac"] == expected
    # Physical order flipped: the explicit head ref still decides.
    data["nodes"].reverse()
    assert manifest.node_by_urn(data, urn)["mac"] == expected
    assert manifest.node_by_path(data, "reports/r.md")["mac"] == expected
    assert manifest.node_by_slug(data, "r.md")["mac"] == expected


def _two_urns_each_with_two_versions_sharing_a_slug():
    """Two URNs (`reports/r.md`, `archive/r.md`) with two versions each.

    Duplicate basenames across folders are valid, so a slug lookup spans both
    URNs and every version carries the slug ``r.md``.
    """
    data = _valid_manifest()
    root = data["manifest"]["rootMac"]
    folder_a = data["nodes"][0]
    doc_a1 = data["nodes"][1]
    doc_a2 = dict(doc_a1)
    doc_a2["mac"] = "c" * 64
    doc_a2["contentHash"] = identity.content_hash(b"a2")
    doc_a2["previous"] = doc_a1["mac"]

    folder_b_mac = identity.node_mac(root, identity.component_for_folder("archive"))
    folder_b = dict(folder_a)
    folder_b.update(
        {"urn": "urn:org:folder:archive", "mac": folder_b_mac, "slug": "archive"}
    )
    doc_b1 = dict(doc_a1)
    doc_b1.update(
        {
            "urn": "urn:org:doc:archive/r.md",
            "mac": identity.node_mac(
                folder_b_mac, identity.component_for_doc("r.md", b"b1")
            ),
            "parentMac": folder_b_mac,
            "contentHash": identity.content_hash(b"b1"),
        }
    )
    doc_b2 = dict(doc_b1)
    doc_b2["mac"] = "d" * 64
    doc_b2["contentHash"] = identity.content_hash(b"b2")
    doc_b2["previous"] = doc_b1["mac"]

    data["nodes"] = [folder_a, doc_a1, doc_a2, folder_b, doc_b1, doc_b2]
    data["currentVersions"] = {
        doc_a1["urn"]: doc_a2["mac"],
        doc_b1["urn"]: doc_b2["mac"],
    }
    return data


def test_duplicate_slug_selects_the_urn_first_then_its_head_ref():
    """Issue #99 review: a slug shared by two URNs must not change cross-URN
    selection. The candidate URN is the one owning the last matching node (the
    pre-#99 rule) and only then does that URN's explicit head ref pick the
    version -- never the head ref of the first match's URN."""
    data = _two_urns_each_with_two_versions_sharing_a_slug()
    doc_a1, doc_a2, doc_b1, doc_b2 = (n for n in data["nodes"] if n["kind"] == "doc")
    assert doc_a1["slug"] == doc_b1["slug"] == "r.md"
    # Pre-#99 the last match in physical order won: B2. Resolving the head ref
    # of matches[0]'s URN instead would answer A2.
    assert manifest.node_by_slug(data, "r.md")["mac"] == doc_b2["mac"]
    # Each URN still resolves to its own explicit head, not to its last node.
    assert manifest.node_by_urn(data, doc_a1["urn"])["mac"] == doc_a2["mac"]
    assert manifest.node_by_urn(data, doc_b1["urn"])["mac"] == doc_b2["mac"]
    assert manifest.node_by_path(data, "archive/r.md")["mac"] == doc_b2["mac"]
    # Reordering one URN's own versions leaves that URN's resolution untouched.
    data["nodes"] = [doc_a2, doc_a1, doc_b1, doc_b2]
    assert manifest.node_by_urn(data, doc_a1["urn"])["mac"] == doc_a2["mac"]


def test_lineage_tip_is_derived_from_previous_links():
    """The tip is the version no other version names as its ``previous``."""
    data = _two_version_manifest()
    urn = data["nodes"][1]["urn"]
    assert (
        manifest.lineage_tip_mac(manifest.versions_of(data, urn))
        == (data["nodes"][2]["mac"])
    )
    # A fork (two tips) is reported as ambiguous, not silently resolved.
    v3 = dict(data["nodes"][2])
    v3["mac"] = "e" * 64
    v3["contentHash"] = identity.content_hash(b"fork")
    v3["previous"] = data["nodes"][1]["mac"]
    data["nodes"].append(v3)
    assert manifest.lineage_tip_mac(manifest.versions_of(data, urn)) is None


def test_current_version_legacy_manifest_falls_back_to_lineage_tip():
    """A pre-#99 manifest (no head-ref map) keeps resolving to the tip."""
    data = _two_version_manifest()
    del data["currentVersions"]
    urn = data["nodes"][1]["urn"]
    assert "currentVersions" not in data
    assert manifest.node_by_urn(data, urn)["mac"] == data["nodes"][2]["mac"]
    assert manifest.current_versions(data) == {}


def test_structural_issues_flags_head_ref_that_is_not_the_tip():
    """A head ref naming a non-tip version is reported."""
    data = _two_version_manifest()
    urn = data["nodes"][1]["urn"]
    data["currentVersions"][urn] = data["nodes"][1]["mac"]
    issues = manifest.structural_issues(data)
    assert any("is not the lineage tip" in i for i in issues)


def test_structural_issues_flags_dangling_head_ref():
    """A head ref naming a version that is not in the manifest is reported."""
    data = _two_version_manifest()
    urn = data["nodes"][1]["urn"]
    data["currentVersions"][urn] = "a" * 64
    issues = manifest.structural_issues(data)
    assert any("points at unknown version" in i for i in issues)


def test_structural_issues_flags_forked_lineage():
    """A URN whose versions do not form a single chain is reported."""
    data = _two_version_manifest()
    v1 = data["nodes"][1]
    v3 = dict(data["nodes"][2])
    v3["mac"] = "e" * 64
    v3["contentHash"] = identity.content_hash(b"fork")
    v3["previous"] = v1["mac"]
    data["nodes"].append(v3)
    issues = manifest.structural_issues(data)
    assert any("does not have exactly one tip" in i for i in issues)


def test_structural_issues_clean_when_head_ref_is_the_tip():
    """A head ref on the lineage tip produces no current-version issue."""
    data = _two_version_manifest()
    issues = manifest.structural_issues(data)
    assert not any("currentVersions" in i or "one tip" in i for i in issues)


def test_add_node_sets_explicit_head_ref_and_leaves_nodes_untouched():
    """Appending a version writes the head ref; existing nodes stay as-is."""
    data = _two_version_manifest()
    repo = manifest.ManifestRepository.__new__(manifest.ManifestRepository)
    repo._data = data
    urn = data["nodes"][1]["urn"]
    before = [dict(node) for node in data["nodes"]]
    v3 = dict(data["nodes"][2])
    v3["mac"] = "e" * 64
    v3["previous"] = data["nodes"][2]["mac"]
    repo.add_node(v3)
    assert data["currentVersions"][urn] == v3["mac"]
    assert data["nodes"][:-1] == before
    assert manifest.node_by_urn(data, urn)["mac"] == v3["mac"]


def test_add_node_does_not_add_a_head_ref_for_folders():
    """Folders are not versioned; they must not appear in the head-ref map."""
    data = _valid_manifest()
    repo = manifest.ManifestRepository.__new__(manifest.ManifestRepository)
    repo._data = data
    folder = dict(data["nodes"][0])
    folder["mac"] = "f" * 64
    repo.add_node(folder)
    assert data.get("currentVersions", {}) == {}


def test_validate_enforces_head_ref_shape_only():
    """The head-ref map is part of the load-time schema (shape, not targets)."""
    data = _two_version_manifest()
    assert manifest.validate(data) == []
    data["currentVersions"] = ["not", "a", "map"]
    errors = manifest.validate(data)
    assert any("currentVersions must be a mapping" in e for e in errors)
    data["currentVersions"] = {"urn:org:doc:reports/r.md": "zz"}
    errors = manifest.validate(data)
    assert any("must be a 64-hex version MAC" in e for e in errors)


def test_dangling_head_ref_is_a_verify_time_issue_not_a_load_error():
    """A well-formed ref naming a missing version must not block load()."""
    data = _two_version_manifest()
    urn = data["nodes"][1]["urn"]
    data["currentVersions"][urn] = "a" * 64
    assert manifest.validate(data) == []
    issues = manifest.structural_issues(data)
    assert any("points at unknown version" in i for i in issues)
