"""
merge_access(): implements the hybrid model of section 5.5 of the spec.

- Base level (RBAC): the role's access_level, inherited in practice from
  the department (the validator already checked that they are coherent).
- Role exception: security_exceptions — applies to any actor of the role.
- Actor exception (ABAC): actor_exceptions — applies only to that actor.

This avoids the "role explosion" documented in the RBAC/ABAC/PBAC
literature (NIST): you don't create a new role for every one-off
exception.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..spec.model import Actor, OrgSpec, Role


@dataclass
class ResolvedAccess:
    label: str
    categories: list[int]
    role_exceptions: list[str]
    actor_exceptions: list[str]


def merge_access(spec: OrgSpec, role: Role, actor: Actor) -> ResolvedAccess:
    base = spec.policies.access_levels[role.access_level]
    return ResolvedAccess(
        label=base.label,
        categories=list(base.categories),
        role_exceptions=list(role.security_exceptions),
        actor_exceptions=list(actor.actor_exceptions),
    )
