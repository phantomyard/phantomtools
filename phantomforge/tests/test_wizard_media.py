"""
Regression tests for the adversarial-review MEDIUM findings in the wizard
area (v0.4.13):

- M1: setup batch apply is transactional — a mid-batch duplicate id
  leaves org.yaml untouched (no partial mutation)
- M2: mutations are serialized by a file lock — concurrent read-modify-
  write cycles cannot lose each other's update
- M3: org ids are validated against the identifier grammar before they
  are joined into a filesystem path (no traversal via `../../x`)
"""

import tempfile
import threading
import unittest
from pathlib import Path

import yaml
from click.testing import CliRunner

from phantomforge.cli import main
from phantomforge.wizard.mutations import add_actor
from phantomforge.wizard.new_org import new_org

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


def _copy_au(tmp: Path) -> Path:
    org_path = tmp / "org.yaml"
    org_path.write_bytes(AU_ORG.read_bytes())
    return org_path


class TestTransactionalBatch(unittest.TestCase):
    """M1: a duplicate actor id mid-batch must not leave a partial org."""

    def _setup_answers_with_duplicate(self) -> str:
        # create-new org with two personas that share an actor id
        return "\n".join(  # noqa: FLY002 — answer flow reads clearer as a list
            [
                "n",  # no existing org.yaml
                "tx-org",  # id
                "Tx Org",  # name
                "ngo",  # sector
                "en",  # lang
                "Direccion",  # dept 1
                "",  # end departments
                "pepe",  # actor id (typed)
                "operaciones",  # role for pepe
                "pepe",  # SAME actor id again
                "operaciones",  # role
                "n",  # no more personas
                "y",  # apply
            ]
        )

    def test_duplicate_persona_id_leaves_no_partial_org(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "orgs"
            pb = Path(tmp) / "pb"
            (pb / "pepe").mkdir(parents=True)
            (pb / "pepe" / "SOUL.md").write_text("# persona\n", encoding="utf-8")
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["setup", "--phantombot-dir", str(pb), "--base", str(base)],
                input=self._setup_answers_with_duplicate(),
            )
            # the batch fails cleanly (exit != 0) and no org.yaml exists
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertFalse((base / "tx-org" / "org.yaml").exists())


class TestMutationLock(unittest.TestCase):
    """M2: concurrent mutations serialize; no lost update."""

    def test_concurrent_adds_both_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path = _copy_au(Path(tmp))
            errors: list[Exception] = []

            def worker(actor_id: str):
                try:
                    add_actor(org_path, actor_id, "vendedor", [])
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            # fire N adds at the same time from N threads
            threads = [
                threading.Thread(target=worker, args=(f"extra{i}",)) for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            doc = yaml.safe_load(org_path.read_text(encoding="utf-8"))
            actor_ids = {a["id"] for a in doc["actors"]}
            self.assertTrue({"extra0", "extra7"}.issubset(actor_ids), actor_ids)
            self.assertEqual(
                len([a for a in doc["actors"] if a["id"].startswith("extra")]), 8
            )


class TestOrgIdValidation(unittest.TestCase):
    """M3: org ids must match the identifier grammar before use as a path."""

    def test_new_org_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(ValueError):
                new_org("../../escape", "Bad", "ngo", ["en"], base_dir=base)
            self.assertFalse((base / ".." / "escape").exists())

    def test_new_org_rejects_slash(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            new_org("a/b", "Bad", "ngo", ["en"], base_dir=Path(tmp))

    def test_new_org_rejects_leading_dash_underscore(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("-x", "_x", "x y", ".hidden", "x,y"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    new_org(bad, "Bad", "ngo", ["en"], base_dir=Path(tmp))

    def test_new_org_accepts_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = new_org("valid-org_2", "Good", "ngo", ["en"], base_dir=Path(tmp))
            self.assertTrue(p.exists())


class TestSetupPathTraversal(unittest.TestCase):
    """M3: pf setup create-new with a traversal id is refused."""

    def test_setup_create_new_rejects_traversal_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            pb = Path(tmp) / "pb"
            base = Path(tmp) / "orgs"
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["setup", "--phantombot-dir", str(pb), "--base", str(base)],
                input="n\n../../escape\nBad Org\nngo\nen\nDireccion\n\ny\n",
            )
            self.assertEqual(result.exit_code, 1)
            self.assertIn("Invalid organization id", result.output)
            self.assertFalse((base.parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
