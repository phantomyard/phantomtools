"""Unit tests for `pm check-infra` persona-state checks (``check_persona_state``).

These cover the two ``phantomchat.json`` guards that previously had zero
coverage (see PR #21 re-review):

- the bridge npub must never be present in ``allowed_npubs`` — that list is a
  trust grant (allowlisted senders skip the threat judge); and
- for personas with meeting access, the bridge npub must be registered in the
  untrusted ``relay_npubs`` tier (phantombot #423).

``check_persona_state`` is read-only and mirrors the exact ``apply`` logic, so
these tests exercise the same code path the reviewer flagged as uncovered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from phantommeet.infra import ProbeResult, check_persona_state

BRIDGE_RELAY = "ws://private.relay"
BRIDGE_NPUB = "npub1bridge"


def _manifest() -> dict[str, Any]:
    return {
        "org": "test-org",
        "language": "en",
        "version": "0.0.0",
        "roles": {"maria": "responsible"},
        "permissions": {"full": ["maria"], "scoped": {}, "restricted": {}},
        "rooms": {
            "naming": "{YYYY-MM-DD}-{HH-MM}_{topic}",
            "active_room_required": True,
        },
        "bridge": {"relay": BRIDGE_RELAY, "npub": BRIDGE_NPUB},
        "legacy_kb_files": [],
        "tools": [],
    }


def _write_phantomchat(persona_dir: Path, data: dict[str, Any]) -> None:
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "phantomchat.json").write_text(json.dumps(data), encoding="utf-8")


def _phantomchat_result(results: list[ProbeResult]) -> ProbeResult:
    for result in results:
        if result.name.endswith(" phantomchat"):
            return result
    raise AssertionError(f"no phantomchat probe result in {results!r}")


def test_check_persona_state_fails_bridge_npub_in_allowed_npubs(
    tmp_path: Path,
) -> None:
    """The bridge npub in ``allowed_npubs`` is a trust grant and must FAIL."""
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {
            "relays": [BRIDGE_RELAY],
            "allowed_npubs": [BRIDGE_NPUB],
            # Present in relay_npubs too, so only the allowlist guard fires.
            "relay_npubs": [BRIDGE_NPUB],
        },
    )
    results = check_persona_state("maria", persona_dir, _manifest())
    result = _phantomchat_result(results)
    assert result.state == "fail"
    assert "bridge npub in allowed_npubs" in result.detail


def test_check_persona_state_fails_missing_relay_npubs(tmp_path: Path) -> None:
    """A persona with access whose ``phantomchat.json`` lacks the bridge npub
    in the untrusted ``relay_npubs`` tier must FAIL."""
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {
            "relays": [BRIDGE_RELAY],
            "allowed_npubs": ["npub1existing"],
            "relay_npubs": [],
        },
    )
    results = check_persona_state("maria", persona_dir, _manifest())
    result = _phantomchat_result(results)
    assert result.state == "fail"
    assert "bridge npub missing from relay_npubs" in result.detail


def test_check_persona_state_patched_is_ok(tmp_path: Path) -> None:
    """A correctly patched ``phantomchat.json`` (private relay first, bridge
    npub in relay_npubs only) reports OK."""
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {
            "relays": [BRIDGE_RELAY],
            "allowed_npubs": ["npub1existing"],
            "relay_npubs": [BRIDGE_NPUB],
        },
    )
    results = check_persona_state("maria", persona_dir, _manifest())
    result = _phantomchat_result(results)
    assert result.state == "ok"
    assert result.detail == "patched"


def test_check_persona_state_skips_relay_npubs_without_access(
    tmp_path: Path,
) -> None:
    """A persona without meeting access is not required to carry the bridge
    npub in ``relay_npubs`` (include_bridge=False)."""
    manifest = _manifest()
    manifest["permissions"] = {"full": [], "scoped": {}, "restricted": {}}
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {"relays": [BRIDGE_RELAY], "allowed_npubs": []},
    )
    results = check_persona_state("maria", persona_dir, manifest)
    result = _phantomchat_result(results)
    assert result.state == "ok"
    assert result.detail == "patched"
