"""Malformed counters must never be normalized into signed checkpoints."""

import copy

import pytest
import yaml
from click.testing import CliRunner

from phantomdocs import cli
from phantomdocs.identity import root_mac
from phantomdocs.manifest import empty_manifest, validate


def namespace():
    return empty_manifest("sample-org", "docs", root_mac("sample-org", "", "docs"))


@pytest.mark.parametrize("field", ["auditSeq", "headSeq", "sealedHeadSeq"])
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1"])
def test_header_counter_types_are_exact(field, value):
    data = namespace()
    data["manifest"][field] = value
    assert any(field in error for error in validate(data))


@pytest.mark.parametrize("field", ["auditSeq", "headSeq", "sealedHeadSeq"])
@pytest.mark.parametrize("value", [None, 0, 1, 100])
def test_header_retains_optional_legacy_and_valid_counts(field, value):
    data = namespace()
    data["manifest"][field] = value
    assert validate(data) == []


@pytest.mark.parametrize("field", ["auditSeq", "cs", "n", "seq"])
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1"])
def test_historical_counter_shapes_refuse_coercible_values(field, value):
    data = namespace()
    if field == "seq":
        data["manifest"]["profileTransition"] = {field: value}
    elif field == "n":
        data["manifest"]["revocations"] = [{field: value}]
    else:
        data["manifest"]["seals"] = [{"cs": 1, field: value}]
    assert any(field in error and "integer" in error for error in validate(data))


@pytest.mark.parametrize("command", ["verify", "seal", "recover"])
@pytest.mark.parametrize("field", ["auditSeq", "headSeq"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", -1])
def test_cli_refuses_before_signing_or_changing_store(
    tmp_path, monkeypatch, command, field, value
):
    data = namespace()
    data["manifest"][field] = value
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    audit_path = tmp_path / "audit.log"
    audit_path.write_bytes(b"legacy evidence remains unchanged\n")
    before = path.read_bytes(), audit_path.read_bytes()
    monkeypatch.setattr(
        cli, "_read_secret_file", lambda *_: pytest.fail("signing key accessed")
    )
    args = [command, "--root", str(tmp_path)]
    if command == "seal":
        args += ["--nsec-file", "unused"]
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code != 0
    assert field in result.output and "integer" in result.output
    assert "Traceback" not in result.output
    assert before == (path.read_bytes(), audit_path.read_bytes())


@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1"])
def test_direct_historical_verifier_returns_counter_error(value):
    data = {"seals": [{"cs": 1, "auditSeq": value}]}
    before = copy.deepcopy(data)
    issues = cli._seal_history_problems(data, "0" * 64)
    assert any("auditSeq" in issue for issue in issues)
    assert data == before


def test_counter_helper_only_defaults_null_or_absent():
    assert cli._seal_counter({}, "auditSeq") == 0
    assert cli._seal_counter({"auditSeq": None}, "auditSeq") == 0
    assert cli._seal_counter({"auditSeq": 0}, "auditSeq") == 0
    with pytest.raises(ValueError):
        cli._seal_counter({}, "cs", required=True)
