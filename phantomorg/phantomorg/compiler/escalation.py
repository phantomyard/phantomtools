"""
Resolves, for a given role, the escalation_matrix entries that apply
(its own and the wildcard '*' ones), ready to render in the SOUL.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..spec.model import OrgSpec


@dataclass
class ResolvedEscalation:
    to_name: str
    condition: str
    cross_department: bool


def escalation_paths_for(spec: OrgSpec, role_id: str) -> list[ResolvedEscalation]:
    paths: list[ResolvedEscalation] = []
    for entry in spec.escalation_matrix:
        if entry.from_ == role_id or entry.from_ == "*":
            to_role = spec.role_by_id(entry.to)
            paths.append(
                ResolvedEscalation(
                    to_name=to_role.name,
                    condition=entry.condition,
                    cross_department=entry.cross_department,
                )
            )
    return paths
