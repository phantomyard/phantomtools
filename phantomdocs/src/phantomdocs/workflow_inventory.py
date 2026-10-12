"""Read-only inventory of explicitly declared runtime files.

Never launches a tool, imports a deployment script, reads credentials, scans a
filesystem tree or writes configuration/cache/store/Drive. Observed hashes are
candidates for operator review, not automatic runtime approval.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from copy import deepcopy
from pathlib import Path

from .workflow_processing import private_directory
from .workflows import WorkflowError, digest, validate_contract


def _credential_path(path):
    name = path.name.casefold().replace("_", "-")
    return (
        name.startswith(".env")
        or "service-account" in name
        or "credential" in name
        or re.search(r"(?:^|[.\-])(tokens?|secrets?|nsec)(?:[.\-]|$)", name) is not None
        or name
        in {
            "token",
            "token.json",
            "tokens.json",
            "credentials.json",
            "service-account.json",
            "nsec",
            "nsec.txt",
            "id-rsa",
            "id-ed25519",
        }
    )


def runtime_inventory(installation, *, tools=None, processing=None):
    candidate = deepcopy(installation)
    if tools is not None:
        if not isinstance(tools, dict):
            raise WorkflowError("tool overlay must be an explicit object")
        candidate["tools"] = deepcopy(tools)
    if processing is not None:
        candidate["processing"] = deepcopy(processing)
    validate_contract(candidate, "installation")
    entries = []
    for capability, binding in sorted(candidate["tools"].items()):
        for field, pin in (("entrypoint", "sha256"), ("script", "script_sha256")):
            if field in binding:
                entries.append(
                    (f"tools.{capability}.{field}", binding[field], binding.get(pin))
                )
    profile = candidate.get("processing")
    if profile:
        for role, specification in sorted(profile["inputs"].items()):
            probe = specification.get("probe", {})
            for field, pin in (("entrypoint", "sha256"), ("script", "script_sha256")):
                if field in probe:
                    entries.append(
                        (
                            f"processing.inputs.{role}.probe.{field}",
                            probe[field],
                            probe.get(pin),
                        )
                    )
        for index, resource in enumerate(profile["resources"]):
            entries.append(
                (f"processing.resources.{index}", resource["path"], resource["sha256"])
            )
    observations, cache = [], {}
    for label, value, pin in entries:
        path = Path(value)
        row = {
            "descriptor": label,
            "path": value,
            "declared_sha256": pin,
            "observed_sha256": None,
        }
        if _credential_path(path):
            row.update(path=None, status="excluded-credential-path")
            observations.append(row)
            continue
        if not path.is_absolute():
            row["status"] = "absolute-path-required"
            observations.append(row)
            continue
        try:
            resolved = path.resolve(strict=True)
            if _credential_path(resolved):
                row.update(path=None, status="excluded-credential-path")
                observations.append(row)
                continue
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode):
                row["status"] = "regular-file-required"
                observations.append(row)
                continue
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            key = (str(resolved), signature)
            if key not in cache:
                value_hash = hashlib.sha256()
                with resolved.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        value_hash.update(chunk)
                    after = os.fstat(stream.fileno())
                current = resolved.stat()
                if any(
                    (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                    != signature
                    for value in (before, after, current)
                ):
                    row["status"] = "changed-during-read"
                    observations.append(row)
                    continue
                cache[key] = value_hash.hexdigest()
            observed = cache[key]
            row.update(
                resolved_path=str(resolved),
                observed_sha256=observed,
                size=info.st_size,
                mode=f"{stat.S_IMODE(info.st_mode):04o}",
                symlink=path.is_symlink(),
                group_or_world_writable=bool(stat.S_IMODE(info.st_mode) & 0o022),
            )
            row["status"] = (
                "unconfigured-pin"
                if not pin or pin == "0" * 64
                else "match"
                if pin == observed
                else "mismatch"
            )
            if label.endswith(".entrypoint"):
                row["executable_access"] = os.access(resolved, os.X_OK)
                if not row["executable_access"]:
                    row["status"] = "executable-access-required"
        except (OSError, RuntimeError):
            row["status"] = "missing-or-unreadable"
        observations.append(row)
    directory = {"configured": profile is not None, "status": "profile-missing"}
    if profile:
        try:
            private_directory(profile["work_root"])
            directory["status"] = "private-directory-present"
        except (OSError, WorkflowError):
            directory["status"] = "private-directory-required"
    blockers = [
        row["descriptor"] + ":" + row["status"]
        for row in observations
        if row["status"] != "match"
    ]
    if directory["status"] != "private-directory-present":
        blockers.append(directory["status"])
    if not observations:
        blockers.append("no-declared-runtime-files")
    return {
        "version": 1,
        "kind": "workflow-runtime-inventory",
        "organization": candidate["organization"],
        "input_installation_hash": digest(installation),
        "candidate_installation_hash": digest(candidate),
        "overlays_applied": tools is not None or processing is not None,
        "observations": observations,
        "work_directory": directory,
        "blockers": blockers,
        "pins_match": not blockers,
        "read_only": True,
        "execution_ready": False,
        "requires_operator_review": True,
        "coverage": "explicitly-declared-files-only",
        "not_verified": [
            "source-provenance",
            "imported-dependencies",
            "models-not-listed",
            "renderer-metadata-and-visual-QA",
            "credentials-and-data-disclosure-policy",
            "OS-resource-limits",
            "live-job-compatibility",
        ],
    }
