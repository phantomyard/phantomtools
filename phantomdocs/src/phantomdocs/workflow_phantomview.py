"""Export declared installation dependencies, never document/job observations."""

from __future__ import annotations

import re
from copy import deepcopy

from .workflows import WorkflowError, digest, validate_contract

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}\Z")
_KINDS = {"phantomyard", "service", "runtime", "library", "external", "data"}
_RELATIONS = {"requires", "optional", "reads", "publishes", "runs_on", "uses"}


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise WorkflowError("unsupported installation manifest")
    components = manifest.get("components", [])
    relations = manifest.get("relations", [])
    if (
        not isinstance(components, list)
        or not isinstance(relations, list)
        or len(components) > 150
        or len(relations) > 1000
    ):
        raise WorkflowError("installation manifest exceeds collector limits")
    identities = {}
    for item in components:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not _ID.fullmatch(item["id"])
            or item["id"].startswith(("persona:", "tool:"))
            or item["id"] in identities
            or not isinstance(item.get("org"), str)
            or not item["org"]
            or item.get("kind") not in _KINDS
        ):
            raise WorkflowError("invalid installation component")
        identities[item["id"]] = item["org"]
    for edge in relations:
        if (
            not isinstance(edge, dict)
            or edge.get("type") not in _RELATIONS
            or edge.get("from") not in identities
            or edge.get("to") not in identities
            or identities[edge["from"]] != identities[edge["to"]]
        ):
            raise WorkflowError("invalid installation relation")
    return manifest


def installation_map(installation, registry_data, base=None):
    """Build a static PhantomView v1 manifest with collision-safe additive merge.

    This is configuration disclosure, not an authorized document status export.
    No actor, route destination, file path, document reference or job enters it.
    Existing base entries and bindings are preserved, including their probes.
    Newly generated components have no executable or filesystem probes.
    """
    validate_contract(installation, "installation")
    result = deepcopy(base) if base is not None else {"version": 1}
    _validate_manifest(result)
    result.setdefault("components", [])
    result.setdefault("relations", [])
    existing = {c["id"]: c for c in result["components"]}
    org = installation["organization"]

    def component(key, name, kind, version=None):
        identity = "pdwf:" + digest([org, key])[:32]
        value = {
            "id": identity,
            "org": org,
            "name": name,
            "kind": kind,
            "probe": {"type": "none"},
            "description": "Declared workflow dependency; runtime status unknown.",
        }
        if version is not None:
            value["declared_version"] = version
        if identity in existing:
            if existing[identity] != value:
                raise WorkflowError("installation component collision requires review")
        else:
            existing[identity] = value
            result["components"].append(value)
        return identity

    def relation(source, target, kind):
        edge = {"from": source, "to": target, "type": kind}
        if edge not in result["relations"]:
            result["relations"].append(edge)

    package = component("package", "PhantomDocs workflows", "phantomyard")
    protocols = registry_data[1]
    selected = sorted(
        {(r["protocol"], r["protocol_version"]) for r in installation["routes"]}
    )
    for key in selected:
        if key not in protocols:
            raise WorkflowError("installation selects an unknown protocol")
        protocol = protocols[key]
        roles = set(protocol["initial_roles"] + protocol["final_roles"])
        for step in protocol["steps"]:
            roles.update(step["inputs"] + step["outputs"])
        artifacts = {
            role: component([key, "role", role], role, "data") for role in sorted(roles)
        }
        steps = {
            step["id"]: component(
                [key, "step", step["id"]],
                f"{key[0]}: {step['id']}",
                "service",
                key[1],
            )
            for step in protocol["steps"]
        }
        produced = {role for step in protocol["steps"] for role in step["outputs"]}
        archive = None
        if produced.intersection(protocol["final_roles"]):
            archive = component(
                [key, "archive"], "Independent organizational archiver", "external"
            )
            for role in protocol["final_roles"]:
                relation(archive, artifacts[role], "reads")
            for step in protocol["steps"]:
                if set(step["outputs"]).intersection(protocol["final_roles"]):
                    relation(archive, steps[step["id"]], "requires")
        for step in protocol["steps"]:
            identity = steps[step["id"]]
            relation(identity, package, "requires")
            for needed in step["needs"]:
                relation(identity, steps[needed], "requires")
            for role in step["inputs"]:
                relation(identity, artifacts[role], "reads")
            for role in step["outputs"]:
                relation(identity, artifacts[role], "publishes")
            if archive and step["capability"] == "documents.index":
                relation(identity, archive, "requires")
            binding = installation["tools"].get(step["capability"])
            if binding:
                runtime = component(
                    ["binding", step["capability"], binding["id"]],
                    binding["id"],
                    "runtime",
                    binding["version"],
                )
                relation(identity, runtime, "uses")
    return _validate_manifest(result)
