import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from phantomorg.importer import (
    ImportFindings,
    audit_persona_dir,
    render_org_yaml_fragment,
    resolve_against_org,
)
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"

_FAKE_IDENTITY = """# Identity
Name: marcos
**Role**: Director de Operaciones
**Reports to**: CEO
**Channel**: @Marcos_Ops_bot
"""

_FAKE_TOOLS = """# Tools
- email
- drive
- calendar
"""


class TestImportAudit(unittest.TestCase):
    def test_audit_detects_bot_role_and_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text(_FAKE_IDENTITY, encoding="utf-8")
            (persona_dir / "tools.md").write_text(_FAKE_TOOLS, encoding="utf-8")

            findings = audit_persona_dir(persona_dir)

            self.assertEqual(findings.actor_id, "marcos")
            self.assertEqual(findings.telegram_bot, "@Marcos_Ops_bot")
            self.assertEqual(findings.role_name_guess, "Director de Operaciones")
            self.assertEqual(findings.reports_to_guess, "CEO")
            self.assertEqual(set(findings.tools_guess), {"email", "drive", "calendar"})
            # we don't provide SOUL.md, so there must be an explicit warning
            self.assertTrue(any("SOUL.md" in w for w in findings.warnings))

    def test_audit_warns_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "empty"
            persona_dir.mkdir()
            findings = audit_persona_dir(persona_dir)
            self.assertTrue(len(findings.warnings) > 0)
            self.assertIsNone(findings.telegram_bot)
            self.assertEqual(findings.tools_guess, [])

    def test_audit_missing_tools_md_produces_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "identity_only"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text(_FAKE_IDENTITY, encoding="utf-8")
            findings = audit_persona_dir(persona_dir)
            self.assertTrue(any("tools.md" in w for w in findings.warnings))
            self.assertEqual(findings.tools_guess, [])

    def test_render_fragment_includes_detected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text(_FAKE_IDENTITY, encoding="utf-8")
            (persona_dir / "tools.md").write_text(_FAKE_TOOLS, encoding="utf-8")

            findings = audit_persona_dir(persona_dir)
            fragment = render_org_yaml_fragment(
                findings, role_id="ops_lead", department_id="operaciones"
            )

            self.assertIn("id: marcos", fragment)
            self.assertIn("Director de Operaciones", fragment)
            self.assertIn("@Marcos_Ops_bot", fragment)
            self.assertIn("email", fragment)

    def test_render_fragment_flags_missing_fields_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "unknown"
            persona_dir.mkdir()
            findings = audit_persona_dir(persona_dir)
            fragment = render_org_yaml_fragment(
                findings, role_id="x", department_id="direccion"
            )
            self.assertIn("FILL_IN", fragment)


class TestResolveAgainstOrg(unittest.TestCase):
    """
    Reproduces the real gap reported: 'reports_to_guess' was free text
    that was never translated into a real role_id. These tests use
    exactly the pattern found in the original Aquaponics United audit
    (Roberto escalating to "Paco, Salvador o Fran": several names, some
    are real roles/actors of the spec, others are external humans who are
    not).
    """

    def setUp(self):
        self.au_spec = load_org_yaml(AU_ORG)

    def test_resolves_unambiguous_match_by_actor_id(self):
        findings = ImportFindings(
            actor_id="x", reports_to_guess="Paco, Salvador o Fran"
        )
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertEqual(
            resolved.resolved_reports_to_role_id, "ceo"
        )  # paco -> role ceo
        self.assertEqual(resolved.ambiguous_candidates, [])
        # Salvador and Fran are not roles/actors in the spec (they are external humans)
        self.assertIn("Salvador", resolved.unmatched_candidates)
        self.assertIn("Fran", resolved.unmatched_candidates)
        self.assertTrue(any("ceo" in note for note in resolved.resolution_notes))

    def test_suggests_department_from_resolved_role(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="Pepa")
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertEqual(resolved.resolved_reports_to_role_id, "chief_of_staff")
        self.assertEqual(resolved.suggested_department_id, "direccion")

    def test_ambiguous_when_candidates_map_to_different_roles(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="Pepa o Roberto")
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertIsNone(resolved.resolved_reports_to_role_id)
        self.assertEqual(resolved.ambiguous_candidates, ["cfo", "chief_of_staff"])

    def test_no_match_at_all(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="Juan Nadie")
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertIsNone(resolved.resolved_reports_to_role_id)
        self.assertEqual(resolved.ambiguous_candidates, [])
        self.assertIn("Juan Nadie", resolved.unmatched_candidates)

    def test_no_reports_to_guess_at_all(self):
        findings = ImportFindings(actor_id="x", reports_to_guess=None)
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertIsNone(resolved.resolved_reports_to_role_id)

    def test_fragment_uses_resolved_role_id(self):
        findings = ImportFindings(actor_id="marcos", reports_to_guess="Pepa")
        resolved = resolve_against_org(findings, self.au_spec)
        fragment = render_org_yaml_fragment(
            findings,
            role_id="new_role",
            department_id="operaciones",
            resolved=resolved,
        )
        self.assertIn("reports_to: chief_of_staff", fragment)

    def test_fragment_flags_ambiguity_explicitly(self):
        findings = ImportFindings(actor_id="marcos", reports_to_guess="Pepa o Roberto")
        resolved = resolve_against_org(findings, self.au_spec)
        fragment = render_org_yaml_fragment(
            findings,
            role_id="new_role",
            department_id="operaciones",
            resolved=resolved,
        )
        self.assertIn("reports_to: null", fragment)
        self.assertIn("AMBIGUOUS", fragment)
        self.assertIn("cfo", fragment)
        self.assertIn("chief_of_staff", fragment)


class TestFuzzyMatching(unittest.TestCase):
    """
    Closes the declared gap: before, there was only exact or substring
    matching; a typo or a reasonable variant of the name did not resolve
    anything. Now there is a third level (difflib, stdlib) that does
    detect it, but ALWAYS flagged as approximate — never with the same
    confidence as an exact match.
    """

    def setUp(self):
        self.au_spec = load_org_yaml(AU_ORG)

    def test_typo_in_actor_id_resolves_via_fuzzy_match(self):
        # "Robrto" (missing the 'e'): neither exact nor substring of "roberto", but fuzzy does match.
        findings = ImportFindings(actor_id="x", reports_to_guess="Robrto")
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertEqual(resolved.resolved_reports_to_role_id, "cfo")
        self.assertEqual(len(resolved.fuzzy_matches), 1)
        cand, _, role_id = resolved.fuzzy_matches[0]
        self.assertEqual(cand, "Robrto")
        self.assertEqual(role_id, "cfo")
        self.assertTrue(any("fuzzy match" in n for n in resolved.resolution_notes))

    def test_completely_unrelated_word_does_not_fuzzy_match(self):
        # Nothing remotely similar to an AU role/actor: it must not force a match.
        findings = ImportFindings(actor_id="x", reports_to_guess="Xylophone")
        resolved = resolve_against_org(findings, self.au_spec)

        self.assertIsNone(resolved.resolved_reports_to_role_id)
        self.assertEqual(resolved.fuzzy_matches, [])
        self.assertIn("Xylophone", resolved.unmatched_candidates)

    def test_exact_match_is_not_reported_as_fuzzy(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="Pepa")
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertEqual(resolved.fuzzy_matches, [])  # exact match, not fuzzy


class TestDepartmentSuggestion(unittest.TestCase):
    """
    Closes the second declared gap: before, if the text did not mention
    anyone already existing in the spec (or there was no reports_to at
    all), there was no way to suggest a department — --department always
    had to be passed by hand. Now there are two additional paths.
    """

    def setUp(self):
        self.au_spec = load_org_yaml(AU_ORG)

    def test_department_guess_resolves_exactly(self):
        findings = ImportFindings(actor_id="x", department_guess="Formación")
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertEqual(resolved.suggested_department_id, "formacion")
        self.assertIn("department_guess", resolved.department_source)

    def test_department_guess_resolves_with_typo_via_fuzzy(self):
        findings = ImportFindings(
            actor_id="x", department_guess="Formacion"
        )  # no accent
        resolved = resolve_against_org(findings, self.au_spec)
        # "Formacion" without the accent: it is not an exact substring of "formación"
        # because of the accent, but it should still resolve by fuzzy match.
        self.assertEqual(resolved.suggested_department_id, "formacion")

    def test_department_guess_no_match_leaves_unresolved_with_reason(self):
        findings = ImportFindings(
            actor_id="x", department_guess="Marketing Digital Internacional"
        )
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertIsNone(resolved.suggested_department_id)
        self.assertTrue(any("doesn't match" in n for n in resolved.resolution_notes))

    def test_role_name_hint_suggests_department_with_low_confidence_note(self):
        findings = ImportFindings(
            actor_id="x", role_name_guess="Director de Operaciones"
        )
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertEqual(resolved.suggested_department_id, "operaciones")
        self.assertEqual(resolved.department_source, "role_name_hint")
        self.assertTrue(any("low confidence" in n for n in resolved.resolution_notes))

    def test_resolved_superior_takes_priority_over_department_guess(self):
        # If "reports_to" resolves, the department comes from there, not from
        # department_guess (more reliable: it is the role's real department).
        findings = ImportFindings(
            actor_id="x",
            reports_to_guess="Pepa",
            department_guess="Formación",
        )
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertEqual(
            resolved.suggested_department_id, "direccion"
        )  # real department of chief_of_staff
        self.assertEqual(resolved.department_source, "resolved_superior")

    def test_no_hints_at_all_leaves_clear_reason(self):
        findings = ImportFindings(actor_id="x")
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertIsNone(resolved.suggested_department_id)
        self.assertTrue(
            any(
                "Could not infer the department" in n for n in resolved.resolution_notes
            )
        )


class TestImportAuditHardening(unittest.TestCase):
    """cli-tests F5/F6/F10/F11: normalized actor ids, unreadable persona
    files, possessive-name split noise, and reports_to_human passthrough."""

    def setUp(self):
        self.au_spec = load_org_yaml(AU_ORG)

    # --- F6: unreadable persona files must warn, not crash -----------
    def test_binary_identity_file_warns_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            # UTF-16 content is not decodable as UTF-8 (raw bytes).
            (persona_dir / "IDENTITY.md").write_bytes(b"\xff\xfeR\x00o\x00l\x00e\x00")

            findings = audit_persona_dir(persona_dir)
            self.assertTrue(any("Could not read" in w for w in findings.warnings))
            # The rest of the audit must still run (no crash).
            self.assertIsNone(findings.role_name_guess)

    def test_binary_tools_md_warns_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
            (persona_dir / "tools.md").write_bytes(b"\xff\xfe- \x00tool\x00")

            findings = audit_persona_dir(persona_dir)
            self.assertTrue(
                any("Could not read tools.md" in w for w in findings.warnings)
            )
            self.assertEqual(findings.tools_guess, [])

    # --- F5: actor id normalization -----------------------------------
    def test_actor_id_normalized_from_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "Carla Gómez"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text(
                "**Role**: Coordinadora\n", encoding="utf-8"
            )

            findings = audit_persona_dir(persona_dir)
            self.assertEqual(findings.actor_id, "carla_gomez")
            self.assertTrue(any("normalized" in w for w in findings.warnings))

    def test_actor_id_left_alone_when_already_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            findings = audit_persona_dir(persona_dir)
            self.assertEqual(findings.actor_id, "marcos")

    # --- F10: possessive/contraction split noise ----------------------
    def test_possessive_name_does_not_pollute_unmatched(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="John O'Connor")
        resolved = resolve_against_org(findings, self.au_spec)
        # 'Connor is split noise from the \bo\b on the possessive; it
        # must never surface as an external human candidate.
        self.assertNotIn("'Connor", resolved.unmatched_candidates)
        self.assertNotIn("Connor", resolved.unmatched_candidates)

    def test_split_drops_non_alphabetic_fragments(self):
        findings = ImportFindings(actor_id="x", reports_to_guess="Pepa, 123")
        resolved = resolve_against_org(findings, self.au_spec)
        self.assertNotIn("123", resolved.unmatched_candidates)

    # --- F11: reports_to_human in fragment + apply --------------------
    def test_fragment_emits_reports_to_human_for_unmatched(self):
        findings = ImportFindings(actor_id="marcos", reports_to_guess="Pepa, Salvador")
        resolved = resolve_against_org(findings, self.au_spec)
        fragment = render_org_yaml_fragment(
            findings,
            role_id="new_role",
            department_id="operaciones",
            resolved=resolved,
        )
        self.assertIn("reports_to: chief_of_staff", fragment)
        self.assertIn('reports_to_human: "Salvador"', fragment)
        self.assertIn("REVIEW", fragment)

    def test_fragment_omits_reports_to_human_when_everything_resolves(self):
        findings = ImportFindings(actor_id="marcos", reports_to_guess="Pepa")
        resolved = resolve_against_org(findings, self.au_spec)
        fragment = render_org_yaml_fragment(
            findings,
            role_id="new_role",
            department_id="operaciones",
            resolved=resolved,
        )
        self.assertIn("reports_to: chief_of_staff", fragment)
        self.assertNotIn("reports_to_human", fragment)

    def test_apply_passes_reports_to_human_into_org_yaml(self):
        from click.testing import CliRunner

        from phantomorg.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            target_org = Path(tmp) / "org.yaml"
            shutil.copy(AU_ORG, target_org)
            persona_dir = Path(tmp) / "marcos"
            persona_dir.mkdir()
            (persona_dir / "IDENTITY.md").write_text(
                "**Role**: Director de Algo\n**Reports to**: Pepa, Salvador\n",
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "import-audit",
                    "--persona-dir",
                    str(persona_dir),
                    "--role-id",
                    "new_role",
                    "--against-org",
                    str(target_org),
                    "--apply",
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            doc = yaml.safe_load(target_org.read_text(encoding="utf-8"))
            role = next(r for r in doc["roles"] if r["id"] == "new_role")
            self.assertEqual(role["reports_to"], "chief_of_staff")
            self.assertEqual(role["reports_to_human"], "Salvador")


if __name__ == "__main__":
    unittest.main()
