"""Fail-closed, explicit input projection for a G1 candidate build.

This module prepares compiler inputs; it does not authorize a boundary or
install a bundle. The operator-reviewed policy is tied to a source digest and
must enumerate every model section whose content may enter the bundle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

import yaml

from ..spec.loader import _UniqueKeyLoader
from ..spec.model import OrgSpec
from ..spec.shape_validator import ShapeError, validate_shape
from ..validator.graph import EscalationCycleError, check_no_cycles
from ..validator.refs import check_references
from .build import build


class BoundaryBuildError(ValueError):
    """An input or output cannot be proven to match the boundary policy."""


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SELECTORS = (
    "department_ids",
    "role_ids",
    "actor_ids",
    "human_ids",
    "access_level_ids",
    "security_category_ids",
)
_TOP_KEYS = {
    "format_version",
    "boundary_id",
    "declaration_digest",
    "source_digest",
    "organization",
    "communication",
    *_SELECTORS,
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_policy(path: Path) -> tuple[dict, str]:
    try:
        policy_bytes = path.read_bytes()
        data = json.loads(policy_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryBuildError(f"projection policy cannot be read: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _TOP_KEYS:
        raise BoundaryBuildError("projection policy has missing or unknown fields")
    if data["format_version"] != 1:
        raise BoundaryBuildError("unsupported projection policy version")
    if not isinstance(data["boundary_id"], str) or not _ID.fullmatch(
        data["boundary_id"]
    ):
        raise BoundaryBuildError("invalid boundary_id")
    for key in ("declaration_digest", "source_digest"):
        if not isinstance(data[key], str) or not _DIGEST.fullmatch(data[key]):
            raise BoundaryBuildError(f"invalid {key}")
    for key in _SELECTORS:
        values = data[key]
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v and v != "*" for v in values
        ):
            raise BoundaryBuildError(f"{key} must be an explicit ID list")
        if len(values) != len(set(values)):
            raise BoundaryBuildError(f"{key} contains duplicate IDs")
    if not data["actor_ids"] or not data["role_ids"] or not data["department_ids"]:
        raise BoundaryBuildError("projection requires actors, roles and departments")
    for key in ("organization", "communication"):
        if not isinstance(data[key], dict):
            raise BoundaryBuildError(f"{key} must be an explicit object")
    if "channels" in data["communication"]:
        raise BoundaryBuildError("G1 candidates cannot enable communication adapters")
    return data, _sha256(policy_bytes)


def _select(items: list[dict], ids: list[str], label: str) -> list[dict]:
    by_id = {item["id"]: item for item in items}
    missing = set(ids) - set(by_id)
    if missing:
        raise BoundaryBuildError(f"unknown {label}: {', '.join(sorted(missing))}")
    return [copy.deepcopy(by_id[item_id]) for item_id in ids]


def _check_declaration(path: Path, policy: dict) -> None:
    """Bind the candidate to the exact A0 host declaration, not its authority."""
    try:
        content = path.read_bytes()
        declaration = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryBuildError(f"boundary declaration cannot be read: {exc}") from exc
    if _sha256(content) != policy["declaration_digest"]:
        raise BoundaryBuildError("declaration digest differs from reviewed policy")
    if not isinstance(declaration, dict) or declaration.get("schema_version") != 1:
        raise BoundaryBuildError("invalid A0 boundary declaration")
    boundaries = declaration.get("boundaries")
    if not isinstance(boundaries, list):
        raise BoundaryBuildError("declaration has no boundaries")
    matching = [
        item
        for item in boundaries
        if isinstance(item, dict) and item.get("boundary_id") == policy["boundary_id"]
    ]
    if len(matching) != 1:
        raise BoundaryBuildError(
            "declaration must contain exactly one matching boundary"
        )
    workloads = matching[0].get("workloads")
    if not isinstance(workloads, list) or not all(
        isinstance(item, dict) and isinstance(item.get("workload_id"), str)
        for item in workloads
    ):
        raise BoundaryBuildError("declaration workloads invalid")
    if set(policy["actor_ids"]) != {item["workload_id"] for item in workloads}:
        raise BoundaryBuildError("projected actors differ from declared workloads")
    if matching[0].get("shared_record_isolation") != "not_provided_at_g1":
        raise BoundaryBuildError("declaration must state G1 record-isolation limit")


def project_source(
    source: Path, policy_path: Path, declaration_path: Path
) -> tuple[OrgSpec, dict]:
    """Return only explicitly selected source fields and provenance."""
    policy, policy_digest = _read_policy(policy_path)
    _check_declaration(declaration_path, policy)
    try:
        source_bytes = source.read_bytes()
    except OSError as exc:
        raise BoundaryBuildError(f"source cannot be read: {exc}") from exc
    source_digest = _sha256(source_bytes)
    if source_digest != policy["source_digest"]:
        raise BoundaryBuildError("source digest differs from reviewed policy")
    try:
        raw = yaml.load(source_bytes.decode("utf-8"), Loader=_UniqueKeyLoader)  # nosec B506
        validate_shape(raw)
    except (UnicodeError, yaml.YAMLError, ShapeError, RecursionError) as exc:
        raise BoundaryBuildError(f"source org.yaml invalid: {exc}") from exc

    projected = {
        "version": raw["version"],
        "organization": copy.deepcopy(policy["organization"]),
        "departments": _select(
            raw["departments"], policy["department_ids"], "departments"
        ),
        "roles": _select(raw["roles"], policy["role_ids"], "roles"),
        "actors": _select(raw["actors"], policy["actor_ids"], "actors"),
        "humans": _select(raw.get("humans", []), policy["human_ids"], "humans"),
        "policies": {
            "access_levels": {},
            "security_categories": {},
        },
        "escalation_matrix": [],
        "communication": copy.deepcopy(policy["communication"]),
    }
    # A selected actor's legacy capabilities and endpoints are not grants.
    # The G1 candidate carries no unconverted connector or transport adapter.
    for actor in projected["actors"]:
        actor["tools"] = []
        actor["tools_excluded"] = []
        actor["telegram_bot"] = None
        actor["npub"] = None
        actor["email"] = None
    # A role's human escalation text is not an ID and can name an excluded
    # human. Require an explicit boundary-owned replacement in a later policy.
    for role in projected["roles"]:
        role["reports_to_human"] = None
    for selector, source_key in (
        ("access_level_ids", "access_levels"),
        ("security_category_ids", "security_categories"),
    ):
        available = raw["policies"][source_key]
        missing = set(policy[selector]) - set(available)
        if missing:
            raise BoundaryBuildError(
                f"unknown {source_key}: {', '.join(sorted(missing))}"
            )
        projected["policies"][source_key] = {
            key: copy.deepcopy(available[key]) for key in policy[selector]
        }

    # Escalation text can contain private names or routing instructions. G1
    # intentionally inherits none; a later reviewed boundary-specific policy
    # may add its own routes. Likewise, opaque `documents` never crosses here.
    try:
        validate_shape(projected)
        spec = OrgSpec.from_dict(projected)
    except (ShapeError, KeyError, TypeError, ValueError) as exc:
        raise BoundaryBuildError(f"projected model invalid: {exc}") from exc
    problems = check_references(spec)
    try:
        check_no_cycles(spec)
    except EscalationCycleError as exc:
        problems.append(str(exc))
    if problems:
        raise BoundaryBuildError("projected references invalid: " + "; ".join(problems))

    projection_bytes = json.dumps(projected, sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    provenance = {
        "format_version": 1,
        "boundary_id": policy["boundary_id"],
        "declaration_digest": policy["declaration_digest"],
        "source_digest": source_digest,
        "policy_digest": policy_digest,
        "projection_digest": _sha256(projection_bytes),
        "actor_ids": policy["actor_ids"],
    }
    return spec, provenance


def build_candidate(
    source: Path, policy_path: Path, declaration_path: Path, out_dir: Path
) -> dict:
    """Compile a fresh candidate and record every output path and digest.

    The output must not exist. In particular, an old candidate must never be
    merged into a new boundary build because the ordinary compiler preserves
    runtime-owned and manually edited content.
    """
    spec, provenance = project_source(source, policy_path, declaration_path)
    if out_dir.exists() or out_dir.is_symlink():
        raise BoundaryBuildError("candidate output must not already exist")
    parent = out_dir.parent.resolve()
    if not parent.is_dir():
        raise BoundaryBuildError("candidate parent directory does not exist")
    temp = Path(tempfile.mkdtemp(prefix=".boundary-candidate-", dir=parent))
    try:
        build(spec, temp)
        files: dict[str, str] = {}
        for path in sorted(temp.rglob("*")):
            if path.is_symlink():
                raise BoundaryBuildError("candidate contains a symlink")
            if path.is_file():
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise BoundaryBuildError("candidate contains a non-regular file")
                files[path.relative_to(temp).as_posix()] = _sha256(path.read_bytes())
        provenance["files"] = files
        (temp / "candidate-manifest.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.rename(temp, out_dir)
    except Exception:
        shutil.rmtree(temp)
        raise
    return provenance
