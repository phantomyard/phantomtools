"""A0 containment validators.

Two layers, matching the "Limits of A0" list in ``docs/containment/README.md``:

1. ``schema_errors`` checks a document against ``inventory-v1.schema.json`` or
   ``boundary-declaration-v1.schema.json``.
2. ``cross_check_errors`` compares a boundary declaration against the
   inventory it cites: every declared ``store_id`` and ``workload_id`` exists
   on the referenced host, the declaration's ``inventory_digest`` matches the
   inventory, and inventory entries marked ``unreachable`` (or a host with
   ``unknowns``) are answered by a declaration exclusion.

A validating declaration is still a claim, not access: it does not prove the
owner's authorization and it does not read secret values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from .collector import inventory_digest

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "containment"

SCHEMA_FILES = {
    "inventory": "inventory-v1.schema.json",
    "declaration": "boundary-declaration-v1.schema.json",
}


def load_document(path: str | Path) -> dict[str, Any]:
    """Load a JSON containment document.

    Raises ``OSError``/``UnicodeError`` when the file cannot be read and
    ``json.JSONDecodeError`` (a ``ValueError``) when it is not JSON; callers
    turn either into a one-line failure instead of a traceback.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_schema(kind: str, contracts_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        filename = SCHEMA_FILES[kind]
    except KeyError:
        raise ValueError(
            f"unknown document kind {kind!r}; expected one of {sorted(SCHEMA_FILES)}"
        ) from None
    schema_path = Path(contracts_dir or CONTRACTS_DIR) / filename
    return json.loads(schema_path.read_text(encoding="utf-8"))


def detect_kind(document: dict[str, Any]) -> str:
    """Guess the document kind from its top-level keys."""
    if "declaration_id" in document or "boundaries" in document:
        return "declaration"
    if "inventory_id" in document or "hosts" in document:
        return "inventory"
    raise ValueError("cannot detect document kind; pass an explicit kind")


def schema_errors(
    kind: str,
    document: dict[str, Any],
    contracts_dir: str | Path | None = None,
) -> list[str]:
    """Return one message per schema violation (empty list means valid)."""
    schema = load_schema(kind, contracts_dir)
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(
        validator.iter_errors(document), key=lambda err: list(err.absolute_path)
    )
    return [
        f"{'/'.join(str(part) for part in err.absolute_path) or '<root>'}: {err.message}"
        for err in errors
    ]


def _unreachable_entries(host: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if host.get("evidence", {}).get("status") == "unreachable":
        items.append(("host", str(host.get("host_id"))))
    for store in host.get("stores", []):
        if store.get("evidence", {}).get("status") == "unreachable":
            items.append(("store", str(store.get("store_id"))))
    for workload in host.get("workload_identities", []):
        if workload.get("evidence", {}).get("status") == "unreachable":
            items.append(("workload", str(workload.get("workload_id"))))
    for surface in host.get("maintenance_surfaces", []):
        if surface.get("evidence", {}).get("status") == "unreachable":
            items.append(("surface", str(surface.get("surface_id"))))
    return items


def cross_check_errors(
    inventory: dict[str, Any], declaration: dict[str, Any]
) -> list[str]:
    """Compare a boundary declaration against its inventory.

    Only the referenced host is in scope: a declaration authorizes one host,
    so entries on other hosts are not its concern. An exclusion covers an
    unreachable entry when its ``scope`` equals the entry id or the host id.
    """
    errors: list[str] = []
    host_id = declaration.get("host_id")
    hosts = {host.get("host_id"): host for host in inventory.get("hosts", [])}
    host = hosts.get(host_id)
    if host is None:
        return [f"declaration host_id {host_id!r} is not present in the inventory"]

    expected = inventory_digest(inventory)
    declared = declaration.get("inventory_digest")
    if declared != expected:
        errors.append(
            f"declaration inventory_digest {declared!r} does not match the "
            f"inventory digest {expected!r}"
        )

    store_ids = {store.get("store_id") for store in host.get("stores", [])}
    workload_ids = {
        workload.get("workload_id") for workload in host.get("workload_identities", [])
    }
    scopes = {rule.get("scope") for rule in declaration.get("exclusions", [])}

    for boundary in declaration.get("boundaries", []):
        boundary_id = boundary.get("boundary_id")
        for store_id in boundary.get("store_ids", []):
            if store_id not in store_ids:
                errors.append(
                    f"boundary {boundary_id!r} declares store_id {store_id!r} "
                    f"which is not in inventory host {host_id!r}"
                )
        for workload in boundary.get("workloads", []):
            workload_id = workload.get("workload_id")
            if workload_id not in workload_ids:
                errors.append(
                    f"boundary {boundary_id!r} declares workload_id {workload_id!r} "
                    f"which is not in inventory host {host_id!r}"
                )

    for label, identifier in _unreachable_entries(host):
        if identifier not in scopes and host_id not in scopes:
            errors.append(
                f"inventory {label} {identifier!r} is unreachable but no "
                f"declaration exclusion covers it (scope {identifier!r} or {host_id!r})"
            )

    # Unknowns are free text, so they cannot be matched by scope; require the
    # declaration to carry at least one exclusion when the inventory records any.
    has_unknowns = bool(inventory.get("unknowns")) or bool(host.get("unknowns"))
    if has_unknowns and not declaration.get("exclusions"):
        errors.append(
            "inventory records unknowns but the declaration declares no exclusions"
        )

    return errors
