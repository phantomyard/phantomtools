"""Derive a PhantomMeet manifest from a PhantomOrg org model (org.yaml).

The org model is the single source of truth for the organization hierarchy
(departments, roles, actors). This module reads it and produces a complete
PhantomMeet manifest, so the meeting capability is granted *intrinsically*
to the roles declared as directive/support — no hand-maintained persona
list.

A small *base manifest* supplies everything that is not part of the org
model: bridge endpoint, room naming, storage policy, org-specific knowledge
(kb_appendix) and the derivation rules (which org roles map to
responsible/lead/support).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .manifest import ManifestError

DEFAULT_DERIVE = {
    "directive_roles": [],  # org roles -> responsible (full: any room + recordings)
    "lead_roles": [],  # org roles -> lead (schedule own project, join any room)
    "support_roles": [],  # org roles -> support (join invited rooms, no scheduling)
}


def load_org_model(path: str | Path) -> dict[str, Any]:
    """Load and validate a PhantomOrg org model."""
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"org model not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in org model {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"org model must be a YAML mapping: {p}")
    org = raw.get("organization")
    if not isinstance(org, dict) or not org.get("id"):
        raise ManifestError(f"org model {p} is missing organization.id")
    for section in ("roles", "actors"):
        if section in raw and not isinstance(raw[section], list):
            raise ManifestError(f"org model {p}: '{section}' must be a list")
    return raw


def load_base(path: str | Path) -> dict[str, Any]:
    """Load the base manifest (bridge/rooms/storage/kb_appendix/derive rules)."""
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"base manifest not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in base manifest {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"base manifest must be a YAML mapping: {p}")
    derive = raw.get("derive", {}) or {}
    if not isinstance(derive, dict):
        raise ManifestError("base manifest 'derive' must be a mapping")
    directive = set(derive.get("directive_roles", []) or [])
    lead = set(derive.get("lead_roles", []) or [])
    support = set(derive.get("support_roles", []) or [])
    for name, roles_set in (("lead", lead), ("support", support)):
        for role_id in roles_set:
            if not isinstance(role_id, str) or not role_id:
                raise ManifestError(
                    f"derive.{name}_roles entries must be non-empty strings"
                )
    overlaps = (directive & lead) | (directive & support) | (lead & support)
    if overlaps:
        raise ManifestError(
            "derive roles overlap: a role cannot be directive/lead/support at once: "
            f"{', '.join(sorted(overlaps))}"
        )
    raw.setdefault("bridge", {})
    raw.setdefault("rooms", {})
    raw.setdefault("storage", {})
    raw.setdefault("legacy_kb_files", [])
    raw.setdefault("kb_appendix", [])
    raw.setdefault("infra", {})
    return raw


def _roles_by_id(org_model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    roles = org_model.get("roles") or []
    return {r["id"]: r for r in roles if isinstance(r, dict) and r.get("id")}


def derive_manifest(
    org_path: str | Path, base_path: str | Path
) -> tuple[dict[str, Any], list[str]]:
    """Derive a full PhantomMeet manifest from an org model + base manifest.

    Returns ``(manifest, warnings)``. Warnings never fail the derivation;
    they flag actors/roles that were intentionally not mapped.
    """
    org_model = load_org_model(org_path)
    base = load_base(base_path)
    warnings: list[str] = []

    org = org_model["organization"]
    org_id = org["id"]
    language = org.get("default_language") or base.get("language") or "es"

    derive = dict(DEFAULT_DERIVE)
    derive.update(base.get("derive", {}) or {})
    directive_roles = set(derive["directive_roles"] or [])
    lead_roles = set(derive["lead_roles"] or [])
    support_roles = set(derive["support_roles"] or [])

    if not directive_roles and not lead_roles and not support_roles:
        warnings.append(
            "derive rules map no roles (directive_roles/lead_roles/"
            "support_roles empty); "
            "the derived manifest grants the meeting capability to nobody"
        )

    roles_by_id = _roles_by_id(org_model)
    for role_id in sorted(directive_roles | lead_roles | support_roles):
        if role_id not in roles_by_id:
            warnings.append(f"derive rule references unknown org role {role_id!r}")

    roles: dict[str, str] = {}
    full: list[str] = []

    actors = org_model.get("actors") or []
    for actor in actors:
        if not isinstance(actor, dict) or not actor.get("id"):
            continue
        pid = actor["id"]
        role_id = actor.get("role")
        if not role_id:
            warnings.append(
                f"actor {pid!r} has no role; not granted meeting capability"
            )
            continue
        if role_id not in roles_by_id:
            warnings.append(
                f"actor {pid!r} references unknown role {role_id!r}; skipped"
            )
            continue
        if role_id in directive_roles:
            roles[pid] = "responsible"
            full.append(pid)
        elif role_id in lead_roles:
            roles[pid] = "lead"
        elif role_id in support_roles:
            roles[pid] = "support"
        # actors whose role is not in the derive rules keep no capability.

    # --- escalation map (support/lead actor -> responsible actor) ----------
    # The org model is the single source of truth for *who escalates to whom*:
    # each support role's escalation target comes from the org's
    # escalation_matrix (fallback: the role's reports_to). We resolve that
    # role to the responsible actor(s) holding it in this manifest. This keeps
    # the base manifest free of hierarchy duplication — no drift possible.
    #
    # Semantics per tier:
    #   - support actors escalate *every* online-meeting request.
    #   - lead actors are responsible *within their project* and escalate only
    #     requests that fall *outside* their project.
    # Both tiers use the same org escalation target (the org model does not
    # distinguish; the rendered Meetings.md phrases it per tier).
    escalation: dict[str, str] = {}
    matrix_by_from: dict[str, str] = {}
    for entry in org_model.get("escalation_matrix") or []:
        if isinstance(entry, dict) and entry.get("from") and entry.get("to"):
            matrix_by_from.setdefault(entry["from"], entry["to"])
    role_to_actors: dict[str, list[str]] = {}
    for actor in actors:
        if isinstance(actor, dict) and actor.get("id") and actor.get("role"):
            role_to_actors.setdefault(actor["role"], []).append(actor["id"])

    for actor in actors:
        if not isinstance(actor, dict) or not actor.get("id"):
            continue
        pid = actor["id"]
        if roles.get(pid) not in ("support", "lead"):
            continue
        role_id = actor.get("role")
        role_info = roles_by_id.get(role_id) or {}
        target_role = matrix_by_from.get(role_id) or role_info.get("reports_to")
        if not target_role or target_role not in roles_by_id:
            warnings.append(
                f"{'support' if roles[pid] == 'support' else 'lead'} actor {pid!r} "
                "has no escalation target in the org model "
                "(escalation_matrix/reports_to); Meetings.md will use "
                "the generic escalation rule"
            )
            continue
        candidates = [
            c
            for c in role_to_actors.get(target_role, [])
            if roles.get(c) == "responsible"
        ]
        if not candidates:
            warnings.append(
                f"{'support' if roles[pid] == 'support' else 'lead'} actor {pid!r} "
                f"escalates to role {target_role!r} but no responsible actor "
                "with that role is in the manifest"
            )
            continue
        escalation[pid] = candidates[0]

    manifest = {
        "org": org_id,
        "language": language,
        "version": base.get("version", "0.0.0"),
        "bridge": base["bridge"],
        "rooms": base["rooms"],
        "roles": roles,
        "permissions": {"full": full},
        "escalation": escalation,
        "storage": base["storage"],
        "defaults": base.get("defaults", {}),
        "legacy_kb_files": base.get("legacy_kb_files", []),
        "kb_appendix": base.get("kb_appendix", []),
        "infra": base.get("infra", {}),
        "invite": base.get("invite", {}),
        "tools": base.get("tools", []),
    }
    return manifest, warnings
