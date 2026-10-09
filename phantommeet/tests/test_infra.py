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
        "permissions": {"full": ["maria"]},
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
    """A correctly patched ``phantomchat.json`` (private relay present, bridge
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


def test_check_persona_state_relay_order_is_not_asserted(tmp_path: Path) -> None:
    """The relay's position is not asserted: the runtime resolves the list from
    the served source, so a private relay first *or* last both pass."""
    orders = (
        [BRIDGE_RELAY, "wss://public.relay"],
        ["wss://public.relay", BRIDGE_RELAY],
    )
    for relays in orders:
        persona_dir = tmp_path / "maria"
        _write_phantomchat(
            persona_dir,
            {
                "relays": relays,
                "allowed_npubs": ["npub1existing"],
                "relay_npubs": [BRIDGE_NPUB],
            },
        )
        results = check_persona_state("maria", persona_dir, _manifest())
        result = _phantomchat_result(results)
        assert result.state == "ok", (relays, result.detail)


def test_check_persona_state_fails_relay_missing(tmp_path: Path) -> None:
    """Presence is still required: a phantomchat.json without the org's private
    relay FAILS, wherever it would have sat."""
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {
            "relays": ["wss://public.relay"],
            "allowed_npubs": ["npub1existing"],
            "relay_npubs": [BRIDGE_NPUB],
        },
    )
    results = check_persona_state("maria", persona_dir, _manifest())
    result = _phantomchat_result(results)
    assert result.state == "fail"
    assert "missing from relays" in result.detail


def test_check_persona_state_skips_relay_npubs_without_access(
    tmp_path: Path,
) -> None:
    """A persona without meeting access is not required to carry the bridge
    npub in ``relay_npubs`` (include_bridge=False)."""
    manifest = _manifest()
    manifest["roles"] = {}  # maria has no role -> no meeting access
    manifest["permissions"] = {"full": []}
    persona_dir = tmp_path / "maria"
    _write_phantomchat(
        persona_dir,
        {"relays": [BRIDGE_RELAY], "allowed_npubs": []},
    )
    results = check_persona_state("maria", persona_dir, manifest)
    result = _phantomchat_result(results)
    assert result.state == "ok"
    assert result.detail == "patched"


def test_check_persona_state_fails_stale_tool_content(tmp_path: Path) -> None:
    """A tool that exists but whose content differs from the rendered template
    must FAIL (content comparison, not presence-only)."""
    manifest = _manifest()
    manifest["invite"] = {
        "phantombot_bin": "phantombot",
        "meet_base_url": "https://meet.example.invalid",
        "tool": {
            "template": "tools/meeting-invite.sh.j2",
            "dest": "tools/meeting-invite.sh",
            "chmod": "0o755",
        },
        "roles": ["maria"],
    }
    persona_dir = tmp_path / "maria"
    tool = persona_dir / "tools" / "meeting-invite.sh"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/usr/bin/env bash\necho stale\n", encoding="utf-8")
    results = check_persona_state("maria", persona_dir, manifest)
    stale = [r for r in results if r.name.endswith(" tool tools/meeting-invite.sh")]
    assert stale and stale[0].state == "fail"
    assert "stale content" in stale[0].detail


def test_check_persona_state_ok_fresh_tool_content(tmp_path: Path) -> None:
    """A tool whose content matches the rendered template reports OK."""
    from phantommeet.apply import render_tool_content

    manifest = _manifest()
    manifest["invite"] = {
        "phantombot_bin": "phantombot",
        "meet_base_url": "https://meet.example.invalid",
        "tool": {
            "template": "tools/meeting-invite.sh.j2",
            "dest": "tools/meeting-invite.sh",
            "chmod": "0o755",
        },
        "roles": ["maria"],
    }
    persona_dir = tmp_path / "maria"
    tool = persona_dir / "tools" / "meeting-invite.sh"
    tool.parent.mkdir(parents=True, exist_ok=True)
    expected = render_tool_content(manifest["invite"]["tool"], "maria", manifest, "en")
    tool.write_text(expected, encoding="utf-8")
    tool.chmod(0o755)  # match the manifest's requested mode
    results = check_persona_state("maria", persona_dir, manifest)
    fresh = [r for r in results if r.name.endswith(" tool tools/meeting-invite.sh")]
    assert fresh and fresh[0].state == "ok"


def test_check_persona_state_fails_wrong_tool_mode(tmp_path: Path) -> None:
    """A tool whose content matches but whose permission bits differ from the
    manifest ``chmod`` must FAIL (mode comparison, not content-only)."""
    from phantommeet.apply import render_tool_content

    manifest = _manifest()
    manifest["invite"] = {
        "phantombot_bin": "phantombot",
        "meet_base_url": "https://meet.example.invalid",
        "tool": {
            "template": "tools/meeting-invite.sh.j2",
            "dest": "tools/meeting-invite.sh",
            "chmod": "0o755",
        },
        "roles": ["maria"],
    }
    persona_dir = tmp_path / "maria"
    tool = persona_dir / "tools" / "meeting-invite.sh"
    tool.parent.mkdir(parents=True, exist_ok=True)
    expected = render_tool_content(manifest["invite"]["tool"], "maria", manifest, "en")
    tool.write_text(expected, encoding="utf-8")
    tool.chmod(0o600)  # wrong mode: manifest requests 0755
    results = check_persona_state("maria", persona_dir, manifest)
    wrong = [r for r in results if r.name.endswith(" tool tools/meeting-invite.sh")]
    assert wrong and wrong[0].state == "fail"
    assert "wrong mode" in wrong[0].detail


def test_run_checks_covers_scoped_personas(tmp_path: Path) -> None:
    """check-infra must verify personas granted access via ``permissions.scoped``,
    not just ``permissions.full`` / ``roles`` personas."""
    from phantommeet.infra import run_checks

    manifest = _manifest()
    manifest["roles"] = {"maria": "responsible", "pedro": "lead"}
    manifest["permissions"] = {
        "full": ["maria"],
        "scoped": {"example-project": ["pedro"]},
        "restricted": {},
    }
    target = tmp_path
    (target / "pedro").mkdir(parents=True, exist_ok=True)
    results = run_checks(manifest, target=target)
    names = [r.name for r in results]
    assert any(n.startswith("pedro:") for n in names)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _renderable_manifest() -> dict[str, Any]:
    """The smoke base manifest (protocol + kb_appendix + storage + defaults)
    plus the roles/permissions the persona-state checks need, so the protocol
    template renders exactly as in a real deployment."""
    import yaml

    manifest: dict[str, Any] = yaml.safe_load(
        (FIXTURES / "base.smoke.yaml").read_text(encoding="utf-8")
    )
    manifest["roles"] = {"maria": "responsible"}
    manifest["permissions"] = {"full": ["maria"]}
    return manifest


def _render_managed_body_for_test(manifest: dict[str, Any], persona_id: str) -> str:
    """Reproduce *by hand* what ``apply`` writes as the Meetings.md managed
    body: the protocol template followed by the manifest ``kb_appendix``
    blocks. Kept independent of ``_render_kb_body`` so the test pins the
    doctor against the spec, not against the implementation under test."""
    from phantommeet.apply import _env, _persona_context, _render_template

    lang = manifest["language"]
    env = _env(lang)
    ctx = _persona_context(persona_id, manifest)
    kb_name = "protocol.en.md" if lang == "en" else "protocol.es.md"
    body = _render_template(env, kb_name, ctx)
    appendix = manifest.get("kb_appendix", [])
    if appendix:
        rendered = [env.from_string(a).render(**ctx).rstrip() for a in appendix]
        body += "\n---\n\n" + "\n\n---\n\n".join(rendered) + "\n"
    return body


def test_check_persona_state_meetings_current_includes_kb_appendix(
    tmp_path: Path,
) -> None:
    """Regression (#128): the doctor must compare Meetings.md against the
    protocol *plus* the manifest ``kb_appendix`` -- exactly what ``apply``
    writes. Comparing only the protocol flagged every applied persona as
    ``stale content`` whenever the manifest carried a ``kb_appendix``."""
    from phantommeet.apply import MARKER_END, MARKER_START

    manifest = _renderable_manifest()
    # Two blocks on purpose: the join separator between appendix blocks is
    # only emitted with >= 2 of them. With a single block, mutating the
    # separator in _render_kb_body would leave this test green.
    assert len(manifest.get("kb_appendix", [])) >= 2, (
        "fixture must exercise the appendix join separator"
    )
    persona_dir = tmp_path / "maria"
    kb = persona_dir / "kb" / "procedures" / "Meetings.md"
    kb.parent.mkdir(parents=True)
    body = _render_managed_body_for_test(manifest, "maria")
    assert "\n\n---\n\n" in body, (
        "the two appendix blocks must be joined by the exact separator"
    )
    kb.write_text(f"{MARKER_START}\n{body}{MARKER_END}\n", encoding="utf-8")

    results = check_persona_state("maria", persona_dir, manifest)
    meetings = [r for r in results if r.name.endswith(" Meetings.md")]
    assert meetings and meetings[0].state == "ok", meetings


def test_check_persona_state_meetings_stale_when_appendix_missing(
    tmp_path: Path,
) -> None:
    """A Meetings.md whose managed body is the protocol only (a partial
    deployment that dropped the ``kb_appendix``) must FAIL as stale, proving
    the appendix is part of what the doctor compares."""
    from phantommeet.apply import MARKER_END, MARKER_START

    manifest = _renderable_manifest()
    persona_dir = tmp_path / "maria"
    kb = persona_dir / "kb" / "procedures" / "Meetings.md"
    kb.parent.mkdir(parents=True)
    protocol_only = _render_managed_body_for_test(
        {**manifest, "kb_appendix": []}, "maria"
    )
    kb.write_text(f"{MARKER_START}\n{protocol_only}{MARKER_END}\n", encoding="utf-8")

    results = check_persona_state("maria", persona_dir, manifest)
    meetings = [r for r in results if r.name.endswith(" Meetings.md")]
    assert meetings and meetings[0].state == "fail"
    assert "stale content" in meetings[0].detail
