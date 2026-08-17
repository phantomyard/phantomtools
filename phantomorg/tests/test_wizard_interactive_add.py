import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from phantomorg.wizard import interactive
from phantomorg.wizard.new_org import new_org

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


class TestAddDepartmentWizardChoices(unittest.TestCase):
    @patch("phantomorg.wizard.interactive.add_department")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_offers_existing_departments_as_parent_choices(
        self, mock_prompt, mock_add_department
    ):
        mock_prompt.side_effect = ["new_dept", "New Dept", "operaciones", "level-2"]
        interactive.run_add_department_wizard(AU_ORG)

        parent_call = mock_prompt.call_args_list[2]
        choices = parent_call.kwargs["type"].choices
        self.assertIn("none", choices)
        self.assertIn("operaciones", choices)
        self.assertIn("direccion", choices)

        mock_add_department.assert_called_once_with(
            AU_ORG, "new_dept", "New Dept", "operaciones", "level-2"
        )

    @patch("phantomorg.wizard.interactive.add_department")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_ninguno_maps_to_none_parent(self, mock_prompt, mock_add_department):
        mock_prompt.side_effect = ["root2", "Root 2", "none", "level-3"]
        interactive.run_add_department_wizard(AU_ORG)
        mock_add_department.assert_called_once_with(
            AU_ORG, "root2", "Root 2", None, "level-3"
        )


class TestAddRoleWizardChoices(unittest.TestCase):
    @patch("phantomorg.wizard.interactive.add_role")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_offers_existing_departments_and_roles(self, mock_prompt, mock_add_role):
        mock_prompt.side_effect = [
            "new_role",
            "New Role",
            "operaciones",
            "chief_of_staff",
            "",
            "level-2",
            "",
        ]
        interactive.run_add_role_wizard(AU_ORG)

        dept_call = mock_prompt.call_args_list[2]
        self.assertEqual(
            set(dept_call.kwargs["type"].choices),
            {"direccion", "operaciones", "formacion", "finanzas"},
        )

        reports_to_call = mock_prompt.call_args_list[3]
        reports_to_choices = set(reports_to_call.kwargs["type"].choices)
        self.assertIn("none", reports_to_choices)
        self.assertIn("ceo", reports_to_choices)
        self.assertIn("chief_of_staff", reports_to_choices)

        mock_add_role.assert_called_once_with(
            AU_ORG,
            "new_role",
            "New Role",
            "operaciones",
            "chief_of_staff",
            "level-2",
            functions=[],
            reports_to_human=None,
        )

    @patch("phantomorg.wizard.interactive.add_role")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_ninguno_maps_to_none_reports_to(self, mock_prompt, mock_add_role):
        mock_prompt.side_effect = [
            "root_role",
            "Root Role",
            "direccion",
            "none",
            "",
            "level-3",
            "",
        ]
        interactive.run_add_role_wizard(AU_ORG)
        args, _ = mock_add_role.call_args
        self.assertIsNone(args[4])  # positional reports_to

    def test_raises_if_no_departments_exist_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path = new_org("empty", "Empty", "pyme", ["es"], base_dir=Path(tmp))
            with self.assertRaises(SystemExit):
                # new_org already creates "direccion" by default without --template... so
                # we force a truly empty case by removing 'departments'.
                doc = yaml.safe_load(org_path.read_text(encoding="utf-8"))
                doc["departments"] = []
                org_path.write_text(
                    yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
                )
                interactive.run_add_role_wizard(org_path)


class TestAddActorWizardChoices(unittest.TestCase):
    @patch("phantomorg.wizard.interactive.add_actor")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_offers_existing_roles(self, mock_prompt, mock_add_actor):
        mock_prompt.side_effect = ["new_actor", "cfo", "", ""]
        interactive.run_add_actor_wizard(AU_ORG)

        role_call = mock_prompt.call_args_list[1]
        self.assertEqual(
            set(role_call.kwargs["type"].choices),
            {"ceo", "chief_of_staff", "cfo", "project_lead", "training_lead"},
        )
        mock_add_actor.assert_called_once_with(
            AU_ORG, "new_actor", "cfo", [], telegram_bot=None
        )

    def test_raises_if_no_roles_exist_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path = new_org("empty2", "Empty 2", "pyme", ["es"], base_dir=Path(tmp))
            with self.assertRaises(SystemExit):
                interactive.run_add_actor_wizard(org_path)  # roles: [] freshly created


if __name__ == "__main__":
    unittest.main()
