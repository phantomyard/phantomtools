import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from coincurve import PrivateKey
from fixtures import adapter, create

from phantomblog import access, core, mcp, server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = create(self.root)
        key = PrivateKey()
        self.httpd = server.make_server(
            self.root, 0, access_identities=[key.public_key_xonly.format().hex()]
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.httpd.server_port
        event = {
            "pubkey": key.public_key_xonly.format().hex(),
            "created_at": int(time.time()),
            "kind": 27235,
            "content": "",
            "tags": [
                ["u", f"http://127.0.0.1:{self.port}/api/access-link"],
                ["method", "POST"],
                ["payload", core.digest(b"{}")],
            ],
        }
        event["id"] = access.event_digest(event)
        event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
        link = access.request_link(event, self.port)
        status, _, headers = self.request(
            "/api/redeem", {"code": link.split("#c=")[1]}, auth=False
        )
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.session_headers = headers

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def request(self, path, body=None, auth=True, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if auth:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        connection.request(
            "POST" if body is not None else "GET",
            path,
            json.dumps(body) if body is not None else None,
            headers,
        )
        response = connection.getresponse()
        data = response.read()
        status = response.status
        headers = dict(response.getheaders())
        connection.close()
        return status, data, headers

    def test_authentication_and_origin_boundary(self):
        self.assertEqual(self.request("/api/state", auth=False)[0], 401)
        self.assertEqual(self.request("/api/state")[0], 200)
        self.assertEqual(
            self.request("/api/state", origin="https://evil.invalid")[0], 403
        )
        self.assertEqual(self.request("/api/save", {}, auth=False)[0], 401)
        self.assertEqual(
            self.request("/api/login", {"token": "wrong"}, auth=False)[0], 401
        )

    def test_login_cookie_is_httponly(self):
        headers = self.session_headers
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_no_source_files_served_and_preview_is_sandboxable(self):
        self.assertEqual(self.request("/blog.json")[0], 404)
        self.assertEqual(self.request("/../blog.json")[0], 404)
        status, data, headers = self.request("/preview?page=blog-fixture-000-es.html")
        self.assertEqual(status, 200)
        self.assertIn(b'<html lang="es">', data)
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_save_and_stale_edit(self):
        model, revision = core.load(self.root)
        model["site"]["name"] = "Saved in dashboard"
        self.assertEqual(
            self.request("/api/save", {"model": model, "revision": revision})[0], 200
        )
        self.assertEqual(
            self.request("/api/save", {"model": model, "revision": revision})[0], 409
        )

    def test_invalid_template_preserves_file(self):
        _model, revision = core.load(self.root)
        path = self.root / "templates/page.html"
        before = path.read_bytes()
        status, _, _ = self.request(
            "/api/template",
            {
                "revision": revision,
                "hash": core.digest(before),
                "text": "<script>oops</script>",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(path.read_bytes(), before)

    def test_image_upload_validates_and_preserves_existing(self):
        source = (self.root / "assets/fixture.png").read_bytes()
        data = {"name": "new-image.png", "base64": base64.b64encode(source).decode()}
        self.assertEqual(self.request("/api/asset", data)[0], 200)
        data["base64"] = base64.b64encode(source + b"changed").decode()
        self.assertEqual(self.request("/api/asset", data)[0], 409)
        self.assertEqual((self.root / "assets/new-image.png").read_bytes(), source)
        data["name"] = "../bad.png"
        self.assertEqual(self.request("/api/asset", data)[0], 422)

    def test_live_verifier_checks_exact_revision(self):
        # Separate synthetic HTML server; no external network or credentials.
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from phantomblog import connectors

        body = b"<html><body>verified fixture</body></html>"

        class PublicHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path == "/redirect":
                    handler.send_response(302)
                    handler.send_header("Location", "/page")
                    handler.end_headers()
                    return
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html")
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(handler, *args):
                pass

        public = HTTPServer(("127.0.0.1", 0), PublicHandler)
        thread = threading.Thread(target=public.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{public.server_port}/page"
            self.assertTrue(
                connectors.verify_live(url, core.digest(body))["liveVerified"]
            )
            with self.assertRaises(connectors.ExternalFailure):
                connectors.verify_live(url, "0" * 64)
            with self.assertRaises(connectors.ExternalFailure):
                connectors.verify_live(
                    url.replace("/page", "/redirect"), core.digest(body)
                )
        finally:
            public.shutdown()
            public.server_close()
            thread.join()

    def test_agent_proposal_review_and_apply(self):
        model, revision = core.load(self.root)
        model["site"]["name"] = "Reviewed agent change"
        mcp.call(
            self.root, "phantomblog_propose", {"model": model, "revision": revision}
        )
        _, data, _ = self.request("/api/proposal")
        proposal = json.loads(data)
        self.assertEqual(proposal["reviewRevision"], revision)
        self.assertEqual(proposal["changes"][0]["field"], "Site / Name")
        self.assertEqual(proposal["changes"][0]["before"], "PhantomBlog test fixture")
        self.assertEqual(
            core.load(self.root)[0]["site"]["name"], "PhantomBlog test fixture"
        )
        self.assertEqual(
            self.request(
                "/api/apply-proposal", {"proposalHash": proposal["proposalHash"]}
            )[0],
            200,
        )
        self.assertEqual(
            core.load(self.root)[0]["site"]["name"], "Reviewed agent change"
        )

    def test_connection_mapping_preview_is_read_only_and_not_account_verification(self):
        before = core.load(self.root)[1]
        status, data, _ = self.request(
            "/api/connection-preview", {"id": "fixture", "connection": adapter()}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["mappingValid"])
        self.assertFalse(json.loads(data)["accountVerified"])
        self.assertEqual(core.load(self.root)[1], before)
        invalid = adapter()
        invalid["capabilities"]["share"]["arguments"]["access_token"] = (
            "synthetic-secret"
        )
        self.assertEqual(
            self.request(
                "/api/connection-preview", {"id": "fixture", "connection": invalid}
            )[0],
            422,
        )
        self.assertEqual(
            self.request(
                "/api/connection-preview",
                {"id": "fixture", "connection": adapter()},
                auth=False,
            )[0],
            401,
        )
        self.assertEqual(core.load(self.root)[1], before)
        invalid = adapter("message", "facebook")
        self.assertEqual(
            self.request(
                "/api/connection-preview", {"id": "fixture", "connection": invalid}
            )[0],
            422,
        )

    def test_external_execution_requires_reviewed_plan(self):
        self.assertEqual(self.request("/api/execute", {"approval": "invented"})[0], 422)

    def test_readonly_commands_do_not_create_output(self):
        self.request("/api/build", {"mode": "dry-run"})
        self.request("/api/build", {"mode": "check"})
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
