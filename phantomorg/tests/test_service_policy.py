"""Portable service intent never grants approval or cross-persona session access."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml
from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.compiler.service_policy import compile_service_policy
from phantomorg.spec.loader import load_org_yaml
from phantomorg.spec.service_policy import ServicePolicy, ServicePolicyError
from phantomorg.spec.shape_validator import ShapeError, validate_shape
from phantomorg.validator import validate_org

ROOT = Path(__file__).parent.parent
ORG = ROOT / "organizations/verdant-aquaponics/org.yaml"


def policy():
    return {
        "version": 1,
        "default_access": "deny",
        "services": [
            {
                "id": "browser",
                "kind": "private",
                "owner_actor": "dana",
                "consumers": [{"kind": "actor", "id": "dana"}],
                "approval": "per-request",
                "approvers": ["lucia", "marco"],
            },
            {
                "id": "view",
                "kind": "shared",
                "consumers": [],
                "approval": "none",
                "approvers": [],
            },
            {
                "id": "worker",
                "kind": "shared",
                "consumers": [{"kind": "service", "id": "view"}],
                "approval": "none",
                "approvers": [],
            },
        ],
    }


class TestServicePolicy(unittest.TestCase):
    def raw(self):
        raw = yaml.safe_load(ORG.read_text(encoding="utf-8"))
        raw["service_policy"] = policy()
        return raw

    def write(self, folder, raw):
        path = Path(folder) / "org.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    def test_private_session_is_not_shared_with_approvers(self):
        for c in ({"kind": "actor", "id": "lucia"}, {"kind": "service", "id": "view"}):
            raw = policy()
            raw["services"][0]["consumers"].append(c)
            with self.subTest(c=c), self.assertRaises(ServicePolicyError):
                ServicePolicy.from_dict(raw)

    def test_invalid_shape_fails_closed(self):
        mutations = [
            lambda p: p.update(default_access="allow"),
            lambda p: p.update(version=True),
            lambda p: p.update(services=None),
            lambda p: p.update(port=18802),
            lambda p: p["services"][0].update(password="forbidden"),
            lambda p: p["services"][0].update(approvers=[]),
            lambda p: p["services"][0].update(approval="none"),
            lambda p: p["services"][1].update(owner_actor="marco"),
            lambda p: p["services"].append(copy.deepcopy(p["services"][0])),
            lambda p: p["services"][0].update(consumers=[]),
        ]
        for mutate in mutations:
            raw = policy()
            mutate(raw)
            with self.subTest(raw=raw), self.assertRaises(ServicePolicyError):
                ServicePolicy.from_dict(raw)

    def test_unknown_references_block_validation_and_export(self):
        for field in ("owner_actor", "approvers", "consumers"):
            raw = self.raw()
            s = raw["service_policy"]["services"][0]
            if field == "owner_actor":
                s.update(
                    owner_actor="unknown",
                    consumers=[{"kind": "actor", "id": "unknown"}],
                )
            elif field == "approvers":
                s[field] = ["unknown"]
            else:
                raw["service_policy"]["services"][2][field] = [
                    {"kind": "service", "id": "unknown"}
                ]
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                spec, result = validate_org(self.write(folder, raw))
                self.assertFalse(result.ok)
                with self.assertRaises(ServicePolicyError):
                    compile_service_policy(spec)

    def test_private_principal_and_self_consumer_rejected(self):
        for name in ("browser", "worker"):
            raw = policy()
            raw["services"][2]["consumers"] = [{"kind": "service", "id": name}]
            self.assertTrue(
                ServicePolicy.from_dict(raw).reference_errors(
                    {"dana", "lucia", "marco"}
                )
            )

    def test_deterministic_export_and_policy_change(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = self.raw()
            path = self.write(folder, raw)
            spec, result = validate_org(path)
            self.assertTrue(result.ok, result.errors)
            original = compile_service_policy(spec)
            raw["service_policy"]["services"].reverse()
            raw["service_policy"]["services"][-1]["approvers"].reverse()
            self.write(folder, raw)
            self.assertEqual(original, compile_service_policy(load_org_yaml(path)))
            raw["service_policy"]["services"][-1]["approvers"] = ["marco"]
            self.write(folder, raw)
            changed = compile_service_policy(load_org_yaml(path))
            self.assertNotEqual(
                json.loads(original)["policy_sha256"],
                json.loads(changed)["policy_sha256"],
            )

    def test_legacy_org_does_not_gain_implicit_policy(self):
        spec, result = validate_org(ORG)
        self.assertTrue(result.ok)
        self.assertIsNone(spec.service_policy)
        with self.assertRaises(ServicePolicyError):
            compile_service_policy(spec)

    def test_schema_and_runtime_accept_and_reject_examples(self):
        raw = self.raw()
        schema = json.loads((ROOT / "phantomorg/spec/schema.json").read_text())
        validate_shape(raw)
        jsonschema.validate(raw, schema)
        raw["service_policy"]["default_access"] = "allow"
        with self.assertRaises(ShapeError):
            validate_shape(raw)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(raw, schema)

    def test_cli_idempotence_failure_preserves_artifact_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, self.raw())
            out = Path(folder) / "policy.json"
            args = ["service-policy", "--org", str(path), "--out", str(out)]
            runner = CliRunner()
            result = runner.invoke(main, args)
            self.assertEqual(result.exit_code, 0, result.output)
            content, stamp = out.read_bytes(), out.stat().st_mtime_ns
            self.assertEqual(runner.invoke(main, args).exit_code, 0)
            self.assertEqual(out.stat().st_mtime_ns, stamp)
            source = path.read_bytes()
            self.assertNotEqual(
                runner.invoke(
                    main, ["service-policy", "--org", str(path), "--out", str(path)]
                ).exit_code,
                0,
            )
            self.assertEqual(source, path.read_bytes())
            raw = self.raw()
            raw["service_policy"]["services"][0]["approvers"] = ["unknown"]
            self.write(folder, raw)
            self.assertNotEqual(runner.invoke(main, args).exit_code, 0)
            self.assertEqual(out.read_bytes(), content)
