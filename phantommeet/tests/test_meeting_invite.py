"""Behavioural tests for the meeting-invite tool's recorder preflight (v0).

The subject under test is the REAL rendered bash tool
(``templates/tools/meeting-invite.sh.j2`` processed by ``render_tool_content``),
run as a subprocess against:

- a fake bridge ``/status`` server (``http.server`` on a background thread)
  that answers a configurable ``recorder`` verdict and requires the admin
  bearer token, like the real bridge; and
- a fake ``phantombot`` binary on ``PATH`` that records its argv, so we can
  assert exactly what was (and was not) notified.

This covers the v0 rule: refuse to invite while the recorder is not ready,
notify the responsible persona with the reason, and let ``--force`` convene
anyway.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from phantommeet.apply import render_tool_content

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "example-org.yaml"
PERSONA = "maria"  # in invite.roles of the example manifest
TOKEN = "test-admin-token"


def _render_script(bridge_url: str, tmp_path: Path) -> Path:
    """Render the invite tool for PERSONA pointing at ``bridge_url``."""
    manifest = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    manifest.setdefault("invite", {})["bridge_status_url"] = bridge_url
    content = render_tool_content(
        manifest["invite"]["tool"], PERSONA, manifest, manifest["language"]
    )
    assert content is not None
    script = tmp_path / "meeting-invite.sh"
    script.write_text(content, encoding="utf-8")
    script.chmod(0o755)
    return script


class _BridgeHandler(http.server.BaseHTTPRequestHandler):
    """Answers GET /status with a fixed body; requires the admin bearer token."""

    def do_GET(self) -> None:
        """Answer the health/status GET (http.server handler)."""
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"admin token required"}')
            return
        body = self.server.body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@contextmanager
def fake_bridge(recorder: dict | None) -> Iterator[str]:
    """Start a fake bridge /status server and yield its URL."""
    body = json.dumps({"ok": True, "recorder": recorder}).encode("utf-8")
    server = http.server.HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    server.body = body  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/status"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _fake_phantombot(tmp_path: Path) -> tuple[Path, Path]:
    """Write a fake ``phantombot`` that logs its argv; return (bindir, log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "phantombot.log"
    stub = bindir / "phantombot"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "msg=\"${*//$'\\n'/ }\"\n"
        'printf \'%s\\n\' "$msg" >> "$FAKE_PHANTOMBOT_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bindir, log


def _run(
    script: Path,
    bindir: Path,
    log: Path,
    *,
    token: str | None = TOKEN,
    force: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    args = [
        "--title",
        "Junta directiva",
        "--datetime",
        "2026-08-10T17:00:00",
        "--recipients",
        "@pepa,@paco",
    ]
    if force:
        args.append("--force")
    if dry_run:
        args.append("--dry-run")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    env["FAKE_PHANTOMBOT_LOG"] = str(log)
    if token is None:
        env.pop("PHANTOMBRIDGE_ADMIN_TOKEN", None)
    else:
        env["PHANTOMBRIDGE_ADMIN_TOKEN"] = token
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _log_lines(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_ready_sends_the_invitation(tmp_path: Path) -> None:
    """Recorder ready -> the invitation is broadcast, exit 0."""
    with fake_bridge({"ready": True, "reason": "recorder healthy and idle"}) as url:
        script = _render_script(url, tmp_path)
        bindir, log = _fake_phantombot(tmp_path)
        proc = _run(script, bindir, log)
    assert proc.returncode == 0, proc.stderr
    lines = _log_lines(log)
    assert len(lines) == 1 and lines[0].startswith("notify")
    assert "reunión" in lines[0].lower()
    assert "recorder   : ready" in proc.stdout
    assert "NOT sent" not in proc.stdout


def test_not_ready_withholds_the_invitation_and_notifies(tmp_path: Path) -> None:
    """Recorder not ready -> no invitation; the responsible persona is notified."""
    with fake_bridge(
        {"ready": False, "reason": "recorder not healthy (UNHEALTHY)"}
    ) as url:
        script = _render_script(url, tmp_path)
        bindir, log = _fake_phantombot(tmp_path)
        proc = _run(script, bindir, log)
    assert proc.returncode != 0
    lines = _log_lines(log)
    assert len(lines) == 1 and lines[0].startswith("notify")
    assert "grabadora no está disponible" in lines[0]
    assert "UNHEALTHY" in lines[0]
    assert "invitation NOT sent" in proc.stdout


def test_force_convenes_anyway_without_recording(tmp_path: Path) -> None:
    """--force overrides the gate: the invitation is sent, with a warning."""
    with fake_bridge(
        {"ready": False, "reason": "recorder busy (a recording is in progress)"}
    ) as url:
        script = _render_script(url, tmp_path)
        bindir, log = _fake_phantombot(tmp_path)
        proc = _run(script, bindir, log, force=True)
    assert proc.returncode == 0, proc.stderr
    lines = _log_lines(log)
    assert len(lines) == 1 and lines[0].startswith("notify")
    # The invitation (not the refusal) was sent; the warning names --force.
    assert "no está disponible" not in lines[0]
    assert "--force" in proc.stderr


def test_missing_token_refuses(tmp_path: Path) -> None:
    """No admin token in the environment -> refuse (never invite silently)."""
    with fake_bridge({"ready": True, "reason": "recorder healthy and idle"}) as url:
        script = _render_script(url, tmp_path)
        bindir, log = _fake_phantombot(tmp_path)
        proc = _run(script, bindir, log, token=None)
    assert proc.returncode != 0
    lines = _log_lines(log)
    assert len(lines) == 1 and "PHANTOMBRIDGE_ADMIN_TOKEN" in lines[0]
    assert "invitation NOT sent" in proc.stdout


def test_unreachable_bridge_refuses(tmp_path: Path) -> None:
    """An unreachable bridge -> refuse (we cannot confirm the recorder)."""
    script = _render_script("http://127.0.0.1:1/status", tmp_path)
    bindir, log = _fake_phantombot(tmp_path)
    proc = _run(script, bindir, log)
    assert proc.returncode != 0
    lines = _log_lines(log)
    assert len(lines) == 1 and "unreachable" in lines[0]
    assert "invitation NOT sent" in proc.stdout


def test_dry_run_previews_without_notifying(tmp_path: Path) -> None:
    """--dry-run is a pure preview: it prints the invitation and never calls
    phantombot, and it skips the recorder check entirely."""
    script = _render_script("http://127.0.0.1:1/status", tmp_path)
    bindir, log = _fake_phantombot(tmp_path)
    proc = _run(script, bindir, log, dry_run=True)
    assert proc.returncode == 0, proc.stderr
    assert _log_lines(log) == []
    assert "Reunión: Junta directiva" in proc.stdout
    assert "not checked (--dry-run)" in proc.stdout
