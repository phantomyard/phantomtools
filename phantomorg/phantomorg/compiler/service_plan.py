"""Offline binding review. No subprocesses, network probes or host mutations."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import TypedDict

from ..spec.model import OrgSpec
from .service_policy import compile_service_policy


class ServicePlanError(ValueError):
    """Malformed or mismatched installation input."""


class _Binding(TypedDict):
    account: str
    units: list[tuple[str, str]]
    endpoint: dict | None


def _object(value: object, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ServicePlanError(f"{where}: expected exactly {sorted(keys)}")
    return value


def _rows(value: object, where: str) -> list:
    if not isinstance(value, list):
        raise ServicePlanError(f"{where}: expected a list")
    return value


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}", value
    ):
        raise ServicePlanError(f"{where}: invalid name")
    return value


def _unit(raw: object) -> tuple[str, str]:
    row = _object(raw, {"name", "scope"}, "unit")
    if row["scope"] not in ("system", "user"):
        raise ServicePlanError("unit.scope: expected system or user")
    if not isinstance(row["name"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_@.:-]+[.]service", row["name"]
    ):
        raise ServicePlanError("unit.name: expected an instantiated service unit")
    if "@.service" in row["name"]:
        raise ServicePlanError("unit.name: template instances must be explicit")
    return row["scope"], row["name"]


def _account(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?", value
    ):
        raise ServicePlanError("account: invalid installation account name")
    return value


def _endpoint(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ServicePlanError("endpoint: expected a mapping")
    if raw.get("transport") not in ("tcp", "udp"):
        raise ServicePlanError(
            "endpoint.transport: this planner supports TCP/UDP loopback only"
        )
    row = _object(raw, {"transport", "address", "port"}, "endpoint")
    try:
        address = ipaddress.ip_address(row["address"])
    except (ValueError, TypeError) as exc:
        raise ServicePlanError(
            "endpoint.address: expected a literal loopback IP"
        ) from exc
    if (
        not isinstance(row["address"], str)
        or not address.is_loopback
        or "%" in row["address"]
    ):
        raise ServicePlanError("endpoint.address: expected a literal loopback IP")
    if type(row["port"]) is not int or not 1 <= row["port"] <= 65535:
        raise ServicePlanError("endpoint.port: expected an integer from 1 to 65535")
    return {"transport": row["transport"], "address": str(address), "port": row["port"]}


def compile_service_plan(
    spec: OrgSpec, raw_bindings: object, raw_inventory: object
) -> dict:
    """Review trusted installation declarations against an operator snapshot.

    A reviewable plan is not deployable, and inventory is not an attestation.
    All unknown runtime dependencies remain explicit mandatory review items.
    """
    policy = json.loads(compile_service_policy(spec))
    bindings = _object(
        raw_bindings,
        {
            "version",
            "organization_id",
            "policy_sha256",
            "host_id",
            "actors",
            "services",
        },
        "bindings",
    )
    inventory = _object(
        raw_inventory, {"version", "host_id", "accounts", "units"}, "inventory"
    )
    for data in (bindings, inventory):
        if type(data["version"]) is not int or data["version"] != 1:
            raise ServicePlanError("bindings/inventory version must be 1")
        _name(data["host_id"], "host_id")
    if bindings["host_id"] != inventory["host_id"]:
        raise ServicePlanError("inventory host does not match installation")
    if bindings["organization_id"] != policy["organization_id"]:
        raise ServicePlanError("installation belongs to a different organization")
    if bindings["policy_sha256"] != policy["policy_sha256"]:
        raise ServicePlanError(
            "installation policy pin does not match current organization intent"
        )

    accounts = {}
    for raw in _rows(inventory["accounts"], "inventory.accounts"):
        row = _object(raw, {"name", "uid"}, "inventory account")
        name = _account(row["name"])
        if type(row["uid"]) is not int or row["uid"] < 0 or name in accounts:
            raise ServicePlanError(
                "inventory account: invalid UID or duplicate account"
            )
        accounts[name] = row["uid"]

    observed = {}
    for raw in _rows(inventory["units"], "inventory.units"):
        row = _object(
            raw, {"name", "scope", "account", "namespace", "active"}, "observed unit"
        )
        scope, name = _unit({"name": row["name"], "scope": row["scope"]})
        account = _account(row["account"])
        if account not in accounts or type(row["active"]) is not bool:
            raise ServicePlanError(
                "observed unit: unknown account or invalid active flag"
            )
        if (
            not isinstance(row["namespace"], str)
            or not row["namespace"]
            or len(row["namespace"]) > 256
        ):
            raise ServicePlanError("observed unit: expected namespace identifier")
        # System unit names are host-global; user names belong to their manager account.
        key = (scope, name, account if scope == "user" else "")
        if key in observed:
            raise ServicePlanError("inventory contains duplicate unit identity")
        observed[key] = row

    declared: dict[tuple[str, str], _Binding] = {}
    expected_actors = set(policy["actors"])
    services = {s["id"]: s for s in policy["services"]}
    for kind, expected in (("actor", expected_actors), ("service", set(services))):
        rows = bindings["actors" if kind == "actor" else "services"]
        ids = set()
        for raw in _rows(rows, f"bindings.{kind}s"):
            keys = {"id", "account", "units"} | (
                {"endpoint"} if kind == "service" else set()
            )
            row = _object(raw, keys, f"{kind} binding")
            identity = _name(row["id"], "principal.id")
            if identity not in expected or identity in ids:
                raise ServicePlanError(
                    f"unknown or duplicate {kind} binding: {identity}"
                )
            ids.add(identity)
            account = _account(row["account"])
            units = [_unit(u) for u in _rows(row["units"], "principal.units")]
            if not units or len(set(units)) != len(units):
                raise ServicePlanError(
                    "each principal needs explicit unique service units"
                )
            declared[(kind, identity)] = {
                "account": account,
                "units": units,
                "endpoint": _endpoint(row["endpoint"]) if kind == "service" else None,
            }
        if ids != expected:
            raise ServicePlanError(f"missing {kind} bindings: {sorted(expected - ids)}")

    blockers = []
    domains: dict[tuple[str, str], str] = {}
    uid_domains: dict[int, tuple[str, str]] = {}
    actions = []
    bound_units = set()
    endpoint_keys = set()
    for (kind, identity), binding in sorted(declared.items()):
        service = services.get(identity) if kind == "service" else None
        domain = (
            ("actor", service["owner_actor"])
            if service and service["kind"] == "private"
            else (kind, identity)
        )
        account = binding["account"]
        if domain in domains and domains[domain] != account:
            blockers.append(
                f"{kind}:{identity}: private service and owner must use the same account"
            )
        domains[domain] = account
        uid = accounts.get(account)
        if uid is None or uid == 0:
            blockers.append(
                f"{kind}:{identity}: account must exist and must not be root"
            )
        elif uid in uid_domains and uid_domains[uid] != domain:
            blockers.append(
                f"{kind}:{identity}: UID is shared by different isolation domains"
            )
        else:
            uid_domains[uid] = domain
        # Delimiter concatenation is ambiguous across organization/actor IDs.
        principal_key = json.dumps(
            [spec.organization.id, *domain], separators=(",", ":")
        )
        namespace = (
            "/run/netns/po-" + hashlib.sha256(principal_key.encode()).hexdigest()
        )
        endpoint = binding["endpoint"]
        if endpoint:
            endpoint_key = (
                namespace,
                endpoint["transport"],
                endpoint["address"],
                endpoint["port"],
            )
            if endpoint_key in endpoint_keys:
                blockers.append(
                    f"{kind}:{identity}: endpoint collides within its namespace"
                )
            endpoint_keys.add(endpoint_key)
        if service and service["approval"] == "per-request":
            blockers.append(
                f"service:{identity}: request approval enforcement has no verified runtime adapter"
            )
        for scope, name in sorted(binding["units"]):
            key = (scope, name, account if scope == "user" else "")
            if key in bound_units:
                blockers.append(
                    f"{kind}:{identity}: unit is assigned more than once: {name}"
                )
            bound_units.add(key)
            current = observed.get(key)
            if current is None or current["account"] != account:
                blockers.append(
                    f"{kind}:{identity}: unit/account not confirmed by inventory: {name}"
                )
            actions.append(
                {
                    "principal": {"kind": kind, "id": identity},
                    "unit": name,
                    "scope": scope,
                    "account": account,
                    "observed_namespace": current["namespace"] if current else None,
                    "proposed_namespace": namespace,
                    "active": current["active"] if current else None,
                }
            )
    for key, row in observed.items():
        if row["account"] in domains.values() and key not in bound_units:
            blockers.append(
                f"unmapped service under contained account: {row['account']}:{row['name']}"
            )
    connections = [
        {
            "source": consumer,
            "target_service": service["id"],
            "endpoint": declared[("service", service["id"])]["endpoint"],
            "gateway_verified": False,
        }
        for service in policy["services"]
        for consumer in service["consumers"]
    ]
    return {
        "format_version": 1,
        "organization_id": spec.organization.id,
        "host_id": bindings["host_id"],
        "policy_sha256": policy["policy_sha256"],
        "status": "blocked" if blockers else "review-required",
        "deployable": False,
        "enforcement_applied": False,
        "blockers": sorted(set(blockers)),
        "units": actions,
        "connections": connections,
        "bindings_sha256": hashlib.sha256(
            json.dumps(bindings, sort_keys=True).encode()
        ).hexdigest(),
        "inventory_sha256": hashlib.sha256(
            json.dumps(inventory, sort_keys=True).encode()
        ).hexdigest(),
        "outside_scope_units": [
            row for key, row in sorted(observed.items()) if key not in bound_units
        ],
        "required_reviews": [
            "inventory completeness and provenance",
            "all account processes and user managers",
            "DNS, internet egress and NAT",
            "shared-service authenticated gateways",
            "socket activation and on-demand launchers",
            "unit hardening and profile preservation",
            "host/peer/IPv6 firewall paths",
            "approval protocol and credential bypass",
            "Linux syntax verification, operator approval and rollback tests",
        ],
    }
