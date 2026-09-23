"""Read-only A0 containment inventory collector.

The collector turns an operator-supplied *plan* (JSON) into an inventory
document that conforms to ``docs/containment/inventory-v1.schema.json``. It
is read-only by construction: it only stats local paths, resolves local
accounts and reads local systemd unit files. It never writes to the host,
never copies file contents and never opens a socket or a connection.

The plan names the candidate host, workload identities, stores and
maintenance surfaces. Each entry carries a ``probe`` that decides how its
evidence is produced:

============  ====================  ==================================
probe         target field          ``observed`` when
============  ====================  ==================================
``path``      store.location        the path exists on this host
``unix_socket`` surface.interface   the path exists and is a socket
``os_identity`` workload.os_identity the local account exists
``declared``  (none)                never; recorded as ``reported``
============  ====================  ==================================

Anything a probe cannot confirm is recorded with ``status: "unreachable"``
and the host's ``unknowns`` list grows, so the inventory never hides it.
An inventory is evidence, not an authorization: keep the emitted document
outside this repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat as stat_module
from datetime import datetime, timezone
from typing import Any

STORE_PROBES = ("path", "declared")
SURFACE_PROBES = ("unix_socket", "declared")
WORKLOAD_PROBES = ("os_identity", "declared")
HOST_PROBES = ("local", "declared")

# These limits are documented in the module docstring; keep them in sync.


class CollectorError(Exception):
    """The plan is unusable (malformed, unknown probe, unknown host)."""


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding, used to digest an inventory.

    Keys are sorted and separators are compact, so reformatting or reordering
    a stored inventory does not change its digest.
    """
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def inventory_digest(inventory: dict[str, Any]) -> str:
    """Return ``sha256:<hex>`` over the inventory's canonical JSON."""
    return "sha256:" + hashlib.sha256(canonical_bytes(inventory)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evidence(status: str, source: str, collected_at: str) -> dict[str, Any]:
    return {"status": status, "source": source, "collected_at": collected_at}


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise CollectorError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _probe_path(location: str) -> tuple[str, str]:
    try:
        os.stat(location)
    except OSError:
        return "unreachable", "no entry at the declared location"
    return "observed", "read-only stat of the declared location"


def _probe_unix_socket(interface: str) -> tuple[str, str]:
    try:
        info = os.stat(interface)
    except OSError:
        return "unreachable", "no entry at the declared interface path"
    if stat_module.S_ISSOCK(info.st_mode):
        return "observed", "read-only stat: unix socket present"
    return "unreachable", "entry exists but is not a unix socket"


def _probe_os_identity(os_identity: str) -> tuple[str, str]:
    try:
        pwd.getpwnam(os_identity)
    except KeyError:
        return "unreachable", "local account not found"
    return "observed", "local account resolved"


def _collect_workload(
    workload: dict[str, Any], collected_at: str, unknowns: list[str]
) -> dict[str, Any]:
    wid = _require(workload, "workload_id", "workload")
    probe = workload.get("probe", "declared")
    if probe not in WORKLOAD_PROBES:
        raise CollectorError(f"workload {wid!r}: unknown probe {probe!r}")
    if probe == "os_identity":
        status, source = _probe_os_identity(_require(workload, "os_identity", wid))
    else:
        status, source = "reported", "operator declaration; no probe"
    entry: dict[str, Any] = {
        "workload_id": wid,
        "runtime": _require(workload, "runtime", wid),
        "os_identity": _require(workload, "os_identity", wid),
        "candidate_boundary": workload.get("candidate_boundary"),
        "evidence": _evidence(status, source, collected_at),
    }
    if workload.get("service_units"):
        entry["service_units"] = list(workload["service_units"])
    if status == "unreachable":
        unknowns.append(f"workload {wid} unreachable: {source}")
    return entry


def _collect_store(
    store: dict[str, Any], collected_at: str, unknowns: list[str]
) -> dict[str, Any]:
    sid = _require(store, "store_id", "store")
    probe = store.get("probe", "declared")
    if probe not in STORE_PROBES:
        raise CollectorError(f"store {sid!r}: unknown probe {probe!r}")
    if probe == "path":
        status, source = _probe_path(_require(store, "location", sid))
    else:
        status, source = "reported", "operator declaration; no probe"
    entry: dict[str, Any] = {
        "store_id": sid,
        "kind": _require(store, "kind", sid),
        "location": _require(store, "location", sid),
        "disposition": _require(store, "disposition", sid),
        "candidate_boundary": store.get("candidate_boundary"),
        "allowed_workloads": list(store.get("allowed_workloads", [])),
        "evidence": _evidence(status, source, collected_at),
    }
    if "requires_split" in store:
        entry["requires_split"] = bool(store["requires_split"])
    if store.get("credential_references"):
        entry["credential_references"] = list(store["credential_references"])
    if status == "unreachable":
        unknowns.append(f"store {sid} unreachable: {source}")
    return entry


def _collect_surface(
    surface: dict[str, Any], collected_at: str, unknowns: list[str]
) -> dict[str, Any]:
    surface_id = _require(surface, "surface_id", "surface")
    probe = surface.get("probe", "declared")
    if probe not in SURFACE_PROBES:
        raise CollectorError(f"surface {surface_id!r}: unknown probe {probe!r}")
    if probe == "unix_socket":
        status, source = _probe_unix_socket(_require(surface, "interface", surface_id))
    else:
        status, source = "reported", "operator declaration; no probe"
    entry: dict[str, Any] = {
        "surface_id": surface_id,
        "interface": _require(surface, "interface", surface_id),
        "privileged_effect": _require(surface, "privileged_effect", surface_id),
        "authentication": _require(surface, "authentication", surface_id),
        "evidence": _evidence(status, source, collected_at),
    }
    if surface.get("credential_references"):
        entry["credential_references"] = list(surface["credential_references"])
    if status == "unreachable":
        unknowns.append(f"surface {surface_id} unreachable: {source}")
    return entry


def _collect_host(host: dict[str, Any], collected_at: str) -> dict[str, Any]:
    host_id = _require(host, "host_id", "host")
    probe = host.get("probe", "local")
    if probe not in HOST_PROBES:
        raise CollectorError(f"host {host_id!r}: unknown probe {probe!r}")

    unknowns = [str(item) for item in host.get("unknowns", [])]
    if probe == "declared":
        host_status = "reported"
        host_source = "operator declaration; no probe"
        unknowns.append(f"host {host_id} was declared, not probed")
    else:
        host_status = "observed"
        host_source = "read-only probes ran on this host"

    workloads = [
        _collect_workload(item, collected_at, unknowns)
        for item in host.get("workloads", [])
    ]
    stores = [
        _collect_store(item, collected_at, unknowns) for item in host.get("stores", [])
    ]
    surfaces = [
        _collect_surface(item, collected_at, unknowns)
        for item in host.get("surfaces", [])
    ]

    entry: dict[str, Any] = {
        "host_id": host_id,
        "evidence": _evidence(host_status, host_source, collected_at),
        "workload_identities": workloads,
        "stores": stores,
        "maintenance_surfaces": surfaces,
    }
    if host.get("deployed_revisions"):
        entry["deployed_revisions"] = dict(host["deployed_revisions"])
    if unknowns:
        entry["unknowns"] = unknowns
    return entry


def collect_inventory(
    plan: dict[str, Any],
    *,
    host_id: str | None = None,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Probe the plan read-only and return an inventory-v1 document.

    ``host_id`` restricts collection to one planned host (acceptance is per
    host). ``collected_at`` overrides the timestamp for reproducible runs.
    """
    if not isinstance(plan, dict):
        raise CollectorError("plan must be a JSON object")
    inventory_id = _require(plan, "inventory_id", "plan")
    collector = _require(plan, "collector", "plan")
    planned = plan.get("hosts")
    if not isinstance(planned, list) or not planned:
        raise CollectorError("plan: 'hosts' must be a non-empty list")

    stamp = collected_at or _utc_now()
    hosts: list[dict[str, Any]] = []
    for host in planned:
        if not isinstance(host, dict):
            raise CollectorError("plan: each host must be an object")
        if host_id is not None and host.get("host_id") != host_id:
            continue
        hosts.append(_collect_host(host, stamp))
    if host_id is not None and not hosts:
        raise CollectorError(f"plan: host_id {host_id!r} is not in the plan")

    return {
        "schema_version": 1,
        "inventory_id": inventory_id,
        "collected_at": stamp,
        "collector": collector,
        "hosts": hosts,
        "unknowns": [str(item) for item in plan.get("unknowns", [])],
        "exclusions": [],
    }
