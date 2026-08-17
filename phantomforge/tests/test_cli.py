import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from click.testing import CliRunner

from phantomforge.cli import main

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"
UCG_ORG = Path(__file__).parent.parent / "organizations/united-capital-group/org.yaml"

_FAKE_PERSONA_IDENTITY = """# Identity
**Role**: Analista de Datos
**Reports to**: Pepa
**Channel**: @Analista_bot
"""
_FAKE_PERSONA_TOOLS = """# Tools
- email
- drive
"""


class _TmpOrgsTestCase(unittest.TestCase):
    """Copies the real organizations/ into a per-test tmp dir, so the repo is never touched."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.orgs_dir = self.tmp / "organizations"
        shutil.copytree(Path(__file__).parent.parent / "organizations", self.orgs_dir)
        self.runner = CliRunner()

    def tearDown(self):
        self._tmpdir.cleanup()


class TestBasicCommands(_TmpOrgsTestCase):
    def test_validate_au(self):
        result = self.runner.invoke(
            main,
            ["validate", "--org", str(self.orgs_dir / "aquaponics-united/org.yaml")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("valid", result.output)

    def test_templates_lists_ngo(self):
        result = self.runner.invoke(main, ["templates"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ngo", result.output)

    def test_list_orgs(self):
        result = self.runner.invoke(main, ["list-orgs", "--base", str(self.orgs_dir)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("aquaponics-united", result.output)
        self.assertIn("united-capital-group", result.output)

    def test_build_then_deploy(self):
        out_dir = self.tmp / "dist"
        result = self.runner.invoke(
            main,
            [
                "build",
                "--org",
                str(self.orgs_dir / "aquaponics-united/org.yaml"),
                "--out",
                str(out_dir),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((out_dir / "alma" / "SOUL.md").exists())

        target_dir = self.tmp / "target"
        result = self.runner.invoke(
            main,
            ["deploy", "--from", str(out_dir), "--target", str(target_dir), "--yes"],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((target_dir / "alma").exists())


class TestBuildAllDeployAll(_TmpOrgsTestCase):
    def test_build_all_compiles_both_orgs(self):
        out_base = self.tmp / "dist"
        result = self.runner.invoke(
            main, ["build-all", "--base", str(self.orgs_dir), "--out", str(out_base)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((out_base / "aquaponics-united" / "alma" / "SOUL.md").exists())
        self.assertTrue(
            (out_base / "united-capital-group" / "anna" / "SOUL.md").exists()
        )
        self.assertIn("2 organization(s) compiled", result.output)

    def test_build_all_skips_invalid_org_without_aborting(self):
        # We break united-capital-group on purpose (invalid reference).
        ucg_path = self.orgs_dir / "united-capital-group/org.yaml"
        doc = yaml.safe_load(ucg_path.read_text(encoding="utf-8"))
        doc["actors"][0]["role"] = "role-that-does-not-exist"
        ucg_path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

        out_base = self.tmp / "dist"
        result = self.runner.invoke(
            main, ["build-all", "--base", str(self.orgs_dir), "--out", str(out_base)]
        )
        self.assertEqual(
            result.exit_code, 0, result.output
        )  # does not abort: 1 ok, 1 skipped
        self.assertTrue((out_base / "aquaponics-united" / "alma" / "SOUL.md").exists())
        self.assertFalse((out_base / "united-capital-group").exists())
        self.assertIn("1 organization(s) compiled, 1 skipped", result.output)

    def test_deploy_all_after_build_all(self):
        out_base, target_dir = self.tmp / "dist", self.tmp / "target"
        self.runner.invoke(
            main, ["build-all", "--base", str(self.orgs_dir), "--out", str(out_base)]
        )

        result = self.runner.invoke(
            main,
            [
                "deploy-all",
                "--base",
                str(self.orgs_dir),
                "--dist-base",
                str(out_base),
                "--target",
                str(target_dir),
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((target_dir / "alma").exists())
        self.assertTrue((target_dir / "anna").exists())

    def test_deploy_all_announces_archive_dir_creation(self):
        out_base, target_dir = self.tmp / "dist", self.tmp / "target"
        # First deploy: the manifest creates personas-archive/ -> announced.
        self.runner.invoke(
            main, ["build-all", "--base", str(self.orgs_dir), "--out", str(out_base)]
        )
        result = self.runner.invoke(
            main,
            [
                "deploy-all",
                "--base",
                str(self.orgs_dir),
                "--dist-base",
                str(out_base),
                "--target",
                str(target_dir),
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Backup archive created:", result.output)
        self.assertIn("personas-archive", result.output)

    def test_deploy_all_reports_missing_build(self):
        out_base, target_dir = self.tmp / "dist", self.tmp / "target"
        out_base.mkdir()
        # We only build AU manually, leaving UCG without a build.
        self.runner.invoke(
            main,
            [
                "build",
                "--org",
                str(self.orgs_dir / "aquaponics-united/org.yaml"),
                "--out",
                str(out_base / "aquaponics-united"),
            ],
        )

        result = self.runner.invoke(
            main,
            [
                "deploy-all",
                "--base",
                str(self.orgs_dir),
                "--dist-base",
                str(out_base),
                "--target",
                str(target_dir),
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no build", result.output)
        self.assertIn("1 organization(s) deployed", result.output)


class TestDeployPruneCli(_TmpOrgsTestCase):
    def test_prune_flag_removes_stale_actor(self):
        out_dir, target_dir = self.tmp / "dist", self.tmp / "target"
        au_org = self.orgs_dir / "aquaponics-united/org.yaml"

        self.runner.invoke(main, ["build", "--org", str(au_org), "--out", str(out_dir)])
        self.runner.invoke(
            main,
            ["deploy", "--from", str(out_dir), "--target", str(target_dir), "--yes"],
        )
        self.assertTrue((target_dir / "elena").exists())

        # We remove elena from the spec and recompile: her folder in out_dir
        # disappears from the build, but would stay orphaned in target without --prune.
        self.runner.invoke(
            main, ["remove-actor", "--org", str(au_org), "--id", "elena", "--yes"]
        )
        shutil.rmtree(out_dir)
        self.runner.invoke(main, ["build", "--org", str(au_org), "--out", str(out_dir)])

        result = self.runner.invoke(
            main,
            [
                "deploy",
                "--from",
                str(out_dir),
                "--target",
                str(target_dir),
                "--prune",
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((target_dir / "elena").exists())
        self.assertTrue((target_dir / "alma").exists())


class TestImportAuditApply(_TmpOrgsTestCase):
    def test_apply_adds_role_and_actor_to_target_org(self):
        persona_dir = self.tmp / "personas" / "carla"
        persona_dir.mkdir(parents=True)
        (persona_dir / "IDENTITY.md").write_text(
            _FAKE_PERSONA_IDENTITY, encoding="utf-8"
        )
        (persona_dir / "tools.md").write_text(_FAKE_PERSONA_TOOLS, encoding="utf-8")

        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        result = self.runner.invoke(
            main,
            [
                "import-audit",
                "--persona-dir",
                str(persona_dir),
                "--role-id",
                "analista",
                "--against-org",
                str(au_org),
                "--apply",
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Applied", result.output)

        doc = yaml.safe_load(au_org.read_text(encoding="utf-8"))
        self.assertIn("analista", {r["id"] for r in doc["roles"]})
        self.assertIn("carla", {a["id"] for a in doc["actors"]})
        carla = next(a for a in doc["actors"] if a["id"] == "carla")
        self.assertEqual(carla["telegram_bot"], "@Analista_bot")

        analista_role = next(r for r in doc["roles"] if r["id"] == "analista")
        self.assertEqual(
            analista_role["reports_to"], "chief_of_staff"
        )  # resolved from "Pepa"

        result = self.runner.invoke(main, ["validate", "--org", str(au_org)])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_apply_requires_against_org(self):
        persona_dir = self.tmp / "personas" / "alone"
        persona_dir.mkdir(parents=True)
        result = self.runner.invoke(
            main,
            [
                "import-audit",
                "--persona-dir",
                str(persona_dir),
                "--role-id",
                "x",
                "--department",
                "direccion",
                "--apply",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--against-org", result.output)

    def test_apply_cancelled_without_yes_when_declined(self):
        persona_dir = self.tmp / "personas" / "carla2"
        persona_dir.mkdir(parents=True)
        (persona_dir / "IDENTITY.md").write_text(
            _FAKE_PERSONA_IDENTITY, encoding="utf-8"
        )

        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        result = self.runner.invoke(
            main,
            [
                "import-audit",
                "--persona-dir",
                str(persona_dir),
                "--role-id",
                "analista2",
                "--against-org",
                str(au_org),
                "--apply",
            ],
            input="n\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Cancelled", result.output)

        doc = yaml.safe_load(au_org.read_text(encoding="utf-8"))
        self.assertNotIn("analista2", {r["id"] for r in doc["roles"]})

    def test_apply_rejects_duplicate_role_id(self):
        persona_dir = self.tmp / "personas" / "another"
        persona_dir.mkdir(parents=True)
        (persona_dir / "IDENTITY.md").write_text(
            _FAKE_PERSONA_IDENTITY, encoding="utf-8"
        )

        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        result = self.runner.invoke(
            main,
            [
                "import-audit",
                "--persona-dir",
                str(persona_dir),
                "--role-id",
                "ceo",  # already exists
                "--department",
                "direccion",
                "--against-org",
                str(au_org),
                "--apply",
                "--yes",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already exists", result.output)


class TestAddCommandsRejectDuplicates(_TmpOrgsTestCase):
    def test_add_role_cli_rejects_duplicate(self):
        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        result = self.runner.invoke(
            main,
            [
                "add-role",
                "--org",
                str(au_org),
                "--id",
                "ceo",
                "--name",
                "Other",
                "--department",
                "direccion",
                "--access-level",
                "level-3",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already exists", result.output)


class TestPathOptionHardening(_TmpOrgsTestCase):
    """cli-tests F7: path options expand ~ and refuse wrong kinds."""

    def test_target_expands_tilde(self):
        # ~/personas must resolve to the HOME dir, never a literal
        # "~/personas" directory under the CWD.
        out_dir = self.tmp / "dist"
        self.runner.invoke(
            main,
            [
                "build",
                "--org",
                str(self.orgs_dir / "aquaponics-united/org.yaml"),
                "--out",
                str(out_dir),
            ],
        )
        with tempfile.TemporaryDirectory() as fake_home:
            result = self.runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(out_dir),
                    "--target",
                    "~/personas",
                    "--yes",
                    "--force",
                ],
                env={"HOME": fake_home},
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((Path(fake_home) / "personas" / "alma").exists())
            # No literal "~" directory may exist under the CWD.
            self.assertFalse((Path.cwd() / "~").exists())

    def test_persona_dir_rejects_a_file(self):
        some_file = self.tmp / "not_a_dir"
        some_file.write_text("x", encoding="utf-8")
        result = self.runner.invoke(
            main, ["import-audit", "--persona-dir", str(some_file), "--role-id", "x"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("is a file", result.output)

    def test_deploy_from_rejects_a_file(self):
        some_file = self.tmp / "not_a_dir"
        some_file.write_text("x", encoding="utf-8")
        result = self.runner.invoke(main, ["deploy", "--from", str(some_file), "--yes"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("is a file", result.output)


class TestInterruptHandling(_TmpOrgsTestCase):
    """cli-tests F8: Ctrl+C mid-deploy leaves a clear hint, not a raw traceback."""

    def test_deploy_keyboard_interrupt_hints_rollback(self):
        out_dir = self.tmp / "dist"
        self.runner.invoke(
            main,
            [
                "build",
                "--org",
                str(self.orgs_dir / "aquaponics-united/org.yaml"),
                "--out",
                str(out_dir),
            ],
        )
        target_dir = self.tmp / "target"
        with mock.patch(
            "phantomforge.cli.deploy_target",
            side_effect=KeyboardInterrupt,
        ):
            result = self.runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(out_dir),
                    "--target",
                    str(target_dir),
                    "--yes",
                ],
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("rollback", result.output)
        self.assertNotIn("Traceback", result.output)


class TestRenameConfirmation(_TmpOrgsTestCase):
    """cli-tests F9: rename-* confirm unless --yes (matching remove-*)."""

    def test_rename_role_cancelled_without_yes(self):
        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        before = au_org.read_bytes()
        result = self.runner.invoke(
            main,
            [
                "rename-role",
                "--org",
                str(au_org),
                "--old-id",
                "cfo",
                "--new-id",
                "cfo2",
            ],
            input="n\n",
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cancelled", result.output)
        self.assertEqual(au_org.read_bytes(), before)

    def test_rename_actor_with_yes_applies(self):
        au_org = self.orgs_dir / "aquaponics-united/org.yaml"
        result = self.runner.invoke(
            main,
            [
                "rename-actor",
                "--org",
                str(au_org),
                "--old-id",
                "alma",
                "--new-id",
                "alma2",
                "--yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("alma2", result.output)


if __name__ == "__main__":
    unittest.main()
