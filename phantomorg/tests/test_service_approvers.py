"""Human approvers are distinct identities, never inferred actors or consumers."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from phantomorg.compiler.service_policy import compile_service_policy
from phantomorg.spec.service_policy import ServicePolicy, ServicePolicyError
from phantomorg.spec.shape_validator import ShapeError, validate_shape
from phantomorg.validator import validate_org

ROOT = Path(__file__).parent.parent


class TestHumanApprovers(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load(
            (ROOT / "organizations/verdant-aquaponics/org.yaml").read_text()
        )
        self.raw.setdefault("humans", []).append(
            {"id": "president", "name": "Example President", "role": "President"}
        )
        self.raw["service_policy"] = {
            "version": 2,
            "default_access": "deny",
            "services": [
                {
                    "id": "research",
                    "kind": "private",
                    "owner_actor": "dana",
                    "consumers": [{"kind": "actor", "id": "dana"}],
                    "approval": "per-request",
                    "approvers": [
                        {"kind": "actor", "id": "marco"},
                        {"kind": "human", "id": "president"},
                    ],
                }
            ],
        }

    def validate(self, raw):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "org.yaml"
            path.write_text(yaml.safe_dump(raw))
            return validate_org(path)

    def test_typed_approval_does_not_grant_session_access(self):
        spec, result = self.validate(self.raw)
        self.assertTrue(result.ok, result.errors)
        output = json.loads(compile_service_policy(spec))
        self.assertEqual(output["format_version"], 2)
        self.assertIn("president", output["humans"])
        self.assertNotIn("president", output["actors"])
        self.assertEqual(
            output["services"][0]["consumers"], [{"kind": "actor", "id": "dana"}]
        )
        self.assertEqual(
            output["services"][0]["approvers"],
            self.raw["service_policy"]["services"][0]["approvers"],
        )

    def test_actor_and_human_namespaces_cannot_substitute_for_each_other(self):
        for kind, identity in (("human", "marco"), ("actor", "president")):
            candidate = copy.deepcopy(self.raw)
            candidate["service_policy"]["services"][0]["approvers"] = [
                {"kind": kind, "id": identity}
            ]
            spec, result = self.validate(candidate)
            with self.subTest(kind=kind):
                self.assertFalse(result.ok)
                with self.assertRaises(ServicePolicyError):
                    compile_service_policy(spec)

    def test_schema_and_runtime_reject_ambiguous_or_authority_bearing_input(self):
        schema = json.loads((ROOT / "phantomorg/spec/schema.json").read_text())
        jsonschema.validate(self.raw, schema)
        validate_shape(self.raw)
        invalid = [
            [{"kind": "service", "id": "research"}],
            ["president"],
            [{"kind": "human", "id": "president", "approved": True}],
            [
                {"kind": "human", "id": "president"},
                {"kind": "human", "id": "president"},
            ],
        ]
        for approvers in invalid:
            candidate = copy.deepcopy(self.raw)
            candidate["service_policy"]["services"][0]["approvers"] = approvers
            with self.subTest(approvers=approvers):
                with self.assertRaises(ShapeError):
                    validate_shape(candidate)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(candidate, schema)

    def test_v1_does_not_silently_accept_v2_approvers(self):
        self.raw["service_policy"]["version"] = 1
        with self.assertRaises(ServicePolicyError):
            ServicePolicy.from_dict(self.raw["service_policy"])

    def test_order_is_canonical_and_human_removal_invalidates_policy(self):
        spec, result = self.validate(self.raw)
        original = compile_service_policy(spec)
        self.raw["service_policy"]["services"][0]["approvers"].reverse()
        spec, result = self.validate(self.raw)
        self.assertTrue(result.ok)
        self.assertEqual(original, compile_service_policy(spec))
        self.raw["humans"] = [h for h in self.raw["humans"] if h["id"] != "president"]
        spec, result = self.validate(self.raw)
        self.assertFalse(result.ok)
        with self.assertRaises(ServicePolicyError):
            compile_service_policy(spec)
