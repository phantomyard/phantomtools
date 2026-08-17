"""Smoke tests: the CLI must derive, validate and apply manifests end to end."""

import os
import subprocess
import sys
from pathlib import Path

import yaml

from phantommeet.apply import _persona_context, _tool_env
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
    # derive rules map ceo/cfo -> responsible, project_lead -> lead (scoped),
    # training_lead -> support (restricted)
    assert "maria=responsible" in proc.stdout
    assert "pedro=lead" in proc.stdout
    assert "lucia=support" in proc.stdout
    # escalation map: support/lead actor -> responsible actor, derived from
    # the org escalation_matrix (training_lead -> ceo -> maria; project_lead
    # -> ceo -> maria).
    manifest = yaml.safe_load(derived.read_text(encoding="utf-8"))
    assert manifest["escalation"] == {"lucia": "maria", "pedro": "maria"}
    # permissions: three tiers — full, scoped (prefix-scoped leads), restricted.
    assert manifest["permissions"] == {
        "full": ["maria", "juan"],
        "scoped": {"example-project": ["pedro"]},
        "restricted": {"formacion": ["lucia"]},
    }
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
    # Communication channel section (phantomforge is the source of truth).
    assert "## Canal de comunicación" in meetings
    assert "phantomforge" in meetings
    # Pre-flight request check section with defaults table.
    assert "## Antes de actuar: comprobación de la solicitud" in meetings
    assert "`18:00`" in meetings
    assert "60` min" in meetings


def test_meetings_md_renders_scoped_responsible_for_lead(tmp_path: Path) -> None:
    """Lead personas render as scoped-responsible in Meetings.md: they
    schedule within their scope and escalate only out-of-scope requests."""
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
    # Scoped responsible: can schedule within scope.
    assert "Eres **responsable de las reuniones online dentro de tu ámbito**" in meetings
    assert "**'example-project-*'**: las agendas con `meeting-invite.sh`" in meetings
    # Out-of-scope escalates to the concrete persona.
    assert "Las reuniones fuera de tu ámbito escalan a **maria**." in meetings
    # Lead must NOT get the support "No agendes" rule.
    assert "**No agendes reuniones online**" not in meetings
    # Lead role label.
    assert "Lead de Proyecto" in meetings
    # Canonical escalation section with @-mention format (out-of-scope only).
    assert "## Escalado de solicitudes de reunión" in meetings
    assert "`@maria <solicitud con los parámetros exactos recibidos>`" in meetings
    assert "Eres responsable de las reuniones online **dentro de tu ámbito**" in meetings
    # Destination is per-scope: the lead answers with their project's folder,
    # not the org-wide recordings folder.
    assert "`example-meetings` (la carpeta de reuniones de tu ámbito en Drive)" in meetings
    assert "`Grabaciones`" not in meetings
    # Custody: the lead uploads her scope's recordings; org custodian is backup.
    assert "**Pedro** sube el MP4" in meetings
    assert "carpeta `example-meetings/`" in meetings
    assert "lo hace **Maria** (custodia de la organización)" in meetings


def test_meeting_invite_schedules_autojoin_per_persona() -> None:
    """meeting-invite.sh must schedule one auto-join task per attending persona
    (with its own nick and its own runtime) and skip humans."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    # KNOWN_PERSONAS comes from the manifest roles (example org).
    assert 'KNOWN_PERSONAS="maria juan pedro lucia"' in rendered

    script = REPO / "_test_invite.sh"
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
                "--password",
                "secreto1",
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
    # One auto-join per attending persona, each with its own nick.
    assert "(persona maria)" in out and "--nick maria" in out
    assert "(persona juan)" in out and "--nick juan" in out
    assert "(persona pedro)" in out and "--nick pedro" in out
    # Password is propagated to each task.
    assert "--password secreto1" in out
    # Human recipients (e.g. @salvador) do NOT get an auto-join task.
    assert "(persona salvador)" not in out
    assert "--nick salvador" not in out
    # Invitation goes to the coordination group with the room link.
    assert "https://meet.example.invalid/2026-08-14-18-00_junta" in out


def test_meeting_invite_resolves_bot_handles_to_personas() -> None:
    """Recipients addressed by Telegram bot handle (e.g. @<bot_handle>) must be
    resolved back to their persona so the auto-join task lands in the right
    runtime (regression: handle suffix never matched KNOWN_PERSONAS)."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    bots = manifest.get("invite", {}).get("telegram_bots", {})
    assert bots, "example manifest should define invite.telegram_bots"
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

    script = REPO / "_test_invite_bot_handles.sh"
    script.write_text(rendered)
    try:
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--title",
                "Example Project Coordinación",
                "--topic",
                "example_project_coordinacion",
                "--datetime",
                "2026-08-11T09:00:00+02:00",
                "--recipients",
                "@President_bot,@CEO_bot,@maria,@ProjectLead_bot,@Unknown_bot",
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
    # Bot handles resolve to their personas and get auto-join tasks.
    # @CEO_bot -> maria; @ProjectLead_bot -> pedro; plain name @maria too.
    assert "(persona maria)" in out and "--nick maria" in out
    assert "(persona pedro)" in out and "--nick pedro" in out
    # Unknown/out-of-map handles and humans get no task.
    assert "(persona salvador)" not in out
    assert "(persona unknown)" not in out
    assert "--nick unknown_bot" not in out


def test_meeting_invite_custom_card_renders_manifest_template() -> None:
    """invite.card overrides the built-in announcement format and its
    %TOKENS% are substituted with runtime values."""
    manifest = yaml.safe_load((EXAMPLES / "example-org.yaml").read_text())
    assert "card" in manifest["invite"], "example manifest should define invite.card"
    ctx = _persona_context("maria", manifest)
    rendered = _tool_env("es").get_template("meeting-invite.sh.j2").render(**ctx)

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
                "--password",
                "clave42",
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
    assert (
        "🔗 https://meet.example.invalid/2026-08-14-18-00_asamblea_general" in out
    )
    assert "🔒 Contraseña: clave42" in out
    assert "%TITLE%" not in out


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
        "Example Org\n"
        "━━━━━━━━━━━━\n"
        "📅 Reunión: %TITLE%\n"
        "🕐 %DATETIME%\n"
        "━━━━━━━━━━━━"
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
