"""Memory scope derivation + scopes.json artifact (v0.6.0).

Covers:

- derive_scopes chain rule (default): self + transitive subordinates;
  full access for top-of-chain, highest access level, category-0.
- derive_scopes department rule: same department + descendants; root
  department gets full visibility.
- Unknown rule rejected.
- build() writes out_dir/scopes.json (only on full builds, not --only).
- The artifact is deterministic (identical content -> not rewritten).
- deploy() transports scopes.json to the DATA DIR (target.parent,
  e.g. ~/.local/share/phantombot/scopes.json), atomically, and
  refuses symlinks on both sides. It never writes inside the target
  tree, so the rollback byte-for-byte invariant is preserved.
- Backward compatible: an old build without scopes.json deploys without
  writing anything new.
"""

import json
import tempfile
import unittest
from pathlib import Path

from phantomforge.compiler import build
from phantomforge.compiler.scopes import (
    SCOPES_FILENAME,
    ScopeError,
    derive_scopes,
    serialize_scopes,
)
from phantomforge.deploy.target import DeployCollisionError, deploy
from phantomforge.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"

# Expected chain scopes for the AU org model (see organizations/
# aquaponics-united/org.yaml):
#   paco  = ceo            (reports_to null, category-0)      -> full
#   pepa  = chief_of_staff (reports to ceo; alma+elena report to her)
#   roberto = cfo          (reports to ceo, no subordinates)  -> self only
#   alma  = project_lead   (reports to chief_of_staff)        -> self only
#   elena = training_lead  (reports to chief_of_staff)        -> self only
EXPECTED_CHAIN = {
    "paco": ["*"],
    "pepa": ["alma", "elena", "pepa"],
    "roberto": ["roberto"],
    "alma": ["alma"],
    "elena": ["elena"],
}

# Department rule: direccion (paco, pepa) is the root -> full; the other
# departments have a single actor each -> self only.
EXPECTED_DEPARTMENT = {
    "paco": ["*"],
    "pepa": ["*"],
    "roberto": ["roberto"],
    "alma": ["alma"],
    "elena": ["elena"],
}


class TestDeriveScopes(unittest.TestCase):
    def test_chain_rule_au(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(derive_scopes(spec, rule="chain"), EXPECTED_CHAIN)

    def test_department_rule_au(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(derive_scopes(spec, rule="department"), EXPECTED_DEPARTMENT)

    def test_default_rule_is_chain(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(derive_scopes(spec), derive_scopes(spec, rule="chain"))

    def test_unknown_rule_rejected(self):
        spec = load_org_yaml(AU_ORG)
        with self.assertRaises(ScopeError):
            derive_scopes(spec, rule="everything")

    def test_serialize_is_deterministic(self):
        spec = load_org_yaml(AU_ORG)
        scopes = derive_scopes(spec)
        a = serialize_scopes(spec, scopes, "chain")
        b = serialize_scopes(spec, scopes, "chain")
        self.assertEqual(a, b)
        payload = json.loads(a)
        self.assertEqual(payload["org"], "aquaponics-united")
        self.assertEqual(payload["rule"], "chain")
        self.assertEqual(payload["scopes"], scopes)


class TestBuildWritesScopes(unittest.TestCase):
    def test_build_writes_scopes_json(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir)
            scopes_path = out_dir / SCOPES_FILENAME
            self.assertTrue(scopes_path.exists())
            payload = json.loads(scopes_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["scopes"], EXPECTED_CHAIN)
            # Reported under the __scopes__ key.
            self.assertIn("__scopes__", written)
            self.assertEqual(written["__scopes__"], [scopes_path])

    def test_build_only_actor_skips_scopes(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="alma")
            self.assertFalse((out_dir / SCOPES_FILENAME).exists())

    def test_build_idempotent_no_rewrite(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir)
            scopes_path = out_dir / SCOPES_FILENAME
            content = scopes_path.read_text(encoding="utf-8")
            written = build(spec, out_dir)
            self.assertEqual(scopes_path.read_text(encoding="utf-8"), content)
            # Nothing reported as written on the second pass.
            self.assertNotIn("__scopes__", written)

    def test_build_department_rule(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, scope_rule="department")
            payload = json.loads(
                (out_dir / SCOPES_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(payload["rule"], "department")
            self.assertEqual(payload["scopes"], EXPECTED_DEPARTMENT)


class TestDeployScopes(unittest.TestCase):
    def _build(self, tmp: Path, rule: str = "chain") -> Path:
        spec = load_org_yaml(AU_ORG)
        out = tmp / "dist"
        build(spec, out, scope_rule=rule)
        return out

    def test_deploy_transports_scopes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compiled = self._build(root)
            target = root / "personas"
            result = deploy(compiled, target)
            self.assertTrue(result.scopes_written)
            # Written to the DATA DIR (target.parent), never inside the
            # target tree (rollback invariant: target stays byte-for-byte
            # restorable).
            dest = target.parent / SCOPES_FILENAME
            self.assertTrue(dest.exists())
            self.assertFalse((target / SCOPES_FILENAME).exists())
            payload = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(payload["scopes"], EXPECTED_CHAIN)

    def test_deploy_backward_compatible_without_scopes(self):
        spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compiled = root / "dist"
            build(spec, compiled)
            # Simulate an old build: remove the artifact.
            (compiled / SCOPES_FILENAME).unlink()
            target = root / "personas"
            result = deploy(compiled, target)
            self.assertFalse(result.scopes_written)
            self.assertFalse((target.parent / SCOPES_FILENAME).exists())

    def test_deploy_refuses_symlinked_compiled_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compiled = self._build(root)
            scopes = compiled / SCOPES_FILENAME
            real = compiled / "scopes.real"
            real.write_text("{}", encoding="utf-8")
            scopes.unlink()
            scopes.symlink_to(real)
            target = root / "personas"
            with self.assertRaises(DeployCollisionError):
                deploy(compiled, target)

    def test_deploy_refuses_symlinked_target_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compiled = self._build(root)
            target = root / "personas"
            target.mkdir(parents=True, exist_ok=True)
            (target.parent / SCOPES_FILENAME).symlink_to(root / "victim.json")
            with self.assertRaises(DeployCollisionError):
                deploy(compiled, target)


if __name__ == "__main__":
    unittest.main()
