"""End-to-end tests for the recorder guardian (``tools/recorder-guardian.sh``).

The subject under test is the REAL bash script, run as a subprocess against a
local fake health server (``http.server`` on a background thread). The server
silences the default request log (``log_message``) so a chatty access log can
never hang or pollute the test runner.

Every test controls ``PATH``:
- a ``systemctl`` stub that prints nothing (the unit is unknown, so the grace
  brake does not apply and the guardian may act), or
- a ``systemctl`` stub that prints the current time in systemd's
  ``ExecMainStartTimestamp`` format (a fresh start -> in the grace window).

``GUARDIAN_RECHECK`` is forced to 0 so the two in-run readings are immediate
and the suite stays fast.
"""

from __future__ import annotations

import http.server
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "recorder-guardian.sh"

HEALTHY = (
    '{"status":{"busyStatus":"IDLE","health":{"healthStatus":"HEALTHY","details":{}}}}'
)
UNHEALTHY = (
    '{"status":{"busyStatus":"IDLE",'
    '"health":{"healthStatus":"UNHEALTHY","details":{}}}}'
)
BUSY_UNHEALTHY = (
    '{"status":{"busyStatus":"BUSY",'
    '"health":{"healthStatus":"UNHEALTHY","details":{}}}}'
)
NO_HEALTH_KEY = '{"status":{"busyStatus":"IDLE"}}'


def _make_handler(payload: str) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a request handler that always answers ``payload`` (silent log)."""
    body = payload.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            # Silence the default stderr access log.
            return

    return Handler


@contextmanager
def fake_health_server(payload: str) -> Iterator[str]:
    """Start a throwaway HTTP server on 127.0.0.1 and yield its health URL."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(payload))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/jibri/api/v1.0/health"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _systemctl_stub(bindir: Path, *, recent: bool) -> Path:
    """Create a ``systemctl`` stub on ``bindir`` and return the directory.

    ``recent=False`` prints nothing (the unit is unknown -> not in the grace
    window), so the guardian proceeds. ``recent=True`` prints the current time
    in systemd's ``ExecMainStartTimestamp`` format (a fresh start -> in grace).
    """
    bindir.mkdir(parents=True, exist_ok=True)
    if recent:
        body = "#!/usr/bin/env bash\nexec date '+%a %Y-%m-%d %H:%M:%S %Z'\n"
    else:
        body = "#!/usr/bin/env bash\nexit 0\n"
    stub = bindir / "systemctl"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return bindir


def _run(
    url: str,
    actions_file: Path,
    *,
    systemctl_dir: Path | None = None,
    path_only: str | None = None,
    observe: bool = False,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GUARDIAN_HEALTH_URL": url,
        "GUARDIAN_RECHECK": "0",  # keep the suite fast
        "GUARDIAN_GRACE": "180",
        "GUARDIAN_ACTION": f"echo act >> {shlex.quote(str(actions_file))}",
    }
    if observe:
        env["GUARDIAN_OBSERVE"] = "1"
    if path_only is not None:
        env["PATH"] = path_only
    elif systemctl_dir is not None:
        env["PATH"] = f"{systemctl_dir}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def _actions(actions_file: Path) -> int:
    if not actions_file.exists():
        return 0
    return len(actions_file.read_text(encoding="utf-8").splitlines())


def test_healthy_is_silent_and_does_not_act(tmp_path: Path) -> None:
    """A healthy recorder -> exit 0, no output at all, no action."""
    actions = tmp_path / "actions.log"
    with fake_health_server(HEALTHY) as url:
        proc = _run(url, actions)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert _actions(actions) == 0


def test_unhealthy_twice_acts_exactly_once(tmp_path: Path) -> None:
    """Two consecutive bad readings -> the action runs exactly once, exit 0."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=False)
    with fake_health_server(UNHEALTHY) as url:
        proc = _run(url, actions, systemctl_dir=stub)
    assert proc.returncode == 0
    assert _actions(actions) == 1
    assert "restarting the recorder" in proc.stderr


def test_busy_never_acts(tmp_path: Path) -> None:
    """A recording in progress is never interrupted, whatever the health says."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=False)
    with fake_health_server(BUSY_UNHEALTHY) as url:
        proc = _run(url, actions, systemctl_dir=stub)
    assert proc.returncode == 0
    assert _actions(actions) == 0
    # BUSY is a normal state (a recording is running): the guardian stays silent.
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_no_answer_acts(tmp_path: Path) -> None:
    """The health endpoint does not answer -> treated as bad, action runs."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=False)
    # Port 1 is a closed endpoint: the health read gets no answer.
    proc = _run(
        "http://127.0.0.1:1/jibri/api/v1.0/health",
        actions,
        systemctl_dir=stub,
    )
    assert proc.returncode == 0
    assert _actions(actions) == 1


def test_unknown_shape_does_not_act(tmp_path: Path) -> None:
    """A parseable ``status`` object without a health key is OK, not a restart:
    a payload-shape change must never cause a restart loop."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=False)
    with fake_health_server(NO_HEALTH_KEY) as url:
        proc = _run(url, actions, systemctl_dir=stub)
    assert proc.returncode == 0
    assert _actions(actions) == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_recently_started_does_not_act(tmp_path: Path) -> None:
    """An unhealthy recorder whose main process just started is left alone
    (the loop brake), even with two bad readings."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=True)
    with fake_health_server(UNHEALTHY) as url:
        proc = _run(url, actions, systemctl_dir=stub)
    assert proc.returncode == 0
    assert _actions(actions) == 0
    # The log line proves the brake fired on the fresh start time.
    assert "holding off" in proc.stderr


def test_observe_mode_never_executes_the_action(tmp_path: Path) -> None:
    """GUARDIAN_OBSERVE=1 logs the intent but never runs the action."""
    actions = tmp_path / "actions.log"
    stub = _systemctl_stub(tmp_path / "bin", recent=False)
    with fake_health_server(UNHEALTHY) as url:
        proc = _run(url, actions, systemctl_dir=stub, observe=True)
    assert proc.returncode == 0
    assert _actions(actions) == 0
    assert "observe/log-only" in proc.stderr


def test_missing_python3_exits_nonzero_and_does_not_act(tmp_path: Path) -> None:
    """Without python3 the guardian cannot classify at all: it exits non-zero
    and does not touch the recorder."""
    actions = tmp_path / "actions.log"
    # A PATH holding only bash: python3 is absent.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(shutil.which("bash") or "/bin/bash", bindir / "bash")
    proc = _run(
        "http://127.0.0.1:1/jibri/api/v1.0/health",
        actions,
        path_only=str(bindir),
    )
    assert proc.returncode != 0
    assert _actions(actions) == 0
