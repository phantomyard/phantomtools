"""Compile explicit service intent for a separately authorized deployment adapter."""

from __future__ import annotations

import hashlib
import json

from ..spec.model import OrgSpec
from ..spec.service_policy import ServicePolicyError


def compile_service_policy(spec: OrgSpec) -> str:
    """Export deterministically; absence or invalid references never imply permission.

    This artifact is neither an approval receipt nor proof of OS enforcement.
    Installation consumers must authenticate its provenance independently.
    """
    policy = spec.service_policy
    if policy is None:
        raise ServicePolicyError("organization declares no service_policy")
    errors = policy.reference_errors({a.id for a in spec.actors})
    if errors:
        raise ServicePolicyError("; ".join(errors))
    payload = {
        "format_version": 1,
        "organization_id": spec.organization.id,
        "default_access": "deny",
        "actors": sorted(a.id for a in spec.actors),
        "services": [
            {
                "id": s.id,
                "kind": s.kind,
                "owner_actor": s.owner_actor,
                "consumers": [
                    {"kind": c.kind, "id": c.id}
                    for c in sorted(s.consumers, key=lambda c: (c.kind, c.id))
                ],
                "approval": s.approval,
                "approvers": sorted(s.approvers),
            }
            for s in sorted(policy.services, key=lambda s: s.id)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["policy_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
