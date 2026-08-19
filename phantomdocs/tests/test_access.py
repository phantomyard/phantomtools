import textwrap

from phantomdocs.access import (
    can_read,
    can_write,
    load_org,
    resolved_categories,
)

ORG_YAML = textwrap.dedent("""\
    version: 1
    policies:
      access_levels:
        level-3: { label: Executive, categories: [1, 2, 3] }
        level-2: { label: Operative, categories: [1, 2] }
        level-1: { label: Restricted, categories: [1] }
      security_categories:
        category-0: { label: "Absolute exception" }
        category-1: { label: Public }
        category-2: { label: Confidential }
        category-3: { label: "Sensitive financial" }
    roles:
      - id: cfo
        access_level: level-2
        security_exceptions: []
    actors:
      - id: roberto
        role: cfo
        actor_exceptions: []
      - id: elena
        role: cfo
        actor_exceptions: [category-3]
""")


def _org(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG_YAML, encoding="utf-8")
    return load_org(str(p))


def test_resolved_categories(tmp_path):
    org = _org(tmp_path)
    assert resolved_categories(org, "roberto") == [1, 2]
    assert resolved_categories(org, "elena") == [1, 2, 3]
    assert resolved_categories(org, "unknown") == []


def test_can_read_fail_closed(tmp_path):
    org = _org(tmp_path)
    assert can_read(org, "roberto", 2) is True
    assert can_read(org, "roberto", 3) is False
    assert can_read(org, "unknown", 1) is False


def test_can_write_requires_owners(tmp_path):
    """No owners -> denied, even for a category the actor can read (spec §9)."""
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1) is False
    assert can_write(org, "roberto", 1, []) is False
    assert can_write(org, "roberto", 1, None) is False


def test_can_write_requires_read(tmp_path):
    """An owner who cannot read the category cannot write it."""
    org = _org(tmp_path)
    # roberto has clearance [1,2]; category-3 is unreadable -> write denied.
    assert can_write(org, "roberto", 3, ["cfo"]) is False


def test_can_write_allows_role_owner(tmp_path):
    """owners may name a ROLE id; any actor holding that role may write."""
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1, ["cfo"]) is True
    assert can_write(org, "elena", 1, ["cfo"]) is True


def test_can_write_allows_actor_owner(tmp_path):
    """owners may name an ACTOR id directly."""
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1, ["roberto"]) is True
    assert can_write(org, "elena", 1, ["roberto"]) is False


def test_can_write_denies_unrelated_same_category(tmp_path):
    """An actor who can read the category but is not an owner is denied."""
    org = _org(tmp_path)
    assert can_read(org, "elena", 1) is True
    assert can_write(org, "elena", 1, ["roberto"]) is False


def test_load_org_accepts_version_1(tmp_path):
    assert load_org(str(_org_path(tmp_path))) is not None


def test_load_org_rejects_missing_version(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(
        "policies:\n  access_levels: {}\n  security_categories: {}\n",
        encoding="utf-8",
    )
    import pytest

    with pytest.raises(ValueError, match="schema version must be 1"):
        load_org(str(p))


def test_load_org_rejects_unknown_version(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text("version: 2\norganization:\n  id: demo\n", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="schema version must be 1"):
        load_org(str(p))


def _org_path(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG_YAML, encoding="utf-8")
    return p
