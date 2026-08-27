"""ManifestRepository — the typed boundary over the manifest dict (issue #46).

The repository wraps the YAML persistence DTO and exposes typed accessors and
mutators so callers stop reaching into ``manifest["nodes"]`` / ``manifest["refs"]``
directly. These tests cover the header accessors, the mutations, and the
delegated lookups.
"""

from phantomdocs import identity, manifest


def _repo():
    data = manifest.empty_manifest("org", "docs", identity.root_mac("org", "", "docs"))
    return manifest.ManifestRepository(data)


def _folder_node(repo):
    mac = identity.node_mac(repo.root_mac, identity.component_for_folder("reports"))
    return {
        "urn": "urn:org:folder:reports",
        "mac": mac,
        "parentMac": repo.root_mac,
        "kind": "folder",
        "slug": "reports",
        "category": "category-1",
        "owners": ["cfo"],
    }


def test_header_accessors():
    repo = _repo()
    assert repo.org == "org"
    assert repo.namespace == "docs"
    assert repo.tenant == "single"
    assert repo.root_mac == identity.root_mac("org", "", "docs")


def test_add_node_appends_and_returns():
    repo = _repo()
    node = _folder_node(repo)
    returned = repo.add_node(node)
    assert returned is node
    assert repo.nodes == [node]
    assert repo.data["nodes"] == [node]  # mutates the wrapped dict


def test_add_version_aliases_add_node():
    repo = _repo()
    node = {"urn": "u", "mac": "a" * 64, "kind": "doc"}
    repo.add_version(node)
    assert repo.nodes == [node]


def test_set_ref_and_lookup():
    repo = _repo()
    node = _folder_node(repo)
    repo.add_node(node)
    repo.set_ref("latest", {"mac": node["mac"]})
    assert repo.refs == {"latest": {"mac": node["mac"]}}
    assert repo.node_by_mac(node["mac"]) is node


def test_lookups_delegate():
    repo = _repo()
    node = _folder_node(repo)
    repo.add_node(node)
    assert repo.node_by_urn("urn:org:folder:reports") is node
    assert repo.node_by_path("reports") is node
    assert repo.node_by_slug("reports") is node
    assert repo.resolve_node("reports") is node
    assert repo.versions_of("urn:org:folder:reports") == [node]


def test_refs_defaults_to_empty_dict():
    repo = _repo()
    assert repo.refs == {}
    assert repo.nodes == []
