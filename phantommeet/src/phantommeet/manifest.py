"""Manifest loading and validation.

The manifest is the single source of truth for any organization-specific
value. PhantomMeet contains no hardcoded organization data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ManifestError(Exception):
    """Raised when a manifest is missing or invalid."""


REQUIRED_FIELDS = ("org", "language", "permissions", "roles")

# Mandatory tokens in invite.card (the announcement card). Without these the
# invitation is unusable: what meeting (title), when (datetime) and where
# (link). The remaining tokens (%RECIPIENTS%, %ROOM%, %PASSWORD_LINE%) are
# optional and up to the installer's branding.
REQUIRED_CARD_TOKENS = ("%TITLE%", "%DATETIME%", "%LINK%")
VALID_LANGUAGES = ("en", "es")
VALID_ROLES = ("responsible", "lead", "support")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a PhantomMeet manifest."""
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"manifest not found: {p}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"manifest must be a YAML mapping: {p}")

    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ManifestError(f"manifest missing required field(s): {', '.join(missing)}")

    lang = raw["language"]
    if lang not in VALID_LANGUAGES:
        raise ManifestError(f"language must be one of {VALID_LANGUAGES}, got: {lang!r}")

    perms = raw["permissions"]
    if not isinstance(perms, dict) or "full" not in perms:
        raise ManifestError("permissions.full (list of persona ids) is required")

    roles = raw["roles"]
    if not isinstance(roles, dict):
        raise ManifestError("roles must be a mapping persona_id -> role")
    for pid, role in roles.items():
        if role not in VALID_ROLES:
            raise ManifestError(
                f"invalid role {role!r} for persona {pid!r}; "
                f"expected one of {VALID_ROLES}"
            )

    # Normalize defaults so the rest of the code can rely on their shape.
    raw.setdefault("version", "0.0.0")
    raw.setdefault("bridge", {})
    raw.setdefault("rooms", {})
    raw["rooms"].setdefault("suffix", "@conference.example.org")
    raw["rooms"].setdefault("naming", "{YYYY-MM-DD}-{HH-MM}_{topic}")
    raw["rooms"].setdefault("active_room_required", True)
    perms.setdefault("restricted", {})
    perms.setdefault("scoped", {})
    raw.setdefault(
        "storage", {"decided_by": "responsible", "cleanup_after_confirm": True}
    )
    # Recordings: the server-side directory where jibri drops recordings
    # automatically (fixed per deployment), and the default destination
    # folder in Drive where recordings are uploaded (default of the
    # "Destino" variable — the responsible can override it per meeting).
    # A config default (operator-overridable), not an in-process temp file.
    raw["storage"].setdefault("recordings_dir", "/tmp/phantommeet-recordings")  # nosec B108
    raw["storage"].setdefault("drive_folder", "Grabaciones")
    # Persona that performs Drive custody for org-wide meetings (fallback for
    # scoped leads is the same custodian). Empty means "org responsible".
    raw["storage"].setdefault("custodian", "")
    # Per-scope meeting folders: a lead answers with their own project's
    # folder (e.g. example-project -> example-meetings), not the org-wide one.
    raw["storage"].setdefault("meeting_folders", {})
    if not isinstance(raw["storage"]["meeting_folders"], dict):
        raise ManifestError("storage.meeting_folders must be a mapping")
    # Defaults for the meeting-request variables (used when a human request
    # omits a variable — preformatted/normalized before acting).
    raw.setdefault(
        "defaults",
        {
            "title": "reunion",
            "time": "18:00",
            "duration_min": 60,
            "recipients": [],
            "sensitive": False,
        },
    )
    if not isinstance(raw["defaults"], dict):
        raise ManifestError("defaults must be a mapping")
    raw["defaults"].setdefault("title", "reunion")
    raw["defaults"].setdefault("time", "18:00")
    raw["defaults"].setdefault("duration_min", 60)
    raw["defaults"].setdefault("recipients", [])
    raw["defaults"].setdefault("sensitive", False)
    validate_infra_section(raw)

    # Org-specific knowledge appended to the rendered protocol, and legacy
    # kb files superseded by Meetings.md (removed after a successful write).
    raw.setdefault("kb_appendix", [])
    if isinstance(raw["kb_appendix"], str):
        raw["kb_appendix"] = [raw["kb_appendix"]]
    if not isinstance(raw["kb_appendix"], list) or not all(
        isinstance(x, str) for x in raw["kb_appendix"]
    ):
        raise ManifestError("kb_appendix must be a list of markdown strings")
    raw.setdefault("legacy_kb_files", [])

    # Every persona mentioned in roles or permissions must have a role.
    mentioned = set(roles) | set(perms.get("full", []))
    for prefix_ids in perms.get("restricted", {}).values():
        mentioned.update(prefix_ids)
    for prefix_ids in perms.get("scoped", {}).values():
        mentioned.update(prefix_ids)
    for pid in mentioned:
        if pid not in roles:
            raise ManifestError(f"persona {pid!r} is in permissions but has no role")

    normalize_invite_section(raw)
    normalize_tools_section(raw)

    return raw


def normalize_invite_section(raw: dict[str, Any]) -> None:
    """Normalize/validate the optional ``invite`` section.

    ``invite`` configures the meeting invitation tool:

    .. code-block:: yaml

       invite:
         phantombot_bin: phantombot       # binary on PATH (or absolute)
         meet_base_url: https://meet.<domain>
         send_via: phantombot-notify      # mechanism (agnostic)
         tool:                            # tool spec (see apply.install_tools)
           template: tools/meeting-invite.sh.j2
           dest: tools/meeting-invite.sh
           chmod: 0o755
         roles: [<persona_id>, ...]       # decided interactively at install

    ``roles`` is *not* required here: it is decided by the human operator
    at apply time (``--ask-roles``) and persisted back into the manifest.
    """
    invite = raw.get("invite")
    if invite is None:
        return
    if not isinstance(invite, dict):
        raise ManifestError("invite must be a mapping")
    invite.setdefault("send_via", "phantombot-notify")
    if invite.get("send_via") not in ("phantombot-notify", "telegram-api", "manual"):
        raise ManifestError(
            "invite.send_via must be one of phantombot-notify|telegram-api|manual, "
            f"got: {invite.get('send_via')!r}"
        )
    if "roles" in invite:
        roles = invite["roles"]
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise ManifestError("invite.roles must be a list of persona ids")
        for pid in roles:
            if pid not in raw["roles"]:
                raise ManifestError(
                    f"invite.roles references persona {pid!r} with no role"
                )
    if "card" in invite:
        if not isinstance(invite["card"], str):
            raise ManifestError(
                "invite.card must be a string (announcement card template)"
            )
        missing = [t for t in REQUIRED_CARD_TOKENS if t not in invite["card"]]
        if missing:
            raise ManifestError(
                "invite.card is missing mandatory token(s): "
                + ", ".join(missing)
                + f" (mandatory: {', '.join(REQUIRED_CARD_TOKENS)})"
            )
    if "telegram_bots" in invite:
        bots = invite["telegram_bots"]
        if not isinstance(bots, dict):
            raise ManifestError("invite.telegram_bots must be a mapping")
        for pid, handle in bots.items():
            if pid not in raw["roles"]:
                raise ManifestError(
                    f"invite.telegram_bots references persona {pid!r} with no role"
                )
            if not isinstance(handle, str) or not handle.startswith("@"):
                raise ManifestError(
                    f"invite.telegram_bots[{pid!r}] must be a @handle string"
                )


def normalize_tools_section(raw: dict[str, Any]) -> None:
    """Normalize/validate the optional ``tools`` section.

    ``tools`` is a list of additional tool specs (see apply.install_tools)
    installed into *every* persona in the manifest. The meeting-invite tool
    itself is declared under ``invite.tool`` and only installed to
    ``invite.roles``.
    """
    tools = raw.get("tools")
    if tools is None:
        raw["tools"] = []
        return
    if not isinstance(tools, list):
        raise ManifestError("tools must be a list")
    for spec in tools:
        if isinstance(spec, str):
            continue
        if not isinstance(spec, dict) or not spec.get("dest"):
            raise ManifestError(
                "tools entries must be a destination string or a mapping with 'dest'"
            )
        if "template" in spec and not isinstance(spec["template"], str):
            raise ManifestError(
                f"tools[{spec.get('dest')!r}].template must be a string"
            )


def validate_infra_section(raw: dict[str, Any]) -> None:
    """Validate the optional ``infra`` section of a manifest."""
    infra = raw.get("infra")
    if infra is None:
        return
    if not isinstance(infra, dict):
        raise ManifestError("infra must be a mapping")

    for key in ("checks", "persona_checks"):
        if key in infra and not isinstance(infra[key], list):
            raise ManifestError(f"infra.{key} must be a list")

    for check in infra.get("checks", []) or []:
        if not isinstance(check, dict):
            raise ManifestError("infra.checks entries must be mappings")
        name = check.get("name")
        ctype = check.get("type")
        if not name or not isinstance(name, str):
            raise ManifestError("infra.checks entries need a string 'name'")
        if ctype not in ("http", "ws", "command", "file", "env"):
            raise ManifestError(
                f"infra.checks[{name!r}]: type must be http|ws|command|file|env"
            )
        if ctype in ("http", "ws") and not check.get("url"):
            raise ManifestError(f"infra.checks[{name!r}]: '{ctype}' probe needs 'url'")
        if ctype == "command" and not check.get("cmd"):
            raise ManifestError(f"infra.checks[{name!r}]: 'command' probe needs 'cmd'")
        if ctype == "file" and not check.get("path"):
            raise ManifestError(f"infra.checks[{name!r}]: 'file' probe needs 'path'")
        if ctype == "env" and (not check.get("path") or not check.get("key")):
            raise ManifestError(
                f"infra.checks[{name!r}]: 'env' probe needs 'path' and 'key'"
            )

    for check in infra.get("persona_checks", []) or []:
        if not isinstance(check, dict):
            raise ManifestError("infra.persona_checks entries must be mappings")
        if not check.get("persona") or not isinstance(check["persona"], str):
            raise ManifestError("infra.persona_checks entries need a string 'persona'")
        if check.get("type") != "command" or not check.get("cmd"):
            raise ManifestError(
                f"infra.persona_checks[{check.get('persona')!r}]: "
                "type must be 'command' with a 'cmd'"
            )


def access_for(persona_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the access descriptor for a persona.

    Result keys: ``kind`` (full|scoped|restricted|none), ``prefix``
    (scoped/restricted only), ``summary`` (human-readable, in the manifest
    language).
    """
    perms = manifest["permissions"]
    lang = manifest["language"]

    if persona_id in perms.get("full", []):
        return {
            "kind": "full",
            "summary": (
                "Full access to all meeting rooms."
                if lang == "en"
                else "Acceso completo a todas las salas de reunión."
            ),
        }

    for prefix, ids in perms.get("scoped", {}).items():
        if persona_id in ids:
            return {
                "kind": "scoped",
                "prefix": prefix,
                "summary": (
                    f"You schedule and join online meetings within your project "
                    f"scope '{prefix}-*'."
                    if lang == "en"
                    else f"Agendas y participas en reuniones online dentro de tu "
                    f"ámbito de proyecto '{prefix}-*'."
                ),
            }

    for prefix, ids in perms.get("restricted", {}).items():
        if persona_id in ids:
            return {
                "kind": "restricted",
                "prefix": prefix,
                "summary": (
                    f"You take part in rooms you are invited to (invitation URL is "
                    f"your ticket; org scope '{prefix}-*')."
                    if lang == "en"
                    else f"Participas en las salas a las que te inviten (la URL de "
                    f"invitación es tu ticket; ámbito '{prefix}-*')."
                ),
            }

    return {
        "kind": "none",
        "summary": (
            "No access to meeting rooms."
            if lang == "en"
            else "Sin acceso a salas de reunión."
        ),
    }
