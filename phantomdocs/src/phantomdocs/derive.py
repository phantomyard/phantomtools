"""Derive a manifest from a PhantomOrg org model (spec §6, §9).

The org.yaml is the source of truth for the org id; PhantomDocs derives the
namespace manifest from it so the operator never types the org id by hand.
The access model itself is read at resolution time (see access.py), not baked
into the manifest.
"""

from __future__ import annotations

from typing import Any

from .access import validate_org_schema
from .identity import root_mac
from .manifest import empty_manifest


def derive_manifest(
    org: dict[str, Any], namespace: str, org_pubkey: str = ""
) -> dict[str, Any]:
    validate_org_schema(org)
    org_id = (org.get("organization") or {}).get("id")
    if not org_id:
        raise ValueError("org.yaml is missing organization.id")
    mac = root_mac(org_id, org_pubkey, namespace)
    return empty_manifest(org_id, namespace, mac)
