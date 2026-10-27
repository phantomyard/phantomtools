import os
import tempfile
import unittest
from copy import deepcopy
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from unittest.mock import patch

from fixtures import adapter, create

from phantomblog import connectors, core


class FakeRunner:
    def __init__(self):
        self.calls, self.captures, self.fail = [], [], set()

    def call(self, connection, spec, arguments):
        self.calls.append((connection, spec, arguments))
        if spec["tool"] in self.fail:
            raise connectors.ExternalFailure("Synthetic failure")
        return {"structuredContent": {"ok": True, "id": "synthetic-receipt"}}

    def capture(self, persona, text):
        self.captures.append((persona, text))
        return True

    def describe(self, connection):
        return {
            "tools": [{"name": c["tool"]} for c in connection["capabilities"].values()]
        }


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = create(self.root)
        self.model["connections"] = {
            "deployment": adapter("deploy", "deployment"),
            "facebook": adapter(),
            "linkedin": adapter(provider="linkedin"),
            "chat": adapter("message", "phantomchat"),
        }
        self.model["connections"]["linkedin"]["capabilities"]["share"]["tool"] = (
            "fixture_linkedin"
        )
        self.write()
        self.runner = FakeRunner()

    def write(self):
        core.atomic(self.root / "blog.json", core.encoded(self.model))

    def plan(self):
        return connectors.prepare(
            self.root, "fixture-000", "en", "deployment", ["facebook", "linkedin"]
        )

    def verify(self, url, checksum):
        self.assertEqual(len(self.runner.calls), 1)
        return {"url": url, "contentHash": checksum, "liveVerified": True}

    def test_prepare_is_readonly_and_not_live(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = self.plan()
        self.assertFalse(plan["liveVerified"])
        self.assertEqual(
            before,
            {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()},
        )

    def test_execution_order_and_deduplication(self):
        plan = self.plan()
        result = connectors.execute(
            self.root, plan, plan["approval"], self.runner, self.verify
        )
        self.assertFalse(result["errors"])
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(len(self.runner.captures), 3)
        self.assertTrue(all(p == "synthetic-editor" for p, _ in self.runner.captures))
        connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        self.assertEqual(len(self.runner.calls), 3)

    def test_facebook_mapping_receives_exact_approved_html_hash(self):
        self.model["connections"]["facebook"]["capabilities"]["share"]["arguments"] = {
            "url": "${url}",
            "message": "${title}",
            "contentHash": "${content_hash}",
            "idempotencyKey": "${idempotency_key}",
        }
        self.write()
        plan = self.plan()
        connectors.execute(self.root, plan, plan["approval"], self.runner, self.verify)
        args = self.runner.calls[1][2]
        self.assertEqual(args["contentHash"], plan["contentHash"])
        self.assertEqual(args["url"], plan["url"])
        self.assertEqual(len(args["idempotencyKey"]), 64)

    def test_no_share_before_live_confirmation(self):
        plan = self.plan()

        def failed(url, checksum):
            raise connectors.ExternalFailure("Not live yet")

        with self.assertRaises(connectors.ExternalFailure):
            connectors.execute(self.root, plan, plan["approval"], self.runner, failed)
        self.assertEqual(len(self.runner.calls), 1)
        connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        self.assertEqual(len(self.runner.calls), 3)

    def test_partial_failure_visible_and_not_blindly_retried(self):
        self.runner.fail.add("fixture_share")
        plan = self.plan()
        result = connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        self.assertIn("facebook", result["errors"])
        self.assertIn("linkedin", result["shares"])
        self.runner.fail.clear()
        connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        self.assertEqual(len(self.runner.calls), 3)

    def test_stale_source_template_body_or_asset_invalidates_approval(self):
        plan = self.plan()
        self.model["site"]["name"] = "Changed"
        self.write()
        with self.assertRaises(core.Invalid):
            connectors.execute(self.root, plan, plan["approval"], self.runner)
        self.assertFalse(self.runner.calls)

    def test_asset_change_invalidates_approval_even_without_catalogue_edit(self):
        plan = self.plan()
        path = self.root / "assets/fixture.png"
        core.atomic(path, path.read_bytes() + b"changed")
        with self.assertRaises(core.Invalid):
            connectors.execute(self.root, plan, plan["approval"], self.runner)
        self.assertFalse(self.runner.calls)

    def test_reconciliation_is_explicit_and_retains_history(self):
        self.runner.fail.add("fixture_share")
        plan = self.plan()
        connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        state = connectors.execution_state(self.root)
        key = next(
            k for k, v in state["operations"].items() if v["state"] == "uncertain"
        )
        connectors.reconcile(self.root, key, confirmed_not_sent=True)
        self.runner.fail.clear()
        connectors.execute(
            self.root,
            plan,
            plan["approval"],
            self.runner,
            lambda u, h: {"liveVerified": True},
        )
        entry = connectors.execution_state(self.root)["operations"][key]
        self.assertEqual(entry["state"], "complete")
        self.assertEqual(len(entry["reconciliations"]), 1)
        self.assertEqual(len(self.runner.calls), 4)

    def test_missing_capability_refused(self):
        with self.assertRaises(core.Invalid):
            connectors.prepare(self.root, "fixture-000", "en", "facebook", [])

    def test_credentials_not_accepted_in_mapping(self):
        for key in ("access_token", "password", "nsec", "authorization"):
            config = deepcopy(self.model["connections"])
            config["facebook"]["capabilities"]["share"]["arguments"][key] = "secret"
            with self.assertRaises(core.Invalid):
                connectors.validate_connections(config)

    def test_response_contract(self):
        spec = self.model["connections"]["facebook"]["capabilities"]["share"]
        for response in (
            {"isError": True},
            {"structuredContent": {"ok": False, "id": "x"}},
            {"content": [{"type": "text", "text": "sent!"}]},
            {"structuredContent": {"ok": True}},
        ):
            with self.assertRaises(connectors.ExternalFailure):
                connectors.receipt(response, spec)
        self.assertEqual(
            connectors.receipt(
                {"content": [{"type": "text", "text": '{"ok":true,"id":"fixture"}'}]},
                spec,
            ),
            "fixture",
        )

    def test_phantomchat_requests_require_configured_adapter_and_revision(self):
        _, revision = core.load(self.root)
        result = connectors.message(
            self.root,
            "chat",
            "Please review this draft",
            "fixture-000",
            revision,
            self.runner,
        )
        self.assertEqual(result["state"], "complete")
        request = self.runner.calls[0][2]["request"]
        self.assertIn("phantomblog-review-request-v1", request)
        self.assertIn(revision, request)
        with self.assertRaises(core.Invalid):
            connectors.message(
                self.root, "chat", "Request", "fixture-000", "stale", self.runner
            )

    def test_configured_tools_discovery_not_account_verification(self):
        results = connectors.connection_status(self.root, self.runner)
        self.assertTrue(all(r["state"] == "discovered" for r in results))
        self.runner.describe = lambda c: {
            "tools": [
                {
                    "name": "unrelated",
                    "description": " ".join(
                        s["tool"] for s in c["capabilities"].values()
                    ),
                }
            ]
        }
        results = connectors.connection_status(self.root, self.runner)
        self.assertTrue(all(r["state"] == "tool-not-discovered" for r in results))

    def test_native_runtime_discovery_listing_is_names_only(self):
        with (
            patch(
                "phantomblog.connectors.shutil.which",
                return_value="/fixture/phantombot",
            ),
            patch("phantomblog.connectors.subprocess.run") as run,
        ):
            runner = connectors.Runner()
            run.return_value.returncode = 0
            run.return_value.stdout = (
                "fixture-server: 1 tool(s)\n  fixture_tool  — Fixture description\n"
            )
            self.assertEqual(
                runner.describe(
                    {"server": "fixture-server", "persona": "synthetic-editor"}
                ),
                {"tools": [{"name": "fixture_tool"}]},
            )
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            with self.assertRaises(connectors.ExternalFailure):
                runner.run(["mcp", "call", "fixture-server", "fixture_tool"])
            run.return_value.stdout = (
                "fixture-server: 2 tool(s)\n  fixture_tool  — Fixture description\n"
            )
            with self.assertRaises(connectors.ExternalFailure):
                runner.describe(
                    {"server": "fixture-server", "persona": "synthetic-editor"}
                )

    def test_runner_uses_explicit_persona_and_no_shell(self):
        with (
            patch(
                "phantomblog.connectors.shutil.which",
                return_value="/fixture/phantombot",
            ),
            patch("phantomblog.connectors.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = '{"structuredContent":{"ok":true,"id":"x"}}'
            runner = connectors.Runner()
            c = self.model["connections"]["facebook"]
            runner.call(
                c, c["capabilities"]["share"], {"text": "$(do-not-run) `literal`"}
            )
            argv = run.call_args.args[0]
            self.assertIn("--persona", argv)
            self.assertIn("synthetic-editor", argv)
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("$(do-not-run)", argv[-1])
        with (
            patch(
                "phantomblog.connectors.shutil.which",
                return_value="C:/fixture/phantombot.cmd",
            ),
            patch("phantomblog.connectors.subprocess.run") as run,
        ):
            runner = connectors.Runner()
            self.assertFalse(runner.capture("synthetic-editor", "Untrusted $(text)"))
            run.assert_not_called()
        with (
            patch.dict(
                os.environ,
                {"PHANTOMBLOG_PHANTOMBOT_EXECUTABLE": str(self.root / "missing.exe")},
            ),
            self.assertRaises(core.Invalid),
        ):
            connectors.Runner()
        binary = self.root / "fixture-runtime.exe"
        binary.write_bytes(b"Synthetic path fixture; never execute")
        with patch.dict(os.environ, {"PHANTOMBLOG_PHANTOMBOT_EXECUTABLE": str(binary)}):
            self.assertEqual(connectors.Runner().executable, str(binary))

    def test_lock_release_tolerates_a_removed_file(self):
        with core.lock(self.root):
            core.contained(self.root, ".phantomblog-lock").unlink()
        self.assertFalse(core.contained(self.root, ".phantomblog-lock").exists())


class LiveVerificationTests(unittest.TestCase):
    def test_private_or_unresolved_hosts_are_refused_before_connecting(self):
        for entries in (
            [],
            [(2, 1, 6, "", ("127.0.0.1", 443))],
            [(2, 1, 6, "", ("10.0.0.5", 443))],
        ):
            with (
                self.subTest(entries=entries),
                patch(
                    "phantomblog.connectors.socket.getaddrinfo", return_value=entries
                ),
                patch("phantomblog.connectors.build_opener") as opener,
            ):
                with self.assertRaises(connectors.ExternalFailure):
                    connectors.verify_live("https://example.com/blog-1.html", "0" * 64)
                opener.assert_not_called()

    def test_local_verification_accepts_loopback_only(self):
        with patch(
            "phantomblog.connectors.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            self.assertEqual(
                connectors.verified_addresses("localhost", 80, True), ["127.0.0.1"]
            )
        with (
            patch(
                "phantomblog.connectors.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 80))],
            ),
            self.assertRaises(connectors.ExternalFailure),
        ):
            connectors.verified_addresses("public.example", 80, True)

    def test_live_verification_dials_the_validated_address(self):
        body = b"<html>fixture</html>"

        class Response:
            status = 200

            def __init__(self):
                self.headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size=-1):
                return body

        opener = unittest.mock.Mock()
        opener.open.return_value = Response()
        with (
            patch(
                "phantomblog.connectors.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch("phantomblog.connectors.build_opener", return_value=opener) as build,
        ):
            result = connectors.verify_live(
                "https://example.com/blog-1.html", core.digest(body)
            )
        handler = build.call_args.args[1]
        self.assertIsInstance(handler, connectors.PinnedHTTPSHandler)
        self.assertEqual(handler.address, "93.184.216.34")
        self.assertTrue(result["liveVerified"])

    def test_pinned_connection_dials_the_validated_address(self):
        with patch("phantomblog.connectors.socket.create_connection") as create:
            create.return_value = unittest.mock.Mock()
            connection = connectors.pinned(HTTPConnection, "93.184.216.34", False)(
                "example.com", 443
            )
            connection.connect()
            create.assert_called_once_with(
                ("93.184.216.34", 443), connection.timeout, None
            )

    def test_pinned_https_connection_keeps_the_hostname_for_tls(self):
        with patch("phantomblog.connectors.socket.create_connection") as create:
            create.return_value = unittest.mock.Mock()
            connection = connectors.pinned(HTTPSConnection, "93.184.216.34", True)(
                "example.com"
            )
            connection._context = unittest.mock.Mock()
            connection.connect()
            create.assert_called_once_with(
                ("93.184.216.34", 443), connection.timeout, None
            )
            connection._context.wrap_socket.assert_called_once_with(
                create.return_value, server_hostname="example.com"
            )


if __name__ == "__main__":
    unittest.main()
