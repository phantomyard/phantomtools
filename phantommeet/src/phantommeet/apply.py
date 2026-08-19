"""Apply the PhantomMeet update package to a PhantomOrg persona installation.

All operations are **idempotent** (safe to re-run) and support ``--dry-run``
to report every change without writing anything.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .manifest import access_for, load_manifest

MARKER_START = "<!-- phantommeet:start -->"
MARKER_END = "<!-- phantommeet:end -->"

# Legacy headers from pre-PhantomMeet manual installs (ad-hoc protocol 1.0).
# PhantomMeet replaces these with its managed section.
LEGACY_SECTION_PATTERNS = [
    re.compile(
        r"^## 📡 Salas Jitsi \(protocolo 1\.0.*$\n(?:(?!^## ).*\n)*", re.MULTILINE
    ),
    re.compile(
        r"^## 📡 Meetings? \(protocolo 1\.0.*$\n(?:(?!^## ).*\n)*", re.MULTILINE
    ),
]

# Files PhantomMeet manages, relative to a persona directory.
KB_REL = Path("kb/procedures/Meetings.md")
MEMORY_REL = Path("MEMORY.md")
PHANTOMCHAT_REL = Path("phantomchat.json")

# Reversible patch bookkeeping: records only the field delta PhantomMeet
# owns in phantomchat.json (the private relay it moves to the front), never a
# frozen snapshot of the whole file. ``pm unapply`` consumes it.
PHANTOMCHAT_DELTA_REL = Path(".phantommeet-phantomchat.delta.json")

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class Change:
    """A single proposed change (written only when not dry-running)."""

    persona: str
    rel_path: Path
    action: str  # write | upsert | patch | skip
    detail: str = ""

    def __str__(self) -> str:
        return (
            f"[{self.action:>7}] {self.persona}/{self.rel_path} {self.detail}".rstrip()
        )


@dataclass
class ApplyResult:
    """Summary of an apply run."""

    changes: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> list[Change]:
        return [c for c in self.changes if c.action == "skip"]

    @property
    def pending(self) -> list[Change]:
        return [c for c in self.changes if c.action != "skip"]


@dataclass
class _PendingWrite:
    """A planned file write, committed only after the whole plan is clean."""

    dest: Path
    content: str
    mode: int | None = None


def _atomic_write(dest: Path, content: str, mode: int | None = None) -> None:
    """Write ``content`` atomically: temp file in the same directory, then
    ``os.replace``. Never writes through a symlink (the preflight already
    refused symlinked destinations)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _env(lang: str) -> Environment:
    # Templates are Markdown/persona files, not HTML: autoescape stays off
    # for all extensions (select_autoescape with an empty enabled set).
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "kb"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_template(env: Environment, name: str, ctx: dict[str, Any]) -> str:
    template = env.get_template(name)
    return template.render(**ctx).rstrip() + "\n"


def _persona_context(persona_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Build the Jinja context for a persona (role-aware)."""
    lang = manifest["language"]
    role = manifest["roles"].get(persona_id)
    access = access_for(persona_id, manifest)

    role_label = {
        "responsible": "Responsible",
        "lead": "Project Lead",
        "support": "Support",
    }[role]
    if lang == "es":
        role_label = {
            "responsible": "Responsable",
            "lead": "Lead de Proyecto",
            "support": "Soporte",
        }[role]

    rooms = manifest["rooms"]

    if access["kind"] == "full":
        permissions_detail = (
            "You have **full access** to all meeting rooms."
            if lang == "en"
            else "Tienes **acceso completo** a todas las salas de reunión."
        )
    elif access["kind"] == "scoped":
        permissions_detail = (
            "You are **responsible for online meetings within your scope** "
            f"**'{access['prefix']}-*'**: you schedule them with "
            "`meeting-invite.sh`. If a meeting request is **outside your scope** "
            "(another project or an org-wide AU meeting), **escalate it to the "
            "responsible persona** with the exact parameters received."
            if lang == "en"
            else "Eres **responsable de las reuniones online dentro de tu ámbito** "
            f"**'{access['prefix']}-*'**: las agendas con `meeting-invite.sh`. "
            "Si una solicitud de reunión es de **fuera de tu ámbito** (otro "
            "proyecto o una reunión general de AU), **escálala a la persona "
            "responsable** con los parámetros exactos recibidos."
        )
    elif access["kind"] == "restricted":
        permissions_detail = (
            "You take part in the rooms you are **invited to** (the invitation "
            "URL is your ticket). Your org scope marker is "
            f"**'{access['prefix']}-*'**. "
            "**Do not schedule online meetings**: if you receive an online-meeting "
            "request, **escalate it to the responsible persona** with the exact "
            "parameters received. Other agenda items (appointments, calendar, "
            "reminders) you handle as usual."
            if lang == "en"
            else "Participas en las salas a las que te **inviten** (la URL de "
            f"invitación es tu ticket). Tu ámbito de organización es "
            f"**'{access['prefix']}-*'**. "
            "**No agendes reuniones online**: si recibes una solicitud de reunión "
            "online, **escálala a la persona responsable** con los parámetros "
            "exactos recibidos. El resto de la agenda (citas, calendario, "
            "recordatorios) la gestionas con normalidad."
        )
    else:
        permissions_detail = (
            "You have **no access** to meeting rooms."
            if lang == "en"
            else "No tienes **acceso** a las salas de reunión."
        )

    # Explicit escalation target for support/lead personas, derived from the
    # org model (see derive.py). When present, the generic "escalate it to the
    # responsible persona" rule names the concrete persona (and handle). For
    # lead personas the escalation applies only to *out-of-scope* requests;
    # the rendered sentence is phrased per tier.
    escalation_target = manifest.get("escalation", {}).get(persona_id)
    if escalation_target:
        handles = manifest.get("invite", {}).get("telegram_bots", {}) or {}
        handle = handles.get(escalation_target)
        mention = f"**{escalation_target}**" + (f" ({handle})" if handle else "")
        if access["kind"] == "scoped":
            escalation_line = (
                f" Meetings outside your scope escalate to {mention}."
                if lang == "en"
                else f" Las reuniones fuera de tu ámbito escalan a {mention}."
            )
        else:
            escalation_line = (
                f" Your escalation contact for online meetings is {mention}."
                if lang == "en"
                else f" Tu responsable de escalado para reuniones online es {mention}."
            )
        permissions_detail += escalation_line

    # Canonical escalation rule text per tier (used by the Meetings.md
    # "Escalation" section). support escalates *every* request; lead (scoped
    # responsible) escalates only out-of-scope ones; responsible never escalates.
    if access["kind"] == "scoped":
        escalation_rule = (
            "You are responsible for online meetings **within your scope**. "
            "Requests **outside your scope** (another project or an org-wide "
            "meeting) are escalated to the responsible persona with the exact "
            "parameters received."
            if lang == "en"
            else "Eres responsable de las reuniones online **dentro de tu ámbito**. "
            "Las solicitudes de **fuera de tu ámbito** (otro proyecto o una "
            "reunión general de la organización) se escalan a la persona "
            "responsable con los parámetros exactos recibidos."
        )
    elif access["kind"] == "restricted":
        escalation_rule = (
            "You do **not** schedule online meetings: if you receive an "
            "online-meeting request, **escalate it to the responsible persona** "
            "with the exact parameters received."
            if lang == "en"
            else "**No agendes reuniones online**: si recibes una solicitud de "
            "reunión online, **escálala a la persona responsable** con los "
            "parámetros exactos recibidos."
        )
    else:
        escalation_rule = (
            "You schedule online meetings for the whole organization."
            if lang == "en"
            else "Agendas las reuniones online de toda la organización."
        )

    # Destination folder is **per scope** (not a global default): a lead
    # (scoped responsible) answers with their own project's meeting folder,
    # not the org-wide recordings folder. The org-wide drive_folder is the
    # default only for full responsibles.
    storage = manifest.get("storage", {}) or {}
    drive_folder = storage.get("drive_folder", "Grabaciones")
    meeting_folders = storage.get("meeting_folders", {}) or {}
    custodian = storage.get("custodian", "") or ""
    if access["kind"] == "scoped":
        destination_folder = meeting_folders.get(access["prefix"], drive_folder)
        destination_note = (
            "your project's meeting folder in Drive"
            if lang == "en"
            else "la carpeta de reuniones de tu ámbito en Drive"
        )
        destination_owner = persona_id.capitalize()
        destination_custodian = custodian.capitalize() if custodian else ""
    elif access["kind"] == "full":
        destination_folder = drive_folder
        destination_note = (
            "the org's recordings folder in Drive"
            if lang == "en"
            else "la carpeta de grabaciones de la organización en Drive"
        )
        destination_owner = custodian.capitalize() if custodian else ""
        destination_custodian = ""
    else:
        destination_folder = ""
        destination_note = ""
        destination_owner = ""
        destination_custodian = ""

    # Build a friendly example room name from the naming convention.
    naming = rooms.get("naming", "{YYYY-MM-DD}-{HH-MM}_{topic}")
    room_example = naming
    for token, value in (
        ("{type}", "project"),
        ("{YYYY-MM-DD}", "2026-08-07"),
        ("{DD-MM-YYYY}", "07-08-2026"),
        ("{HH-MM}", "18-00"),
        ("{YYYY}", "2026"),
        ("{MM}", "08"),
        ("{DD}", "07"),
        ("{topic}", "topic"),
    ):
        room_example = room_example.replace(token, value)
    # Generic fallback for any other brace token, e.g. custom conventions.
    room_example = re.sub(r"\{[^}]*\}", "x", room_example)

    return {
        "org": manifest["org"],
        "version": manifest.get("version", "?"),
        "relay": manifest.get("bridge", {}).get("relay", "?"),
        "name": persona_id,
        "role_label": role_label,
        "access_summary": access["summary"],
        "permissions_detail": permissions_detail,
        "escalation_target": escalation_target or "",
        "escalation_rule": escalation_rule,
        "active_room_required": rooms.get("active_room_required", True),
        "naming": naming,
        "room_example": room_example,
        "language": lang,
        "invite": manifest.get("invite", {}),
        "rooms": rooms,
        "roles": manifest.get("roles", {}),
        "storage": manifest.get("storage", {}),
        "defaults": manifest.get("defaults", {}),
        "destination_folder": destination_folder,
        "destination_note": destination_note,
        "destination_owner": destination_owner,
        "destination_custodian": destination_custodian,
    }


def _render_memory_section(ctx: dict[str, Any], lang: str) -> str:
    # Markdown template (not HTML) — autoescape off, see _env().
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "memory"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    name = "section.en.md" if lang == "en" else "section.es.md"
    # No trailing newline: the caller owns newline placement around the markers,
    # so that replace/insert cycles are byte-identical (idempotent).
    return env.get_template(name).render(**ctx).strip()


def _strip_legacy_memory_sections(text: str) -> str:
    """Remove legacy pre-PhantomMeet meeting sections (ad-hoc protocol 1.0).

    These were inserted manually before PhantomMeet existed and have no
    markers. PhantomMeet replaces them with its managed section.
    """
    for pattern in LEGACY_SECTION_PATTERNS:
        text = pattern.sub("", text)
    # Collapse 3+ consecutive blank lines into 2.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _upsert_between_markers(text: str, section: str) -> str:
    """Replace the region between the markers, or insert the section at the top.

    Also strips legacy pre-PhantomMeet meeting sections (no markers) so the
    managed section is the single source of truth.
    """
    text = _strip_legacy_memory_sections(text)
    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.DOTALL
    )
    if MARKER_START in text and MARKER_END in text:
        return pattern.sub(section, text, count=1)
    # Insert after the first line starting with '#' (title), else at the very top.
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return (
                "".join(lines[: i + 1])
                + "\n"
                + section
                + "\n"
                + "".join(lines[i + 1 :])
            )
    return section + "\n" + text


def _render_kb_frontmatter(ctx: dict[str, Any], lang: str) -> str:
    """OKF frontmatter for Meetings.md (regenerated every apply).

    ``type`` is OKF's only required field; ``title`` / ``tags`` / ``aliases``
    are the high-weight BM25F fields, so a note without them only ever
    competes at body weight.
    """
    org = ctx["org"]
    org_slug = re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-")
    today = datetime.now(timezone.utc).date().isoformat()
    if lang == "es":
        return (
            "---\n"
            "type: procedure\n"
            f"title: Protocolo de reuniones — {org}\n"
            "description: Cómo esta persona se une, participa y registra "
            "reuniones vía el puente de PhantomMeet.\n"
            f"tags: [reuniones, phantommeet, {org_slug}]\n"
            "aliases: [protocolo de reuniones, cómo entrar a una reunión, "
            "grabaciones, formato DM del puente, invitación a reunión]\n"
            f"created: {today}\n"
            f"updated: {today}\n"
            "---\n"
        )
    return (
        "---\n"
        "type: procedure\n"
        f"title: Meeting protocol — {org}\n"
        "description: How this persona joins, participates in, and records "
        "meetings via the PhantomMeet bridge.\n"
        f"tags: [meetings, phantommeet, {org_slug}]\n"
        "aliases: [meeting protocol, how to join a meeting, grabaciones, "
        "bridge DM format, meeting invite]\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "---\n"
    )


def _supersede_legacy_kb(text: str) -> str:
    """Return ``text`` with a superseded banner inserted so the note's OKF
    frontmatter (if any) stays at the very top and keeps parsing as
    frontmatter. The banner goes immediately after any leading YAML
    frontmatter block; without frontmatter it is simply prepended."""
    banner = "> Superseded by [[procedures/Meetings]]\n"
    if text.startswith("> Superseded by [[procedures/Meetings]]"):
        return text
    rest, frontmatter = _split_frontmatter(text)
    if frontmatter:
        return frontmatter + banner + "\n" + rest.lstrip("\n")
    return banner + "\n" + text


def _contained_path(persona_dir: Path, rel: str | Path) -> Path | None:
    """Resolve ``rel`` inside ``persona_dir``; None if it escapes the dir.

    Manifest-controlled paths (``legacy_kb_files``, tool ``dest``) are
    untrusted: resolve them and refuse anything that leaves the persona
    directory (path traversal).
    """
    base = persona_dir.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _contained_dest(persona_dir: Path, rel: str | Path) -> Path | None:
    """Resolve a manifest-controlled destination strictly inside the persona
    directory, refusing path traversal AND symlinks in the resolved path.

    ``resolve()`` follows symlinks, so a symlinked directory inside the
    persona could silently redirect a write outside it; the containment check
    alone would not catch that. Returns None for any escape or symlink.
    """
    contained = _contained_path(persona_dir, rel)
    if contained is None:
        return None
    # Every component from the persona dir down to the target must not be a
    # symlink (the persona dir itself may legitimately be a symlink, e.g. a
    # deploy-managed mount point).
    base = persona_dir.resolve()
    cursor = base
    for part in contained.relative_to(base).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    return contained


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a leading YAML frontmatter block (``---`` ... ``---``) from the
    rest. Returns ``(rest_without_frontmatter, frontmatter_block)``.

    ``frontmatter_block`` is ``""`` when the text does not start with a
    frontmatter block. This is used to keep operator content that sits
    between the frontmatter and the managed markers intact across applies.
    """
    if not text.startswith("---"):
        return text, ""
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening "---"; scan for the closing "---" line.
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :]), "".join(lines[: i + 1])
    # No closing "---": not a frontmatter block.
    return text, ""


def _upsert_kb(existing: str, frontmatter: str, body: str) -> str:
    """Upsert the managed Meetings.md: frontmatter + marker-delimited body.

    The OKF frontmatter sits at the very top (it must be first for the
    indexer); the generated body lives between the phantommeet markers.
    Anything the persona added is preserved: operator content between the
    frontmatter and the start marker (prefix) AND after the end marker
    (suffix) survive a re-apply.
    """
    managed_body = f"{MARKER_START}\n{body}{MARKER_END}\n"
    if MARKER_START in existing and MARKER_END in existing:
        head = existing.split(MARKER_START, 1)[0]
        tail = existing.split(MARKER_END, 1)[1]
        # head = frontmatter + any operator content before the block.
        operator_prefix, _ = _split_frontmatter(head)
        return frontmatter + operator_prefix + managed_body + tail
    # No markers yet: preserve the whole pre-existing file below the block.
    if existing.strip():
        return frontmatter + managed_body + "\n" + existing
    return frontmatter + managed_body


def _patch_phantomchat(
    data: dict[str, Any], relay: str, bridge_npub: str | None, include_bridge: bool
) -> tuple[dict[str, Any], str | None]:
    """Ensure the private relay is first. Returns ``(patched, relay_added)``
    where ``relay_added`` is the relay string when PhantomMeet *added* it to
    the list (it was not present before) — that is the owned delta — or None
    when it was already present (mere reorder) or empty.

    The bridge npub is NOT added to ``allowed_npubs``: that is a trust grant
    in phantombot (allowlisted senders skip the threat judge), not a delivery
    ACL. Until phantombot ships the untrusted ``relay_npubs`` tier
    (phantomyard/phantombot#400), PhantomMeet fails closed and never mutates
    ``allowed_npubs``.
    """
    relays = list(data.get("relays", []))
    relay_added: str | None = None
    if relay and relay not in relays:
        relay_added = relay
    if relay and relay in relays:
        relays.remove(relay)
    if relay:
        relays.insert(0, relay)
    data["relays"] = relays

    # Deliberately unused: named so call sites and the meaning stay explicit.
    _ = (bridge_npub, include_bridge)

    return data, relay_added


def _personas_in_manifest(manifest: dict[str, Any]) -> list[str]:
    """Personas that receive an update: those with a role or a permission."""
    perms = manifest["permissions"]
    ids = set(manifest["roles"]) | set(perms.get("full", []))
    for prefix_ids in perms.get("scoped", {}).values():
        ids.update(prefix_ids)
    for prefix_ids in perms.get("restricted", {}).values():
        ids.update(prefix_ids)
    return sorted(ids)


def _shquote(value: Any) -> str:
    """Jinja filter: shell-quote a scalar for safe embedding in bash source.

    Produces a single-quoted string via ``shlex.quote`` so a manifest value
    can never break out of an assignment and execute (render-time shell
    injection)."""
    return shlex.quote(str(value))


def _tool_env(lang: str) -> Environment:
    """Jinja environment for the tools templates (templates/tools/)."""
    # Shell-script template (not HTML) — autoescape off, see _env().
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "tools"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["shquote"] = _shquote
    return env


def install_tools(
    manifest: dict[str, Any],
    persona_dir: Path,
    persona_id: str,
) -> tuple[list[Change], list[_PendingWrite]]:
    """Plan the installation of the manifest-declared tools into a persona
    directory. Returns ``(changes, pending_writes)``; nothing is written here
    (the caller commits all writes together, atomically, after the whole
    plan is clean).

    Tools come from ``manifest['tools']`` (a list of tool specs) and/or the
    ``invite.tool`` spec (the meeting invitation script). Each spec is:

    .. code-block:: yaml

       - name: meeting-invite
         template: tools/meeting-invite.sh.j2   # optional: render a template
         dest: tools/meeting-invite.sh          # required: destination (relative to persona dir)
         chmod: 0o755                           # optional: permissions

    A tool spec may also be a plain ``str`` (destination path) for static
    files shipped with the package. ``invite.roles`` decides which personas
    get the meeting-invite tool; everyone else does not.
    """
    changes: list[Change] = []
    writes: list[_PendingWrite] = []
    lang = manifest["language"]
    tool_specs = list(manifest.get("tools", []) or [])

    invite = manifest.get("invite", {}) or {}
    if invite.get("tool"):
        # Only personas granted scheduling may hold the invite tool.
        invite_roles = set(invite.get("roles", []) or [])
        if persona_id in invite_roles:
            tool_specs.append(invite["tool"])

    if not tool_specs:
        return changes, writes

    tool_env = _tool_env(lang)
    for spec in tool_specs:
        if isinstance(spec, str):
            dest = Path(spec)
            template_name = None
        else:
            dest = Path(spec["dest"])
            template_name = spec.get("template")

        # Manifest-controlled destination: resolve strictly inside the persona
        # directory (no path traversal, no symlinked components). This check
        # runs FIRST, before any content is read or rendered, so a traversing
        # destination can never touch the filesystem outside the persona.
        dest_abs = _contained_dest(persona_dir, dest)
        if dest_abs is None:
            changes.append(
                Change(
                    persona_id,
                    dest,
                    "error",
                    "destination escapes the persona directory",
                )
            )
            continue

        if template_name:
            # The loader is rooted at templates/tools/ — strip a leading
            # "tools/" prefix so manifest specs can use either form.
            template_name = template_name.removeprefix("tools/")
            try:
                content = _render_template(
                    tool_env, template_name, _persona_context(persona_id, manifest)
                )
            except Exception as exc:  # noqa: BLE001
                changes.append(
                    Change(
                        persona_id, dest, "error", f"template {template_name!r}: {exc}"
                    )
                )
                continue
        else:
            pkg_tool = TEMPLATES_DIR / "tools" / dest.name
            if not pkg_tool.exists():
                changes.append(
                    Change(
                        persona_id, dest, "error", f"static tool not found: {pkg_tool}"
                    )
                )
                continue
            content = pkg_tool.read_text(encoding="utf-8")

        mode = spec.get("chmod") if isinstance(spec, dict) else None
        if isinstance(mode, str):
            # YAML reads 0o755 as a string; parse octal / decimal / symbolic.
            try:
                mode = int(mode, 8) if mode.startswith("0o") else int(mode, 0)
            except ValueError:
                mode = None

        needs_chmod = False
        if dest_abs.exists():
            try:
                current_mode = dest_abs.stat().st_mode & 0o777
            except OSError:
                current_mode = None
            needs_chmod = mode is not None and current_mode != mode

        if (
            dest_abs.exists()
            and dest_abs.read_text(encoding="utf-8") == content
            and not needs_chmod
        ):
            changes.append(Change(persona_id, dest, "skip", "(up to date)"))
            continue

        changes.append(Change(persona_id, dest, "write"))
        writes.append(_PendingWrite(dest_abs, content, mode))

    return changes, writes


def apply_manifest(
    manifest_path: str | Path,
    target: str | Path,
    dry_run: bool = False,
    verbose: bool = False,
    invite_roles: list[str] | None = None,
    ask_roles: bool = False,
    card_file: str | None = None,
    ask_card: bool = False,
) -> ApplyResult:
    """Apply the manifest to ``target`` (root of the persona installation).

    ``card_file`` reads the announcement card from a file (overrides
    ``invite.card`` for this run only, without persisting). ``ask_card``
    interactively asks the operator for the card and persists the choice
    back into the manifest (like ``ask_roles``).
    """
    result = ApplyResult()
    manifest = load_manifest(manifest_path)
    root = Path(target)
    if not root.is_dir():
        result.errors.append(f"target is not a directory: {root}")
        return result

    lang = manifest["language"]
    bridge = manifest.get("bridge", {})
    relay = bridge.get("relay", "")
    bridge_npub = bridge.get("npub", "")
    env = _env(lang)

    # --- interactive role decision (human decides who may schedule) ---------
    invite = manifest.get("invite", {}) or {}
    if invite.get("tool"):
        if invite_roles is not None:
            # One-shot flag: grant these roles for this run only (not persisted).
            manifest["invite"]["roles"] = invite_roles
        elif ask_roles or "roles" not in invite:
            from .discovery import discover, prompt_for_roles

            discovery = discover(root)
            existing = invite.get("roles", []) or []
            if ask_roles or not existing:
                chosen = prompt_for_roles(discovery, existing=existing)
                if not chosen:
                    result.errors.append("no invite.roles selected; aborting")
                    return result
                manifest["invite"]["roles"] = chosen
                # Persist the decision back into the manifest file.
                if not dry_run:
                    _persist_invite_roles(manifest_path, chosen)
                result.changes.append(
                    Change("*", Path("invite.roles"), "patch", f"{', '.join(chosen)}")
                )
            else:
                result.changes.append(
                    Change("*", Path("invite.roles"), "skip", "(already set)")
                )

        # --- announcement card configuration --------------------------------
        if card_file is not None:
            # One-shot flag: read the card from a file (not persisted).
            try:
                card_text = Path(card_file).read_text(encoding="utf-8").strip("\n")
            except OSError as exc:
                result.errors.append(f"cannot read card file: {exc}")
                return result
            from .manifest import REQUIRED_CARD_TOKENS

            missing = [t for t in REQUIRED_CARD_TOKENS if t not in card_text]
            if missing:
                result.errors.append(
                    "invite.card (from file) is missing mandatory token(s): "
                    + ", ".join(missing)
                    + f" (mandatory: {', '.join(REQUIRED_CARD_TOKENS)})"
                )
                return result
            manifest["invite"]["card"] = card_text
        elif ask_card:
            from .discovery import prompt_for_card

            existing = invite.get("card")
            chosen = prompt_for_card(existing=existing)
            if chosen is None and existing is None:
                # Operator aborted the prompt (Ctrl-C).
                result.errors.append("card prompt aborted; aborting apply")
                return result
            if chosen is None:
                # 'clear' -> remove the custom card (built-in format).
                manifest["invite"].pop("card", None)
            else:
                manifest["invite"]["card"] = chosen
            # Re-validate the chosen card (mandatory tokens) before writing.
            from .manifest import REQUIRED_CARD_TOKENS

            if chosen is not None:
                missing = [t for t in REQUIRED_CARD_TOKENS if t not in chosen]
                if missing:
                    result.errors.append(
                        "invite.card is missing mandatory token(s): "
                        + ", ".join(missing)
                        + f" (mandatory: {', '.join(REQUIRED_CARD_TOKENS)}); "
                        "apply aborted"
                    )
                    return result
            if not dry_run:
                _persist_invite_card(manifest_path, chosen)
            result.changes.append(
                Change(
                    "*",
                    Path("invite.card"),
                    "patch" if chosen is not None else "remove",
                    "(custom card)" if chosen is not None else "(built-in format)",
                )
            )

    # ------------------------------------------------------------------
    # Phase 1 — plan: compute every proposed change and validate it without
    # mutating anything. A single plan error aborts before the first write
    # (no partial apply).
    # ------------------------------------------------------------------
    pending_writes: list[_PendingWrite] = []
    phantomchat_deltas: dict[str, str | None] = {}

    for persona_id in _personas_in_manifest(manifest):
        persona_dir = root / persona_id
        if not persona_dir.is_dir():
            result.errors.append(f"persona directory not found: {persona_dir}")
            continue

        ctx = _persona_context(persona_id, manifest)

        # 1) KB protocol file: OKF frontmatter + marker-delimited managed body.
        kb_name = "protocol.en.md" if lang == "en" else "protocol.es.md"
        try:
            kb_body = _render_template(env, kb_name, ctx)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{persona_id}: template {kb_name!r} failed: {exc}")
            continue
        appendix = manifest.get("kb_appendix", [])
        if appendix:
            # kb_appendix blocks are Jinja templates too: org-specific text in
            # base.yaml can use ctx tokens (e.g. destination_folder/owner) so
            # the appendix renders per-role, not as static text.
            try:
                rendered = [env.from_string(a).render(**ctx).rstrip() for a in appendix]
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{persona_id}: kb_appendix render failed: {exc}")
                continue
            kb_body += "\n---\n\n" + "\n\n---\n\n".join(rendered) + "\n"
        frontmatter = _render_kb_frontmatter(ctx, lang)
        kb_dest = persona_dir / KB_REL
        kb_existing = kb_dest.read_text(encoding="utf-8") if kb_dest.exists() else ""
        kb_new = _upsert_kb(kb_existing, frontmatter, kb_body)
        if kb_new == kb_existing:
            result.changes.append(Change(persona_id, KB_REL, "skip", "(up to date)"))
        else:
            result.changes.append(Change(persona_id, KB_REL, "upsert"))
            pending_writes.append(_PendingWrite(kb_dest, kb_new))

        # 2) Legacy kb files superseded by Meetings.md are deprecated in
        # place (a banner after any leading frontmatter) — never deleted.
        # KB recall walks [[wikilinks]] outward, so deleting a note severs
        # every path that ran through it and leaves dangling links elsewhere.
        for legacy in manifest.get("legacy_kb_files", []):
            legacy_dest = _contained_path(persona_dir, legacy)
            if legacy_dest is None:
                result.errors.append(
                    f"{persona_id}/{legacy}: path escapes the persona directory"
                )
                continue
            if legacy_dest.exists():
                text = legacy_dest.read_text(encoding="utf-8")
                if text.startswith("> Superseded by [[procedures/Meetings]]"):
                    result.changes.append(
                        Change(persona_id, Path(legacy), "skip", "(already superseded)")
                    )
                else:
                    result.changes.append(
                        Change(
                            persona_id,
                            Path(legacy),
                            "deprecate",
                            "(superseded by Meetings.md)",
                        )
                    )
                    pending_writes.append(
                        _PendingWrite(legacy_dest, _supersede_legacy_kb(text))
                    )

        # 3) MEMORY.md section (idempotent upsert between markers).
        memory_dest = persona_dir / MEMORY_REL
        memory_text = (
            memory_dest.read_text(encoding="utf-8") if memory_dest.exists() else ""
        )
        section = _render_memory_section(ctx, lang)
        new_memory = _upsert_between_markers(memory_text, section)
        if new_memory == memory_text:
            result.changes.append(
                Change(persona_id, MEMORY_REL, "skip", "(up to date)")
            )
        else:
            result.changes.append(Change(persona_id, MEMORY_REL, "upsert"))
            pending_writes.append(_PendingWrite(memory_dest, new_memory))

        # 4) phantomchat.json patch (reversible via owned-field delta).
        pc_dest = persona_dir / PHANTOMCHAT_REL
        include_bridge = access_for(persona_id, manifest)["kind"] != "none"
        if pc_dest.exists():
            original_text = pc_dest.read_text(encoding="utf-8")
            try:
                pc_data = json.loads(original_text)
            except json.JSONDecodeError as exc:
                # Preflight: the JSON error is recorded and the whole apply
                # aborts BEFORE anything (KB/MEMORY.md/tools) is written.
                result.errors.append(
                    f"{persona_id}/phantomchat.json: invalid JSON ({exc})"
                )
                continue
            patched, relay_added = _patch_phantomchat(
                pc_data, relay, bridge_npub, include_bridge
            )
            if patched == json.loads(original_text):
                result.changes.append(
                    Change(persona_id, PHANTOMCHAT_REL, "skip", "(up to date)")
                )
            else:
                result.changes.append(Change(persona_id, PHANTOMCHAT_REL, "patch"))
                phantomchat_deltas[persona_id] = relay_added
                pending_writes.append(
                    _PendingWrite(
                        pc_dest,
                        json.dumps(patched, indent=2, ensure_ascii=False) + "\n",
                    )
                )
        else:
            result.changes.append(
                Change(persona_id, PHANTOMCHAT_REL, "skip", "(absent)")
            )

        # 5) Manifest-declared tools (e.g. the meeting-invite script).
        tool_changes, tool_writes = install_tools(manifest, persona_dir, persona_id)
        for change in tool_changes:
            if change.action == "error":
                result.errors.append(
                    f"{persona_id}: tool {change.rel_path}: {change.detail}"
                )
        result.changes.extend(tool_changes)
        pending_writes.extend(tool_writes)

    # ------------------------------------------------------------------
    # Phase 2 — commit. Any preflight error aborts before the first write.
    # ------------------------------------------------------------------
    if result.errors:
        return result
    if not dry_run:
        for w in pending_writes:
            _atomic_write(w.dest, w.content, w.mode)
        # Record the owned phantomchat delta only for personas whose file was
        # actually patched. A delta records ONLY the field we own (the relay we
        # added), never a frozen snapshot of unrelated operator config.
        for persona_id, relay_added in phantomchat_deltas.items():
            delta_dest = root / persona_id / PHANTOMCHAT_DELTA_REL
            if relay_added is not None:
                _atomic_write(
                    delta_dest,
                    json.dumps({"relay_added": relay_added}, indent=2) + "\n",
                )
            else:
                # No owned delta (mere reorder of an existing relay): drop any
                # stale delta so `pm unapply` has nothing stale to reverse.
                delta_dest.unlink(missing_ok=True)

    return result


def unapply_manifest(manifest_path: str | Path, target: str | Path) -> ApplyResult:
    """Reverse PhantomMeet's owned changes for every persona in the manifest.

    Only PhantomMeet-owned state is touched, and only by reversing recorded
    deltas — never by replacing unrelated current configuration:

    - ``phantomchat.json``: remove the relay recorded in the owned delta
      (``.phantommeet-phantomchat.delta.json``), leaving every other relay and
      all other fields exactly as they are.
    - ``kb/procedures/Meetings.md``: strip the managed marker block, keeping
      the OKF frontmatter and any operator content around it.
    - ``MEMORY.md``: strip the managed marker section, keeping the rest.

    Idempotent: running it twice is a no-op on the second pass.
    """
    result = ApplyResult()
    manifest = load_manifest(manifest_path)
    root = Path(target)
    if not root.is_dir():
        result.errors.append(f"target is not a directory: {root}")
        return result

    for persona_id in _personas_in_manifest(manifest):
        persona_dir = root / persona_id
        if not persona_dir.is_dir():
            continue

        # 1) phantomchat.json — reverse the owned relay delta.
        pc_dest = persona_dir / PHANTOMCHAT_REL
        delta_dest = persona_dir / PHANTOMCHAT_DELTA_REL
        if delta_dest.exists() and pc_dest.exists():
            try:
                delta = json.loads(delta_dest.read_text(encoding="utf-8"))
                relay_added = delta.get("relay_added")
            except (OSError, ValueError):
                relay_added = None
            if relay_added:
                try:
                    data = json.loads(pc_dest.read_text(encoding="utf-8"))
                except ValueError as exc:
                    result.errors.append(
                        f"{persona_id}/phantomchat.json: invalid JSON ({exc})"
                    )
                else:
                    relays = [r for r in data.get("relays", []) if r != relay_added]
                    data["relays"] = relays
                    result.changes.append(
                        Change(
                            persona_id,
                            PHANTOMCHAT_REL,
                            "patch",
                            f"removed relay {relay_added!r}",
                        )
                    )
                    _atomic_write(
                        pc_dest,
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    )
            delta_dest.unlink(missing_ok=True)
            result.changes.append(
                Change(persona_id, PHANTOMCHAT_DELTA_REL, "remove", "(delta)")
            )

        # 2) Meetings.md — strip the managed block (keep frontmatter + rest).
        kb_dest = persona_dir / KB_REL
        if kb_dest.exists():
            text = kb_dest.read_text(encoding="utf-8")
            if MARKER_START in text and MARKER_END in text:
                head = text.split(MARKER_START, 1)[0]
                tail = text.split(MARKER_END, 1)[1]
                stripped = head.rstrip() + "\n" + tail.lstrip("\n")
                if stripped != text:
                    _atomic_write(kb_dest, stripped)
                    result.changes.append(
                        Change(persona_id, KB_REL, "remove", "(managed block)")
                    )
                else:
                    result.changes.append(
                        Change(persona_id, KB_REL, "skip", "(already stripped)")
                    )

        # 3) MEMORY.md — strip the managed section.
        memory_dest = persona_dir / MEMORY_REL
        if memory_dest.exists():
            text = memory_dest.read_text(encoding="utf-8")
            if MARKER_START in text and MARKER_END in text:
                pattern = re.compile(
                    re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END) + r"\n?",
                    re.DOTALL,
                )
                stripped = pattern.sub("", text, count=1)
                stripped = re.sub(r"\n{3,}", "\n\n", stripped)
                if stripped != text:
                    _atomic_write(memory_dest, stripped)
                    result.changes.append(
                        Change(persona_id, MEMORY_REL, "remove", "(managed section)")
                    )
                else:
                    result.changes.append(
                        Change(persona_id, MEMORY_REL, "skip", "(already stripped)")
                    )

    return result


def _persist_invite_card(manifest_path: str | Path, card: str | None) -> None:
    """Write the chosen ``invite.card`` back into the manifest file.

    ``None`` removes the field (built-in format). Preserves comments and
    formatting via ruamel.yaml when available; falls back to a plain YAML
    round-trip otherwise.
    """
    p = Path(manifest_path)
    try:
        from ruamel.yaml import YAML

        yaml_obj = YAML()
        data = yaml_obj.load(p.read_text(encoding="utf-8"))
        invite = data.setdefault("invite", {})
        if card is None:
            invite.pop("card", None)
        else:
            invite["card"] = card
        with p.open("w", encoding="utf-8") as fh:
            yaml_obj.dump(data, fh)
        return
    except ImportError:
        pass
    # ruamel.yaml is optional; on any failure fall back to PyYAML.
    except Exception:  # noqa: BLE001, S110  # nosec B110
        pass

    import yaml as pyyaml

    data = pyyaml.safe_load(p.read_text(encoding="utf-8"))
    invite = data.setdefault("invite", {})
    if card is None:
        invite.pop("card", None)
    else:
        invite["card"] = card
    p.write_text(
        pyyaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _persist_invite_roles(manifest_path: str | Path, roles: list[str]) -> None:
    """Write the chosen ``invite.roles`` back into the manifest file.

    Preserves comments and formatting via ruamel.yaml when available;
    falls back to a plain YAML round-trip otherwise.
    """
    p = Path(manifest_path)
    try:
        from ruamel.yaml import YAML

        yaml_obj = YAML()
        data = yaml_obj.load(p.read_text(encoding="utf-8"))
        data.setdefault("invite", {})["roles"] = roles
        with p.open("w", encoding="utf-8") as fh:
            yaml_obj.dump(data, fh)
        return
    except ImportError:
        pass
    # ruamel.yaml is optional; on any failure fall back to PyYAML.
    except Exception:  # noqa: BLE001, S110  # nosec B110
        pass

    import yaml as pyyaml

    data = pyyaml.safe_load(p.read_text(encoding="utf-8"))
    data.setdefault("invite", {})["roles"] = roles
    p.write_text(
        pyyaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
