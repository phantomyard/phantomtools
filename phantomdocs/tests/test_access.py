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
        category-4: { label: "Sensitive project (umbrella)", scope: project }
        category-4-almaponia: { label: "Sensitive - ALMAPONIA", scope: project, owner: almaponia }
        category-4-proyecto2: { label: "Sensitive - Proyecto2", scope: project, owner: proyecto2 }
        category-4-almaponia-finance: { label: "Sensitive - ALMAPONIA finance", scope: project, owner: almaponia }
    roles:
      - id: cfo
        access_level: level-2
        security_exceptions: []
      - id: chief_of_staff
        access_level: level-2
        security_exceptions: [category-3, category-4]
      - id: project_lead
        access_level: level-2
        security_exceptions: []
    actors:
      - id: roberto
        role: cfo
        actor_exceptions: []
      - id: elena
        role: cfo
        actor_exceptions: [category-3]
      - id: pepa
        role: chief_of_staff
        actor_exceptions: []
      - id: alma
        role: project_lead
        actor_exceptions: [category-4-almaponia]
""")


def _org(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG_YAML, encoding="utf-8")
    return load_org(str(p))


def test_resolved_categories(tmp_path):
    org = _org(tmp_path)
    assert resolved_categories(org, "roberto") == ["category-1", "category-2"]
    assert resolved_categories(org, "elena") == [
        "category-1",
        "category-2",
        "category-3",
    ]
    assert resolved_categories(org, "pepa") == [
        "category-1",
        "category-2",
        "category-3",
        "category-4",
    ]
    assert resolved_categories(org, "alma") == [
        "category-1",
        "category-2",
        "category-4-almaponia",
    ]
    assert resolved_categories(org, "unknown") == []


def test_can_read_fail_closed(tmp_path):
    org = _org(tmp_path)
    assert can_read(org, "roberto", 2) is True
    assert can_read(org, "roberto", 3) is False
    assert can_read(org, "unknown", 1) is False


def test_can_read_hierarchical_umbrella(tmp_path):
    """Holding the parent category-4 grants every category-4-* leaf."""
    org = _org(tmp_path)
    # pepa holds category-4 (umbrella) -> any project's sensitive docs
    assert can_read(org, "pepa", "category-4-almaponia") is True
    assert can_read(org, "pepa", "category-4-proyecto2") is True
    # alma holds only category-4-almaponia -> only her project
    assert can_read(org, "alma", "category-4-almaponia") is True
    assert can_read(org, "alma", "category-4-proyecto2") is False
    # alma cannot read org-level category-3 (finance/secretariat credentials)
    assert can_read(org, "alma", "category-3") is False
    assert can_read(org, "alma", 3) is False
    # roberto has category-3 but NOT category-4 -> cannot read project-sensitive
    assert can_read(org, "roberto", "category-4-almaponia") is False


def test_can_read_undeclared_category_fails_closed(tmp_path):
    """An undeclared category has no place in the declared hierarchy, so it is
    denied even for an umbrella holder (issue #45: explicit relations)."""
    org = _org(tmp_path)
    # pepa holds category-4 (umbrella), but "category-4-typo" is not declared.
    assert can_read(org, "pepa", "category-4-typo") is False


def test_can_read_leaf_grants_own_subcategory(tmp_path):
    """A project-specific leaf grants its own declared sub-categories (same
    branch), but a peer project's leaf does not."""
    org = _org(tmp_path)
    assert can_read(org, "alma", "category-4-almaponia-finance") is True
    assert can_read(org, "alma", "category-4-proyecto2") is False


def test_can_write_requires_owners(tmp_path):
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1) is False
    assert can_write(org, "roberto", 1, []) is False
    assert can_write(org, "roberto", 1, None) is False


def test_can_write_requires_read(tmp_path):
    org = _org(tmp_path)
    assert can_write(org, "roberto", 3, ["cfo"]) is False


def test_can_write_allows_role_owner(tmp_path):
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1, ["cfo"]) is True
    assert can_write(org, "elena", 1, ["cfo"]) is True


def test_can_write_allows_actor_owner(tmp_path):
    org = _org(tmp_path)
    assert can_write(org, "roberto", 1, ["roberto"]) is True
    assert can_write(org, "elena", 1, ["roberto"]) is False


def test_can_write_project_scope(tmp_path):
    """A project lead writes their project's sensitive docs; an overseer
    (umbrella category-4 + owner role) can too; a peer lead cannot."""
    org = _org(tmp_path)
    owners = ["alma", "chief_of_staff", "cfo"]
    # alma is owner (actor) and can read category-4-almaponia
    assert can_write(org, "alma", "category-4-almaponia", owners) is True
    # pepa (chief_of_staff, category-4 umbrella + owner role) can write
    assert can_write(org, "pepa", "category-4-almaponia", owners) is True
    # roberto has no category-4 -> cannot read -> cannot write (cfo owner or not)
    assert can_write(org, "roberto", "category-4-almaponia", owners) is False
    # a peer lead cannot write a project she does not own
    assert can_write(org, "alma", "category-4-proyecto2", owners) is False


def test_can_write_denies_unrelated_same_category(tmp_path):
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


def test_malformed_categories_none_fails_closed(tmp_path):
    """`categories: null` (non-list) must not crash and must grant nothing
    (issue #77)."""
    p = tmp_path / "org.yaml"
    p.write_text(
        "version: 1\n"
        "organization: {id: org1}\n"
        "policies:\n"
        "  access_levels:\n"
        "    level-1: {categories: null}\n"
        "  security_categories: {category-1: {}}\n"
        "roles:\n"
        "  - {id: ceo, access_level: level-1, security_exceptions: null}\n"
        "actors:\n"
        "  - {id: marco, role: ceo, actor_exceptions: null}\n",
        encoding="utf-8",
    )
    org = load_org(str(p))
    assert resolved_categories(org, "marco") == []
    assert can_read(org, "marco", "category-1") is False


def test_malformed_categories_string_fails_closed(tmp_path):
    """A string `categories` must not be iterated char-by-char (type
    confusion) — it grants nothing (issue #77)."""
    p = tmp_path / "org.yaml"
    p.write_text(
        "version: 1\n"
        "organization: {id: org1}\n"
        "policies:\n"
        "  access_levels:\n"
        "    level-1: {categories: \"1\"}\n"
        "  security_categories: {category-1: {}}\n"
        "roles:\n"
        "  - {id: ceo, access_level: level-1, security_exceptions: []}\n"
        "actors:\n"
        "  - {id: marco, role: ceo, actor_exceptions: []}\n",
        encoding="utf-8",
    )
    org = load_org(str(p))
    assert resolved_categories(org, "marco") == []
    assert can_read(org, "marco", 1) is False
