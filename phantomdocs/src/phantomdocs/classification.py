"""Read-only classification planning; no permission or document mutations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .access import can_read, can_write, normalize_category, policy_hash
from .manifest import (
    node_by_urn,
    resolve_node,
    structural_issues,
    validate,
    versions_of,
)
from .storage import location_uri


def classification_plan(
    data: dict[str, Any],
    org: dict[str, Any],
    actor_id: str,
    *,
    ref: str,
    expected_mac: str,
    expected_category: str,
    target_category: str,
    reason: str,
) -> dict[str, Any]:
    """Describe a proposed whole-document transition without authorizing it.

    The CLI supplies its authenticated actor and loaded organization. A returned
    plan is a review artifact, never a credential or an executable migration.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        raise ValueError("a non-empty reason of at most 1024 characters is required")
    if not isinstance(data, dict) or validate(data) or structural_issues(data):
        raise ValueError("manifest structure must pass verification before planning")
    selected = resolve_node(data, ref)
    if selected is None or selected.get("kind") != "doc":
        raise ValueError("classification planning requires an existing document")
    current = node_by_urn(data, selected["urn"])
    if current is None or current["mac"] != expected_mac:
        raise ValueError("stale current version; review the document again")
    old = normalize_category(expected_category)
    target = normalize_category(target_category)
    if normalize_category(current["category"]) != old:
        raise ValueError("stale category; review the document again")
    if old == target:
        raise ValueError("the target category must differ from the current category")
    owners = current.get("owners") or []
    if not all(can_write(org, actor_id, cat, owners) for cat in (old, target)):
        raise ValueError(
            "denied: write access to both categories and owners is required"
        )
    history = versions_of(data, current["urn"])
    if any(
        normalize_category(node["category"]) != old
        or set(node.get("owners") or []) != set(owners)
        for node in history
    ):
        raise ValueError(
            "mixed historical categories or owners require separate review"
        )
    sources = {
        location_uri(loc)
        for node in history
        for loc in node.get("locations", [])
        if "ref" in loc
    }
    if any(
        node.get("urn") != current["urn"]
        and any(
            "ref" in loc and location_uri(loc) in sources
            for loc in node.get("locations", [])
        )
        for node in data["nodes"]
    ):
        raise ValueError("source reference belongs to multiple document identities")
    gained, lost, retained = [], [], []
    for identity in sorted({actor["id"] for actor in org.get("actors", [])}):
        before = can_read(org, identity, old)
        after = can_read(org, identity, target)
        if after and not before:
            gained.append(identity)
        elif before and not after:
            lost.append(identity)
        elif before and after:
            retained.append(identity)
    snapshot = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return {
        "schema": "phantomdocs.classification-plan/v1",
        "status": "review-required",
        "execution_available": False,
        "history_scope": "all-versions",
        "actor": actor_id,
        "urn": current["urn"],
        "expected_current_mac": current["mac"],
        "expected_manifest_digest": hashlib.sha256(
            snapshot.encode("utf-8")
        ).hexdigest(),
        "expected_policy_hash": policy_hash(org),
        "from_category": old,
        "target_category": target,
        "owners": sorted(owners),
        "reason": reason.strip(),
        "versions": [
            {"mac": node["mac"], "content_hash": node["contentHash"]}
            for node in history
        ],
        "reader_impact": {"gained": gained, "lost": lost, "retained": retained},
        "blockers": [
            "Signed native reclassification is not implemented.",
            "Verify content, signatures, audit and trust anchors before any transition.",
            "Readers must enforce one effective category across historical versions.",
            "Old readers must refuse stores using classification transitions.",
        ],
    }
