"""G1 producer canaries: projection must precede the ordinary compiler."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from phantomorg.cli import main
from phantomorg.compiler.boundary import BoundaryBuildError, build_candidate

FIXTURE = (
    Path(__file__).parents[1] / "organizations" / "verdant-aquaponics" / "org.yaml"
)
ORG_CANARY = "ORG_PLANE_CANARY_8c5e9f"
SCHEMA = (
    Path(__file__).parents[1] / "docs" / "containment" / "projection-v1.schema.json"
)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _inputs(tmp_path):
    source = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    source["humans"][0]["email"] = f"{ORG_CANARY}@example.invalid"
    source["roles"][0]["description"] = ORG_CANARY
    source["actors"][0]["tools"].append(ORG_CANARY)
    source["roles"][3]["reports_to"] = None
    source["departments"][1]["parent"] = None
    source["actors"][3]["tools"].append(ORG_CANARY)
    source["roles"][3]["reports_to_human"] = ORG_CANARY
    source["communication"]["channels"]["human"]["group"] = ORG_CANARY
    org_path = tmp_path / "org.yaml"
    org_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
    declaration = {
        "schema_version": 1,
        "declaration_id": "synthetic-project-declaration",
        "host_id": "synthetic-host",
        "inventory_digest": _digest(b"synthetic inventory"),
        "authorized_at": "2026-09-21T12:00:00Z",
        "authorized_by": "synthetic-owner",
        "boundaries": [
            {
                "boundary_id": "greenroot",
                "workspace_ids": ["greenroot-workspace"],
                "workloads": [{"workload_id": "dana", "os_identity": "svc-dana"}],
                "store_ids": ["greenroot-persona-store"],
                "shared_record_isolation": "not_provided_at_g1",
            }
        ],
        "exclusions": [],
    }
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    policy = {
        "format_version": 1,
        "boundary_id": "greenroot",
        "declaration_digest": _digest(declaration_path.read_bytes()),
        "source_digest": _digest(org_path.read_bytes()),
        "organization": {
            "id": "greenroot",
            "name": "Greenroot",
            "sector": "project",
            "languages": ["es", "en"],
            "default_language": "es",
        },
        "communication": {
            "request_id_format": "GR-{yyyymmdd}-{seq4}",
            "message_types": ["REQUEST", "INFORM"],
            "max_hops": 1,
        },
        "department_ids": ["operaciones"],
        "role_ids": ["project_lead"],
        "actor_ids": ["dana"],
        "human_ids": ["mirta"],
        "access_level_ids": ["level-2"],
        "security_category_ids": ["category-1", "category-2"],
    }
    policy_path = tmp_path / "projection.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    return org_path, policy_path, declaration_path, policy


def test_compiler_output_excludes_other_boundary_and_unconverted_adapters(tmp_path):
    source, policy_path, declaration_path, policy = _inputs(tmp_path)
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(
        policy
    )
    out = tmp_path / "candidate"
    manifest = build_candidate(source, policy_path, declaration_path, out)
    assert manifest["actor_ids"] == ["dana"]
    assert (out / "HUMANS.md").read_text(encoding="utf-8").find("mirta") != -1
    for file in out.rglob("*"):
        if file.is_file():
            contents = file.read_bytes()
            assert ORG_CANARY.encode() not in contents, file
            assert b"@dana_bot" not in contents, file
    assert "marco" not in (out / "HUMANS.md").read_text(encoding="utf-8")
    assert not list(out.rglob("phantomchat.json"))
    with pytest.raises(BoundaryBuildError, match="must not already exist"):
        build_candidate(source, policy_path, declaration_path, out)


def test_digest_change_and_unselected_reference_fail_closed(tmp_path):
    source, policy_path, declaration_path, policy = _inputs(tmp_path)
    source.write_bytes(source.read_bytes() + b"\n# changed\n")
    with pytest.raises(BoundaryBuildError, match="source digest"):
        build_candidate(source, policy_path, declaration_path, tmp_path / "candidate")
    policy["source_digest"] = _digest(source.read_bytes())
    policy["role_ids"] = ["project_lead"]
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["roles"][3]["reports_to"] = "chief_of_staff"
    source.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    policy["source_digest"] = _digest(source.read_bytes())
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(BoundaryBuildError, match="projected references invalid"):
        build_candidate(source, policy_path, declaration_path, tmp_path / "candidate")


def test_policy_cannot_enable_legacy_channels(tmp_path):
    source, policy_path, declaration_path, policy = _inputs(tmp_path)
    policy["communication"]["channels"] = {"human": {"platform": "telegram"}}
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(
        BoundaryBuildError, match="cannot enable communication adapters"
    ):
        build_candidate(source, policy_path, declaration_path, tmp_path / "candidate")


def test_declaration_mismatch_fails_closed(tmp_path):
    source, policy_path, declaration_path, policy = _inputs(tmp_path)
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    declaration["boundaries"][0]["workloads"][0]["workload_id"] = "marco"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    with pytest.raises(BoundaryBuildError, match="declaration digest"):
        build_candidate(source, policy_path, declaration_path, tmp_path / "candidate")
    policy["declaration_digest"] = _digest(declaration_path.read_bytes())
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(BoundaryBuildError, match="declared workloads"):
        build_candidate(source, policy_path, declaration_path, tmp_path / "candidate")


def test_sequential_cross_boundary_builds_do_not_reuse_output(tmp_path):
    source, policy_path, declaration_path, policy = _inputs(tmp_path)
    build_candidate(source, policy_path, declaration_path, tmp_path / "project")
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    declaration["boundaries"][0]["boundary_id"] = "org"
    declaration["boundaries"][0]["workloads"][0]["workload_id"] = "marco"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    policy.update(
        {
            "boundary_id": "org",
            "declaration_digest": _digest(declaration_path.read_bytes()),
            "department_ids": ["direccion"],
            "role_ids": ["ceo"],
            "actor_ids": ["marco"],
            "human_ids": ["mar"],
            "access_level_ids": ["level-3"],
            "security_category_ids": [
                "category-0",
                "category-1",
                "category-2",
                "category-3",
            ],
        }
    )
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    build_candidate(source, policy_path, declaration_path, tmp_path / "org")
    assert ORG_CANARY in (tmp_path / "org" / "HUMANS.md").read_text(encoding="utf-8")
    assert ORG_CANARY not in (tmp_path / "project" / "HUMANS.md").read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / "project" / "marco").exists()


def test_cli_builds_candidate(tmp_path):
    source, policy_path, declaration_path, _ = _inputs(tmp_path)
    out = tmp_path / "candidate"
    result = CliRunner().invoke(
        main,
        [
            "build-boundary",
            "--org",
            str(source),
            "--projection",
            str(policy_path),
            "--declaration",
            str(declaration_path),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "candidate-manifest.json").exists()
