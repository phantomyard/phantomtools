"""
Scope derivation: per-actor memory/visibility scopes from the org model.

The org model already encodes the hierarchy (departments with parent,
roles with reports_to/access_level, actors with exceptions). This module
derives, for every actor, the list of other actors whose memory it may
search — the "scope" a runtime like phantombot can enforce at query time
(`memory search`).

The output is a pure function of org.yaml (derived state): it holds NO
runtime data, so it is never backed up by the deploy/rollback machinery
— any build regenerates it.

Rules
-----

``chain`` (default)
    Each actor sees their own memory plus the memory of every actor
    whose role is a direct or transitive subordinate (reports_to
    closure). Full visibility ("*") for: roles at the top of the chain
    (reports_to is null), the highest access level defined in policies,
    and category-0 exceptions.

    Design principle (AU 2026-08-11): there is NO interdepartmental
    shared memory. Cross-department communication goes through the
    Telegram coordination channel, never through memory search — so an
    actor never sees another actor's memory unless they are in the same
    command chain (their own branch, downward). E.g. the CFO sees only
    their own memory; the CEO (apex) sees everyone.

``department``
    Each actor sees their own department (same department id) plus the
    departments that are descendants of it in the department tree.
    Actors in a root department (parent: null) get full visibility.
    Cross-department visibility is only ever downward in the tree.
"""

from __future__ import annotations

import json
from typing import Any

from ..spec.model import OrgSpec

SCOPES_FILENAME = "scopes.json"
SCOPES_FORMAT_VERSION = 1

VALID_RULES = ("chain", "department")


class ScopeError(ValueError):
    """Raised when a scope rule cannot be applied to the org model."""


def _level_rank(level_id: str) -> int:
    """Rank of an access level id ("level-3" -> 3). Unknown ids rank 0."""
    if level_id.startswith("level-") and level_id[6:].isdigit():
        return int(level_id[6:])
    return 0


def _has_full_access(spec: OrgSpec, role_id: str, actor_id: str) -> bool:
    """Full visibility: top of the reporting chain, highest access level,
    or a category-0 exception (actor or role)."""
    role = spec.role_by_id(role_id)
    actor = spec.actor_by_id(actor_id)
    if role.reports_to is None:
        return True
    if "category-0" in actor.actor_exceptions:
        return True
    if "category-0" in role.security_exceptions:
        return True
    levels = spec.policies.access_levels
    if levels and role.access_level:
        top = max(levels.keys(), key=_level_rank)
        if role.access_level == top:
            return True
    return False


def _actors_for_role(spec: OrgSpec, role_id: str) -> list[str]:
    return [a.id for a in spec.actors if a.role == role_id]


def _transitive_subordinates(spec: OrgSpec, role_id: str) -> set[str]:
    """Closure of reports_to: all roles that report (directly or
    transitively) to ``role_id``."""
    seen: set[str] = set()
    stack = [r.id for r in spec.subordinates_of(role_id)]
    while stack:
        rid = stack.pop()
        if rid in seen:
            continue
        seen.add(rid)
        stack.extend(r.id for r in spec.subordinates_of(rid))
    return seen


def _department_descendants(spec: OrgSpec, dept_id: str) -> set[str]:
    """Closure of department parent: this department plus every department
    that is a descendant of it in the department tree."""
    children: dict[str, list[str]] = {}
    for d in spec.departments:
        children.setdefault(d.parent or "", []).append(d.id)
    seen: set[str] = set()
    stack = [dept_id]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


def derive_scopes(spec: OrgSpec, rule: str = "chain") -> dict[str, list[str]]:
    """Map actor id -> list of visible actor ids (or ["*"] for full)."""
    if rule not in VALID_RULES:
        raise ScopeError(
            f"unknown scope rule {rule!r} (valid: {', '.join(VALID_RULES)})"
        )

    # role_id -> actor ids
    actors_by_role: dict[str, list[str]] = {}
    for a in spec.actors:
        actors_by_role.setdefault(a.role, []).append(a.id)

    scopes: dict[str, list[str]] = {}
    for actor in spec.actors:
        role = spec.role_by_id(actor.role)
        if _has_full_access(spec, role.id, actor.id):
            scopes[actor.id] = ["*"]
            continue

        # Department rule: actors in a root department (parent: null) see
        # the whole org ("root department sees everything").
        if rule == "department":
            dept = spec.department_by_id(role.department)
            if dept.parent is None:
                scopes[actor.id] = ["*"]
                continue

        visible: set[str] = {actor.id}
        if rule == "chain":
            for sub_role in _transitive_subordinates(spec, role.id):
                visible.update(actors_by_role.get(sub_role, []))
        elif rule == "department":
            # Own department + descendants of it in the department tree.
            visible_depts = _department_descendants(spec, role.department)
            for other in spec.actors:
                other_role = spec.role_by_id(other.role)
                if other_role.department in visible_depts:
                    visible.add(other.id)
        scopes[actor.id] = sorted(visible)

    return scopes


def serialize_scopes(spec: OrgSpec, scopes: dict[str, list[str]], rule: str) -> str:
    """Deterministic JSON for the scopes artifact (no timestamps: a build
    that changes nothing writes nothing)."""
    payload: dict[str, Any] = {
        "format_version": SCOPES_FORMAT_VERSION,
        "org": spec.organization.id,
        "rule": rule,
        "scopes": scopes,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def merge_scopes_payloads(existing: dict, incoming: dict) -> dict:
    """Union two scopes.json payloads (multi-org deploy-all).

    The data dir holds a single scopes.json; deploying a second org must
    UNION the ``scopes`` maps rather than let the last org overwrite the
    first. Actor ids are globally unique across organizations (``deploy``
    rejects a cross-org collision on any shared actor id), so the union is
    lossless. ``orgs`` becomes the sorted list of contributing org ids;
    ``rule`` is taken from the incoming build (``build-all`` compiles every
    org with the same default rule, so they agree).
    """
    scopes: dict[str, list[str]] = dict(existing.get("scopes") or {})
    scopes.update(incoming.get("scopes") or {})

    orgs: set[str] = set()
    for payload in (existing, incoming):
        org_id = payload.get("org")
        if org_id:
            orgs.add(str(org_id))
        for o in payload.get("orgs") or []:
            orgs.add(str(o))

    return {
        "format_version": incoming.get("format_version", SCOPES_FORMAT_VERSION),
        "orgs": sorted(orgs),
        "rule": incoming.get("rule") or existing.get("rule"),
        "scopes": scopes,
    }


def merge_scopes_json(existing_text: str, incoming_text: str) -> str:
    """Merge two serialized scopes.json documents (deploy-all across orgs)."""
    existing = json.loads(existing_text)
    incoming = json.loads(incoming_text)
    return (
        json.dumps(merge_scopes_payloads(existing, incoming), indent=2, sort_keys=True)
        + "\n"
    )
