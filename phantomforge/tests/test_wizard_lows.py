"""Regression tests for the wizard LOW findings (L2-L5) from the
adversarial review of `pf setup` / mutations / compiler templates:

- L2: `pf setup` on a malformed org.yaml crashes with a raw KeyError
  traceback mid-wizard (missing organization.id / dept / role ids).
- L3: wizard mode leaks raw tracebacks for predictable errors
  (DuplicateIdError from add-*, FileExistsError from new-org).
- L4: no directory fsync after os.replace in backup_org_file / _save
  (rename itself not durable across a power loss).
- L5: Jinja2 environment uses default Undefined — a typo in a template
  variable renders silently empty instead of failing the build.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from click.testing import CliRunner
from jinja2 import StrictUndefined, UndefinedError

from phantomforge.cli import main
from phantomforge.compiler.build import _env
from phantomforge.wizard import interactive, mutations


def _write_org(tmp: Path, doc: dict) -> Path:
    org = tmp / "mi-org" / "org.yaml"
    org.parent.mkdir(parents=True)
    org.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return org


def _valid_doc(org_id: str = "mi-org") -> dict:
    return {
        "version": 1,
        "organization": {
            "id": org_id,
            "name": "Mi Org",
            "sector": "pyme",
            "languages": ["es"],
            "default_language": "es",
        },
        "departments": [
            {
                "id": "ventas",
                "name": "Ventas",
                "parent": None,
                "access_policy": "level-2",
            },
        ],
        "roles": [],
        "actors": [],
    }


class TestL2SetupMalformedOrg(unittest.TestCase):
    """`pf setup` on a malformed org.yaml must fail with a friendly
    message, not a raw KeyError traceback."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.runner = CliRunner()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_setup(self, org: Path, extra_answers: str = ""):
        # Provide an empty phantombot dir so the wizard skips the
        # personas-root question entirely and goes straight to the org
        # question ("y" = already have an org.yaml, then the path). The
        # structure check must fire BEFORE persona questions, so extra
        # answers are irrelevant.
        pb = self.tmp / "personas"
        pb.mkdir(exist_ok=True)
        answers = f"y\n{org}\n" + extra_answers
        return self.runner.invoke(
            main,
            [
                "setup",
                "--base",
                str(self.tmp / "orgs"),
                "--phantombot-dir",
                str(pb),
            ],
            input=answers,
        )

    def test_missing_organization_id(self):
        doc = _valid_doc()
        del doc["organization"]["id"]
        org = _write_org(self.tmp, doc)
        result = self._run_setup(org)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("not a valid org.yaml", result.output)
        self.assertIn("organization.id", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_department_missing_id(self):
        doc = _valid_doc()
        doc["departments"][0].pop("id")
        org = _write_org(self.tmp, doc)
        result = self._run_setup(org)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("not a valid org.yaml", result.output)
        self.assertIn("departments", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_role_missing_id(self):
        doc = _valid_doc()
        doc["roles"] = [
            {
                "name": "Sin Id",
                "department": "ventas",
                "reports_to": None,
                "reports_to_human": None,
                "functions": [],
                "access_level": "level-2",
                "security_exceptions": [],
            }
        ]
        org = _write_org(self.tmp, doc)
        result = self._run_setup(org)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("roles", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_valid_org_structure_passes_check(self):
        # The helper itself accepts a well-formed doc (no exception).
        org = _write_org(self.tmp, _valid_doc())
        with mock.patch(
            "phantomforge.wizard.interactive.find_personas_dirs", return_value=[]
        ):
            interactive._require_org_doc_structure(yaml.safe_load(org.read_text()), org)


class TestL3WizardCatchesPredictableErrors(unittest.TestCase):
    """Wizard mode must present DuplicateIdError / FileExistsError as
    clean messages, not raw tracebacks."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.runner = CliRunner()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_new_org_wizard_existing_file_is_clean(self):
        # The wizard calls new_org() which raises FileExistsError when
        # the org dir already exists. _run_wizard must present it as a
        # clean message, not a raw traceback. (Mocked: new-org has no
        # --base, so a real call would touch ./organizations in CWD.)
        def _boom(*a, **k):
            raise FileExistsError("/x/org.yaml already exists")

        from phantomforge.wizard import interactive

        with mock.patch.object(interactive, "new_org", side_effect=_boom):
            result = self.runner.invoke(
                main,
                ["new-org"],
                input="mi-org\nMi Org\npyme\nes\nnone\n",
            )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_add_department_wizard_duplicate_is_clean(self):
        org = _write_org(self.tmp, _valid_doc())
        # First add "ventas" via wizard -> DuplicateIdError.
        answers = "ventas\nVentas\nnone\nlevel-2\n"
        result = self.runner.invoke(
            main, ["add-department", "--org", str(org)], input=answers
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_setup_wizard_duplicate_persona_is_clean(self):
        # A persona whose id is already an actor in the org -> the apply
        # step raises DuplicateIdError; it must surface as a message.
        doc = _valid_doc()
        doc["actors"] = [
            {
                "id": "maria",
                "role": None,
                "telegram_bot": None,
                "tools": [],
                "tools_excluded": [],
                "actor_exceptions": [],
                "tone": None,
            }
        ]
        org = _write_org(self.tmp, doc)
        pb = self.tmp / "personas"
        (pb / "maria").mkdir(parents=True)
        (pb / "maria" / "SOUL.md").write_text(
            "# maria\nRole: Vendedor", encoding="utf-8"
        )

        # y (have org) | path | dept | role | no new persona | apply
        answers = f"y\n{org}\nventas\nvendedor\nn\ny\n"
        result = self.runner.invoke(
            main,
            ["setup", "--base", str(self.tmp / "orgs"), "--phantombot-dir", str(pb)],
            input=answers,
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertNotIn("Traceback", result.output)


class TestL4DirFsync(unittest.TestCase):
    """backup_org_file / _save fsync the parent directory after the
    os.replace, so the rename itself is durable."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.org = self.tmp / "org.yaml"
        self.org.write_text("version: 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_backup_org_file_fsyncs_parent_dir(self):
        with (
            mock.patch("phantomforge.wizard.mutations.os.replace") as mock_replace,
            mock.patch("phantomforge.wizard.mutations._fsync_dir") as mock_fsync,
        ):
            backup = mutations.backup_org_file(self.org)
            self.assertIsNotNone(backup)
            mock_replace.assert_called_once()
            mock_fsync.assert_called_once_with(backup.parent)

    def test_save_fsyncs_parent_dir(self):
        with (
            mock.patch("phantomforge.wizard.mutations.os.replace") as mock_replace,
            mock.patch("phantomforge.wizard.mutations._fsync_dir") as mock_fsync,
        ):
            mutations._save(self.org, {"version": 1})
            # Two replaces: one for the backup (inside backup_org_file),
            # one for the org.yaml itself. Both must fsync the dir.
            self.assertEqual(mock_replace.call_count, 2)
            self.assertEqual(mock_fsync.call_count, 2)
            mock_fsync.assert_called_with(self.org.parent)

    def test_fsync_dir_is_best_effort(self):
        # A directory that cannot be opened must not raise.
        with mock.patch("phantomforge.wizard.mutations.os.open", side_effect=OSError):
            mutations._fsync_dir(self.tmp)  # must not raise


class TestL5StrictUndefined(unittest.TestCase):
    """A typo in a template variable fails the build loudly instead of
    rendering silently empty."""

    def test_env_uses_strict_undefined(self):
        self.assertIs(_env().undefined, StrictUndefined)

    def test_undefined_variable_raises(self):
        env = _env()
        template = env.from_string("Hello {{ missing_var }}")
        with self.assertRaises(UndefinedError):
            template.render()

    def test_defined_variables_render(self):
        env = _env()
        template = env.from_string("Hello {{ name }}")
        self.assertEqual(template.render(name="world"), "Hello world")


if __name__ == "__main__":
    unittest.main()
