from phantomdocs import identity


def test_content_hash_deterministic():
    assert identity.content_hash(b"hello") == identity.content_hash(b"hello")
    assert len(identity.content_hash(b"hello")) == 64


def test_root_mac_deterministic_and_hex():
    a = identity.root_mac("org", "npubX", "docs")
    b = identity.root_mac("org", "npubX", "docs")
    c = identity.root_mac("org", "npubY", "docs")
    assert a == b
    assert a != c
    assert len(a) == 64


def test_root_mac_distinguishes_orgs_with_empty_pubkey():
    """Two orgs with the documented defaults (empty pubkey, namespace docs)
    must never collide: the validated org id is part of the preimage."""
    alpha = identity.root_mac("alpha", "", "docs")
    beta = identity.root_mac("beta", "", "docs")
    assert alpha != beta


def test_node_mac_inherits_parent():
    parent_a = identity.root_mac("org", "npubX", "docs")
    parent_b = identity.root_mac("org", "npubX", "other")
    comp = identity.component_for_folder("actas")
    assert identity.node_mac(parent_a, comp) != identity.node_mac(parent_b, comp)


def test_doc_mac_binds_content():
    parent = identity.root_mac("org", "npubX", "docs")
    m1 = identity.node_mac(parent, identity.component_for_doc("a.md", b"one"))
    m2 = identity.node_mac(parent, identity.component_for_doc("a.md", b"two"))
    assert m1 != m2


def test_doc_version_mac_binds_predecessor():
    parent = identity.root_mac("org", "npubX", "docs")
    v1 = identity.doc_version_mac(parent, None, "a.md", b"one")
    v2 = identity.doc_version_mac(parent, v1, "a.md", b"two")
    assert v1 != v2
    # The first version is backward-compatible with node_mac(parent, component).
    assert v1 == identity.node_mac(parent, identity.component_for_doc("a.md", b"one"))
    # Same content but a different predecessor -> a distinct identity (rollback).
    restored = identity.doc_version_mac(parent, v2, "a.md", b"one")
    assert restored != v1
    assert restored != v2


def test_self_describing_forms():
    mac = identity.root_mac("org", "npubX", "docs")
    assert identity.full_id(mac) == f"sha2-256-256:{mac}"
    assert identity.display_id(mac) == f"sha2-256-128:{mac[:32]}"
    assert len(mac[:32]) == 32  # 128-bit truncation floor


def test_is_valid_hex64():
    assert identity.is_valid_hex64("a" * 64) is True
    assert identity.is_valid_hex64("g" * 64) is False
    assert identity.is_valid_hex64("a" * 63) is False
    assert identity.is_valid_hex64("A" * 64) is False
