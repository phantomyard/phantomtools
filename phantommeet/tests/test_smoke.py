"""Smoke tests: the CLI must derive, validate and apply manifests end to end."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from phantommeet.apply import (
    _contained_dest,
    _has_supersede_banner,
    _patch_phantomchat,
    _persona_context,
    _supersede_legacy_kb,
    _tool_env,
    _upsert_kb,
)
from phantommeet.manifest import ManifestError, load_manifest

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run_cli(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    full_env = None if env is None else {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-m", "phantommeet.cli", *args],
        cwd=cwd or REPO,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


def test_validate_example_manifest() -> None:
    """The shipped example manifest must validate cleanly."""
    proc = run_cli("validate", "--manifest", str(EXAMPLES / "example-org.yaml"))
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_derive_then_validate_smoke_org(tmp_path: Path) -> None:
    """derive-manifest from org + base, then validate the derived manifest."""
    derived = tmp_path / "derived.yaml"
    proc = run_cli(
        "derive-manifest",
        "--org",
        str(FIXTURES / "org.smoke.yaml"),
        "--base",
        str(FIXTURES / "base.smoke.yaml"),
        "--out",
        str(derived),
    )
    assert proc.returncode == 0, proc.stderr
    assert derived.exists()

    proc = run_cli("validate", "--manifest", str(derived))
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    # derive rules map ceo/cfo -> responsible, project_lead -> lead,
    # training_lead -> support (role-based, no room-name scope)
    assert "maria=responsible" in proc.stdout
    assert "pedro=lead" in proc.stdout
    assert "lucia=support" in proc.stdout
    # escalation map: support/lead actor -> responsible actor, derived from
    # the org escalation_matrix (training_lead -> ceo -> maria; project_lead
    # -> ceo -> maria).
    manifest = yaml.safe_load(derived.read_text(encoding="utf-8"))
    assert manifest["escalation"] == {"lucia": "maria", "pedro": "maria"}
    # permissions: role-based, no room-name scope — only `full` (responsible).
    assert manifest["permissions"] == {"full": ["maria", "juan"]}
    assert manifest["roles"] == {
        "maria": "responsible",
        "juan": "responsible",
        "pedro": "lead",
        "lucia": "support",
    }


def test_meetings_md_renders_explicit_escalation_for_support(tmp_path: Path) -> None:
    """Support personas get the concrete escalation persona in Meetings.md."""
    derived = tmp_path / "derived.yaml"
    proc = run_cli(
        "derive-manifest",
        "--org",
        str(FIXTURES / "org.smoke.yaml"),
        "--base",
        str(FIXTURES / "base.smoke.yaml"),
        "--out",
        str(derived),
    )
    assert proc.returncode == 0, proc.stderr

    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    # Real apply on the tmp manifest (invite.roles is persisted into the
    # tmp copy, not the repo files).
    proc = run_cli(
        "apply",
        "--manifest",
        str(derived),
        "--target",
        str(personas),
        "--invite-roles",
        "ceo",
    )
    assert proc.returncode == 0, proc.stderr

    meetings = (personas / "lucia" / "kb" / "procedures" / "Meetings.md").read_text(
        encoding="utf-8"
    )
    assert "Tu responsable de escalado para reuniones online es **maria**." in meetings
    # The generic rule is still present for the no-escalation case.
    assert "escálala a la persona responsable" in meetings
    # Canonical escalation format: DM starting with @ to the target persona.
    assert "`@maria <solicitud con los parámetros exactos recibidos>`" in meetings
    # Canonical bridge commands block.
    assert "## Comandos del puente (canónico)" in meetings
    assert "`@<persona> <texto>`" in meetings
    assert "`join [<sala>] --nick <tu-nick> [--password ***]`" in meetings
    # Recordings: automatic VPS dir + Drive destination default.
    assert "`/tmp/phantommeet-recordings`" in meetings
    # Support does not schedule: no destination folder for them.
    assert "no agendas; pasa el indicado en la solicitud" in meetings
    assert "`Grabaciones`" not in meetings
    # Custody appendix: no owner/custodian for support (generic wording).
    assert "La persona responsable sube el MP4" in meetings
    # Communication channel section (phantomorg is the source of truth).
    assert "## Canal de comunicación" in meetings
    assert "phantomorg" in meetings
    # Pre-flight request check section with defaults table.
    assert "## Antes de actuar: comprobación de la solicitud" in meetings
    assert "`18:00`" in meetings
    assert "60` min" in meetings


def test_meetings_md_renders_project_responsible_for_lead(tmp_path: Path) -> None:
    """Lead personas render as project-responsible in Meetings.md: they
    schedule their project's meetings and escalate only out-of-project requests."""
    derived = tmp_path / "derived.yaml"
    proc = run_cli(
        "derive-manifest",
        "--org",
        str(FIXTURES / "org.smoke.yaml"),
        "--base",
        str(FIXTURES / "base.smoke.yaml"),
        "--out",
        str(derived),
    )
    assert proc.returncode == 0, proc.stderr

    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    proc = run_cli(
        "apply",
        "--manifest",
        str(derived),
        "--target",
        str(personas),
        "--invite-roles",
        "ceo",
    )
    assert proc.returncode == 0, proc.stderr

    meetings = (personas / "pedro" / "kb" / "procedures" / "Meetings.md").read_text(
        encoding="utf-8"
    )
    # Lead: schedules her project's meetings (no room-name scope).
    assert "Eres **responsable de las reuniones online de tu proyecto**" in meetings
    assert "example-project-*" not in meetings
    # Out-of-project escalates to the concrete persona.
    assert "Las reuniones fuera de tu proyecto escalan a **maria**." in meetings
    # Lead must NOT get the support "No agendes" rule.
    assert "**No agendes reuniones online**" not in meetings
    # Lead role label.
    assert "Lead de Proyecto" in meetings
    # Canonical escalation section with @-mention format (out-of-project only).
    assert "## Escalado de solicitudes de reunión" in meetings
    assert "`@maria <solicitud con los parámetros exactos recibidos>`" in meetings
    assert "Eres responsable de las reuniones online **de tu proyecto**" in meetings
    # Destination is per-project: the lead answers with their project's folder,
    # not the org-wide recordings folder.
    assert (
        "`example-meetings` (la carpeta de reuniones de tu proyecto en Drive)"
        in meetings
    )
    assert "`Grabaciones`" not in meetings
    # Custody: the lead uploads her scope's recordings; org custodian is backup.
    assert "**Pedro** sube el MP4" in meetings
    assert "carpeta `example-meetings/`" in meetings
    assert "lo hace **Maria** (custodia de la organización)" in meetings


def test_meeting_invite_is_notification_only() -> None:
    """meeting-invite.sh must NOT switch personas or schedule tasks in other
    runtimes (cross-persona escalation removed): it sends the invitation as a
    notification only, and each recipient's own runtime decides."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    # The rendered script must not reach across runtime boundaries.
    assert "phantombot persona" not in rendered
    assert "task add" not in rendered
    assert "notify --message" in rendered

    script = REPO / "_test_invite_notify.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "Junta directiva",
                "--type",
                "junta",
                "--topic",
                "junta",
                "--datetime",
                "2026-08-14T18:00:00",
                "--recipients",
                "@maria,@juan,@pedro,@salvador",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Invitation goes to the coordination group with the room link.
    assert "https://meet.example.invalid/2026-08-14-18-00_junta" in out
    # No per-persona auto-join task lines anywhere.
    assert "--nick" not in out
    assert "(persona" not in out


def test_meeting_invite_rejects_invalid_datetime() -> None:
    """A malformed --datetime (e.g. a sed/shell-injection attempt) is rejected
    before any of its substrings reach the room-name logic."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    script = REPO / "_test_invite_baddate.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "x",
                "--datetime",
                "2026-08-14T18:00:00; rm -rf /",
                "--recipients",
                "@maria",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    assert proc.returncode != 0
    assert "ISO 8601" in proc.stderr


def test_meeting_invite_custom_card_renders_manifest_template(tmp_path: Path) -> None:
    """invite.card overrides the built-in announcement format and its
    %TOKENS% are substituted with runtime values. The password is only
    declared via --password-file; it is never read nor broadcast."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    assert "card" in manifest["invite"], "example manifest should define invite.card"
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    pw_file = tmp_path / "pw.txt"
    pw_file.write_text("clave42", encoding="utf-8")

    script = REPO / "_test_invite_card.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "Asamblea General",
                "--datetime",
                "2026-08-14T18:00:00",
                "--recipients",
                "@maria,@salvador",
                "--topic",
                "asamblea general",
                "--password-file",
                str(pw_file),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Custom card: manifest format wins over the built-in one.
    assert "📅 Reunión: Asamblea General" in out
    assert "👥 Destinatarios: @maria,@salvador" in out
    assert "🕐 2026-08-14T18:00:00" in out
    assert "🔗 https://meet.example.invalid/2026-08-14-18-00_asamblea_general" in out
    # The password is declared, never read nor broadcast: the card carries a
    # "shared separately" notice and the secret never reaches stdout/broadcast.
    assert "🔒 Contraseña: se comparte por separado" in out
    assert "clave42" not in out
    assert "%TITLE%" not in out


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("has_card", [True, False], ids=["card", "builtin"])
def test_meeting_invite_never_broadcasts_password(
    tmp_path: Path, lang: str, has_card: bool
) -> None:
    """The room password must never travel in the untargeted `phantombot
    notify` broadcast (it reaches every authorized owner on every channel).
    The script declares the password but never reads or broadcasts it — across
    all four render paths: (custom card | built-in) × (es | en)."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["language"] = lang
    if not has_card:
        manifest["invite"].pop("card", None)
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env(lang).get_template("meeting-invite.sh.j2").render(**ctx)

    pw_file = tmp_path / "pw.txt"
    pw_file.write_text("secreto-supremo", encoding="utf-8")

    # Fake phantombot: records its argv so the test can inspect the broadcast.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "phantombot"
    fake.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "${PHANTOMBOT_ARGS_FILE}"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    args_file = tmp_path / "phantombot-args.txt"

    script = REPO / "_test_invite_broadcast.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "Junta Directiva",
                "--datetime",
                "2026-08-14T18:00:00",
                "--recipients",
                "@maria,@salvador",
                "--topic",
                "junta directiva",
                "--password-file",
                str(pw_file),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PHANTOMBOT_ARGS_FILE": str(args_file),
            },
        )
    finally:
        script.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stderr
    sent = args_file.read_text(encoding="utf-8") if args_file.exists() else ""
    # The broadcast signals "password-protected" but never carries the secret.
    notice = "se comparte por separado" if lang == "es" else "shared separately"
    assert "notify" in sent
    assert notice in sent
    assert "secreto-supremo" not in sent
    assert "secreto-supremo" not in proc.stdout


def test_meeting_invite_card_password_line_omitted_when_empty() -> None:
    """%PASSWORD_LINE% expands to nothing (and no blank line is left)
    when the invitation has no password."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    script = REPO / "_test_invite_nopass.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "Café informal",
                "--datetime",
                "2026-08-14T19:00:00",
                "--recipients",
                "@maria",
                "--topic",
                "cafe",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "📅 Reunión: Café informal" in out
    assert "Contraseña" not in out
    assert "%PASSWORD_LINE%" not in out
    # No dangling blank line from the empty password slot.
    assert "\n\n\n" not in out


def test_invite_card_requires_mandatory_tokens(tmp_path: Path) -> None:
    """A card missing any mandatory token (%TITLE%, %DATETIME%, %LINK%) is
    rejected at load/validate time — broken cards never reach production."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    # Branded card that forgot the join link: must be rejected.
    manifest["invite"]["card"] = (
        "Example Org\n━━━━━━━━━━━━\n📅 Reunión: %TITLE%\n🕐 %DATETIME%\n━━━━━━━━━━━━"
    )
    bad = tmp_path / "card-sin-link.yaml"
    bad.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    try:
        load_manifest(bad)
    except ManifestError as exc:
        assert "%LINK%" in str(exc)
    else:
        raise AssertionError("expected ManifestError for invite.card missing %LINK%")

    # All mandatory tokens present -> loads fine.
    manifest2 = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    # All mandatory tokens present -> loads fine.
    manifest2 = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    good = tmp_path / "card-completa.yaml"
    good.write_text(yaml.safe_dump(manifest2, allow_unicode=True), encoding="utf-8")
    loaded = load_manifest(good)
    assert loaded["invite"]["card"]


def test_apply_card_file_one_shot(tmp_path: Path) -> None:
    """--card-file overrides the card for this run only (not persisted)."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    card = tmp_path / "card.txt"
    card.write_text(
        "EXAMPLE ORG\n"
        "━━━━━━━━━━━━\n"
        "📅 %TITLE%\n"
        "⏰ %DATETIME%\n"
        "📍 %LINK%\n"
        "%PASSWORD_LINE%\n"
        "━━━━━━━━━━━━\n"
        "¡Te esperamos!",
        encoding="utf-8",
    )
    persona = tmp_path / "personas" / "maria"
    persona.mkdir(parents=True)
    # Apply needs every persona dir declared in the manifest.
    for pid in ("juan", "pedro", "lucia"):
        (tmp_path / "personas" / pid).mkdir(parents=True, exist_ok=True)
    before = manifest_path.read_text(encoding="utf-8")

    # Card from file with all mandatory tokens -> applies.
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(tmp_path / "personas"),
        "--card-file",
        str(card),
        "--dry-run",
    )
    assert proc.returncode == 0, proc.stderr
    # Not persisted on dry-run: manifest file unchanged (byte-identical).
    assert manifest_path.read_text(encoding="utf-8") == before

    # Card missing %LINK% -> rejected with a clear error.
    bad = tmp_path / "card-bad.txt"
    bad.write_text("📅 %TITLE%\n⏰ %DATETIME%\n(sin enlace)", encoding="utf-8")
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(tmp_path / "personas"),
        "--card-file",
        str(bad),
        "--dry-run",
    )
    assert proc.returncode != 0
    assert "%LINK%" in proc.stderr


def test_check_infra_log_file(tmp_path: Path) -> None:
    """check-infra reports to screen AND appends to a log file (--log)."""
    manifest = EXAMPLES / "example-org.yaml"
    log = tmp_path / "logs" / "check-infra.log"

    # Local-only run: everything from the derived manifest is probed;
    # python3/bash are generic and should pass on this machine. We don't
    # assert the exit code: org-specific probes (jitsi, relay, bridge...)
    # depend on the network. We only assert the reporting contract.
    proc = run_cli("check-infra", "--manifest", str(manifest), "--log", str(log))
    assert log.exists(), "log file was not created"
    text = log.read_text(encoding="utf-8")
    assert "check-infra" in text
    assert "checks passed" in text
    # Screen output must include the same probe lines.
    assert "[OK]" in proc.stdout or "[FAIL]" in proc.stdout or "[SKIP]" in proc.stdout

    # Default log path (XDG_STATE_HOME) is used when --log is omitted,
    # and --no-log disables the file entirely.
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    proc = run_cli(
        "check-infra",
        "--manifest",
        str(manifest),
        "--no-log",
        env=env,
    )
    assert not (tmp_path / "state" / "phantommeet" / "check-infra.log").exists()


# ---------------------------------------------------------------------------
# PR #21 re-review hardening regressions
# ---------------------------------------------------------------------------


def test_bridge_npub_never_in_allowed_npubs() -> None:
    """The bridge npub must NOT be added to allowed_npubs: that is a trust
    grant (allowlisted senders skip the threat judge). PhantomMeet moves the
    private relay first and registers the bridge npub in relay_npubs (the
    untrusted relay tier) instead."""
    data = {
        "relays": ["wss://public.relay", "ws://private.relay"],
        "allowed_npubs": ["npub1existing"],
    }
    patched, relay_added, npub_added = _patch_phantomchat(
        data, "ws://private.relay", "npub1bridge", include_bridge=True
    )
    # private relay moved to front
    assert patched["relays"][0] == "ws://private.relay"
    # allowed_npubs untouched (bridge npub NOT added)
    assert patched["allowed_npubs"] == ["npub1existing"]
    # bridge npub registered in the untrusted relay_npubs tier, not the allowlist
    assert patched["relay_npubs"] == ["npub1bridge"]
    # owned relay delta is None (relay was already present); npub delta set
    assert relay_added is None
    assert npub_added == "npub1bridge"


def test_patch_phantomchat_skips_bridge_when_excluded() -> None:
    """include_bridge=False leaves relay_npubs (and allowed_npubs) untouched."""
    data = {"relays": ["ws://private.relay"], "allowed_npubs": ["npub1existing"]}
    patched, relay_added, npub_added = _patch_phantomchat(
        data, "ws://private.relay", "npub1bridge", include_bridge=False
    )
    assert patched["relays"] == ["ws://private.relay"]
    assert "relay_npubs" not in patched
    assert patched["allowed_npubs"] == ["npub1existing"]
    assert relay_added is None
    assert npub_added is None


def test_patch_phantomchat_relay_npubs_is_idempotent() -> None:
    """An already-registered bridge npub in relay_npubs is not re-added."""
    data = {
        "relays": ["ws://private.relay"],
        "allowed_npubs": [],
        "relay_npubs": ["npub1bridge", "npub1other"],
    }
    patched, relay_added, npub_added = _patch_phantomchat(
        data, "ws://private.relay", "npub1bridge", include_bridge=True
    )
    assert patched["relay_npubs"] == ["npub1bridge", "npub1other"]
    assert npub_added is None
    assert relay_added is None


def test_patch_phantomchat_records_added_relay_delta() -> None:
    """When PhantomMeet ADDS the private relay (it was not present), the
    owned delta records exactly that relay (and the bridge npub) for
    `pm unapply` to remove."""
    data = {"relays": ["wss://public.relay"], "allowed_npubs": []}
    patched, relay_added, npub_added = _patch_phantomchat(
        data, "ws://private.relay", "npub1bridge", include_bridge=True
    )
    assert patched["relays"] == ["ws://private.relay", "wss://public.relay"]
    assert relay_added == "ws://private.relay"
    assert npub_added == "npub1bridge"
    assert patched["relay_npubs"] == ["npub1bridge"]
    # allowed_npubs still untouched
    assert patched["allowed_npubs"] == []


def test_contained_dest_refuses_path_escape() -> None:
    """A manifest tool destination that escapes the persona directory (path
    traversal) is refused."""
    persona_dir = Path("/tmp/personas/maria")
    assert _contained_dest(persona_dir, "../escaped.sh") is None
    assert _contained_dest(persona_dir, "../../etc/passwd") is None
    assert _contained_dest(persona_dir, "tools/meeting-invite.sh") is not None


def test_upsert_kb_preserves_prefix_and_suffix() -> None:
    """Operator content on BOTH sides of the managed block survives a re-apply."""
    frontmatter = "---\ntype: procedure\ntitle: T\n---\n"
    existing = (
        frontmatter
        + "operator note BEFORE the block\n"
        + "<!-- phantommeet:start -->\nold body\n<!-- phantommeet:end -->\n"
        + "operator note AFTER the block\n"
    )
    out = _upsert_kb(existing, frontmatter, "new body\n")
    assert "operator note BEFORE the block" in out
    assert "operator note AFTER the block" in out
    assert "new body" in out
    assert "old body" not in out
    # frontmatter is still first
    assert out.startswith(frontmatter)


def test_upsert_kb_strips_stale_protocol_duplicate() -> None:
    """A stale duplicate of the managed protocol in the suffix (a previous
    render orphaned outside the markers) is stripped; a trailing operator note
    is kept."""
    frontmatter = "---\ntype: procedure\ntitle: T\n---\n"
    existing = (
        frontmatter
        + "<!-- phantommeet:start -->\nmanaged body\n<!-- phantommeet:end -->\n"
        + "# aquaponics-united — Protocolo de Reuniones\n"
        + "## Rol en reuniones\n- stale line\n"
        + "## Estado de validación\n- nota del operador\n"
    )
    out = _upsert_kb(existing, frontmatter, "managed body\n")
    # stale duplicate gone
    assert "# aquaponics-united — Protocolo de Reuniones" not in out
    assert "## Rol en reuniones" not in out
    assert "stale line" not in out
    # operator note kept
    assert "## Estado de validación" in out
    assert "nota del operador" in out
    # managed body present
    assert "managed body" in out


def test_apply_refuses_tool_path_escape(tmp_path: Path) -> None:
    """A manifest tool with a traversing destination aborts the apply with a
    clear error and does not write anything outside the persona dir."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["tools"] = [{"dest": "../escaped.sh", "chmod": "0o755"}]
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode != 0, proc.stdout
    assert "escapes the persona directory" in proc.stderr
    # nothing escaped the persona dir
    assert not (tmp_path / "escaped.sh").exists()


def test_apply_preflight_aborts_before_partial_write(tmp_path: Path) -> None:
    """An invalid phantomchat.json aborts the apply BEFORE KB/MEMORY.md are
    written (no partial deployment)."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        d = personas / pid
        d.mkdir(parents=True, exist_ok=True)
    # maria has a broken phantomchat.json
    (personas / "maria" / "phantomchat.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode != 0, proc.stdout
    assert "invalid JSON" in proc.stderr
    # KB/MEMORY.md must NOT have been written for ANY persona (preflight).
    for pid in ("maria", "juan", "pedro", "lucia"):
        assert not (personas / pid / "kb" / "procedures" / "Meetings.md").exists(), pid
        assert not (personas / pid / "MEMORY.md").exists(), pid


def test_unapply_reverses_phantomchat_relay(tmp_path: Path) -> None:
    """`pm unapply` removes the owned relay delta without touching unrelated
    configuration."""
    import json

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)
    # maria has phantomchat.json WITHOUT the private relay; apply adds it.
    maria = personas / "maria"
    (maria / "phantomchat.json").write_text(
        json.dumps(
            {
                "relays": ["wss://public.relay"],
                "allowed_npubs": ["npub1operator"],
            }
        ),
        encoding="utf-8",
    )

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr
    applied = json.loads((maria / "phantomchat.json").read_text(encoding="utf-8"))
    assert applied["relays"][0] == "ws://relay.example.invalid:7777"
    # bridge npub registered in relay_npubs, not in allowed_npubs
    bridge_npub = manifest["bridge"]["npub"]
    assert applied["relay_npubs"] == [bridge_npub]
    assert bridge_npub not in applied.get("allowed_npubs", [])

    # unapply removes the owned relay + bridge npub, keeps the operator's npub.
    proc = run_cli(
        "unapply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
    )
    assert proc.returncode == 0, proc.stderr
    restored = json.loads((maria / "phantomchat.json").read_text(encoding="utf-8"))
    assert restored["relays"] == ["wss://public.relay"]
    assert restored["allowed_npubs"] == ["npub1operator"]
    assert "relay_npubs" not in restored
    # delta file consumed
    assert not (maria / ".phantommeet-phantomchat.delta.json").exists()


def test_supersede_legacy_kb_idempotent_with_frontmatter() -> None:
    """A legacy note WITH OKF frontmatter gets the superseded banner exactly
    once: the banner goes after the frontmatter and re-applying is a no-op."""
    text = "---\ntype: note\ntitle: Salas Jitsi\n---\n\nContenido antiguo.\n"
    once = _supersede_legacy_kb(text)
    # frontmatter stays first, banner sits after it (never duplicated)
    assert once.startswith("---\n")
    assert once.count("> Superseded by [[procedures/Meetings]]") == 1
    assert once.index("> Superseded") > once.index("---\n", 1)
    # idempotent: re-applying is a no-op even with frontmatter present
    assert _supersede_legacy_kb(once) == once
    assert _has_supersede_banner(once)


def test_apply_relay_delta_survives_reorder(tmp_path: Path) -> None:
    """Moving the relay down the list and re-applying must NOT erase the owned
    delta: `pm unapply` must still be able to remove the relay afterwards."""
    import json

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)
    maria = personas / "maria"
    relay = "ws://relay.example.invalid:7777"
    bridge_npub = manifest["bridge"]["npub"]
    (maria / "phantomchat.json").write_text(
        json.dumps(
            {"relays": ["wss://public.relay"], "allowed_npubs": ["npub1operator"]}
        ),
        encoding="utf-8",
    )

    # Apply #1: relay is absent → added and recorded in the owned delta.
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr
    delta_path = maria / ".phantommeet-phantomchat.delta.json"
    delta1 = json.loads(delta_path.read_text(encoding="utf-8"))
    assert delta1["relay_added"] == relay
    assert delta1["npub_added"] == bridge_npub

    # Operator moves the relay down (still present, just not first).
    data = json.loads((maria / "phantomchat.json").read_text(encoding="utf-8"))
    assert data["relays"][0] == relay
    data["relays"] = [r for r in data["relays"] if r != relay] + [relay]
    (maria / "phantomchat.json").write_text(json.dumps(data), encoding="utf-8")

    # Apply #2: relay present → reorder only (relay_added None), but the delta
    # must be PRESERVED so `pm unapply` can still reverse it.
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr
    delta2 = json.loads(delta_path.read_text(encoding="utf-8"))
    assert delta2["relay_added"] == relay
    assert delta2["npub_added"] == bridge_npub

    # unapply still removes the owned relay + npub.
    proc = run_cli(
        "unapply", "--manifest", str(manifest_path), "--target", str(personas)
    )
    assert proc.returncode == 0, proc.stderr
    restored = json.loads((maria / "phantomchat.json").read_text(encoding="utf-8"))
    assert relay not in restored["relays"]
    assert bridge_npub not in restored.get("relay_npubs", [])
    assert not delta_path.exists()


def test_no_targeted_delivery_claims() -> None:
    """Shipped content must not promise targeted invitation delivery — the
    only mechanism is untargeted `phantombot notify` (coordinator_chat is gone
    from schema/SPEC, so no generated content may claim a coordination group
    or DM channel)."""
    es = (REPO / "src/phantommeet/templates/kb/protocol.es.md").read_text(
        encoding="utf-8"
    )
    en = (REPO / "src/phantommeet/templates/kb/protocol.en.md").read_text(
        encoding="utf-8"
    )
    base = (EXAMPLES / "base.example.yaml").read_text(encoding="utf-8")
    derived = (EXAMPLES / "example-org.yaml").read_text(encoding="utf-8")

    assert "grupo de coordinación de la organización" not in es
    assert "coordination group or a direct DM" not in en
    assert "Telegram (coordinación o DM)" not in es
    assert "Telegram (coordination or DM)" not in en
    assert "grupo de coordinación o DM" not in base
    assert "grupo de coordinación o DM" not in derived
    assert "se envían por **Telegram**" not in base


def test_apply_ask_roles_not_persisted_on_failed_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    """--ask-roles must NOT rewrite invite.roles when a later persona fails
    preflight: the interactive decision is persisted only after the whole
    plan is clean (deferred manifest persistence)."""
    from phantommeet import discovery
    from phantommeet.apply import apply_manifest

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["invite"].pop("roles", None)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )

    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)
    # maria has a broken phantomchat.json -> preflight fails.
    (personas / "maria" / "phantomchat.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    monkeypatch.setattr(discovery, "prompt_for_roles", lambda *a, **k: ["pedro"])

    result = apply_manifest(str(manifest_path), str(personas), ask_roles=True)
    assert result.errors
    # The manifest must NOT have been rewritten by the failed apply.
    on_disk = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert "roles" not in on_disk.get("invite", {})


def test_apply_ask_roles_persisted_on_clean_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    """--ask-roles DOES persist invite.roles once the plan is clean (positive
    counterpart of the deferred-persistence regression)."""
    from phantommeet import discovery
    from phantommeet.apply import apply_manifest

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["invite"].pop("roles", None)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )

    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(discovery, "prompt_for_roles", lambda *a, **k: ["pedro"])

    result = apply_manifest(str(manifest_path), str(personas), ask_roles=True)
    assert not result.errors
    on_disk = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["invite"]["roles"] == ["pedro"]


def test_apply_corrupt_delta_fails_closed(tmp_path: Path) -> None:
    """A corrupt owned delta must abort the apply (fail-closed), never be
    silently dropped: dropping it orphans the owned relay/npub and `pm
    unapply` could no longer reverse them."""
    import json

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)
    maria = personas / "maria"
    relay = "ws://relay.example.invalid:7777"
    (maria / "phantomchat.json").write_text(
        json.dumps({"relays": ["wss://public.relay"], "allowed_npubs": []}),
        encoding="utf-8",
    )

    # Apply #1: relay absent -> added, delta recorded.
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr
    delta_path = maria / ".phantommeet-phantomchat.delta.json"
    assert delta_path.exists()

    # Corrupt the delta, then move the relay down (reorder scenario).
    delta_path.write_text("{ this is not json", encoding="utf-8")
    data = json.loads((maria / "phantomchat.json").read_text(encoding="utf-8"))
    data["relays"] = [r for r in data["relays"] if r != relay] + [relay]
    (maria / "phantomchat.json").write_text(json.dumps(data), encoding="utf-8")

    # Apply #2 must FAIL (not silently drop the corrupt delta).
    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode != 0
    assert "corrupt" in proc.stderr
    # The corrupt delta must still exist (preserved, not deleted).
    assert delta_path.exists()
    assert "this is not json" in delta_path.read_text(encoding="utf-8")


def test_unapply_corrupt_delta_fails_closed(tmp_path: Path) -> None:
    """`pm unapply` must fail (fail-closed) on a corrupt delta rather than
    silently skip the reversal — otherwise the owned relay/npub stays
    installed with no record to reverse them."""
    import json

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)
    maria = personas / "maria"
    (maria / "phantomchat.json").write_text(
        json.dumps({"relays": ["wss://public.relay"], "allowed_npubs": []}),
        encoding="utf-8",
    )

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr
    delta_path = maria / ".phantommeet-phantomchat.delta.json"
    assert delta_path.exists()

    delta_path.write_text("not json", encoding="utf-8")

    proc = run_cli(
        "unapply", "--manifest", str(manifest_path), "--target", str(personas)
    )
    assert proc.returncode != 0
    assert "corrupt" in proc.stderr
    assert delta_path.exists()  # preserved, not deleted


def test_meeting_invite_shell_quotes_manifest_scalars() -> None:
    """A manifest value with a closing quote + command must NOT execute when
    the rendered script starts (render-time shell injection)."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["invite"]["phantombot_bin"] = 'phantombot"; touch /tmp/pm-injected; echo "'
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    marker = Path("/tmp") / "pm-injected"
    marker.unlink(missing_ok=True)
    script = REPO / "_test_inject.sh"
    script.write_text(rendered)
    try:
        # Actually RUN the script (dry-run): the assignment of the malicious
        # PHANTOMBOT_BIN is the line that would execute the injected command.
        # With shell-quoting it is a literal single-quoted string, so the
        # touch never runs.
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "x",
                "--datetime",
                "2026-08-14T18:00:00",
                "--recipients",
                "@maria",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)
    assert not marker.exists(), "malicious phantombot_bin value must not execute"
    # The script still ran to completion (the injected value is inert).
    assert proc.returncode == 0, proc.stderr


def test_parse_tool_mode_accepts_octal_strings() -> None:
    """chmod strings are octal: "0o755", "0755" and "755" all mean 0o755.
    (The docstring used to advertise "0755"/"755" while int(mode, 0) parsed
    "755" as decimal 755 = 0o1363 and rejected "0755" outright.)"""
    from phantommeet.apply import parse_tool_mode

    assert parse_tool_mode({"chmod": 0o755}) == 0o755
    assert parse_tool_mode({"chmod": "0o755"}) == 0o755
    assert parse_tool_mode({"chmod": "0755"}) == 0o755
    assert parse_tool_mode({"chmod": "755"}) == 0o755
    assert parse_tool_mode({"chmod": "0o600"}) == 0o600
    assert parse_tool_mode({"chmod": "644"}) == 0o644
    assert parse_tool_mode({"chmod": "not-a-mode"}) is None
    assert parse_tool_mode({"chmod": ""}) is None
    assert parse_tool_mode({}) is None
    assert parse_tool_mode("not-a-dict") is None


def test_render_manifest_invite_roundtrips_roles_and_card(tmp_path: Path) -> None:
    """The manifest render applies invite.roles and invite.card together, in
    memory, without touching the file (the caller commits atomically)."""
    from phantommeet.apply import _render_manifest_invite

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["invite"].pop("roles", None)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    before = manifest_path.read_bytes()

    rendered = _render_manifest_invite(
        str(manifest_path),
        roles=["pedro", "lucia"],
        card="new card %TITLE% %DATETIME% %LINK%",
    )
    # Render is read-only: the file is never truncated during render.
    assert manifest_path.read_bytes() == before
    data = yaml.safe_load(rendered)
    assert data["invite"]["roles"] == ["pedro", "lucia"]
    assert data["invite"]["card"] == "new card %TITLE% %DATETIME% %LINK%"

    # card=None removes the field (built-in format).
    rendered = _render_manifest_invite(str(manifest_path), roles=["pedro"], card=None)
    data = yaml.safe_load(rendered)
    assert data["invite"]["roles"] == ["pedro"]
    assert "card" not in data["invite"]


def test_manifest_render_failure_preserves_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed manifest render (ruamel dump raising mid-write) must surface
    as an error and leave the manifest byte-identical — never truncate it in
    place and persist the fragment as the new manifest."""
    import sys
    import types

    from phantommeet import discovery
    from phantommeet.apply import apply_manifest

    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest["invite"].pop("roles", None)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    before = manifest_path.read_bytes()

    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(discovery, "prompt_for_roles", lambda *a, **k: ["pedro"])

    # Inject a fake ruamel.yaml whose dump writes one line then fails, to
    # reproduce the ENOSPC/EIO mid-dump corruption deterministically (ruamel
    # is an optional dependency and may be absent from the test env).
    class _ExplodingYAML:
        def load(self, text):
            return yaml.safe_load(text)

        def dump(self, data, stream=None, *args, **kwargs):
            stream.write("org: acme\n")
            raise OSError("ENOSPC (simulated)")

    fake_ruamel = types.ModuleType("ruamel")
    fake_ruamel.__path__ = []
    fake_ruamel_yaml = types.ModuleType("ruamel.yaml")
    fake_ruamel_yaml.YAML = _ExplodingYAML
    monkeypatch.setitem(sys.modules, "ruamel", fake_ruamel)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", fake_ruamel_yaml)

    result = apply_manifest(str(manifest_path), str(personas), ask_roles=True)

    # (i) the failure surfaces — the render error is recorded, not swallowed.
    assert result.errors
    assert "ENOSPC" in result.errors[0]
    # (ii) the manifest is byte-identical to before.
    assert manifest_path.read_bytes() == before
    # Nothing else was committed either: the render aborts before Phase 2.
    assert not (personas / "maria" / "kb" / "procedures" / "Meetings.md").exists()


def test_meeting_join_tool_renders_persona_identity() -> None:
    """meeting-join.js renders per persona: the nick is the persona's own id
    (never read from an invitation), the bridge npub comes from the manifest,
    and no Jinja delimiters leak into the output."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    assert "npub" in manifest["bridge"]
    for pid in ("maria", "juan", "pedro", "lucia"):
        ctx = _persona_context(pid, manifest)
        rendered = _tool_env("es").get_template("meeting-join.js.j2").render(**ctx)
        # persona identity injected as a JS string literal
        assert f'PERSONA = "{pid}"' in rendered
        # bridge npub rendered from the manifest
        assert f'BRIDGE_NPUB = "{manifest["bridge"]["npub"]}"' in rendered
        # no Jinja leftovers
        assert "{{" not in rendered and "{%" not in rendered
        # the join nick is always derived from the PERSONA constant
        assert "--nick ' + PERSONA" in rendered
        # never hardcodes another persona's nick
        for other in ("maria", "juan", "pedro", "lucia"):
            if other != pid:
                assert f"--nick {other}" not in rendered


def test_meeting_join_tool_deployed_to_all_personas(tmp_path: Path) -> None:
    """pm apply installs meeting-join.js into every persona (not gated by
    invite.roles), with the executable bit set."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr

    for pid in ("maria", "juan", "pedro", "lucia"):
        tool = personas / pid / "tools" / "meeting-join.js"
        assert tool.exists(), f"meeting-join.js missing for {pid}"
        mode = tool.stat().st_mode & 0o777
        assert mode == 0o755, f"meeting-join.js not executable for {pid}: {oct(mode)}"


def test_sala_send_tool_renders_persona_identity() -> None:
    """sala-send.js renders per persona: PERSONA + BRIDGE_NPUB from the
    manifest, no Jinja delimiters leak, and the speak content is `[room] text`."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    assert "npub" in manifest["bridge"]
    for pid in ("maria", "juan", "pedro", "lucia"):
        ctx = _persona_context(pid, manifest)
        rendered = _tool_env("es").get_template("sala-send.js.j2").render(**ctx)
        assert f'PERSONA = "{pid}"' in rendered
        assert f'BRIDGE_NPUB = "{manifest["bridge"]["npub"]}"' in rendered
        assert "{{" not in rendered and "{%" not in rendered
        # speak content builder: `[room] text`
        assert "normalizeRoom(room) + ' ' + text" in rendered


def test_sala_send_tool_deployed_to_all_personas(tmp_path: Path) -> None:
    """pm apply installs sala-send.js into every persona (not gated by
    invite.roles), with the executable bit set."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    personas = tmp_path / "personas"
    for pid in ("maria", "juan", "pedro", "lucia"):
        (personas / pid).mkdir(parents=True, exist_ok=True)

    proc = run_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--target",
        str(personas),
        "--invite-roles",
        "maria",
    )
    assert proc.returncode == 0, proc.stderr

    for pid in ("maria", "juan", "pedro", "lucia"):
        tool = personas / pid / "tools" / "sala-send.js"
        assert tool.exists(), f"sala-send.js missing for {pid}"
        mode = tool.stat().st_mode & 0o777
        assert mode == 0o755, f"sala-send.js not executable for {pid}: {oct(mode)}"
