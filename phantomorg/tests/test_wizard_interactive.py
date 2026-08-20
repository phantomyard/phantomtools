import unittest
from unittest.mock import patch

from phantomorg.wizard import interactive


class TestNewOrgWizardTemplate(unittest.TestCase):
    """
    Before the fix, run_new_org_wizard() did not ask about the template
    and called new_org(...) without `template=`, so answering "ngo" to
    the sector question did not activate the `ngo` template (that
    question only feeds organization.sector, descriptive metadata).
    These tests check that there is now an explicit template question and
    that its answer reaches new_org().
    """

    @patch("phantomorg.wizard.interactive.new_org")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_wizard_passes_none_when_user_picks_ninguna(
        self, mock_prompt, mock_new_org
    ):
        mock_prompt.side_effect = [
            "acme",  # org_id
            "ACME",  # name
            "pyme",  # sector (free text, does not trigger a template)
            "es",  # languages
            "none",  # template
        ]
        interactive.run_new_org_wizard()
        mock_new_org.assert_called_once_with(
            "acme", "ACME", "pyme", ["es"], template=None
        )

    @patch("phantomorg.wizard.interactive.new_org")
    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_wizard_passes_chosen_template(self, mock_prompt, mock_new_org):
        mock_prompt.side_effect = [
            "verdant-aquaponics-3",
            "AU 3",
            "ngo",
            "es",
            "ngo",  # template chosen explicitly
        ]
        interactive.run_new_org_wizard()
        mock_new_org.assert_called_once_with(
            "verdant-aquaponics-3", "AU 3", "ngo", ["es"], template="ngo"
        )

    @patch("phantomorg.wizard.interactive.click.prompt")
    def test_template_prompt_offers_ninguna_plus_available_templates(self, mock_prompt):
        mock_prompt.side_effect = ["x", "X", "s", "es", "none"]
        with patch("phantomorg.wizard.interactive.new_org"):
            interactive.run_new_org_wizard()

        # The last prompt call is the template one; we check its choices.
        last_call = mock_prompt.call_args_list[-1]
        choices = last_call.kwargs["type"].choices
        self.assertIn("none", choices)
        self.assertIn("ngo", choices)


if __name__ == "__main__":
    unittest.main()
