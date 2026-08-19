"""Manifest — the single source of truth (spec §6).

A per-namespace YAML document mapping identity (urn + MAC) to location,
metadata, classification and relations. Version 1 is single-tenant.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.yaml"


class ManifestError(ValueError):
    """Raised when a manifest fails validation."""


def empty_manifest(org: str, namespace: str, root_mac: str) -> dict[str, Any]:
    """A fresh, valid single-tenant manifest."""
    return {
        "manifest": {
            "version": MANIFEST_VERSION,
            "org": org,
            "namespace": namespace,
            "tenant": "single",
            "rootMac": root_mac,
            "signedRootMac": None,
        },
        "refs": {},
        "nodes": [],
    }


def load(path: str) -> dict[str, Any]:
    """Load and validate a manifest from disk."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    errors = validate(data)
    if errors:
        raise ManifestError("; ".join(errors))
    return data


def save(path: str, data: dict[str, Any]) -> None:
    """Atomically write a manifest to disk."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty == valid)."""
    errors: list[str] = []
    m = data.get("manifest", {})
    if m.get("version") != MANIFEST_VERSION:
        errors.append(f"unsupported manifest version: {m.get('version')!r}")
    if m.get("tenant") == "multi":
        errors.append("multi-tenancy is not supported in this version")
    if not m.get("org"):
        errors.append("manifest.org is required")
    if not m.get("rootMac"):
        errors.append("manifest.rootMac is required")
    return errors


def node_by_urn(data: dict[str, Any], urn: str) -> dict[str, Any] | None:
    for node in data.get("nodes", []):
        if node.get("urn") == urn:
            return node
    return None


def node_by_slug(data: dict[str, Any], slug: str) -> dict[str, Any] | None:
    for node in data.get("nodes", []):
        if node.get("slug") == slug:
            return node
    return None


def urn_path(urn: str) -> str:
    """urn:<org>:<kind>:<path> -> <path>."""
    return urn.split(":", 3)[-1]
