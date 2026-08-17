import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build
from phantomorg.compiler.request_id import resolve_request_id_format
from phantomorg.spec.loader import load_org_yaml
from phantomorg.wizard.new_org import new_org

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


class TestRequestIdResolution(unittest.TestCase):
    def test_resolve_replaces_org_id_only(self):
        spec = load_org_yaml(AU_ORG)
        resolved = resolve_request_id_format(spec)
        self.assertEqual(resolved, "aquaponics-united-{yyyymmdd}-{seq4}")
        # yyyymmdd and seq4 must stay literal: they are not compile-time concerns
        self.assertIn("{yyyymmdd}", resolved)
        self.assertIn("{seq4}", resolved)
        self.assertNotIn("{org_id}", resolved)

    def test_soul_md_has_org_id_resolved_not_literal(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="alma")
            soul = (out_dir / "alma" / "SOUL.md").read_text(encoding="utf-8")
            self.assertIn("aquaponics-united-{yyyymmdd}-{seq4}", soul)
            self.assertNotIn("{org_id}", soul)

    def test_new_org_writes_unresolved_template_consistently(self):
        """
        new-org no longer resolves {org_id} early (it used to do it with
        an f-string, inconsistently with hand-written org.yamls). The
        placeholder is left literal in the file; only the compiler
        resolves it, whatever the origin of the org.yaml.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            path = new_org("acme", "ACME Inc", "pyme", ["es"], base_dir=base_dir)
            content = path.read_text(encoding="utf-8")
            self.assertIn("{org_id}-{yyyymmdd}-{seq4}", content)


if __name__ == "__main__":
    unittest.main()
