"""Derive a PhantomMeet manifest from a PhantomForge org model (org.yaml).

The org model is the single source of truth for the organization hierarchy
(departments, roles, actors). This module reads it and produces a complete
PhantomMeet manifest, so the meeting capability is granted *intrinsically*
to the roles declared as directive/support — no hand-maintained persona
list.

A small *base manifest* supplies everything that is not part of the org
model: bridge endpoint, room naming, storage policy, org-specific knowledge
(kb_appendix) and the derivation rules (which org roles map to
responsible/support, and the restricted prefix per support role).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .manifest import ManifestError

DEFAULT_DERIVE = {
    "directive_roles": [],
    "scoped_responsible_roles": {},  # role_id -> scope prefix
    "support_roles": [],
    "restricted_prefixes": {},
}


def load_org_model(path: str | Path) -> dict[str, Any]:
    """Load and validate a PhantomForge org model."""
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
    scoped = derive.get("scoped_responsible_roles", {}) or {}
    support = set(derive.get("support_roles", []) or [])
    if not isinstance(scoped, dict):
        raise ManifestError("derive.scoped_responsible_roles must be a mapping")
    for role_id, prefix in scoped.items():
        if not isinstance(prefix, str) or not prefix:
            raise ManifestError(
                f"derive.scoped_responsible_roles[{role_id!r}] must be a non-empty string"
            )
    overlap = directive & support
    if overlap:
        raise ManifestError(
            f"derive roles overlap (cannot be both directive and support): "
            f"{', '.join(sorted(overlap))}"
        )
    scoped_keys = set(scoped)
    if scoped_keys & directive or scoped_keys & support:
        raise ManifestError(
            "derive roles overlap: a role cannot be directive/support and "
            "scoped_responsible at the same time"
        )
    prefixes = derive.get("restricted_prefixes", {}) or {}
    if not isinstance(prefixes, dict):
        raise ManifestError("derive.restricted_prefixes must be a mapping")
    for role_id, prefix in prefixes.items():
        if not isinstance(prefix, str) or not prefix:
            raise ManifestError(
                f"derive.restricted_prefixes[{role_id!r}] must be a non-empty string"
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
    scoped_responsible = derive["scoped_responsible_roles"] or {}
    support_roles = set(derive["support_roles"] or [])
    prefixes = derive["restricted_prefixes"] or {}

    if not directive_roles and not scoped_responsible and not support_roles:
        warnings.append(
            "derive rules map no roles (directive_roles/scoped_responsible_roles/"
            "support_roles empty); "
            "the derived manifest grants the meeting capability to nobody"
        )

    roles_by_id = _roles_by_id(org_model)
    for role_id in sorted(directive_roles | set(scoped_responsible) | support_roles):
        if role_id not in roles_by_id:
            warnings.append(f"derive rule references unknown org role {role_id!r}")

    roles: dict[str, str] = {}
    full: list[str] = []
    scoped: dict[str, list[str]] = {}
    restricted: dict[str, list[str]] = {}

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
        elif role_id in scoped_responsible:
            prefix = scoped_responsible[role_id]
            roles[pid] = "lead"
            scoped.setdefault(prefix, []).append(pid)
        elif role_id in support_roles:
            prefix = prefixes.get(role_id)
            if not prefix:
                warnings.append(
                    f"actor {pid!r} has support role {role_id!r} but no "
                    f"derive.restricted_prefixes[{role_id!r}] entry; skipped"
                )
                continue
            roles[pid] = "support"
            restricted.setdefault(prefix, []).append(pid)
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
    #   - lead actors are responsible *within their scope* and escalate only
    #     requests that fall *outside* their scope.
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
        "permissions": {"full": full, "scoped": scoped, "restricted": restricted},
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
