import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from phantomorg.spec.loader import load_org_yaml
from phantomorg.validator import validate_org
from phantomorg.wizard.mutations import (
    DuplicateIdError,
    RemovalBlockedError,
    add_actor,
    add_department,
    add_role,
    remove_actor,
    remove_department,
    remove_role,
    rename_actor,
    rename_department,
    rename_role,
)

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


class _AUCopyTestCase(unittest.TestCase):
    """Copies the real AU org.yaml into a per-test tmp, so the original is never touched."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.org_path = Path(self._tmpdir.name) / "org.yaml"
        shutil.copy(AU_ORG, self.org_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _raw(self) -> dict:
        return yaml.safe_load(self.org_path.read_text(encoding="utf-8"))


class TestRemoveDepartment(_AUCopyTestCase):
    def test_blocks_if_roles_assigned(self):
        # "direccion" has roles (ceo, chief_of_staff) assigned.
        with self.assertRaises(RemovalBlockedError):
            remove_department(self.org_path, "direccion")

    def test_blocks_children_without_cascade(self):
        # "direccion" is the parent of operaciones/formacion/finanzas.
        # We empty its roles on purpose to isolate the "children" block.
        doc = self._raw()
        doc["roles"] = [r for r in doc["roles"] if r["department"] != "direccion"]
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        with self.assertRaises(RemovalBlockedError):
            remove_department(self.org_path, "direccion", cascade=False)

    def test_cascade_promotes_children_to_root(self):
        doc = self._raw()
        doc["roles"] = [r for r in doc["roles"] if r["department"] != "direccion"]
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        actions = remove_department(self.org_path, "direccion", cascade=True)
        self.assertTrue(len(actions) >= 1)

        doc2 = self._raw()
        dept_ids = {d["id"] for d in doc2["departments"]}
        self.assertNotIn("direccion", dept_ids)
        for d in doc2["departments"]:
            self.assertIsNone(d["parent"])  # all promoted to root

    def test_remove_department_without_dependents_succeeds(self):
        doc = self._raw()
        doc["departments"].append(
            {
                "id": "temporary",
                "name": "Temporary",
                "parent": None,
                "access_policy": "level-1",
            }
        )
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        remove_department(self.org_path, "temporary")
        doc2 = self._raw()
        self.assertNotIn("temporal", {d["id"] for d in doc2["departments"]})

    def test_nonexistent_department_raises_keyerror(self):
        with self.assertRaises(KeyError):
            remove_department(self.org_path, "does-not-exist")


class TestRemoveRole(_AUCopyTestCase):
    def test_blocks_if_actors_assigned(self):
        with self.assertRaises(RemovalBlockedError):
            remove_role(self.org_path, "ceo")  # marco is assigned

    def test_blocks_even_with_cascade_if_actors_assigned(self):
        # cascade NEVER deletes actors — it must keep blocking.
        with self.assertRaises(RemovalBlockedError):
            remove_role(self.org_path, "chief_of_staff", cascade=True)  # lucia assigned

    def test_blocks_subordinates_and_escalation_without_cascade(self):
        doc = self._raw()
        doc["actors"] = [a for a in doc["actors"] if a["role"] != "ceo"]
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        with self.assertRaises(RemovalBlockedError):
            remove_role(
                self.org_path, "ceo", cascade=False
            )  # chief_of_staff/cfo report to ceo

    def test_cascade_promotes_subordinates_and_cleans_escalation(self):
        doc = self._raw()
        doc["actors"] = [a for a in doc["actors"] if a["role"] != "ceo"]
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        actions = remove_role(self.org_path, "ceo", cascade=True)
        self.assertTrue(len(actions) >= 1)

        doc2 = self._raw()
        role_ids = {r["id"] for r in doc2["roles"]}
        self.assertNotIn("ceo", role_ids)
        for r in doc2["roles"]:
            self.assertNotEqual(r.get("reports_to"), "ceo")
        for e in doc2["escalation_matrix"]:
            self.assertNotEqual(e.get("from"), "ceo")
            self.assertNotEqual(e.get("to"), "ceo")

    def test_nonexistent_role_raises_keyerror(self):
        with self.assertRaises(KeyError):
            remove_role(self.org_path, "does-not-exist")


class TestRemoveActor(_AUCopyTestCase):
    def test_removes_actor_cleanly(self):
        remove_actor(self.org_path, "elias")
        doc = self._raw()
        self.assertNotIn("elias", {a["id"] for a in doc["actors"]})
        # the training_lead role still exists (remove_actor does not touch roles)
        self.assertIn("training_lead", {r["id"] for r in doc["roles"]})

    def test_nonexistent_actor_raises_keyerror(self):
        with self.assertRaises(KeyError):
            remove_actor(self.org_path, "does-not-exist")

    def test_resulting_org_is_still_shape_valid(self):
        remove_actor(self.org_path, "elias")
        # there is still >=1 actor, so it loads without a problem
        spec = load_org_yaml(self.org_path)
        self.assertNotIn("elias", [a.id for a in spec.actors])


class TestRenameDepartment(_AUCopyTestCase):
    def test_renames_and_updates_children_and_roles(self):
        updated = rename_department(self.org_path, "operaciones", "ops")
        self.assertTrue(len(updated) >= 1)

        doc = self._raw()
        self.assertIn("ops", {d["id"] for d in doc["departments"]})
        self.assertNotIn("operaciones", {d["id"] for d in doc["departments"]})
        project_lead = next(r for r in doc["roles"] if r["id"] == "project_lead")
        self.assertEqual(project_lead["department"], "ops")

    def test_clashing_new_id_raises(self):
        with self.assertRaises(ValueError):
            rename_department(self.org_path, "operaciones", "finanzas")

    def test_nonexistent_raises_keyerror(self):
        with self.assertRaises(KeyError):
            rename_department(self.org_path, "does-not-exist", "x")


class TestRenameRole(_AUCopyTestCase):
    def test_renames_and_updates_all_references(self):
        updated = rename_role(self.org_path, "chief_of_staff", "cos")
        self.assertTrue(len(updated) >= 1)

        doc = self._raw()
        role_ids = {r["id"] for r in doc["roles"]}
        self.assertIn("cos", role_ids)
        self.assertNotIn("chief_of_staff", role_ids)

        # actors.lucia.role updated
        lucia = next(a for a in doc["actors"] if a["id"] == "lucia")
        self.assertEqual(lucia["role"], "cos")

        # roles that reported to chief_of_staff now report to cos
        for r in doc["roles"]:
            if r["id"] in ("project_lead", "training_lead"):
                self.assertEqual(r["reports_to"], "cos")

        # escalation_matrix updated
        for e in doc["escalation_matrix"]:
            self.assertNotEqual(e.get("from"), "chief_of_staff")
            self.assertNotEqual(e.get("to"), "chief_of_staff")

    def test_result_is_shape_and_reference_valid(self):
        rename_role(self.org_path, "chief_of_staff", "cos")
        spec = load_org_yaml(self.org_path)  # no debe lanzar
        self.assertEqual(spec.actor_by_id("lucia").role, "cos")

    def test_clashing_new_id_raises(self):
        with self.assertRaises(ValueError):
            rename_role(self.org_path, "chief_of_staff", "cfo")

    def test_nonexistent_raises_keyerror(self):
        with self.assertRaises(KeyError):
            rename_role(self.org_path, "does-not-exist", "x")


class TestRenameActor(_AUCopyTestCase):
    def test_renames_actor_id(self):
        rename_actor(self.org_path, "dana", "alma2")
        doc = self._raw()
        actor_ids = {a["id"] for a in doc["actors"]}
        self.assertIn("alma2", actor_ids)
        self.assertNotIn("dana", actor_ids)

    def test_clashing_new_id_raises(self):
        with self.assertRaises(ValueError):
            rename_actor(self.org_path, "dana", "lucia")

    def test_nonexistent_raises_keyerror(self):
        with self.assertRaises(KeyError):
            rename_actor(self.org_path, "does-not-exist", "x")


class TestDuplicateIdError(_AUCopyTestCase):
    """
    Gap found while implementing import-audit --apply:
    add_department/add_role/add_actor did not check whether the id already
    existed, so a repeated `po add-role --id ceo ...` silently created a
    second entry with the same id (the validator did not detect it because
    check_references only checked references, not uniqueness).
    """

    def test_add_department_rejects_duplicate_id(self):
        with self.assertRaises(DuplicateIdError):
            add_department(
                self.org_path, "direccion", "Other Direction", None, "level-3"
            )

    def test_add_role_rejects_duplicate_id(self):
        with self.assertRaises(DuplicateIdError):
            add_role(self.org_path, "ceo", "Another CEO", "direccion", None, "level-3")

    def test_add_actor_rejects_duplicate_id(self):
        with self.assertRaises(DuplicateIdError):
            add_actor(self.org_path, "marco", "cfo", ["email"])

    def test_validator_flags_duplicate_ids_if_introduced_by_hand(self):
        # Simulates someone editing the YAML by hand and actually introducing a duplicate.
        doc = self._raw()
        doc["roles"].append(dict(doc["roles"][0]))  # duplicates the first role as-is
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        _, result = validate_org(self.org_path)
        self.assertFalse(result.ok)
        self.assertTrue(any("appears 2 times" in e for e in result.errors))

    def test_validator_flags_cross_group_id_collision(self):
        # An actor reusing a role's id: legal per-group, but ids should be
        # unique org-wide to keep hand-edited YAML unambiguous.
        doc = self._raw()
        doc["actors"][0]["id"] = doc["roles"][0]["id"]  # actor.id = role.id
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        _, result = validate_org(self.org_path)
        self.assertFalse(result.ok)
        self.assertTrue(any("unique org-wide" in e for e in result.errors))

    def test_validator_flags_duplicate_escalation_pair(self):
        # Same from->to twice would emit two identical escalation paths in
        # the compiled SOUL; the validator must flag it.
        doc = self._raw()
        doc["escalation_matrix"].append(dict(doc["escalation_matrix"][0]))
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        _, result = validate_org(self.org_path)
        self.assertFalse(result.ok)
        self.assertTrue(any("duplicate entry" in e for e in result.errors))

    def test_validator_flags_duplicate_npub(self):
        # Two actors sharing one npub would make the "which bot do I DM"
        # mapping ambiguous; the validator must flag it.
        doc = self._raw()
        npub = "npub163h60w38hxsva60hjap53n8eh264g923da9qg58q7dqv68hz0evqygqkhf"
        doc["actors"][0]["npub"] = npub
        doc["actors"][1]["npub"] = npub
        self.org_path.write_text(
            yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8"
        )

        _, result = validate_org(self.org_path)
        self.assertFalse(result.ok)
        self.assertTrue(any("npub duplicated" in e for e in result.errors))


class TestOrgBackup(_AUCopyTestCase):
    """Every mutation of org.yaml writes a timestamped .bak first, so a
    wrong edit can always be undone (cp org.yaml.bak-<ts> org.yaml)."""

    def test_mutation_creates_backup_before_writing(self):
        original = self.org_path.read_bytes()
        add_role(
            self.org_path,
            "nuevo-rol",
            "Nuevo Rol",
            "operaciones",
            None,
            "level-2",
        )
        backups = sorted(self.org_path.parent.glob(f"{self.org_path.name}.bak-*"))
        self.assertEqual(len(backups), 1)
        # the backup holds the PRE-mutation content
        self.assertEqual(backups[0].read_bytes(), original)
        # the live file holds the mutated content
        self.assertIn("nuevo-rol", self.org_path.read_text(encoding="utf-8"))

    def test_second_mutation_creates_second_backup(self):
        add_role(
            self.org_path,
            "rol-a",
            "Rol A",
            "operaciones",
            None,
            "level-2",
        )
        add_role(
            self.org_path,
            "rol-b",
            "Rol B",
            "operaciones",
            None,
            "level-2",
        )
        backups = sorted(self.org_path.parent.glob(f"{self.org_path.name}.bak-*"))
        # microsecond timestamps: each mutation gets its own backup
        self.assertEqual(len(backups), 2)


class TestAtomicSave(_AUCopyTestCase):
    """_save() must never let a truncated or half-written org.yaml become
    the live spec: it writes a complete temp file, fsyncs it, then
    atomically renames it over org.yaml. A crash at any point leaves
    either the old complete file or the new complete file — plus the
    .bak recovery point."""

    def test_save_leaves_no_temp_file_behind(self):
        add_role(
            self.org_path,
            "rol-tmp",
            "Rol Tmp",
            "operaciones",
            None,
            "level-2",
        )
        leftovers = list(self.org_path.parent.glob(f"{self.org_path.name}.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_save_uses_atomic_replace(self):
        # os.replace must be the mechanism: the live file is replaced
        # wholesale, never opened for truncating write. (The backup is
        # also atomic now, so os.replace is called twice: once for the
        # .bak file, once for org.yaml itself.)
        with mock.patch("phantomorg.wizard.mutations.os.replace") as mock_replace:
            add_role(
                self.org_path,
                "rol-atomic",
                "Rol Atomic",
                "operaciones",
                None,
                "level-2",
            )
        self.assertEqual(mock_replace.call_count, 2)
        # the final atomic rename lands on org.yaml (the save itself);
        # the first one landed on the .bak backup.
        self.assertTrue(mock_replace.call_args.args[1] == self.org_path)

    def test_save_failure_does_not_corrupt_live_file(self):
        original = self.org_path.read_bytes()
        # simulate a crash mid-write: yaml.safe_dump raises after the
        # temp file was created; the live org.yaml must be untouched
        with (
            mock.patch(
                "phantomorg.wizard.mutations.yaml.safe_dump",
                side_effect=RuntimeError("simulated crash"),
            ),
            self.assertRaises(RuntimeError),
        ):
            add_role(
                self.org_path,
                "rol-crash",
                "Rol Crash",
                "operaciones",
                None,
                "level-2",
            )
        # live spec intact (still the pre-mutation content)
        self.assertEqual(self.org_path.read_bytes(), original)
        # no temp leftovers
        self.assertEqual(
            list(self.org_path.parent.glob(f"{self.org_path.name}.tmp-*")), []
        )

    def test_backup_is_atomic_replace_and_leaves_no_temp(self):
        from phantomorg.wizard.mutations import backup_org_file

        with mock.patch(
            "phantomorg.wizard.mutations.os.replace", wraps=os.replace
        ) as mock_replace:
            backup = backup_org_file(self.org_path)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        # the backup landed via atomic rename, and no temp file remains
        self.assertTrue(mock_replace.called)
        self.assertEqual(list(self.org_path.parent.glob(f"{backup.name}.tmp-*")), [])

    def test_backup_failure_leaves_no_temp(self):
        from phantomorg.wizard.mutations import backup_org_file

        with (
            mock.patch(
                "phantomorg.wizard.mutations.uuid4",
                side_effect=RuntimeError("simulated crash"),
            ),
            self.assertRaises(RuntimeError),
        ):
            backup_org_file(self.org_path)
        self.assertEqual(
            list(self.org_path.parent.glob(f"{self.org_path.name}.bak-*.tmp-*")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
