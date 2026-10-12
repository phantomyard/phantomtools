"""Document workflows are policy-driven, read-only and independent of AU."""

import json
import sys
from copy import deepcopy

import pytest
import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.workflows import (
    WorkflowError,
    dependency_graph,
    digest,
    load_registry,
    plan,
    protocol_order,
    read_json,
    resolve_processor,
    validate_contract,
    validate_job,
)


@pytest.fixture
def model():
    return {
        "version": 1,
        "organization": {"id": "example-org"},
        "roles": [
            {"id": "custodian", "access_level": "internal"},
            {"id": "project-lead", "access_level": "internal"},
        ],
        "actors": [
            {"id": "coordinator", "role": "custodian", "tools": ["documents"]},
            {"id": "remote-lead", "role": "project-lead"},
            {"id": "unmapped", "role": "missing"},
        ],
        "policies": {
            "access_levels": {"internal": {"categories": [2]}},
            "security_categories": {"category-2": {}, "category-3": {}},
        },
        "documents": {
            "inboxes": {
                "coordinator": {"name": "Organization intake", "id": "shared-intake"}
            },
            "naming": {
                "domains": [
                    {
                        "id": "projects",
                        "category": 2,
                        "owners": ["project-lead", "custodian"],
                        "types": [
                            {"id": "report"},
                            {"id": "minutes"},
                            {"id": "sensitive", "category": 3},
                        ],
                    },
                    {
                        "id": "administration",
                        "category": 2,
                        "owners": ["custodian"],
                        "types": [{"id": "record"}],
                    },
                ]
            },
        },
    }


@pytest.fixture
def installation():
    return {
        "version": 1,
        "organization": "example-org",
        "intake": {"inbox": "coordinator"},
        "processor": {"source": "phantombot-default"},
        "routes": [
            {
                "domain": "projects",
                "document_type": "report",
                "protocol": "reference-document",
                "protocol_version": "1.0.0",
                "folder": "project-reports",
                "owner_domains": ["projects"],
            }
        ],
        "tools": {
            "documents.index": {
                "id": "test-indexer",
                "version": "0.3.0",
                "entrypoint": sys.executable,
            }
        },
    }


@pytest.fixture
def job():
    return {
        "version": 1,
        "organization": "example-org",
        "request_id": "request-001",
        "requester": "remote-lead",
        "context": {
            "domain": "projects",
            "document_type": "report",
            "requesting_domain": "projects",
        },
        "artifacts": [
            {
                "id": "source",
                "role": "original",
                "ref": "gdrive://document-001",
                "media_type": "application/pdf",
            }
        ],
    }


def make_plan(job, installation, model, **kwargs):
    return plan(
        job,
        installation,
        model,
        load_registry(),
        kwargs.get("processor", ("coordinator", "phantombot-config")),
        kwargs.get("caller", "coordinator"),
    )


@pytest.mark.parametrize(
    "mime",
    [
        "application/pdf",
        "text/plain",
        "image/png",
        "audio/ogg",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
def test_arbitrary_media_types_do_not_require_transcription(
    job, installation, model, mime
):
    job["artifacts"][0]["media_type"] = mime
    result = make_plan(job, installation, model)
    assert result["status"] == "planned"
    assert not result["execution_ready"]
    assert result["classification"] == {
        "category": "category-2",
        "owners": ["custodian", "project-lead"],
    }
    assert [s["id"] for s in result["steps"]] == ["index"]
    assert result["required_before_execution"][0] == "authenticated-intake"


def test_second_organization_and_personas_are_configuration(job, installation, model):
    model["organization"]["id"] = job["organization"] = installation["organization"] = (
        "another-org"
    )
    model["actors"][0]["id"] = "new-default"
    result = make_plan(
        job,
        installation,
        model,
        processor=("new-default", "phantombot-state"),
        caller="new-default",
    )
    assert result["organization"] == "another-org"
    assert result["processor"]["actor"] == "new-default"


def test_processor_state_precedence_and_invalid_state(tmp_path):
    config, state = tmp_path / "config.toml", tmp_path / "state.json"
    config.write_text('default_persona = "config-persona"\n', encoding="utf-8")
    assert resolve_processor(config) == ("config-persona", "phantombot-config")
    state.write_text('{"default_persona":"runtime-persona"}', encoding="utf-8")
    assert resolve_processor(config, state) == ("runtime-persona", "phantombot-state")
    for value in [None, "", 3]:
        state.write_text(json.dumps({"default_persona": value}), encoding="utf-8")
        with pytest.raises(WorkflowError):
            resolve_processor(config, state)
    state.write_text("{}", encoding="utf-8")
    assert resolve_processor(config, state)[0] == "config-persona"


@pytest.mark.parametrize(
    "change",
    [
        "organization",
        "domain",
        "type",
        "route",
        "owners",
        "caller",
        "processor",
        "requester",
        "category",
        "version",
    ],
)
def test_unknown_or_unauthorized_context_fails_closed(job, installation, model, change):
    kwargs = {}
    if change == "organization":
        job["organization"] = "wrong-org"
    elif change == "domain":
        job["context"]["domain"] = "unknown"
    elif change == "type":
        job["context"]["document_type"] = "unknown"
    elif change == "route":
        installation["routes"][0]["document_type"] = "other"
    elif change == "owners":
        installation["routes"][0]["owners"] = ["unmapped"]
    elif change == "caller":
        kwargs["caller"] = "unmapped"
    elif change == "processor":
        kwargs["processor"] = ("unmapped", "phantombot-config")
    elif change == "requester":
        job["requester"] = "unmapped"
    elif change == "category":
        model["documents"]["naming"]["domains"][0]["category"] = "category-99"
    else:
        installation["routes"][0]["protocol_version"] = "9.9.9"
    with pytest.raises(WorkflowError):
        make_plan(job, installation, model, **kwargs)


def test_default_persona_does_not_gain_custody(job, installation, model):
    model["documents"]["naming"]["domains"][0]["owners"] = ["project-lead"]
    with pytest.raises(WorkflowError, match="default processor"):
        make_plan(job, installation, model)


def test_pins_are_deterministic_and_detached_from_inputs(job, installation, model):
    before = deepcopy((job, installation, model))
    first = make_plan(job, installation, model)
    assert first == make_plan(job, installation, model)
    assert (job, installation, model) == before
    job["artifacts"][0]["ref"] = "gdrive://new-source"
    installation["tools"]["documents.index"]["version"] = "0.4.0"
    assert first["artifacts"][0]["ref"] == "gdrive://document-001"
    assert first["steps"][0]["tool"]["version"] == "0.3.0"
    assert make_plan(job, installation, model)["plan_hash"] != first["plan_hash"]


def test_meeting_dependencies_branding_and_missing_tools(job, installation, model):
    job["context"]["document_type"] = "minutes"
    job["artifacts"][0].update(role="recording", media_type="video/mp4")
    installation["routes"][0].update(
        document_type="minutes",
        protocol="meeting-publication",
        branding_profile="company-brand",
    )
    result = make_plan(job, installation, model)
    assert result["status"] == "blocked"
    assert result["missing_capabilities"] == [
        "documents.brand",
        "documents.summarize",
        "documents.transcribe",
    ]
    assert result["steps"][-1]["inputs"] == [
        "published-transcription",
        "published-summary",
    ]
    graph = dependency_graph(result)
    assert graph["observation"] == "declared-only"
    assert len(graph["components"]) == 5 and len(graph["relations"]) == 5
    assert "artifacts" not in graph and "entrypoint" not in json.dumps(graph)
    del installation["routes"][0]["branding_profile"]
    with pytest.raises(WorkflowError, match="branding"):
        make_plan(job, installation, model)


@pytest.mark.parametrize(
    "failure", ["cycle", "unknown", "input", "overwrite", "index-early"]
)
def test_invalid_protocol_dependencies(failure):
    protocol = deepcopy(load_registry()[1][("meeting-publication", "1.0.0")])
    if failure == "cycle":
        protocol["steps"][0]["needs"] = ["index"]
    elif failure == "unknown":
        protocol["steps"][0]["needs"] = ["missing"]
    elif failure == "input":
        protocol["steps"][1]["needs"] = []
    elif failure == "overwrite":
        protocol["steps"][0]["outputs"] = ["recording"]
    else:
        protocol["steps"][-1]["needs"] = []
    with pytest.raises(WorkflowError):
        protocol_order(protocol)


@pytest.mark.parametrize(
    "ref",
    [
        "gdrive://abc?token=secret",
        "https://example.com/token",
        "ssh://user:password@host/path",
        "gdrive://../bad",
    ],
)
def test_credential_bearing_and_invalid_refs_rejected(job, ref):
    job["artifacts"][0]["ref"] = ref
    with pytest.raises(WorkflowError):
        validate_job(job)


def test_instruction_version_and_directory_integrity(tmp_path):
    registry = deepcopy(load_registry()[0])
    registry["protocols"] = registry["protocols"][:1]
    path = tmp_path / "registry.json"
    registry["protocols"][0]["instructions"] = "../outside.md"
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(WorkflowError, match="relative"):
        load_registry(path)
    registry["protocols"][0]["instructions"] = "protocol.md"
    path.write_text(json.dumps(registry), encoding="utf-8")
    instructions = tmp_path / "protocol.md"
    instructions.write_text("Version one", encoding="utf-8")
    first_hash = load_registry(path)[2]
    instructions.write_text("Unexpected edit", encoding="utf-8")
    assert load_registry(path)[2] != first_hash


def test_unknown_contract_fields_rejected_without_echoing_values(job):
    job["token"] = "DO-NOT-LOG-ME"
    with pytest.raises(WorkflowError) as error:
        validate_job(job)
    assert "DO-NOT-LOG-ME" not in str(error.value)


def test_cli_read_only_plan_and_exit_status(
    tmp_path, job, installation, model, monkeypatch
):
    job_path, install_path, org_path, config = [
        tmp_path / p
        for p in ["job.json", "installation.json", "org.yaml", "config.toml"]
    ]
    job_path.write_text(json.dumps(job), encoding="utf-8")
    install_path.write_text(json.dumps(installation), encoding="utf-8")
    org_path.write_text(yaml.safe_dump(model), encoding="utf-8")
    config.write_text('default_persona = "coordinator"\n', encoding="utf-8")
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "coordinator")

    # Any accidental tool/network/content operation fails this functional test.
    def forbidden(*args, **kwargs):
        raise AssertionError("planning must not execute or read document bytes")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("phantomdocs.cli.read_reference", forbidden)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    args = [
        "workflow",
        "plan",
        "--job",
        str(job_path),
        "--installation",
        str(install_path),
        "--org-yaml",
        str(org_path),
        "--phantombot-config",
        str(config),
    ]
    runner = CliRunner()
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert (
        output["processor"]["actor"] == "coordinator" and not output["execution_ready"]
    )
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    installation["tools"] = {}
    install_path.write_text(json.dumps(installation), encoding="utf-8")
    blocked = runner.invoke(main, args + ["--graph"])
    assert blocked.exit_code == 2
    assert json.loads(blocked.output)["observation"] == "declared-only"
    assert (
        runner.invoke(main, ["workflow", "validate", "job", str(job_path)]).exit_code
        == 0
    )


def test_contract_validator_accepts_packaged_registry():
    registry, _, _ = load_registry()
    validate_contract(registry, "registry")
    assert len(registry["protocols"]) == 2
    assert digest(registry) == digest(deepcopy(registry))


@pytest.mark.parametrize("excluded", ["documents", "documents.index"])
def test_processor_tool_exclusions_are_authoritative(
    job, installation, model, excluded
):
    model["actors"][0]["tools_excluded"] = [excluded]
    with pytest.raises(WorkflowError, match="logical tool"):
        make_plan(job, installation, model)


def test_duplicate_routes_and_unconfigured_intake_fail_closed(job, installation, model):
    installation["routes"].append(deepcopy(installation["routes"][0]))
    with pytest.raises(WorkflowError, match="ambiguous"):
        make_plan(job, installation, model)
    installation["routes"].pop()
    installation["intake"]["inbox"] = "unknown"
    with pytest.raises(WorkflowError, match="intake"):
        make_plan(job, installation, model)


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"requester":"one","requester":"two"}', encoding="utf-8")
    with pytest.raises(WorkflowError, match="duplicate JSON"):
        read_json(path)


def test_repeated_job_artifacts_rejected(job):
    job["artifacts"].append(deepcopy(job["artifacts"][0]))
    with pytest.raises(WorkflowError, match="duplicate artifact"):
        validate_job(job)
