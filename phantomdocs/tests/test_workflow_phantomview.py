"""Static collector manifests disclose dependencies, never case observations."""

import json
from copy import deepcopy

import pytest
import test_workflows as fixtures
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.workflow_phantomview import installation_map
from phantomdocs.workflows import WorkflowError, load_registry


def config():
    value = fixtures.installation.__wrapped__()
    value["routes"][0].update(protocol="meeting-processing", protocol_version="1.0.0")
    return value


def test_meeting_map_requires_archiver_and_never_discloses_route_paths():
    installation = config()
    installation["routes"][0]["archive_ref"] = "gdrive://private-destination"
    result = installation_map(installation, load_registry())
    components = {c["name"]: c for c in result["components"]}
    archive = components["Independent organizational archiver"]["id"]
    index = components["meeting-processing: index"]["id"]
    assert {"from": index, "to": archive, "type": "requires"} in result["relations"]
    assert all(c["probe"] == {"type": "none"} for c in components.values())
    encoded = json.dumps(result)
    for private in ("private-destination", "project-reports", "coordinator"):
        assert private not in encoded
    assert "request_id" not in encoded and "category" not in encoded


def test_reference_only_protocol_does_not_require_archiver():
    result = installation_map(fixtures.installation.__wrapped__(), load_registry())
    assert not any(c["kind"] == "external" for c in result["components"])


def test_merge_preserves_existing_manifest_and_is_idempotent():
    base = {
        "version": 1,
        "components": [
            {"id": "host", "org": "example-org", "kind": "runtime", "name": "Host"}
        ],
        "relations": [],
        "tool_bindings": [
            {"org": "example-org", "tool": "documents", "component": "host"}
        ],
        "persona_bindings": [{"actor": "example-org:coordinator", "component": "host"}],
    }
    before = deepcopy(base)
    first = installation_map(config(), load_registry(), base)
    assert base == before
    assert first["tool_bindings"] == base["tool_bindings"]
    assert first["persona_bindings"] == base["persona_bindings"]
    assert first == installation_map(config(), load_registry(), first)
    first["components"][-1]["name"] = "Changed by operator"
    with pytest.raises(WorkflowError, match="collision"):
        installation_map(config(), load_registry(), first)


@pytest.mark.parametrize("failure", ["dangling", "cross-org", "reserved", "limit"])
def test_invalid_base_is_rejected(failure):
    base = {
        "version": 1,
        "components": [{"id": "x", "org": "other", "kind": "data"}],
        "relations": [],
    }
    if failure == "dangling":
        base["relations"] = [{"from": "x", "to": "missing", "type": "reads"}]
    elif failure == "cross-org":
        base["components"].append({"id": "y", "org": "different", "kind": "data"})
        base["relations"] = [{"from": "x", "to": "y", "type": "reads"}]
    elif failure == "reserved":
        base["components"][0]["id"] = "persona:reserved"
    else:
        base["components"] = [
            {"id": f"x{i}", "org": "other", "kind": "data"} for i in range(150)
        ]
    with pytest.raises(WorkflowError):
        installation_map(config(), load_registry(), base)


def test_cli_outputs_manifest_without_writing_configuration(tmp_path):
    path = tmp_path / "installation.json"
    source = json.dumps(config())
    path.write_text(source)
    result = CliRunner().invoke(
        main, ["workflow", "installation-map", "--installation", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == installation_map(config(), load_registry())
    assert path.read_text() == source and list(tmp_path.iterdir()) == [path]
