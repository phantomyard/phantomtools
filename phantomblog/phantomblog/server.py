"""Loopback dashboard; authenticated API, same-origin checks, no public editor."""

from __future__ import annotations

import re
import secrets
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import access, connectors, core, review


def make_server(
    root,
    port=8787,
    runner=None,
    verifier=connectors.verify_live,
    access_identities=(),
    public_origin=None,
):
    if type(port) is not int or not 0 <= port <= 65535:
        raise core.Invalid("Dashboard port must be between 0 and 65535")
    access.require_verifier()
    root = root.resolve()
    configured_origin = (
        access.public_origin(public_origin, port) if public_origin else None
    )
    links = access.AccessLinks(access_identities)
    sessions = set()
    pending = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log cookies, tokens, requests or provider content.

        def send(self, status, data, mime="application/json", cookie=None):
            if mime == "application/json":
                data = core.encoded(data)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if mime.startswith("text/html") and urlsplit(self.path).path != "/":
                policy = "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            else:
                policy = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            self.send_header("Content-Security-Policy", policy)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def boundary(self):
            loopback = f"http://127.0.0.1:{self.server.server_port}"
            origins = {loopback, configured_origin} - {None}
            # Only explicitly configured origins; never trust forwarded headers.
            for origin in origins:
                if (
                    self.headers.get("Host") == urlsplit(origin).netloc
                    and self.headers.get("Origin", origin) == origin
                    and self.headers.get("Sec-Fetch-Site", "same-origin")
                    in ("same-origin", "none")
                ):
                    self.request_origin = origin
                    return True
            return False

        def authenticated(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                return (
                    "phantomblog" in cookie and cookie["phantomblog"].value in sessions
                )
            except (CookieError, ValueError, TypeError):
                return False

        def create_session(self):
            session = secrets.token_urlsafe(32)
            if len(sessions) >= 100:
                sessions.clear()
            sessions.add(session)
            return session

        def session_response(self, session):
            return self.send(
                200,
                {"ok": True},
                cookie=f"phantomblog={session}; HttpOnly; SameSite=Strict; Secure; Path=/",
            )

        def body(self):
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise core.Invalid("Use application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise core.Invalid("Invalid request length") from exc
            if not 0 < length <= 4_000_000:
                raise core.Invalid("Request must be under 4 MB")
            self.request_body = self.rfile.read(length)
            value = core.decode(self.request_body)
            if not isinstance(value, dict):
                raise core.Invalid("Request must be an object")
            return value

        def do_GET(self):
            if not self.boundary():
                return self.send(403, {"error": "Origin or Host refused"})
            path = unquote(urlsplit(self.path).path)
            if path in ("/", "/dashboard.js", "/dashboard.css"):
                name = {
                    "/": "dashboard.html",
                    "/dashboard.js": "dashboard.js",
                    "/dashboard.css": "dashboard.css",
                }[path]
                mime = (
                    "text/html; charset=utf-8"
                    if path == "/"
                    else (
                        "text/javascript; charset=utf-8"
                        if path.endswith(".js")
                        else "text/css; charset=utf-8"
                    )
                )
                return self.send(200, (core.RESOURCES / name).read_bytes(), mime)
            if not self.authenticated():
                return self.send(401, {"error": "Sign in to manage this workspace"})
            try:
                model, revision = core.load(root)
                if path == "/api/state":
                    return self.send(
                        200,
                        {
                            "model": model,
                            "revision": revision,
                            "providers": connectors.PROVIDERS,
                            "publishing": connectors.execution_state(root),
                        },
                    )
                if path == "/api/proposal":
                    file = core.contained(root, ".phantomblog-proposal.json")
                    proposal = core.decode(file.read_bytes()) if file.exists() else None
                    if proposal and (
                        not isinstance(proposal, dict)
                        or not isinstance(proposal.get("model"), dict)
                    ):
                        raise core.Invalid("Invalid proposal source")
                    return self.send(
                        200,
                        {
                            "proposal": proposal,
                            "proposalHash": core.digest(core.encoded(proposal))
                            if proposal
                            else None,
                            "reviewRevision": revision,
                            "changes": review.compare(model, proposal["model"])
                            if proposal
                            else [],
                        },
                    )
                if path == "/api/template":
                    name = model["theme"].get("templateFile", "page.html")
                    file = core.contained(root / "templates", name, True)
                    return self.send(
                        200,
                        {
                            "text": file.read_text(encoding="utf-8"),
                            "hash": core.digest(file.read_bytes()),
                            "revision": revision,
                        },
                    )
                if path == "/preview":
                    query = parse_qs(urlsplit(self.path).query)
                    name = query.get("page", ["blog.html"])[0]
                    outputs = core.render(root, model, include_drafts=True)
                    if name not in outputs:
                        raise core.Invalid("Preview file not generated")
                    mime = (
                        "text/html; charset=utf-8"
                        if name.endswith(".html")
                        else "text/plain"
                    )
                    return self.send(200, outputs[name], mime)
                # Relative preview assets resolve at the server root. Serve only
                # planned outputs, never a filesystem directory or source JSON.
                name = path.removeprefix("/")
                outputs = core.render(root, model, include_drafts=True)
                if name in outputs and not name.endswith(".html"):
                    import mimetypes

                    return self.send(
                        200,
                        outputs[name],
                        mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                if name in outputs and name.endswith(".html"):
                    return self.send(200, outputs[name], "text/html; charset=utf-8")
                return self.send(404, {"error": "Not found"})
            except (core.Invalid, KeyError, OSError, UnicodeError) as exc:
                return self.send(
                    422,
                    {
                        "error": str(exc)
                        if isinstance(exc, core.Invalid)
                        else "Unable to read workspace"
                    },
                )

        def do_POST(self):
            if not self.boundary():
                return self.send(403, {"error": "Origin or Host refused"})
            try:
                data = self.body()
                path = urlsplit(self.path).path
                if (
                    path in ("/api/access-link", "/api/redeem")
                    and urlsplit(self.path).query
                ):
                    return self.send(
                        400,
                        {"error": "Access credentials must not use query parameters"},
                    )
                if path == "/api/access-link":
                    if data != {} or self.request_body != b"{}":
                        return self.send(
                            400, {"error": "Access requests require an empty object"}
                        )
                    try:
                        link = links.issue(
                            self.headers.get("Authorization", ""),
                            self.request_origin,
                            configured_origin,
                        )
                    except core.Invalid:
                        return self.send(
                            401, {"error": "Signed access request refused"}
                        )
                    return self.send(200, {"link": link})
                if path == "/api/redeem":
                    try:
                        session = links.redeem(data.get("code"), self.create_session)
                    except core.Invalid:
                        return self.send(
                            401,
                            {
                                "error": "Access link is invalid, expired or already used"
                            },
                        )
                    return self.session_response(session)
                if not self.authenticated():
                    return self.send(401, {"error": "Sign in first"})
                if path == "/api/logout":
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    if "phantomblog" in cookie:
                        sessions.discard(cookie["phantomblog"].value)
                    return self.send(
                        200,
                        {"ok": True},
                        cookie="phantomblog=; HttpOnly; SameSite=Strict; Secure; Max-Age=0; Path=/",
                    )
                if path == "/api/save":
                    revision = core.save(root, data["model"], data["revision"])
                    return self.send(200, {"revision": revision})
                if path == "/api/asset":
                    import base64

                    name = data["name"]
                    if not isinstance(name, str) or not re.fullmatch(
                        r"[a-zA-Z0-9_-]+\.(?:png|jpe?g|webp|gif)", name, re.IGNORECASE
                    ):
                        raise core.Invalid(
                            "Upload a PNG, JPEG, WebP or GIF with a simple filename"
                        )
                    try:
                        content = base64.b64decode(data["base64"], validate=True)
                    except (ValueError, TypeError) as exc:
                        raise core.Invalid("Invalid image upload") from exc
                    suffix = name.rsplit(".", 1)[1].lower()
                    valid = (
                        (suffix == "png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                        or (
                            suffix in ("jpg", "jpeg")
                            and content.startswith(b"\xff\xd8\xff")
                        )
                        or (
                            suffix == "webp"
                            and content[:4] == b"RIFF"
                            and content[8:12] == b"WEBP"
                        )
                        or (suffix == "gif" and content[:6] in (b"GIF87a", b"GIF89a"))
                    )
                    if not content or len(content) > 2_500_000 or not valid:
                        raise core.Invalid(
                            "Image type mismatch or upload exceeds 2.5 MB"
                        )
                    core.contained(root, "assets")
                    target = core.contained(root / "assets", name)
                    with core.lock(root):
                        if target.exists() and target.read_bytes() != content:
                            raise core.Conflict(
                                "That image name already exists. Choose another filename."
                            )
                        if not target.exists():
                            core.atomic(target, content)
                    return self.send(200, {"name": name})
                if path == "/api/template":
                    with core.lock(root):
                        model, revision = core.load(root)
                        file = core.contained(
                            root / "templates",
                            model["theme"].get("templateFile", "page.html"),
                            True,
                        )
                        if (
                            revision != data["revision"]
                            or core.digest(file.read_bytes()) != data["hash"]
                        ):
                            raise core.Conflict(
                                "Template changed; reload before saving"
                            )
                        text = data["text"]
                        if not isinstance(text, str) or len(text) > 100_000:
                            raise core.Invalid("Template too large")
                        # Validate fully using a temporary source override rather
                        # than write a potentially invalid template into the project.
                        core.validate_template(text)
                        core.atomic(file, text.encode("utf-8"))
                    return self.send(200, {"hash": core.digest(text.encode())})
                if path == "/api/build":
                    return self.send(200, core.build(root, data.get("mode", "build")))
                if path == "/api/connections":
                    return self.send(
                        200, {"connections": connectors.connection_status(root, runner)}
                    )
                if path == "/api/connection-preview":
                    connectors.validate_connections({data["id"]: data["connection"]})
                    connection = data["connection"]
                    return self.send(
                        200,
                        {
                            "mappingValid": True,
                            "accountVerified": False,
                            "provider": connection["provider"],
                            "server": connection["server"],
                            "persona": connection["persona"],
                            "capabilities": sorted(connection["capabilities"]),
                        },
                    )
                if path == "/api/prepare":
                    plan = connectors.prepare(
                        root,
                        data["slug"],
                        data["language"],
                        data.get("deploy"),
                        data.get("shares"),
                    )
                    pending[plan["approval"]] = plan
                    return self.send(200, plan)
                if path == "/api/execute":
                    approval = data["approval"]
                    plan = pending.pop(approval, None)
                    if not plan:
                        raise core.Invalid(
                            "Review the publishing plan before executing"
                        )
                    return self.send(
                        200, connectors.execute(root, plan, approval, runner, verifier)
                    )
                if path == "/api/message":
                    return self.send(
                        200,
                        connectors.message(
                            root,
                            data["connection"],
                            data["request"],
                            data["slug"],
                            data["revision"],
                            runner,
                        ),
                    )
                if path == "/api/reconcile":
                    if data.get("checkedProviderHistory") is not True:
                        raise core.Invalid(
                            "Check the provider's history before reconciling"
                        )
                    return self.send(
                        200,
                        connectors.reconcile(
                            root,
                            data["operation"],
                            data.get("receipt"),
                            data.get("notSent", False),
                        ),
                    )
                if path == "/api/apply-proposal":
                    proposal = core.decode(
                        core.contained(
                            root, ".phantomblog-proposal.json", True
                        ).read_bytes()
                    )
                    if data["proposalHash"] != core.digest(core.encoded(proposal)):
                        raise core.Conflict("Agent proposal changed; review it again")
                    revision = core.save(root, proposal["model"], proposal["revision"])
                    return self.send(200, {"revision": revision})
                return self.send(404, {"error": "Not found"})
            except core.Conflict as exc:
                return self.send(409, {"error": str(exc)})
            except connectors.ExternalFailure as exc:
                return self.send(502, {"error": str(exc)})
            except (core.Invalid, KeyError, TypeError, OSError, UnicodeError) as exc:
                return self.send(
                    422,
                    {
                        "error": str(exc)
                        if isinstance(exc, core.Invalid)
                        else "Invalid request or unavailable workspace"
                    },
                )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
