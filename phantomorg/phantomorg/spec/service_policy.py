"""Portable service intent; never Linux bindings or runtime approval grants."""

from __future__ import annotations

from dataclasses import dataclass


class ServicePolicyError(ValueError):
    """Invalid service policy shape or references."""


def _mapping(value: object, required: set[str], optional: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise ServicePolicyError(f"{path}: expected a mapping")
    if set(value) - required - optional or required - set(value):
        raise ServicePolicyError(f"{path}: missing or unknown fields")
    return value


def _id(value: object, path: str) -> str:
    from .shape_validator import is_valid_identifier

    if not isinstance(value, str) or not is_valid_identifier(value):
        raise ServicePolicyError(f"{path}: invalid identifier")
    return value


def _list(value: object, path: str) -> list:
    if not isinstance(value, list):
        raise ServicePolicyError(f"{path}: expected a list")
    return value


@dataclass(frozen=True)
class ServiceConsumer:
    kind: str
    id: str


@dataclass(frozen=True)
class Service:
    id: str
    kind: str
    owner_actor: str | None
    consumers: tuple[ServiceConsumer, ...]
    approval: str
    approvers: tuple[str, ...]


@dataclass(frozen=True)
class ServicePolicy:
    services: tuple[Service, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ServicePolicy:
        policy = _mapping(
            raw, {"version", "default_access", "services"}, set(), "service_policy"
        )
        if type(policy["version"]) is not int or policy["version"] != 1:
            raise ServicePolicyError("service_policy.version: expected 1")
        if policy["default_access"] != "deny":
            raise ServicePolicyError("service_policy.default_access: expected deny")
        services = []
        ids = set()
        for entry in _list(policy["services"], "service_policy.services"):
            service = _mapping(
                entry,
                {"id", "kind", "consumers", "approval", "approvers"},
                {"owner_actor"},
                "service_policy.services[]",
            )
            sid = _id(service["id"], "service.id")
            if sid in ids:
                raise ServicePolicyError(f"duplicate service: {sid}")
            ids.add(sid)
            kind = service["kind"]
            if kind not in ("private", "shared"):
                raise ServicePolicyError(
                    f"service {sid}: kind must be private or shared"
                )
            owner = service.get("owner_actor")
            if kind == "private":
                owner = _id(owner, f"service {sid}.owner_actor")
            elif "owner_actor" in service:
                raise ServicePolicyError(
                    f"service {sid}: shared service has no actor owner"
                )
            consumers = []
            for entry in _list(service["consumers"], f"service {sid}.consumers"):
                consumer = _mapping(entry, {"kind", "id"}, set(), "consumer")
                if consumer["kind"] not in ("actor", "service"):
                    raise ServicePolicyError(f"service {sid}: invalid consumer kind")
                consumers.append(
                    ServiceConsumer(
                        consumer["kind"], _id(consumer["id"], "consumer.id")
                    )
                )
            if len(set(consumers)) != len(consumers):
                raise ServicePolicyError(f"service {sid}: duplicate consumers")
            if kind == "private" and (
                owner is None or consumers != [ServiceConsumer("actor", owner)]
            ):
                raise ServicePolicyError(
                    f"service {sid}: only its owning actor may consume a private service"
                )
            approval = service["approval"]
            if approval not in ("none", "per-request"):
                raise ServicePolicyError(f"service {sid}: invalid approval mode")
            approvers = tuple(
                _id(a, "approver") for a in _list(service["approvers"], "approvers")
            )
            if len(set(approvers)) != len(approvers):
                raise ServicePolicyError(f"service {sid}: duplicate approvers")
            if (approval == "per-request") != bool(approvers):
                raise ServicePolicyError(
                    f"service {sid}: per-request approval requires approvers; none forbids them"
                )
            services.append(
                Service(sid, kind, owner, tuple(consumers), approval, approvers)
            )
        return cls(tuple(services))

    def reference_errors(self, actor_ids: set[str]) -> list[str]:
        services = {s.id: s for s in self.services}
        errors = []
        for service in self.services:
            actor_refs = set(service.approvers)
            if service.owner_actor:
                actor_refs.add(service.owner_actor)
            for consumer in service.consumers:
                if consumer.kind == "actor":
                    actor_refs.add(consumer.id)
                elif consumer.id not in services:
                    errors.append(
                        f"service {service.id}: unknown consumer service {consumer.id}"
                    )
                elif services[consumer.id].kind != "shared":
                    errors.append(
                        f"service {service.id}: private services cannot act as shared service principals"
                    )
                elif consumer.id == service.id:
                    errors.append(
                        f"service {service.id}: self-consumer is not an inter-service grant"
                    )
            for actor in sorted(actor_refs - actor_ids):
                errors.append(f"service {service.id}: unknown actor {actor}")
        return errors
