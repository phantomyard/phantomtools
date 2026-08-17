"""Tests for `po setup` (wizard/setup.py pure logic + the CLI command)."""

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.wizard.setup import (
    PersonaPlan,
    SetupPlan,
    _slugify,
    build_org_yaml,
    find_personas_dirs,
)


def _persona(root: Path, actor_id: str, role_line: str | None = None) -> Path:
    d = root / actor_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SOUL.md").write_text(f"# {actor_id}\n{role_line or ''}", encoding="utf-8")
    return d


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_slugify("Project Lead"), "project_lead")

    def test_accents_and_spaces(self):
        self.assertEqual(_slugify("café"), "cafe")

    def test_empty(self):
        self.assertEqual(_slugify("   "), "")


class TestFindPersonasDirs(unittest.TestCase):
    def test_detects_soul_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _persona(root, "pepe")
            _persona(root, "alicia")
            (root / ".hidden").mkdir()
            (root / "not-a-persona").mkdir()
            found = find_personas_dirs(root)
            self.assertEqual({p.name for p in found}, {"pepe", "alicia"})

    def test_missing_root(self):
        self.assertEqual(find_personas_dirs(Path("/nonexistent-xyz")), [])


class TestBuildOrgYaml(unittest.TestCase):
    def _plan(self, personas: list[PersonaPlan]) -> SetupPlan:
        return SetupPlan(
            org_path=Path("/tmp/x/org.yaml"),
            org_id="demo",
            org_name="Demo",
            sector="ngo",
            languages=["en"],
            departments=[
                {
                    "id": "direccion",
                    "name": "Direccion",
                    "parent": None,
                    "access_policy": "level-2",
                },
                {
                    "id": "operaciones",
                    "name": "Operaciones",
                    "parent": None,
                    "access_policy": "level-2",
                },
            ],
            personas=personas,
            create_new_org=True,
        )

    def test_shared_suggested_role_is_deduplicated(self):
        plan = self._plan(
            [
                PersonaPlan(
                    actor_id="pepe",
                    role_id="project_lead",
                    department_id="operaciones",
                    suggested_role="Project Lead",
                ),
                PersonaPlan(
                    actor_id="alicia",
                    role_id="project_lead",
                    department_id="operaciones",
                    suggested_role="Project Lead",
                ),
            ]
        )
        doc = build_org_yaml(plan)
        self.assertEqual(len(doc["roles"]), 1)
        self.assertEqual(doc["roles"][0]["id"], "project_lead")
        self.assertEqual(doc["roles"][0]["name"], "Project Lead")
        self.assertEqual(len(doc["actors"]), 2)
        self.assertEqual(doc["actors"][0]["role"], "project_lead")
        self.assertEqual(doc["actors"][1]["role"], "project_lead")

    def test_distinct_roles_stay_distinct(self):
        plan = self._plan(
            [
                PersonaPlan(
                    actor_id="pepe", role_id="project_lead", department_id="operaciones"
                ),
                PersonaPlan(
                    actor_id="javier", role_id="manager", department_id="direccion"
                ),
            ]
        )
        doc = build_org_yaml(plan)
        self.assertEqual({r["id"] for r in doc["roles"]}, {"project_lead", "manager"})

    def test_existing_role_is_reused_not_created(self):
        plan = self._plan(
            [
                PersonaPlan(
                    actor_id="maria", role_id="vendedor", department_id="ventas"
                ),
            ]
        )
        plan.existing_roles = {"vendedor": "Vendedor"}
        doc = build_org_yaml(plan)
        self.assertEqual(doc["roles"], [])
        self.assertEqual(doc["actors"][0]["role"], "vendedor")


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


class TestSetupCommand(_TmpOrgsTestCase):
    """End-to-end: `po setup` with simulated interactive input."""

    def setUp(self):
        super().setUp()
        self.pb = Path(tempfile.mkdtemp())
        _persona(self.pb, "pepe", "Role: Project Lead")
        _persona(self.pb, "alicia", "Role: Project Lead")

    def tearDown(self):
        shutil.rmtree(self.pb, ignore_errors=True)
        super().tearDown()

    def test_setup_new_org_assigns_and_validates(self):
        # answers: no org.yaml | id | name | sector | lang |
        #          dept1 | dept2 | (end) |
        #          pepe->operaciones->role(enter=project_lead) |
        #          alicia->operaciones->role(enter=project_lead) |
        #          no new persona
        answers = (
            "n\n"
            "demo-org\n"
            "Demo Org\n"
            "ngo\n"
            "en\n"
            "Direccion\n"
            "Operaciones\n"
            "\n"
            "operaciones\n"
            "\n"
            "operaciones\n"
            "\n"
            "n\n"
            "y\n"
        )
        result = self.runner.invoke(
            main,
            ["setup", "--phantombot-dir", str(self.pb), "--base", str(self.tmp)],
            input=answers,
        )
        self.assertEqual(result.exit_code, 0, result.output)

        org_path = self.tmp / "demo-org" / "org.yaml"
        self.assertTrue(org_path.exists())
        doc = yaml.safe_load(org_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["actors"]), 2)
        self.assertEqual(len(doc["roles"]), 1)  # shared project_lead
        self.assertEqual(doc["roles"][0]["name"], "Project Lead")

        # the org must be valid and buildable
        validate = self.runner.invoke(main, ["validate", "--org", str(org_path)])
        self.assertEqual(validate.exit_code, 0, validate.output)
        with tempfile.TemporaryDirectory() as out:
            build = self.runner.invoke(
                main, ["build", "--org", str(org_path), "--out", out]
            )
            self.assertEqual(build.exit_code, 0, build.output)
            self.assertEqual(
                {p.name for p in Path(out).iterdir() if p.is_dir()},
                {"pepe", "alicia"},
            )

    def test_setup_create_new_over_existing_org_backs_up_first(self):
        """Regression (wizard H1): create-new must never silently
        overwrite an existing org.yaml — it warns and backs up first."""
        org_path = self.tmp / "demo-org" / "org.yaml"
        org_path.parent.mkdir(parents=True)
        old_doc = {"version": 1, "organization": {"id": "demo-org", "name": "Old"}}
        org_path.write_text(
            yaml.safe_dump(old_doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # same create-new flow as test_setup_new_org_assigns_and_validates
        answers = (
            "n\n"
            "demo-org\n"
            "Demo Org\n"
            "ngo\n"
            "en\n"
            "Direccion\n"
            "Operaciones\n"
            "\n"
            "operaciones\n"
            "\n"
            "operaciones\n"
            "\n"
            "n\n"
            "y\n"
        )
        result = self.runner.invoke(
            main,
            ["setup", "--phantombot-dir", str(self.pb), "--base", str(self.tmp)],
            input=answers,
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("already exists", result.output)

        # a .bak-* backup with the OLD content must exist
        backups = sorted(org_path.parent.glob("org.yaml.bak-*"))
        self.assertEqual(len(backups), 1, [b.name for b in org_path.parent.iterdir()])
        backup_doc = yaml.safe_load(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(backup_doc["organization"]["name"], "Old")

        # the new content is in place
        doc = yaml.safe_load(org_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["actors"]), 2)
        self.assertEqual(doc["organization"]["name"], "Demo Org")

    def test_setup_reuses_existing_org(self):
        org = self.tmp / "mi-org" / "org.yaml"
        org.parent.mkdir(parents=True)
        doc = {
            "version": 1,
            "organization": {
                "id": "mi-org",
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
            "roles": [
                {
                    "id": "vendedor",
                    "name": "Vendedor",
                    "department": "ventas",
                    "reports_to": None,
                    "reports_to_human": None,
                    "functions": [],
                    "access_level": "level-2",
                    "security_exceptions": [],
                },
            ],
            "actors": [],
            "policies": {
                "access_levels": {
                    "level-3": {"label": "Executive", "categories": [1, 2, 3]},
                    "level-2": {"label": "Operational", "categories": [1, 2]},
                    "level-1": {"label": "Restricted", "categories": [1]},
                },
                "security_categories": {
                    "category-1": {"label": "Public / low internal"},
                    "category-2": {"label": "Confidential"},
                    "category-3": {"label": "Credentials / sensitive financial"},
                },
            },
            "escalation_matrix": [],
            "communication": {
                "request_id_format": "{org_id}-{yyyymmdd}-{seq4}",
                "message_types": ["REQUEST", "INFORM", "ESCALATE", "CONFIRM", "REJECT"],
                "max_hops": 3,
            },
        }
        org.write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        # maria: new persona not in the org; assign existing role 'vendedor'
        _persona(self.pb, "maria", "Role: Vendedor")
        # all 3 personas in the sandbox are reassigned (sorted: alicia,
        # maria, pepe) — each to department 'ventas' + role 'vendedor'
        answers = "\n".join(
            [
                "y",
                str(org),
                # alicia
                "ventas",
                "vendedor",
                # maria
                "ventas",
                "vendedor",
                # pepe
                "ventas",
                "vendedor",
                "n",
                "y",
            ]
        )
        result = self.runner.invoke(
            main,
            ["setup", "--phantombot-dir", str(self.pb)],
            input=answers,
        )
        self.assertEqual(result.exit_code, 0, result.output)
        updated = yaml.safe_load(org.read_text(encoding="utf-8"))
        self.assertEqual(len(updated["actors"]), 3)
        self.assertEqual(
            {a["id"] for a in updated["actors"]}, {"alicia", "maria", "pepe"}
        )
        self.assertTrue(all(a["role"] == "vendedor" for a in updated["actors"]))
        self.assertEqual(len(updated["roles"]), 1)  # not duplicated

        validate = self.runner.invoke(main, ["validate", "--org", str(org)])
        self.assertEqual(validate.exit_code, 0, validate.output)
