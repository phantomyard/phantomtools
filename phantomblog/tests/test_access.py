"""Signed identity and single-use access tests; all keys are ephemeral fixtures."""

import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from coincurve import PrivateKey
from fixtures import create

from phantomblog import access, cli, core, server


@pytest.fixture
def dashboard(tmp_path):
    create(tmp_path)
    key = PrivateKey()
    identity = key.public_key_xonly.format().hex()
    now = [1000.0, 1700000000]
    links = access.AccessLinks([identity], lambda: now[0], lambda: now[1])
    reserved = None  # Reserved fixture slot; no dashboard credential exists.
    with patch.object(server.access, "AccessLinks", return_value=links):
        httpd = server.make_server(tmp_path, 0, access_identities=[identity])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{httpd.server_port}"

    def signed(**changes):
        event = {
            "pubkey": identity,
            "created_at": now[1],
            "kind": 27235,
            "tags": [
                ["u", origin + "/api/access-link"],
                ["method", "POST"],
                ["payload", hashlib.sha256(b"{}").hexdigest()],
                [
                    "nonce",
                    base64.b64encode(
                        key.sign_schnorr(hashlib.sha256(str(now).encode()).digest())
                    ).decode(),
                ],
            ],
            "content": "",
        }
        event.update(changes)
        event["id"] = access.event_digest(event)
        event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
        return event

    def request(path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_port, timeout=5
        )
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        connection.request(
            "POST" if body is not None else "GET", path, payload, request_headers
        )
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return result

    def issue(event=None, headers=None):
        event = signed() if event is None else event
        authorization = "Nostr " + base64.b64encode(core.encoded(event)).decode()
        return request(
            "/api/access-link", {}, {"Authorization": authorization, **(headers or {})}
        )

    def code():
        status, body, _ = issue()
        assert status == 200
        url = json.loads(body)["link"]
        parsed = urlsplit(url)
        assert parsed.path == "/" and not parsed.query
        return parse_qs(parsed.fragment)["c"][0], url

    yield links, now, signed, request, issue, code, reserved, httpd, tmp_path
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def test_valid_link_authenticates_with_existing_session(dashboard):
    _, _, _, request, _, code, _, _, _ = dashboard
    value, url = code()
    assert request(urlsplit(url).path)[0] == 200
    assert request("/api/state")[0] == 401
    status, body, headers = request("/api/redeem", {"code": value})
    assert status == 200 and json.loads(body) == {"ok": True}
    cookie = headers["Set-Cookie"]
    for attribute in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
        assert attribute in cookie
    assert value not in cookie
    session = cookie.split(";")[0]
    assert request("/api/state", headers={"Cookie": session})[0] == 200
    assert request("/api/logout", {}, {"Cookie": session})[0] == 200
    assert request("/api/state", headers={"Cookie": session})[0] == 401


def test_expired_link(dashboard):
    _, now, _, request, _, code, _, _, _ = dashboard
    value, _ = code()
    assert access.LIFETIME == 300
    now[0] += 300
    assert request("/api/redeem", {"code": value})[0] == 401


def test_link_is_valid_until_five_minute_boundary(dashboard):
    _, now, _, request, _, code, _, _, _ = dashboard
    value, _ = code()
    now[0] += 299
    assert request("/api/redeem", {"code": value})[0] == 200


def test_empty_allowlist_server_cannot_create_sessions(tmp_path):
    create(tmp_path)
    httpd = server.make_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path, body in (
            ("/api/access-link", {}),
            ("/api/redeem", {"code": "a" * 43}),
            ("/api/login", {"token": os.urandom(32).hex()}),
        ):
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=5
            )
            try:
                connection.request(
                    "POST",
                    path,
                    json.dumps(body, separators=(",", ":")),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                assert response.status == 401
                assert response.getheader("Set-Cookie") is None
                response.read()
            finally:
                connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_used_and_invalid_links(dashboard):
    _, _, _, request, _, code, _, _, _ = dashboard
    value, _ = code()
    assert request("/api/redeem", {"code": value})[0] == 200
    assert request("/api/redeem", {"code": value})[0] == 401
    for invalid in ("invented", "a" * 43, None, 123, {}):
        assert request("/api/redeem", {"code": invalid})[0] == 401


def test_foreign_origin_and_host_cannot_issue_or_consume(dashboard):
    _, _, signed, request, issue, code, _, _, _ = dashboard
    event = signed()
    assert issue(event, {"Origin": "https://foreign.invalid"})[0] == 403
    assert issue(event, {"Host": "foreign.invalid"})[0] == 403
    assert issue(event)[0] == 200
    value, _ = code()
    assert (
        request("/api/redeem", {"code": value}, {"Origin": "https://foreign.invalid"})[
            0
        ]
        == 403
    )
    assert (
        request("/api/redeem", {"code": value}, {"Sec-Fetch-Site": "cross-site"})[0]
        == 403
    )
    assert request("/api/redeem", {"code": value})[0] == 200


def test_signature_allowlist_freshness_binding_and_replay(dashboard):
    _, now, signed, _, issue, _, _, _, _ = dashboard
    event = signed()
    assert issue(event)[0] == 200
    assert issue(event)[0] == 401
    for changes in (
        {"pubkey": PrivateKey().public_key_xonly.format().hex()},
        {"created_at": now[1] - 61},
        {"created_at": now[1] + 61},
        {"content": "request access"},
        {"kind": 1},
        {
            "tags": [
                ["u", "https://foreign.invalid/api/access-link"],
                ["method", "POST"],
            ]
        },
    ):
        assert issue(signed(**changes))[0] == 401
    bad = signed()
    bad["sig"] = "0" * 128
    assert issue(bad)[0] == 401
    bad = signed()
    bad["id"] = "0" * 64
    assert issue(bad)[0] == 401
    assert (
        issue(headers={"Authorization": "Bearer " + PrivateKey().secret.hex()})[0]
        == 401
    )


def test_credentials_never_in_query_response_or_logs(dashboard, capsys):
    _, _, _, request, _, code, _, _, _ = dashboard
    value, url = code()
    assert "#c=" in url and "?c=" not in url
    status, body, _ = request("/api/redeem?c=" + value, {"code": value})
    assert status == 400 and value.encode() not in body
    assert request("/api/redeem", {"code": value})[0] == 200
    assert request("/api/access-link?c=" + value, {})[0] == 400
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert value.encode() not in request("/dashboard.js")[1]


def test_only_first_concurrent_redeemer_succeeds(dashboard):
    _, _, _, request, _, code, _, _, _ = dashboard
    value, _ = code()
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(
            executor.map(lambda _: request("/api/redeem", {"code": value})[0], range(8))
        )
    assert statuses.count(200) == 1
    assert statuses.count(401) == 7


def test_failed_session_creation_does_not_consume_code(dashboard):
    links, _, _, _, _, code, _, _, _ = dashboard
    value, _ = code()

    def fail():
        raise RuntimeError("Synthetic session failure")

    with pytest.raises(RuntimeError):
        links.redeem(value, fail)
    assert links.redeem(value, lambda: "synthetic-session") == "synthetic-session"


def test_cli_outputs_one_line_without_admin_token(dashboard, capsys, tmp_path):
    _, _, signed, _, _, _, _, httpd, _ = dashboard
    event = tmp_path / "signed-event.json"
    event.write_bytes(core.encoded(signed()))
    assert (
        cli.main(
            [
                "access-link",
                "--port",
                str(httpd.server_port),
                "--signed-event",
                str(event),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert output.startswith(f"http://127.0.0.1:{httpd.server_port}/#c=")


def test_no_allowlist_fails_closed_and_restart_invalidates(dashboard):
    _, _, signed, _, _, code, _, _, _ = dashboard
    empty = access.AccessLinks()
    with pytest.raises(core.Invalid):
        empty.issue(
            "Nostr " + base64.b64encode(core.encoded(signed())).decode(),
            "http://127.0.0.1:8787",
        )
    value, _ = code()
    with pytest.raises(core.Invalid):
        empty.redeem(value, lambda: "session")
    with pytest.raises(core.Invalid):
        access.AccessLinks(["not-a-public-key"])


def test_frontend_cleans_fragment_before_fetch_and_never_persists():
    text = (core.RESOURCES / "dashboard.js").read_text(encoding="utf-8")
    assert text.index("history.replaceState") < text.index("fetch(")
    assert "api('redeem', {code})" in text
    assert "localStorage" not in text and "sessionStorage" not in text
    assert "accessCode = null;" in text


def test_dashboard_fragment_bootstrap_executes():
    node = os.environ.get("PHANTOMBLOG_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the dashboard JavaScript regression")
    subprocess.run(
        [
            node,
            str(Path(__file__).with_name("dashboard_access.cjs")),
            str(core.RESOURCES / "dashboard.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_static_credentials_cannot_authenticate(dashboard):
    _, _, _, request, _, _, _, _, _ = dashboard
    credential = PrivateKey().secret.hex()
    assert request("/api/login", {"token": credential})[0] == 401
    assert (
        request("/api/state", headers={"Authorization": "Bearer " + credential})[0]
        == 401
    )
    with pytest.raises(SystemExit):
        cli.main(["dashboard", "--token-ref", "env:REMOVED"])
    assert not hasattr(server, "token_from_reference")


def test_signed_payload_must_match_exact_request_bytes(dashboard):
    _, _, signed, _, issue, _, _, httpd, _ = dashboard
    event = signed()
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            "/api/access-link",
            b"{ }",
            {
                "Content-Type": "application/json",
                "Authorization": "Nostr "
                + base64.b64encode(core.encoded(event)).decode(),
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 400
    finally:
        connection.close()
    assert issue(event)[0] == 200


def test_malformed_authorization_and_missing_allowlist_dependency(dashboard):
    _, _, _, _, issue, _, _, _, _ = dashboard
    for header in ("Nostr !!!", "Bearer fixture", "Nostr " + "a" * 8192, "Nostr e30="):
        assert issue(headers={"Authorization": header})[0] == 401
    with (
        patch.dict("sys.modules", {"coincurve": None}),
        pytest.raises(core.Invalid, match="Install phantomblog"),
    ):
        access.AccessLinks(["a" * 64])


@pytest.mark.parametrize("identities", [(), ("a" * 64,)])
def test_dashboard_missing_verifier_fails_before_binding(tmp_path, capsys, identities):
    with (
        patch.dict("sys.modules", {"coincurve": None}),
        patch("phantomblog.server.ThreadingHTTPServer") as listener,
    ):
        with pytest.raises(
            core.Invalid, match=r"python3 -m pip install.*phantomblog\[access\]"
        ):
            server.make_server(tmp_path, 0, access_identities=identities)
        listener.assert_not_called()
        arguments = ["--root", str(tmp_path), "dashboard"]
        for identity in identities:
            arguments.extend(["--allow-identity", identity])
        assert cli.main(arguments) == 2
        assert "python3 -m pip install" in capsys.readouterr().err
        listener.assert_not_called()


def test_empty_allowlist_cli_reports_how_to_enable_access(tmp_path, capsys):
    with patch("phantomblog.cli.server.make_server") as make:
        make.return_value.server_port = 8787
        assert cli.main(["--root", str(tmp_path), "dashboard"]) == 0
        message = capsys.readouterr().err
        assert "access is disabled" in message
        assert "--allow-identity <64-hex-pubkey>" in message
        assert make.call_args.kwargs["access_identities"] == []
        make.return_value.server_close.assert_called_once()


def encode_nsec(secret):
    """Synthetic NIP-19 fixture encoder, independent of production decoder."""
    alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    number = int.from_bytes(secret, "big") << 4
    digits = [(number >> (5 * i)) & 31 for i in reversed(range(52))]
    expanded = [ord(c) >> 5 for c in "nsec"] + [0] + [ord(c) & 31 for c in "nsec"]
    checksum = 1
    for digit in expanded + digits + [0] * 6:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ digit
        for bit, generator in enumerate(
            (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
        ):
            if (top >> bit) & 1:
                checksum ^= generator
    checksum ^= 1
    return "nsec1" + "".join(
        alphabet[d]
        for d in digits + [(checksum >> (5 * i)) & 31 for i in reversed(range(6))]
    )


@pytest.mark.parametrize("encoding", ["hex", "nsec"])
def test_persona_cli_signs_and_preserves_disposable_store(tmp_path, capsys, encoding):
    key = PrivateKey()
    store = tmp_path / "private-personas"
    persona = "fixture-" + os.urandom(8).hex()
    identity = store / persona / "identity.json"
    identity.parent.mkdir(parents=True)
    secret = key.secret.hex() if encoding == "hex" else encode_nsec(key.secret)
    identity.write_bytes(core.encoded({"nsec": secret}))
    workspace = tmp_path / "publication"
    workspace.mkdir()
    create(workspace)
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    httpd = server.make_server(
        workspace, 0, access_identities=[key.public_key_xonly.format().hex()]
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(2):  # Fresh nonce permits two requests within the same second.
            assert (
                cli.main(
                    [
                        "access-link",
                        "--persona",
                        persona,
                        "--persona-dir",
                        str(store),
                        "--port",
                        str(httpd.server_port),
                    ]
                )
                == 0
            )
            output = capsys.readouterr()
            link = output.out.strip()
            assert len(output.out.splitlines()) == 1 and not output.err
            assert secret not in output.out and key.secret.hex() not in output.out
            assert re.fullmatch(
                r"/#c=[A-Za-z0-9_-]{43}",
                urlsplit(link).path + "#" + urlsplit(link).fragment,
            )
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=5
            )
            try:
                connection.request(
                    "POST",
                    "/api/redeem",
                    core.encoded({"code": link.split("#c=")[1]}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                assert response.status == 200
                response.read()
                assert "Secure" in response.getheader("Set-Cookie")
            finally:
                connection.close()
        assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before
        assert not (workspace / "public").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_persona_errors_are_private_and_dependency_is_optional(tmp_path, capsys):
    key = PrivateKey()
    path = tmp_path / "fixture" / "identity.json"
    path.parent.mkdir()
    path.write_bytes(core.encoded({"nsec": key.secret.hex() + "invalid"}))
    assert (
        cli.main(
            ["access-link", "--persona", "fixture", "--persona-dir", str(tmp_path)]
        )
        == 2
    )
    output = capsys.readouterr()
    assert key.secret.hex() not in output.err and not output.out
    for persona in ("../fixture", "", "other"):
        with pytest.raises(core.Invalid):
            access.persona_event(persona, tmp_path, 8787)
    with patch.dict(os.environ, {"PHANTOMBLOG_PERSONA_DIR": str(tmp_path)}):
        path.write_bytes(core.encoded({"nsec": key.secret.hex()}))
        assert (
            access.persona_event("fixture", None, 8787)["pubkey"]
            == key.public_key_xonly.format().hex()
        )
    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(core.Invalid, match="persona-dir"),
    ):
        access.persona_event("fixture", None, 8787)
    with patch.dict("sys.modules", {"coincurve": None}):
        access.AccessLinks()  # Disabled issuance needs no cryptographic dependency.
        with pytest.raises(core.Invalid, match="phantomblog\\[access\\]"):
            access.persona_event("fixture", tmp_path, 8787)


def test_nsec_checksum_padding_and_invalid_scalars(tmp_path):
    key = PrivateKey()
    encoded = encode_nsec(key.secret)
    assert access._secret_bytes(encoded) == key.secret
    for value in (
        encoded[:-1] + ("q" if encoded[-1] != "q" else "p"),
        encoded.upper(),
        "npub" + encoded[4:],
        encoded + "q",
        None,
    ):
        with pytest.raises((ValueError, TypeError)):
            access._secret_bytes(value)
    path = tmp_path / "fixture" / "identity.json"
    path.parent.mkdir()
    path.write_bytes(core.encoded({"nsec": "0" * 64}))
    with pytest.raises(core.Invalid, match="invalid"):
        access.persona_event("fixture", tmp_path, 8787)


def test_configurable_origin_delivery_and_exact_browser_boundary(tmp_path, capsys):
    create(tmp_path)
    key = PrivateKey()
    origin = "https://" + os.urandom(8).hex() + ".invalid:9443"
    httpd = server.make_server(
        tmp_path,
        0,
        access_identities=[key.public_key_xonly.format().hex()],
        public_origin=origin,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        store = tmp_path / "personas"
        path = store / "fixture" / "identity.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(core.encoded({"nsec": key.secret.hex()}))
        event = access.persona_event("fixture", store, httpd.server_port)
        link = access.request_link(event, httpd.server_port, origin)
        assert link.startswith(origin + "/#c=") and not urlsplit(link).query
        assert (
            cli.main(
                [
                    "access-link",
                    "--persona",
                    "fixture",
                    "--persona-dir",
                    str(store),
                    "--port",
                    str(httpd.server_port),
                    "--public-origin",
                    origin,
                ]
            )
            == 0
        )
        output = capsys.readouterr()
        assert len(output.out.splitlines()) == 1 and output.out.startswith(
            origin + "/#c="
        )
        assert key.secret.hex() not in output.out + output.err
        assert httpd.server_address[0] == "127.0.0.1"
        code = link.split("#c=")[1]
        for browser_origin, status in (("https://foreign.invalid", 403), (origin, 200)):
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=5
            )
            try:
                connection.request(
                    "POST",
                    "/api/redeem",
                    core.encoded({"code": code}),
                    {
                        "Host": urlsplit(origin).netloc,
                        "Origin": browser_origin,
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                assert response.status == status
                response.read()
            finally:
                connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@fixture.invalid",
        "https://fixture.invalid/path",
        "https://fixture.invalid?c=x",
        "https://fixture.invalid#c=x",
        "http://fixture.invalid",
        "https://fixture.invalid:0",
        "https://fixture.invalid\n",
        "https://fixture.invalid\\evil",
    ],
)
def test_unsafe_delivery_origin_refused(value):
    with pytest.raises(core.Invalid):
        access.public_origin(value, 8787)


def test_no_instance_keys_in_tracked_component_sources():
    root = Path(__file__).resolve().parents[1]
    files = [
        *root.glob("*.md"),
        *root.glob("docs/*.md"),
        *root.glob("phantomblog/*.py"),
        *root.glob("tests/*.py"),
        *root.glob("phantomblog/resources/*"),
        root / "pyproject.toml",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(?:nsec|npub)1[a-z0-9]{58}\b", text), path
        assert not re.search(r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])", text), (
            path
        )
