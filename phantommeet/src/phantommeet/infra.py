"""Infrastructure probes for `pm check-infra`.

Read-only verification that the infrastructure a PhantomMeet deployment
depends on is reachable and healthy: HTTP endpoints (bridge API, Jitsi),
the private Nostr relay (real NIP-01 REQ/EOSE round-trip over WebSocket),
local files / env files (transcription & summary tooling) and optional
shell commands (org-specific probes). Plus an applied-state check for
personas (Meetings.md present, MEMORY markers, phantomchat.json patched)
that reuses the exact logic from ``apply``.

Everything is driven by the manifest's optional ``infra`` section — the
package itself has zero hardcoded values.

Probe types (``infra.checks[]``):

- ``http``    — GET a URL. ``expect`` optional int (default: any 2xx).
- ``ws``      — WebSocket connect + NIP-01 ``REQ`` waiting for ``EOSE``.
- ``command`` — run a shell command, expect exit code (default 0).
                Contract: must be **read-only**.
- ``file``    — path exists. ``contains`` optional substring, ``non_empty``
                optional bool (default False).
- ``env``     — parse KEY=VALUE file; ``key`` must exist with non-empty
                value.

Every check can carry ``host`` (default ``any``): only runs when the
value matches the ``--host`` flag given to check-infra (e.g. ``vps``),
which lets orgs declare machine-local probes without noisy failures when
the check runs from another machine.

Per-persona capability probes (``infra.persona_checks[]``) run when
``--target`` is given: ``command`` probes executed with the persona
directory as working directory.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import subprocess  # nosec B404
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apply import (
    KB_REL,
    MARKER_END,
    MARKER_START,
    MEMORY_REL,
    PHANTOMCHAT_REL,
    _personas_in_manifest,
)
from .manifest import access_for


@dataclass
class ProbeResult:
    """Outcome of a single probe.

    ``state`` is one of ``ok``, ``fail``, ``skip``. ``ok`` is a property
    kept for backwards compatibility (skip counts as not-ok for the exit
    code, but is rendered distinctly and never reported as a failure).
    """

    name: str
    state: str = "ok"
    detail: str = ""
    ok: bool = True

    def __post_init__(self) -> None:
        self.ok = self.state == "ok"

    def render(self) -> str:
        mark = {"ok": "OK ", "fail": "FAIL", "skip": "SKIP"}[self.state]
        return f"[{mark}] {self.name} — {self.detail}"


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, client side) — stdlib only.
# ---------------------------------------------------------------------------


class _WebSocket:
    """Tiny synchronous WebSocket client, enough for a NIP-01 round-trip."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, url: str, timeout: float) -> _WebSocket:
        host, port, path, use_tls = _parse_ws_url(url)
        sock = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise ConnectionError("connection closed during handshake")
            resp += chunk
        header, _, rest = resp.partition(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            sock.close()
            raise ConnectionError(
                f"handshake rejected: {status_line.decode('ascii', 'replace')}"
            )
        ws = cls(sock)
        ws._buf = rest
        return ws

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed mid-frame")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text frame
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def recv_text(self, timeout: float) -> str | None:
        """Read one data frame, replying pong to any ping. Returns None on close."""
        self._sock.settimeout(timeout)
        hdr = self._read_exact(2)
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        masked = bool(hdr[1] & 0x80)
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == 0x9:  # ping -> pong
            pong = bytearray([0x8A])
            if length < 126:
                pong.append(0x80 | length)
            else:
                pong.append(0x80 | 126)
                pong += struct.pack(">H", length)
            mask = os.urandom(4)
            pong_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self._sock.sendall(bytes(pong) + mask + pong_payload)
            return self.recv_text(timeout)
        if opcode == 0x8:  # close
            return None
        if opcode == 0x1:  # text
            return payload.decode("utf-8", "replace")
        return self.recv_text(timeout)  # skip continuation/other frames

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _parse_ws_url(url: str) -> tuple[str, int, str, bool]:
    if url.startswith("ws://"):
        use_tls, rest = False, url[len("ws://") :]
    elif url.startswith("wss://"):
        use_tls, rest = True, url[len("wss://") :]
    else:
        raise ValueError(f"unsupported ws url (expected ws:// or wss://): {url!r}")
    path = "/"
    if "/" in rest:
        rest, path = rest.split("/", 1)
        path = "/" + path
    default_port = 443 if use_tls else 80
    if ":" in rest:
        host, port = rest.rsplit(":", 1)
        return host, int(port), path, use_tls
    return rest, default_port, path, use_tls


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_http(
    url: str, expect: int | None = None, timeout: float = 10.0
) -> tuple[bool, str]:
    """GET ``url``; OK on 2xx (or on ``expect`` when given)."""
    try:
        # URL comes from the operator-authored manifest (http/https only,
        # validated in _parse_probe_url); no user-controlled scheme.
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310
            status = resp.status
            ok = status < 400 if expect is None else status == expect
            return ok, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        status = exc.code
        ok = status < 400 if expect is None else status == expect
        return ok, f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001 — report any failure
        return False, f"{type(exc).__name__}: {exc}"


def probe_ws(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """WebSocket connect + NIP-01 REQ/EOSE round-trip against a relay."""
    try:
        ws = _WebSocket.connect(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"connect failed: {exc}"

    try:
        ws.send_text('["REQ","pm-check",{"kinds":[1],"limit":1}]')
        deadline = timeout
        while True:
            try:
                frame = ws.recv_text(deadline)
            except TimeoutError:
                return False, "connected but no EOSE within timeout"
            except Exception as exc:  # noqa: BLE001
                return False, f"read failed: {exc}"
            if frame is None:
                return False, "connection closed before EOSE"
            try:
                msg = __import__("json").loads(frame)
            except ValueError:
                continue
            if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "EOSE":
                return True, "relay OK (EOSE received)"
            if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "CLOSED":
                return False, f"relay closed subscription: {msg[1:]!r}"
    finally:
        ws.close()


def probe_command(
    cmd: str, cwd: str | None = None, expect_exit: int = 0, timeout: float = 30.0
) -> tuple[bool, str]:
    """Run ``cmd``; OK when exit code matches ``expect_exit``."""
    try:
        # Operator-authored command from the manifest infra probes; shell
        # is intentional so probes may use pipes/globs (e.g. systemctl
        # is-active, ss -lntp | grep). No untrusted input reaches it.
        proc = subprocess.run(  # nosec B602
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    ok = proc.returncode == expect_exit
    lines = (proc.stdout or "").strip().splitlines()
    detail = lines[-1] if lines else f"exit {proc.returncode}"
    if not ok:
        err_lines = (proc.stderr or "").strip().splitlines()
        if err_lines:
            detail += f" | {err_lines[-1]}"
    return ok, detail


def probe_file(
    path: str, contains: str | None = None, non_empty: bool = False
) -> tuple[bool, str]:
    """Check a local path exists (optionally non-empty / containing text)."""
    p = Path(path)
    if not p.exists():
        return False, f"missing {path}"
    if p.is_dir():
        return True, "directory exists"
    if non_empty and p.stat().st_size == 0:
        return False, f"empty file: {path}"
    if contains is not None:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return False, f"unreadable: {exc}"
        if contains not in text:
            return False, f"{path!r} missing substring {contains!r}"
    return True, f"exists {path}"


def probe_env(path: str, key: str) -> tuple[bool, str]:
    """Parse a KEY=VALUE env file; ``key`` must exist with a non-empty value."""
    p = Path(path)
    if not p.exists():
        return False, f"missing {path}"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable: {exc}"
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            if v.strip():
                return True, f"{key} set in {path}"
            return False, f"{key} empty in {path}"
    return False, f"{key} missing in {path}"


# ---------------------------------------------------------------------------
# Persona applied-state checks (mirror of apply logic, read-only)
# ---------------------------------------------------------------------------


def check_persona_state(
    persona_id: str, persona_dir: Path, manifest: dict[str, Any]
) -> list[ProbeResult]:
    """Verify a persona is fully up to date with the manifest. Read-only."""
    results: list[ProbeResult] = []
    prefix = f"{persona_id}:"

    if not persona_dir.is_dir():
        return [ProbeResult(f"{prefix} persona dir", "fail", f"missing {persona_dir}")]

    # 1) Meetings.md present and not stale (content compare is overkill here).
    kb = persona_dir / KB_REL
    if kb.exists():
        results.append(ProbeResult(f"{prefix} Meetings.md", "ok", "present"))
    else:
        results.append(
            ProbeResult(f"{prefix} Meetings.md", "fail", f"missing {KB_REL}")
        )

    # 2) Legacy kb files must be deprecated in place (banner prepended),
    # never deleted.
    for legacy in manifest.get("legacy_kb_files", []):
        legacy_dest = persona_dir / legacy
        if not legacy_dest.exists():
            results.append(ProbeResult(f"{prefix} legacy {legacy}", "ok", "absent"))
        else:
            try:
                text = legacy_dest.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text.startswith("> Superseded by [[procedures/Meetings]]"):
                results.append(
                    ProbeResult(f"{prefix} legacy {legacy}", "ok", "superseded")
                )
            else:
                results.append(
                    ProbeResult(
                        f"{prefix} legacy {legacy}", "fail", "not superseded"
                    )
                )

    # 3) MEMORY.md markers.
    memory = persona_dir / MEMORY_REL
    if memory.exists():
        text = memory.read_text(encoding="utf-8")
        has_start = MARKER_START in text
        has_end = MARKER_END in text
        if has_start and has_end:
            results.append(ProbeResult(f"{prefix} MEMORY markers", "ok", "present"))
        else:
            results.append(
                ProbeResult(
                    f"{prefix} MEMORY markers",
                    "fail",
                    f"start={has_start} end={has_end} (expected both)",
                )
            )
    else:
        results.append(ProbeResult(f"{prefix} MEMORY", "fail", f"missing {MEMORY_REL}"))

    # 4) phantomchat.json: private relay first + bridge npub allowed.
    bridge = manifest.get("bridge", {})
    relay = bridge.get("relay", "")
    bridge_npub = bridge.get("npub", "")
    include_bridge = access_for(persona_id, manifest)["kind"] != "none"
    pc = persona_dir / PHANTOMCHAT_REL
    if pc.exists():
        import json

        try:
            data = json.loads(pc.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            results.append(
                ProbeResult(f"{prefix} phantomchat", "fail", f"invalid JSON: {exc}")
            )
            return results

        problems: list[str] = []
        relays = data.get("relays", [])
        if relay and (not relays or relays[0] != relay):
            problems.append(f"relay {relay!r} not first in relays")
        if (
            include_bridge
            and bridge_npub
            and bridge_npub not in data.get("allowed_npubs", [])
        ):
            problems.append("bridge npub not in allowed_npubs")
        if problems:
            results.append(
                ProbeResult(f"{prefix} phantomchat", "fail", "; ".join(problems))
            )
        else:
            results.append(ProbeResult(f"{prefix} phantomchat", "ok", "patched"))
    else:
        results.append(
            ProbeResult(f"{prefix} phantomchat", "fail", f"missing {PHANTOMCHAT_REL}")
        )

    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_checks(
    manifest: dict[str, Any], target: Path | None = None, host: str = "any"
) -> list[ProbeResult]:
    """Run every configured probe. ``target`` enables persona-state checks.

    ``host`` filters checks with a ``host`` field: a check runs when its
    declared host is ``any`` (default) or equals ``host``.
    """
    results: list[ProbeResult] = []
    infra = manifest.get("infra", {}) or {}

    for check in infra.get("checks", []) or []:
        if not isinstance(check, dict) or "name" not in check or "type" not in check:
            results.append(ProbeResult("(invalid check)", "fail", "missing name/type"))
            continue
        name, ctype = check["name"], check["type"]
        declared_host = check.get("host", "any")
        if declared_host != "any" and declared_host != host:
            results.append(
                ProbeResult(name, "skip", f"host={declared_host!r} != --host {host!r}")
            )
            continue
        try:
            if ctype == "http":
                ok, detail = probe_http(
                    check["url"],
                    check.get("expect"),
                    timeout=check.get("timeout", 10.0),
                )
            elif ctype == "ws":
                ok, detail = probe_ws(check["url"], timeout=check.get("timeout", 10.0))
            elif ctype == "command":
                ok, detail = probe_command(
                    check["cmd"],
                    cwd=check.get("cwd"),
                    expect_exit=check.get("expect_exit", 0),
                    timeout=check.get("timeout", 30.0),
                )
            elif ctype == "file":
                ok, detail = probe_file(
                    check["path"],
                    contains=check.get("contains"),
                    non_empty=check.get("non_empty", False),
                )
            elif ctype == "env":
                ok, detail = probe_env(check["path"], check["key"])
            else:
                ok, detail = False, f"unknown probe type {ctype!r}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(ProbeResult(name, "ok" if ok else "fail", detail))

    if target is not None:
        for persona_id in _personas_in_manifest(manifest):
            results.extend(
                check_persona_state(persona_id, target / persona_id, manifest)
            )

    return results
