"""Read-only document workflow contracts, dependency validation and planning.

Organization-specific policy belongs in installation data, not this module.
Planning never authenticates a remote sender or executes tool bindings.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .access import (
    can_read,
    can_write,
    normalize_category,
    policy_hash,
    validate_org_schema,
)


class WorkflowError(ValueError):
    """Invalid contracts or unauthorized/ambiguous workflow policy."""


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WorkflowError("duplicate JSON object key")
            result[key] = value
        return result

    with Path(path).open(encoding="utf-8") as source:
        return json.load(source, object_pairs_hook=unique_object)


def asset(name):
    return files("phantomdocs").joinpath("workflow_assets", name)


def validate_contract(document, kind):
    schema = json.loads(asset("contract.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator({**schema, "$ref": f"#/$defs/{kind}"})
    errors = sorted(validator.iter_errors(document), key=lambda e: str(list(e.path)))
    if errors:
        # Validation messages can include input values. Do not echo payloads or
        # credentials accidentally embedded in invalid fields.
        where = "/".join(str(p) for p in errors[0].path) or "root"
        raise WorkflowError(
            f"invalid {kind} contract at {where}: {errors[0].validator}"
        )


def digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def reference_is_safe(ref):
    try:
        parsed = urlsplit(ref)
        if (
            parsed.query
            or parsed.fragment
            or parsed.password
            or any(ord(c) < 32 for c in ref)
        ):
            return False
        if parsed.scheme == "gdrive":
            import re

            return bool(re.fullmatch(r"gdrive://[A-Za-z0-9_-]+", ref))
        if parsed.scheme == "file":
            return not parsed.netloc and parsed.path.startswith("/")
        if parsed.scheme == "ssh":
            _ = parsed.port  # reject malformed ports without opening a connection
            return bool(parsed.hostname and parsed.path.startswith("/"))
    except ValueError:
        return False
    return False


def validate_job(job):
    validate_contract(job, "job")
    ids = [a["id"] for a in job["artifacts"]]
    refs = [a["ref"] for a in job["artifacts"]]
    if len(set(ids)) != len(ids) or len(set(refs)) != len(refs):
        raise WorkflowError("duplicate artifact id or reference")
    if not all(reference_is_safe(ref) for ref in refs):
        raise WorkflowError("unsafe or unsupported artifact reference")


def protocol_order(protocol):
    steps = {step["id"]: step for step in protocol["steps"]}
    if len(steps) != len(protocol["steps"]):
        raise WorkflowError("duplicate protocol step")
    for step in steps.values():
        if not set(step["needs"]) <= set(steps):
            raise WorkflowError("unknown step dependency")
    ordered, ancestors, pending = [], {}, set(steps)
    while pending:
        ready = sorted(s for s in pending if set(steps[s]["needs"]) <= set(ancestors))
        if not ready:
            raise WorkflowError("cyclic protocol dependencies")
        for sid in ready:
            parents = set(steps[sid]["needs"])
            ancestors[sid] = parents | set().union(*(ancestors[p] for p in parents))
            ordered.append(steps[sid])
            pending.remove(sid)
    initial = set(protocol["initial_roles"])
    produced = {}
    for step in ordered:
        available = initial | {
            role
            for role, producer in produced.items()
            if producer in ancestors[step["id"]]
        }
        if not set(step["inputs"]) <= available:
            raise WorkflowError("step inputs are not produced by its dependencies")
        for role in step["outputs"]:
            if role in produced or role in initial:
                raise WorkflowError(
                    "artifact roles must have one producer; preserve originals"
                )
            produced[role] = step["id"]
    final = set(protocol["final_roles"])
    indexes = [step for step in ordered if step["capability"] == "documents.index"]
    if not initial or not final or len(indexes) != 1:
        raise WorkflowError(
            "protocol requires initial/final roles and one indexing step"
        )
    index = indexes[0]
    if (
        not final <= set(index["inputs"])
        or index["outputs"]
        or set(steps) - {index["id"]} != ancestors[index["id"]]
    ):
        raise WorkflowError(
            "indexing must consume final artifacts after all processing steps"
        )
    return ordered


def load_registry(path=None):
    if path:
        registry = read_json(path)
        base = Path(path).resolve().parent
    else:
        registry = json.loads(asset("registry.json").read_text(encoding="utf-8"))
        base = asset("registry.json").parent
    validate_contract(registry, "registry")
    versions, instruction_hashes = {}, {}
    for protocol in registry["protocols"]:
        key = (protocol["id"], protocol["version"])
        if key in versions:
            raise WorkflowError("duplicate protocol id/version")
        protocol_order(protocol)
        relative = Path(protocol["instructions"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or ":" in str(relative)
            or "\\" in protocol["instructions"]
        ):
            raise WorkflowError("protocol instructions must be a local relative file")
        instruction = base.joinpath(str(relative))
        # A custom registry may not escape its installed directory through links.
        if path and not instruction.resolve().is_relative_to(base):
            raise WorkflowError("protocol instructions escape the registry directory")
        instruction_hashes[key] = hashlib.sha256(instruction.read_bytes()).hexdigest()
        versions[key] = protocol
    return registry, versions, instruction_hashes


def resolve_processor(config_path, state_path=None):
    # Match the precedence documented in PhantomOrg's Phantombot integration.
    # Missing/invalid explicit state is an error, never a silent fallback.
    if state_path:
        state = read_json(state_path)
        if not isinstance(state, dict):
            raise WorkflowError("invalid Phantombot state")
        if "default_persona" in state:
            actor = state["default_persona"]
            if not isinstance(actor, str) or not actor.strip():
                raise WorkflowError("invalid Phantombot state default persona")
            source = "phantombot-state"
        else:
            actor = None
    else:
        actor = None
    if actor is None:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with Path(config_path).open("rb") as source_file:
            config = tomllib.load(source_file)
        actor, source = config.get("default_persona"), "phantombot-config"
    if not isinstance(actor, str) or not actor.strip():
        raise WorkflowError("Phantombot default persona is missing or invalid")
    return actor, source


def plan(job, installation, org, registry_data, processor, caller):
    validate_job(job)
    validate_contract(installation, "installation")
    validate_org_schema(org)
    registry, protocols, instruction_hashes = registry_data
    organization = org.get("organization", {}).get("id")
    if (
        organization != job["organization"]
        or organization != installation["organization"]
    ):
        raise WorkflowError("organization mismatch")
    inbox = (
        org.get("documents", {}).get("inboxes", {}).get(installation["intake"]["inbox"])
    )
    if not isinstance(inbox, dict) or not isinstance(inbox.get("id"), str):
        raise WorkflowError("intake inbox is not declared in PhantomOrg")
    intake_ref = "gdrive://" + inbox["id"]
    if not reference_is_safe(intake_ref):
        raise WorkflowError("invalid declared intake folder id")
    domain_entries = org.get("documents", {}).get("naming", {}).get("domains", [])
    domains = {d["id"]: d for d in domain_entries}
    if len(domains) != len(domain_entries):
        raise WorkflowError("duplicate declared document domain")
    context = job["context"]
    if context["domain"] not in domains or context["requesting_domain"] not in domains:
        raise WorkflowError("unknown declared document/requesting domain")
    domain = domains[context["domain"]]
    types = [t for t in domain.get("types", []) if t["id"] == context["document_type"]]
    if len(types) != 1:
        raise WorkflowError("unknown or ambiguous declared document type")
    category = normalize_category(types[0].get("category", domain.get("category", "")))
    if category not in org.get("policies", {}).get("security_categories", {}):
        raise WorkflowError("classification is not declared in PhantomOrg")
    if not can_read(org, caller, category):
        raise WorkflowError("caller cannot read this classification")
    selectors = [(r["domain"], r["document_type"]) for r in installation["routes"]]
    if len(set(selectors)) != len(selectors):
        raise WorkflowError("ambiguous installation route")
    routes = [
        r
        for r in installation["routes"]
        if (r["domain"], r["document_type"])
        == (context["domain"], context["document_type"])
    ]
    if len(routes) != 1:
        raise WorkflowError("no explicit installation route for document type")
    route = routes[0]
    if ".." in route["folder"]:
        raise WorkflowError("invalid logical folder")
    declared_owners = set()
    for owner_domain in route["owner_domains"]:
        owner_domain = (
            context["requesting_domain"]
            if owner_domain == "$requesting_domain"
            else owner_domain
        )
        if owner_domain not in domains:
            raise WorkflowError("unknown owner domain")
        declared_owners.update(domains[owner_domain].get("owners", []))
    owners = sorted(set(route.get("owners", declared_owners)))
    known_owners = {r["id"] for r in org.get("roles", [])} | {
        a["id"] for a in org.get("actors", [])
    }
    if (
        not owners
        or not set(owners) <= declared_owners
        or not set(owners) <= known_owners
    ):
        raise WorkflowError("owners must be non-empty declared domain owners")
    if not can_write(org, job["requester"], category, owners):
        raise WorkflowError(
            "requester is not authorized for the selected document context"
        )
    actor, processor_source = processor
    if not can_write(org, actor, category, owners):
        raise WorkflowError(
            "default processor cannot write this classification/ownership"
        )
    key = (route["protocol"], route["protocol_version"])
    if key not in protocols:
        raise WorkflowError("installed protocol id/version does not match the route")
    protocol = protocols[key]
    if {a["role"] for a in job["artifacts"]} != set(protocol["initial_roles"]):
        raise WorkflowError("input artifact roles do not match the selected protocol")
    ordered = protocol_order(protocol)
    processor_actor = next(
        (a for a in org.get("actors", []) if a.get("id") == actor), {}
    )
    granted_tools = set(processor_actor.get("tools", []))
    excluded_tools = set(processor_actor.get("tools_excluded", []))
    for step in ordered:
        capability = step["capability"]
        root_capability = capability.split(".", 1)[0]
        if (
            root_capability not in granted_tools
            or root_capability in excluded_tools
            or capability in excluded_tools
        ):
            raise WorkflowError("processor lacks the declared logical tool capability")
    if any(s["capability"] == "documents.brand" for s in ordered) and not route.get(
        "branding_profile"
    ):
        raise WorkflowError("branding requires an explicit installation profile")
    steps, missing = [], set()
    for step in ordered:
        binding = installation["tools"].get(step["capability"])
        available = False
        if binding:
            entrypoint = binding["entrypoint"]
            available = bool(shutil.which(entrypoint))
            if Path(entrypoint).is_absolute():
                available = Path(entrypoint).is_file() and os.access(
                    entrypoint, os.X_OK
                )
        if not available:
            missing.add(step["capability"])
        steps.append(
            {
                **step,
                "tool": {"id": binding["id"], "version": binding["version"]}
                if binding
                else None,
                "availability": "present" if available else "missing",
                "runtime": "not-observed",
            }
        )
    result = {
        "version": 1,
        "kind": "document-workflow-plan",
        "organization": organization,
        "request_id": job["request_id"],
        "requester": job["requester"],
        "context": context,
        "intake_ref": intake_ref,
        "processor": {"actor": actor, "source": processor_source},
        "protocol": {
            "id": key[0],
            "version": key[1],
            "hash": digest(protocol),
            "instructions_hash": instruction_hashes[key],
        },
        "classification": {"category": category, "owners": owners},
        "destinations": {
            "logical_folder": route["folder"],
            "archive_ref": route.get("archive_ref"),
        },
        "branding_profile": route.get("branding_profile"),
        "steps": steps,
        "artifacts": job["artifacts"],
        "final_roles": protocol["final_roles"],
        "pins": {
            "job": digest(job),
            "org": policy_hash(org),
            "registry": digest(registry),
            "installation": digest(installation),
        },
        "status": "blocked" if missing else "planned",
        "missing_capabilities": sorted(missing),
        "execution_ready": False,
        "required_before_execution": [
            "authenticated-intake",
            "verified-tool-versions",
            "artifact-validation",
            "executor-and-job-ledger",
        ],
    }
    result["plan_hash"] = digest(result)
    return deepcopy(result)


def dependency_graph(planned):
    """Declared graph for observatories; never claim observed execution."""
    prefix = f"{planned['organization']}:{planned['request_id']}:"
    components = [
        {
            "id": prefix + s["id"],
            "kind": "workflow-step",
            "capability": s["capability"],
            "tool": s["tool"],
            "availability": s["availability"],
            "runtime": "not-observed",
        }
        for s in planned["steps"]
    ]
    relations = [
        {"from": prefix + s["id"], "to": prefix + parent, "kind": "depends-on"}
        for s in planned["steps"]
        for parent in s["needs"]
    ]
    return deepcopy(
        {
            "version": 1,
            "kind": "document-workflow-dependencies",
            "plan_hash": planned["plan_hash"],
            "organization": planned["organization"],
            "request_id": planned["request_id"],
            "classification": planned["classification"],
            "processor": planned["processor"],
            "components": components,
            "relations": relations,
            "observation": "declared-only",
        }
    )
