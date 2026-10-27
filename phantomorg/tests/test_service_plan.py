"""Installation review must not turn incomplete observations into deployment authority."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml
from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.compiler.service_plan import ServicePlanError, compile_service_plan
from phantomorg.compiler.service_policy import compile_service_policy
from phantomorg.spec.model import OrgSpec

ROOT = Path(__file__).parent.parent


class TestServicePlan(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load(
            (ROOT / "organizations/verdant-aquaponics/org.yaml").read_text()
        )
        self.raw["service_policy"] = {
            "version": 1,
            "default_access": "deny",
            "services": [
                {
                    "id": "browser",
                    "kind": "private",
                    "owner_actor": "dana",
                    "consumers": [{"kind": "actor", "id": "dana"}],
                    "approval": "none",
                    "approvers": [],
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
        self.spec = OrgSpec.from_dict(self.raw)
        self.bindings = {
            "version": 1,
            "organization_id": self.spec.organization.id,
            "policy_sha256": json.loads(compile_service_policy(self.spec))[
                "policy_sha256"
            ],
            "host_id": "example-host",
            "actors": [],
            "services": [],
        }
        self.inventory = {
            "version": 1,
            "host_id": "example-host",
            "accounts": [],
            "units": [],
        }
        for i, actor in enumerate(self.spec.actors):
            self.add_binding("actors", actor.id, "pb-" + actor.id, 1000 + i)
        for i, service in enumerate(self.spec.service_policy.services):
            account = (
                "pb-" + service.owner_actor
                if service.owner_actor
                else "sv-" + service.id
            )
            self.add_binding("services", service.id, account, 2000 + i)

    def add_binding(self, kind, identity, account, uid):
        unit = {
            "name": identity + ".service",
            "scope": "system" if kind == "actors" else "user",
        }
        row = {"id": identity, "account": account, "units": [unit]}
        if kind == "services":
            row["endpoint"] = {"transport": "tcp", "address": "127.0.0.1", "port": 9000}
        self.bindings[kind].append(row)
        if not any(a["name"] == account for a in self.inventory["accounts"]):
            self.inventory["accounts"].append({"name": account, "uid": uid})
        self.inventory["units"].append(
            {**unit, "account": account, "namespace": "host", "active": True}
        )

    def plan(self):
        return compile_service_plan(self.spec, self.bindings, self.inventory)

    def test_explicit_plan_is_never_deployable_or_applied(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "review-required")
        self.assertFalse(plan["deployable"])
        self.assertFalse(plan["enforcement_applied"])
        namespaces = {u["account"]: u["proposed_namespace"] for u in plan["units"]}
        self.assertNotEqual(namespaces["pb-dana"], namespaces["pb-lucia"])
        self.assertTrue(all(not c["gateway_verified"] for c in plan["connections"]))
        self.assertEqual(plan, self.plan())

    def test_host_org_policy_and_missing_actor_mismatch_fail_before_planning(self):
        for field, value in (
            ("host_id", "other"),
            ("organization_id", "other"),
            ("policy_sha256", "0" * 64),
            ("actors", []),
            ("version", True),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.bindings)
                candidate[field] = value
                with self.assertRaises(ServicePlanError):
                    compile_service_plan(self.spec, candidate, self.inventory)

    def test_shared_uid_is_blocked_even_with_different_account_names(self):
        self.inventory["accounts"][1]["uid"] = self.inventory["accounts"][0]["uid"]
        self.assertTrue(any("UID is shared" in b for b in self.plan()["blockers"]))

    def test_unknown_root_and_unconfirmed_account_are_blocked(self):
        self.inventory["accounts"][0]["uid"] = 0
        self.inventory["units"].pop()
        plan = self.plan()
        self.assertEqual(plan["status"], "blocked")
        self.assertTrue(any("not be root" in b for b in plan["blockers"]))
        self.assertTrue(any("not confirmed" in b for b in plan["blockers"]))

    def test_unmapped_dormant_unit_is_blocked_and_outside_accounts_preserved(self):
        self.inventory["units"].append(
            {
                "name": "hidden-browser.service",
                "scope": "user",
                "account": "pb-dana",
                "namespace": "host",
                "active": False,
            }
        )
        self.inventory["accounts"].append({"name": "other-org", "uid": 4000})
        self.inventory["units"].append(
            {
                "name": "bot.service",
                "scope": "user",
                "account": "other-org",
                "namespace": "host",
                "active": True,
            }
        )
        plan = self.plan()
        self.assertTrue(any("hidden-browser" in b for b in plan["blockers"]))
        self.assertTrue(
            any(u["account"] == "other-org" for u in plan["outside_scope_units"])
        )

    def test_approval_declaration_cannot_pass_as_verified_authorization(self):
        service = replace(
            self.spec.service_policy.services[0],
            approval="per-request",
            approvers=("lucia",),
        )
        self.spec.service_policy = replace(
            self.spec.service_policy,
            services=(service,) + self.spec.service_policy.services[1:],
        )
        self.bindings["policy_sha256"] = json.loads(compile_service_policy(self.spec))[
            "policy_sha256"
        ]
        self.assertTrue(
            any("approval enforcement" in b for b in self.plan()["blockers"])
        )

    def test_private_endpoint_collision_and_namespace_separation(self):
        first = self.plan()
        self.assertFalse(first["blockers"])
        duplicate = replace(self.spec.service_policy.services[0], id="second-browser")
        self.spec.service_policy = replace(
            self.spec.service_policy,
            services=self.spec.service_policy.services + (duplicate,),
        )
        self.add_binding("services", "second-browser", "pb-dana", 3000)
        self.bindings["policy_sha256"] = json.loads(compile_service_policy(self.spec))[
            "policy_sha256"
        ]
        self.assertTrue(any("endpoint collides" in b for b in self.plan()["blockers"]))

    def test_inventory_can_include_non_persona_system_accounts(self):
        self.inventory["accounts"].append({"name": "_apt", "uid": 999})
        self.assertEqual(self.plan()["status"], "review-required")

    def test_duplicate_units_and_private_owner_split_block(self):
        self.bindings["services"][0]["account"] = self.bindings["actors"][0]["account"]
        self.bindings["services"][1]["units"] = self.bindings["actors"][0]["units"]
        blockers = self.plan()["blockers"]
        self.assertTrue(any("same account" in b for b in blockers))
        self.assertTrue(any("assigned more than once" in b for b in blockers))

    def test_unsupported_endpoints_and_shell_unit_strings_are_rejected(self):
        for endpoint in (
            {"transport": "tcp", "address": "0.0.0.0", "port": 9000},
            {"transport": "tcp", "address": "localhost", "port": 9000},
            {"transport": "tcp", "address": "::1", "port": True},
            {"transport": "unix", "path": "/tmp/service"},
        ):
            candidate = copy.deepcopy(self.bindings)
            candidate["services"][0]["endpoint"] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ServicePlanError):
                compile_service_plan(self.spec, candidate, self.inventory)
        self.bindings["actors"][0]["units"][0]["name"] = "bot.service; touch /tmp/file"
        with self.assertRaises(ServicePlanError):
            self.plan()

    def test_cli_blocked_diagnostics_and_bad_json_preserve_previous_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            org, binding, inventory, output = (
                root / p
                for p in ("org.yaml", "bindings.json", "inventory.json", "plan.json")
            )
            org.write_text(yaml.safe_dump(self.raw))
            binding.write_text(json.dumps(self.bindings))
            self.inventory["units"].pop()
            inventory.write_text(json.dumps(self.inventory))
            args = [
                "service-plan",
                "--org",
                str(org),
                "--bindings",
                str(binding),
                "--inventory",
                str(inventory),
                "--out",
                str(output),
            ]
            result = CliRunner().invoke(main, args)
            self.assertEqual(result.exit_code, 2, result.output)
            saved = output.read_bytes()
            self.assertEqual(json.loads(saved)["status"], "blocked")
            binding.write_text('{"version":1,"version":1}')
            result = CliRunner().invoke(main, args)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(output.read_bytes(), saved)
