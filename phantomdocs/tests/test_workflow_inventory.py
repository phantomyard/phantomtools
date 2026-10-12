"""Inventory reads declared files only and does not execute or bless them."""

import json
import os
import sys

import pytest
import test_workflows as fixtures
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.workflow_inventory import runtime_inventory
from phantomdocs.workflow_runner import file_hash


def config(tmp_path):
    work = tmp_path / "private"
    work.mkdir(mode=0o700)
    script = tmp_path / "would-run.py"
    script.write_text("raise RuntimeError('must not execute')")
    value = fixtures.installation.__wrapped__()
    value["tools"]["documents.index"].update(
        entrypoint=sys.executable,
        sha256=file_hash(sys.executable),
        script=str(script),
        script_sha256=file_hash(script),
    )
    value["processing"] = {
        "work_root": str(work),
        "timeout_seconds": 5,
        "max_input_bytes": 1024,
        "max_output_bytes": 1024,
        "inputs": {"original": {"media_types": ["text/plain"], "extension": "txt"}},
        "outputs": {},
        "resources": [],
    }
    return value, work, script


def test_inventory_never_runs_or_creates_cache_and_is_deterministic(tmp_path):
    installation, work, script = config(tmp_path)
    before = script.read_bytes()
    first = runtime_inventory(installation)
    assert first == runtime_inventory(installation)
    assert first["pins_match"] and first["read_only"] and not first["execution_ready"]
    assert first["requires_operator_review"]
    assert all(o["status"] == "match" for o in first["observations"])
    assert not list(work.iterdir()) and script.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing", "mismatch", "zero", "relative"])
def test_inventory_reports_explicit_blockers(tmp_path, failure):
    installation, _, _ = config(tmp_path)
    binding = installation["tools"]["documents.index"]
    if failure == "missing":
        binding["script"] = str(tmp_path / "absent.py")
    elif failure == "relative":
        binding["script"] = "relative.py"
    else:
        binding["script_sha256"] = "0" * 64 if failure == "zero" else "1" * 64
    result = runtime_inventory(installation)
    assert result["blockers"] and not result["pins_match"]
    assert not result["execution_ready"]


def test_overlay_is_observed_without_rewriting_installation(tmp_path):
    installation, _, _ = config(tmp_path)
    original = json.loads(json.dumps(installation))
    tools = json.loads(json.dumps(installation["tools"]))
    tools["documents.index"]["script_sha256"] = "0" * 64
    report = runtime_inventory(installation, tools=tools)
    assert installation == original
    assert (
        report["overlays_applied"]
        and report["candidate_installation_hash"] != report["input_installation_hash"]
    )


def test_credential_resource_is_not_read_or_echoed(tmp_path):
    installation, _, _ = config(tmp_path)
    secret = tmp_path / ".env"
    secret.write_text("NOT_A_REAL_SECRET=synthetic")
    installation["processing"]["resources"] = [
        {"path": str(secret), "sha256": "0" * 64}
    ]
    result = runtime_inventory(installation)
    row = result["observations"][-1]
    assert (
        row["status"] == "excluded-credential-path" and row["observed_sha256"] is None
    )
    assert str(secret) not in json.dumps(
        result
    ) and "NOT_A_REAL_SECRET" not in json.dumps(result)


def test_cli_inventory_returns_two_for_unconfigured_runtime(tmp_path):
    installation, _, _ = config(tmp_path)
    installation["tools"]["documents.index"]["sha256"] = "0" * 64
    path = tmp_path / "installation.json"
    path.write_text(json.dumps(installation))
    before = path.read_bytes()
    result = CliRunner().invoke(
        main, ["workflow", "inventory", "--installation", str(path)]
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["read_only"] and path.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode check")
def test_shared_work_directory_is_reported_not_fixed(tmp_path):
    installation, work, _ = config(tmp_path)
    work.chmod(0o755)
    result = runtime_inventory(installation)
    assert "private-directory-required" in result["blockers"]
    assert work.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX execute permission")
def test_nonexecutable_entrypoint_is_a_blocker(tmp_path):
    installation, _, script = config(tmp_path)
    script.chmod(0o600)
    binding = installation["tools"]["documents.index"]
    binding["entrypoint"] = str(script)
    binding["sha256"] = file_hash(script)
    result = runtime_inventory(installation)
    assert any(
        row["status"] == "executable-access-required" for row in result["observations"]
    )
    assert not result["pins_match"]
