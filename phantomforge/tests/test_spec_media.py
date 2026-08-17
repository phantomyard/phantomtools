"""Spec/validator MEDIUM batch regression tests (v0.4.13).

Covers the findings from the adversarial review of phantomforge/spec/:

- loader F1: all read/parse errors surface as OrgSpecError (never raw
  yaml/OSError), and malformed org.yaml no longer aborts build-all
- loader F4 / validator F1: duplicate YAML keys rejected
- shape F2 / validator F2: identifier regex anchored with fullmatch
  (trailing newline "ceo\\n" rejected)
- shape F7 / validator F5: Windows-reserved device names and
  over-length ids rejected
- shape F5 / validator F6: bool is not accepted where int is required
- shape F2+F6 / validator F3: explicit nulls in int/bool fields rejected
- shape F3 / validator F4: unknown/typo'd keys rejected everywhere
- model: soul_line_budget null normalizes to 300 (never None)
- nullable fields (default_language/owner/scope/parent/reports_to)
  still accept null per the schema contract
"""

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from phantomforge.spec.loader import OrgSpecError, load_org_yaml
from phantomforge.spec.model import Role
from phantomforge.spec.shape_validator import (
    ShapeError,
    is_valid_identifier,
    validate_shape,
)

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


def _load_doc():
    with open(AU_ORG, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestDuplicateKeysRejected(unittest.TestCase):
    def _write(self, tmp, text):
        p = Path(tmp) / "org.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_duplicate_root_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "version: 1\nversion: 1\norganization: {id: x}\n")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)

    def test_duplicate_nested_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(
                tmp,
                "version: 1\norganization: {id: x, id: y}\n",
            )
            with self.assertRaises(OrgSpecError) as ctx:
                load_org_yaml(p)
            self.assertIn("duplicate key", str(ctx.exception))

    def test_duplicate_roles_block_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(
                tmp,
                "version: 1\nroles: [a]\nroles: [b]\norganization: {id: x}\n",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)

    def test_no_duplicate_keys_loads_fine(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(spec.organization.id, "aquaponics-united")


class TestLoaderErrorContract(unittest.TestCase):
    def test_malformed_yaml_is_orgspec_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "org.yaml"
            p.write_text("version: [unclosed\n", encoding="utf-8")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)

    def test_invalid_utf8_is_orgspec_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "org.yaml"
            p.write_bytes(b"version: \xff\xfe broken\n")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)

    def test_directory_as_path_is_orgspec_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(OrgSpecError):
            load_org_yaml(Path(tmp))

    def test_missing_file_is_orgspec_error(self):
        with self.assertRaises(OrgSpecError):
            load_org_yaml("no/such/org.yaml")

    def test_two_documents_is_orgspec_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "org.yaml"
            p.write_text("version: 1\n---\nversion: 2\n", encoding="utf-8")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)

    def test_deeply_nested_yaml_is_orgspec_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "org.yaml"
            p.write_text("a: " + "[" * 2000, encoding="utf-8")
            with self.assertRaises(OrgSpecError):
                load_org_yaml(p)


class TestIdentifierGrammar(unittest.TestCase):
    def test_trailing_newline_rejected(self):
        for bad in ("ceo\n", "ceo\r\n"):
            self.assertFalse(is_valid_identifier(bad), f"{bad!r} accepted")

    def test_surrounding_whitespace_rejected(self):
        for bad in (" ceo", "ceo ", "\tceo"):
            self.assertFalse(is_valid_identifier(bad), f"{bad!r} accepted")

    def test_valid_ids_accepted(self):
        for good in ("a", "a-1", "a_1", "ceo", "x" * 64):
            self.assertTrue(is_valid_identifier(good), f"{good!r} rejected")

    def test_invalid_chars_rejected(self):
        for bad in ("-a", "_a", ".a", "a/b", "a..b", "A", "a b"):
            self.assertFalse(is_valid_identifier(bad), f"{bad!r} accepted")

    def test_windows_reserved_names_rejected(self):
        for bad in ("con", "prn", "aux", "nul"):
            self.assertFalse(is_valid_identifier(bad), f"{bad!r} accepted")
        for i in range(1, 10):
            self.assertFalse(is_valid_identifier(f"com{i}"), f"com{i} accepted")
            self.assertFalse(is_valid_identifier(f"lpt{i}"), f"lpt{i} accepted")

    def test_overlength_id_rejected(self):
        self.assertFalse(is_valid_identifier("x" * 65))

    def test_org_yaml_with_reserved_actor_id_rejected(self):
        doc = _load_doc()
        doc["actors"][0]["id"] = "con"
        with self.assertRaises(ShapeError):
            validate_shape(doc)


class TestBoolNotInt(unittest.TestCase):
    def _mutate(self, mutator):
        doc = copy.deepcopy(_load_doc())
        mutator(doc)
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_version_true_rejected(self):
        self._mutate(lambda d: d.__setitem__("version", True))

    def test_max_hops_true_rejected(self):
        self._mutate(lambda d: d["communication"].__setitem__("max_hops", True))

    def test_categories_true_rejected(self):
        self._mutate(
            lambda d: d["policies"]["access_levels"]["level-1"][
                "categories"
            ].__setitem__(0, True)
        )

    def test_soul_line_budget_true_rejected(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("soul_line_budget", True))


class TestExplicitNullsRejected(unittest.TestCase):
    def _mutate(self, mutator):
        doc = copy.deepcopy(_load_doc())
        mutator(doc)
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_soul_line_budget_null_rejected(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("soul_line_budget", None))

    def test_cross_department_null_rejected(self):
        self._mutate(
            lambda d: d["escalation_matrix"][0].__setitem__("cross_department", None)
        )

    def test_nullable_fields_still_accepted(self):
        doc = copy.deepcopy(_load_doc())
        doc["organization"]["default_language"] = None
        cat = next(iter(doc["policies"]["security_categories"]))
        doc["policies"]["security_categories"][cat]["owner"] = None
        doc["policies"]["security_categories"][cat]["scope"] = None
        doc["departments"][0]["parent"] = None
        doc["roles"][0]["reports_to"] = None
        doc["roles"][0]["reports_to_human"] = None
        validate_shape(doc)  # must not raise


class TestUnknownKeysRejected(unittest.TestCase):
    def _mutate(self, mutator):
        doc = copy.deepcopy(_load_doc())
        mutator(doc)
        with self.assertRaises(ShapeError) as ctx:
            validate_shape(doc)
        return ctx

    def test_root_unknown_key(self):
        ctx = self._mutate(lambda d: d.__setitem__("extra", 1))
        self.assertIn("unknown field", str(ctx.exception))

    def test_org_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["organization"].__setitem__("headquarters", "Madrid")
        )
        self.assertIn("organization", str(ctx.exception))

    def test_role_typo_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["roles"][0].__setitem__("security_excpetions", ["x"])
        )
        self.assertIn("roles[0]", str(ctx.exception))

    def test_actor_typo_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["actors"][0].__setitem__("telegram_bott", "@bot")
        )
        self.assertIn("actors[0]", str(ctx.exception))

    def test_department_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["departments"][0].__setitem__("description", "x")
        )
        self.assertIn("departments[0]", str(ctx.exception))

    def test_escalation_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["escalation_matrix"][0].__setitem__("when", "always")
        )
        self.assertIn("escalation_matrix[0]", str(ctx.exception))

    def test_communication_unknown_key(self):
        ctx = self._mutate(lambda d: d["communication"].__setitem__("hops", 3))
        self.assertIn("communication", str(ctx.exception))

    def test_policies_unknown_key(self):
        ctx = self._mutate(lambda d: d["policies"].__setitem__("approval_levels", {}))
        self.assertIn("policies", str(ctx.exception))

    def test_access_level_unknown_key(self):
        ctx = self._mutate(
            lambda d: d["policies"]["access_levels"]["level-1"].__setitem__(
                "color", "red"
            )
        )
        self.assertIn("access_levels.level-1", str(ctx.exception))

    def test_security_category_unknown_key(self):
        def m2(d):
            cat = next(iter(d["policies"]["security_categories"]))
            d["policies"]["security_categories"][cat]["owner2"] = "x"

        ctx = self._mutate(m2)
        self.assertIn("security_categories", str(ctx.exception))


class TestModelNormalization(unittest.TestCase):
    def test_soul_line_budget_null_becomes_default(self):
        role = Role.from_dict(
            {
                "id": "x",
                "name": "X",
                "department": "d",
                "access_level": "level-1",
                "soul_line_budget": None,
            }
        )
        self.assertEqual(role.soul_line_budget, 300)

    def test_soul_line_budget_absent_becomes_default(self):
        role = Role.from_dict(
            {"id": "x", "name": "X", "department": "d", "access_level": "level-1"}
        )
        self.assertEqual(role.soul_line_budget, 300)


class TestBuildAllSkipsBrokenOrg(unittest.TestCase):
    """A malformed org.yaml must not abort the whole build-all batch."""

    def _write_org(self, base, name, text):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "org.yaml").write_text(text, encoding="utf-8")

    def test_build_all_continues_past_broken_org(self):
        from click.testing import CliRunner

        from phantomforge.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "orgs"
            out = Path(tmp) / "out"
            self._write_org(
                base,
                "good-org",
                Path(AU_ORG).read_text(encoding="utf-8"),
            )
            self._write_org(base, "broken-org", "version: [unclosed\n")

            runner = CliRunner()
            result = runner.invoke(
                main, ["build-all", "--base", str(base), "--out", str(out)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("load error, skipping", result.output)
            self.assertIn("good-org", result.output)
            self.assertTrue((out / "good-org").exists())


if __name__ == "__main__":
    unittest.main()
