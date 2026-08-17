import tempfile
import unittest
from pathlib import Path

from phantomforge.spec.loader import OrgSpecError, load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


class TestSchema(unittest.TestCase):
    def test_loads_au_org_yaml(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(spec.organization.id, "aquaponics-united")
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
        npub = "npub1p585a0cqnf949lc6jsfff49tqenqmxche2khtvsqfhn6p2a83qtspju756"
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
            "npub1p585a0cqnf949lc6jsfff49tqenqmxche2khtvsqfhn6p2a83qtspju756",
        )


if __name__ == "__main__":
    unittest.main()
