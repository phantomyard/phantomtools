import tempfile
import unittest
from pathlib import Path

from phantomorg.spec.loader import OrgSpecError, load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


class TestSchema(unittest.TestCase):
    def test_loads_au_org_yaml(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(spec.organization.id, "verdant-aquaponics")
        self.assertEqual(len(spec.actors), 5)
        self.assertEqual(len(spec.roles), 5)
        self.assertEqual(len(spec.departments), 4)

    def test_rejects_missing_required_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "org.yaml"
            bad.write_text("version: 1\norganization: {id: x}\n", encoding="utf-8")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(bad)

    def test_rejects_nonexistent_file(self):
        with self.assertRaises(OrgSpecError):
            load_org_yaml("no/exists/org.yaml")

    def test_actor_npub_parsed_from_dict(self):
        npub = "npub16fg8f93njtj7nervk94w6kgtdp4vtze8dzfer2qjc394mx6luzgqavqwgg"
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: x, name: X, sector: ngo, languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_policy: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: [], npub: " + npub + "}]\n"
                "policies: {access_levels: {level-3: {label: L, categories: []}}, "
                "security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, message_types: [REQUEST], "
                "max_hops: 3}\n",
                encoding="utf-8",
            )
            spec = load_org_yaml(org)
            self.assertEqual(spec.actors[0].npub, npub)
            self.assertIsNone(spec.actors[0].telegram_bot)

    def test_actor_npub_present_in_au(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(
            spec.actors[0].npub,
            "npub16fg8f93njtj7nervk94w6kgtdp4vtze8dzfer2qjc394mx6luzgqavqwgg",
        )

    def test_security_category_parent_undeclared(self):
        # Issue #100: a parent referencing an undeclared category is a
        # reference error (mirrors the department parent check).
        import yaml as _yaml

        from phantomorg.validator import validate_org

        with tempfile.TemporaryDirectory() as tmp:
            doc = _yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
            doc["policies"]["security_categories"]["category-3"]["parent"] = (
                "category-99"
            )
            p = Path(tmp) / "org.yaml"
            p.write_text(_yaml.safe_dump(doc), encoding="utf-8")
            _, result = validate_org(str(p))
            self.assertTrue(
                any("parent 'category-99' does not exist" in e for e in result.errors)
            )

    def test_security_category_parent_self(self):
        # Issue #100: a category cannot be its own parent.
        import yaml as _yaml

        from phantomorg.validator import validate_org

        with tempfile.TemporaryDirectory() as tmp:
            doc = _yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
            doc["policies"]["security_categories"]["category-3"]["parent"] = (
                "category-3"
            )
            p = Path(tmp) / "org.yaml"
            p.write_text(_yaml.safe_dump(doc), encoding="utf-8")
            _, result = validate_org(str(p))
            self.assertTrue(any("cannot be its own parent" in e for e in result.errors))

    def test_security_category_parent_two_node_cycle(self):
        # Issue #100 (review): a two-node parent cycle must be rejected.
        # a.parent=b + b.parent=a would make can_read() authorize both.
        import yaml as _yaml

        from phantomorg.validator import validate_org

        with tempfile.TemporaryDirectory() as tmp:
            doc = _yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
            doc["policies"]["security_categories"]["category-1"]["parent"] = (
                "category-2"
            )
            doc["policies"]["security_categories"]["category-2"]["parent"] = (
                "category-1"
            )
            p = Path(tmp) / "org.yaml"
            p.write_text(_yaml.safe_dump(doc), encoding="utf-8")
            _, result = validate_org(str(p))
            self.assertTrue(any("parent cycle detected" in e for e in result.errors))

    def test_security_category_parent_three_node_cycle(self):
        # Issue #100 (review): a longer (3-node) parent cycle must be
        # rejected too.
        import yaml as _yaml

        from phantomorg.validator import validate_org

        with tempfile.TemporaryDirectory() as tmp:
            doc = _yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
            doc["policies"]["security_categories"]["category-1"]["parent"] = (
                "category-2"
            )
            doc["policies"]["security_categories"]["category-2"]["parent"] = (
                "category-3"
            )
            doc["policies"]["security_categories"]["category-3"]["parent"] = (
                "category-1"
            )
            p = Path(tmp) / "org.yaml"
            p.write_text(_yaml.safe_dump(doc), encoding="utf-8")
            _, result = validate_org(str(p))
            self.assertTrue(any("parent cycle detected" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
