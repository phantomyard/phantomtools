import tempfile
import unittest
from pathlib import Path

import yaml

from phantomforge.spec.loader import load_org_yaml
from phantomforge.wizard.new_org import new_org
from phantomforge.wizard.templates import available_templates, departments_for


class TestTemplates(unittest.TestCase):
    def test_available_templates_includes_ngo(self):
        self.assertIn("ngo", available_templates())

    def test_unknown_template_raises(self):
        with self.assertRaises(ValueError):
            departments_for("does-not-exist")

    def test_new_org_without_template_has_single_department(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = new_org("acme", "ACME", "pyme", ["es"], base_dir=Path(tmp))
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(len(doc["departments"]), 1)

    def test_new_org_with_ngo_template_has_four_departments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = new_org(
                "aquaponics-united-2",
                "AU 2",
                "ngo",
                ["es"],
                base_dir=Path(tmp),
                template="ngo",
            )
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            dept_ids = {d["id"] for d in doc["departments"]}
            self.assertEqual(
                dept_ids, {"direccion", "operaciones", "formacion", "finanzas"}
            )

    def test_org_created_from_template_is_shape_valid_once_role_and_actor_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = new_org(
                "test-fin",
                "Test Fin",
                "finance",
                ["es"],
                base_dir=Path(tmp),
                template="finance",
            )
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            doc["roles"].append(
                {
                    "id": "ceo",
                    "name": "CEO",
                    "department": "direccion",
                    "reports_to": None,
                    "access_level": "level-3",
                }
            )
            doc["actors"].append({"id": "carla", "role": "ceo", "tools": ["email"]})
            path.write_text(
                yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            spec = load_org_yaml(path)  # no debe lanzar
            self.assertEqual(len(spec.departments), 4)


if __name__ == "__main__":
    unittest.main()
