import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build, get_strings, resolve_lang
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"

_EN_ORG_YAML = """
version: 1
organization:
  id: acme-en
  name: "Acme Inc"
  sector: pyme
  languages: [en, es]
  default_language: en
departments:
  - {id: hq, name: "Headquarters", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: hq
    reports_to: null
    access_level: level-3
    functions: [vision]
actors:
  - id: lisa
    role: ceo
    telegram_bot: "@CEO_ACME_bot"
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
"""


class TestLangResolution(unittest.TestCase):
    def test_au_resolves_to_es(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(resolve_lang(spec), "es")

    def test_explicit_default_language_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path = Path(tmp) / "org.yaml"
            org_path.write_text(_EN_ORG_YAML, encoding="utf-8")
            spec = load_org_yaml(org_path)
            self.assertEqual(resolve_lang(spec), "en")

    def test_unknown_lang_falls_back_to_es_strings(self):
        strings = get_strings("fr")  # unsupported
        self.assertEqual(strings, get_strings("en"))


class TestSoulRendersInResolvedLanguage(unittest.TestCase):
    def test_au_soul_is_in_spanish(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="alma")
            soul = (out_dir / "alma" / "SOUL.md").read_text(encoding="utf-8")

        self.assertIn("Principios de decisión", soul)
        self.assertIn("Alcance y jerarquía", soul)
        self.assertIn("Reglas de escalado", soul)
        self.assertIn("Comunicación entre roles", soul)
        self.assertNotIn("Decision principles", soul)

    def test_en_org_soul_is_in_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            org_path = tmp / "org.yaml"
            org_path.write_text(_EN_ORG_YAML, encoding="utf-8")
            spec = load_org_yaml(org_path)

            out_dir = tmp / "dist"
            build(spec, out_dir, only="lisa")
            soul = (out_dir / "lisa" / "SOUL.md").read_text(encoding="utf-8")

        self.assertIn("Decision principles", soul)
        self.assertIn("Scope and hierarchy", soul)
        self.assertIn("Escalation rules", soul)
        self.assertIn("Cross-role communication", soul)
        self.assertNotIn("Principios de decisión", soul)

    def test_en_identity_and_tools_are_in_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            org_path = tmp / "org.yaml"
            org_path.write_text(_EN_ORG_YAML, encoding="utf-8")
            spec = load_org_yaml(org_path)

            out_dir = tmp / "dist"
            build(spec, out_dir, only="lisa")
            identity = (out_dir / "lisa" / "IDENTITY.md").read_text(encoding="utf-8")
            tools = (out_dir / "lisa" / "tools.md").read_text(encoding="utf-8")

        self.assertIn("**Name**:", identity)
        self.assertIn("**Role**:", identity)
        self.assertIn("Functions", identity)
        self.assertIn("Available logical tools", tools)

    def test_dynamic_content_is_never_translated_only_labels(self):
        # Role/department/function names come from the spec as-is, in any
        # language -- only the fixed labels are translated.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            org_path = tmp / "org.yaml"
            org_path.write_text(_EN_ORG_YAML, encoding="utf-8")
            spec = load_org_yaml(org_path)

            out_dir = tmp / "dist"
            build(spec, out_dir, only="lisa")
            soul = (out_dir / "lisa" / "SOUL.md").read_text(encoding="utf-8")

        self.assertIn("Headquarters", soul)  # real department name, untranslated
        self.assertIn(
            "Executive", soul
        )  # real label from policies.access_levels, untranslated


if __name__ == "__main__":
    unittest.main()
