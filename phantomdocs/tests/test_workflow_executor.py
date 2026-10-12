"""Functional fake Drive execution, crash reconciliation and actual local tools."""

import hashlib
import sys
from copy import deepcopy
from datetime import timedelta

import coincurve
import pytest
import test_workflows as fixtures

from phantomdocs.signing import npub_encode, pubkey_from_nsec
from phantomdocs.workflow_executor import WorkflowExecutor, execution_lock
from phantomdocs.workflow_ledger import WorkflowLedger
from phantomdocs.workflow_requests import sign_request, utc_now, utc_text
from phantomdocs.workflow_runner import file_hash, run_transform
from phantomdocs.workflows import WorkflowError, load_registry, plan


@pytest.fixture
def system(tmp_path):
    org, install, job = (
        fixtures.model.__wrapped__(),
        fixtures.installation.__wrapped__(),
        fixtures.job.__wrapped__(),
    )
    secret = coincurve.PrivateKey().secret.hex()
    org["actors"][1]["npub"] = npub_encode(pubkey_from_nsec(secret))
    install["routes"][0].update(
        protocol="meeting-publication", branding_profile="test-brand"
    )
    job["artifacts"][0].update(role="recording", media_type="audio/wav")
    for capability in (
        "documents.transcribe",
        "documents.summarize",
        "documents.brand",
    ):
        install["tools"][capability] = {
            "id": "fake-drive-tool",
            "version": "1",
            "entrypoint": sys.executable,
        }

    def build(value):
        return plan(
            value,
            install,
            org,
            load_registry(),
            ("coordinator", "phantombot-config"),
            "coordinator",
        )

    signed = sign_request(job, org, secret, utc_text(utc_now() + timedelta(hours=1)))
    path = tmp_path / "ledger.sqlite"
    with WorkflowLedger(path) as ledger:
        ledger.accept(signed, org, "coordinator", build)
    return org, install, job, signed, path, build


class FakeDrive:
    """Committed receipts are durable in the backend, separate from the ledger."""

    def __init__(self):
        self.receipts, self.calls, self.indexed = {}, [], set()
        self.fail_after = None
        self.corrupt = False
        self.source_bytes = b"RIFF fake meeting recording"

    def preflight(self, planned):
        return True

    def perform(self, key, operation, inputs, planned):
        self.calls.append(operation)
        if operation == "$inputs":
            results = [
                {
                    "role": a["role"],
                    "ref": a["ref"],
                    "media_type": a["media_type"],
                    "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                    "size": len(self.source_bytes),
                    "derived_from": [],
                }
                for a in inputs
            ]
        else:
            step = next(s for s in planned["steps"] if s["id"] == operation)
            if step["capability"] == "documents.index":
                self.indexed.update(a["ref"] for a in inputs)
                results = {"indexed_refs": sorted(a["ref"] for a in inputs)}
            else:
                results = [
                    {
                        "role": role,
                        "ref": "gdrive://" + key + "-" + role,
                        "media_type": "application/pdf"
                        if role.startswith("published-")
                        else "text/plain",
                        "sha256": hashlib.sha256(role.encode()).hexdigest(),
                        "size": len(role),
                        "derived_from": sorted(a["ref"] for a in inputs),
                    }
                    for role in step["outputs"]
                ]
        if self.corrupt and isinstance(results, list):
            results[0]["ref"] = (
                inputs[0]["ref"]
                if operation != "$inputs"
                else "gdrive://changed-source"
            )
        self.receipts[key] = deepcopy(results)
        if self.fail_after == operation:
            raise RuntimeError("process stopped after backend commit")
        return results

    def reconcile(self, key, operation, inputs, planned):
        return deepcopy(self.receipts.get(key))


def run(system, adapter):
    org, _, job, _, path, build = system
    with WorkflowLedger(path) as ledger:
        return WorkflowExecutor(ledger, path, adapter).run(
            job["organization"], job["request_id"], org, "coordinator", build
        )


def test_meeting_executes_branding_before_index_and_retries_without_reads(system):
    backend = FakeDrive()
    result = run(system, backend)
    assert result["state"] == "indexed"
    assert backend.calls == [
        "$inputs",
        "transcribe",
        "brand-transcription",
        "summarize",
        "brand-summary",
        "index",
    ]
    assert len(backend.indexed) == 2
    original_calls = list(backend.calls)
    assert run(system, backend) == result
    assert backend.calls == original_calls
    assert backend.source_bytes == b"RIFF fake meeting recording"


@pytest.mark.parametrize(
    "operation", ["$inputs", "transcribe", "brand-summary", "index"]
)
def test_backend_commit_before_ledger_commit_reconciles_without_repeating(
    system, operation
):
    backend = FakeDrive()
    backend.fail_after = operation
    with pytest.raises(RuntimeError):
        run(system, backend)
    backend.fail_after = None
    result = run(system, backend)
    assert result["state"] == "indexed"
    assert backend.calls.count(operation) == 1
    assert len(backend.indexed) == 2


def test_uncertain_result_requires_review_without_blind_repeat(system):
    backend = FakeDrive()
    backend.fail_after = "transcribe"
    with pytest.raises(RuntimeError):
        run(system, backend)
    backend.receipts.clear()
    backend.fail_after = None
    with pytest.raises(WorkflowError, match="requires review"):
        run(system, backend)
    assert backend.calls.count("transcribe") == 1
    assert not backend.indexed


def test_changed_policy_and_wrong_processor_refused_before_effects(system):
    org, install, job, _, path, build = system
    backend = FakeDrive()
    with WorkflowLedger(path) as ledger:
        engine = WorkflowExecutor(ledger, path, backend)
        with pytest.raises(WorkflowError, match="pinned processor"):
            engine.run(
                job["organization"], job["request_id"], org, "remote-lead", build
            )
    install["routes"][0]["folder"] = "other-folder"
    with pytest.raises(WorkflowError, match="stale"):
        run(system, backend)
    assert not backend.calls


def test_replanning_started_execution_is_refused_and_status_observes_steps(system):
    backend = FakeDrive()
    backend.fail_after = "transcribe"
    with pytest.raises(RuntimeError):
        run(system, backend)
    org, _, job, signed, path, build = system
    with WorkflowLedger(path) as ledger:
        with pytest.raises(WorkflowError, match="replanning is forbidden"):
            ledger.accept(signed, org, "coordinator", build, replan=True)
        state = ledger.status(
            job["organization"], job["request_id"], org, "remote-lead"
        )
    assert {r["state"] for r in state["execution"]} == {"started", "complete"}
    assert all("ref" not in r for r in state["execution"])


def test_bad_source_receipt_and_changed_tool_pin_stop_execution(system):
    backend = FakeDrive()
    backend.corrupt = True
    with pytest.raises(WorkflowError, match="preserve"):
        run(system, backend)
    assert backend.calls == ["$inputs"]
    assert not backend.indexed


def test_runner_lock_refuses_a_second_executor(system):
    path = system[4]
    with (
        execution_lock(path),
        pytest.raises(WorkflowError, match="another executor"),
        execution_lock(path),
    ):
        pass


def binding(script):
    return {
        "id": "test-renderer",
        "version": "1",
        "entrypoint": sys.executable,
        "sha256": file_hash(sys.executable),
        "script": str(script),
        "script_sha256": file_hash(script),
        "arguments": ["{input}", "{output}"],
    }


def test_real_subprocess_preserves_input_and_validates_output(tmp_path):
    script = tmp_path / "renderer.py"
    script.write_text(
        "import pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(b'%PDF-1.7\\nrendered\\n%%EOF')"
    )
    source, target = tmp_path / "source.txt", tmp_path / "result.pdf"
    source.write_text("Original transcription", encoding="utf-8")
    result = run_transform(
        binding(script),
        "transcription",
        source,
        target,
        media_type="application/pdf",
        timeout_seconds=5,
        max_output_bytes=1024,
    )
    assert result["media_type"] == "application/pdf"
    assert source.read_text() == "Original transcription"
    assert result["sha256"] == file_hash(target)


@pytest.mark.parametrize("failure", ["pin", "format", "source", "timeout"])
def test_runner_fails_closed(tmp_path, failure):
    script = tmp_path / "tool.py"
    code = "import pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(b'not a PDF')"
    if failure == "source":
        code += "; pathlib.Path(sys.argv[1]).write_text('changed')"
    if failure == "timeout":
        code = "import time; time.sleep(5)"
    script.write_text(code)
    source, target = tmp_path / "source.txt", tmp_path / "output.pdf"
    source.write_text("Preserve")
    tool = binding(script)
    if failure == "pin":
        tool["script_sha256"] = "0" * 64
    with pytest.raises(WorkflowError):
        run_transform(
            tool,
            "transcription",
            source,
            target,
            media_type="application/pdf",
            timeout_seconds=0.1,
            max_output_bytes=1024,
        )
    if failure == "pin":
        assert not target.exists()


def test_observed_graph_is_filtered_and_does_not_export_references(system):
    from phantomdocs.workflows import observed_dependency_graph

    backend = FakeDrive()
    run(system, backend)
    org, _, job, _, path, _ = system
    before = path.read_bytes()
    with WorkflowLedger(path, readonly=True) as ledger:
        record = ledger.status(
            job["organization"], job["request_id"], org, "remote-lead"
        )
        graph = observed_dependency_graph(record)
        with pytest.raises(WorkflowError):
            ledger.status(job["organization"], job["request_id"], org, "unmapped")
    assert path.read_bytes() == before
    assert graph["observation"] == "ledger-receipts"
    assert all(
        c["runtime"] == "complete" and c["observed_at"] for c in graph["components"]
    )
    assert "gdrive://" not in str(graph)
