"""Separated processing/archive/index handover with real service and fake Drive."""

import io
import json
import os
from copy import deepcopy

import pytest
import yaml
from click.testing import CliRunner
from test_workflow_executor import FakeDrive
from test_workflow_executor import system as system_fixture

from phantomdocs.cli import main
from phantomdocs.content import FileContent
from phantomdocs.documents import DocumentError, DocumentService
from phantomdocs.identity import root_mac
from phantomdocs.manifest import empty_manifest, load, save
from phantomdocs.workflow_executor import WorkflowExecutor
from phantomdocs.workflow_handover import export_results, index_archive
from phantomdocs.workflow_ledger import WorkflowLedger
from phantomdocs.workflows import WorkflowError, digest


@pytest.fixture
def prepared(tmp_path):
    system = system_fixture.__wrapped__(tmp_path)
    org, install, job, signed, path, build = system
    install["routes"][0].update(
        protocol="meeting-processing", archive_ref="gdrive://authorized-archive"
    )
    with WorkflowLedger(path) as ledger:
        ledger.accept(signed, org, "coordinator", build, replan=True)
        backend = FakeDrive()
        result = WorkflowExecutor(ledger, path, backend).prepare(
            job["organization"], job["request_id"], org, "coordinator", build
        )
    receipt = {
        "version": 1,
        "kind": "archived-document-results",
        **{
            f: result["results"][f]
            for f in ("organization", "request_id", "plan_hash", "results_hash")
        },
        "artifacts": [
            {
                "role": a["role"],
                "ref": "gdrive://archived-" + a["role"],
                "archive_ref": "gdrive://authorized-archive",
                "sha256": a["sha256"],
                "size": a["size"],
                "slug": "meeting-" + a["role"],
            }
            for a in result["results"]["artifacts"]
        ],
    }
    return system, backend, result, receipt


def test_prepare_stops_before_index_and_export_is_read_only(prepared):
    system, backend, result, _ = prepared
    org, _, job, _, path, build = system
    assert result["state"] == "awaiting-archive" and "index" not in backend.calls
    assert not backend.indexed
    before, calls = path.read_bytes(), list(backend.calls)
    with WorkflowLedger(path, readonly=True) as ledger:
        assert (
            export_results(
                ledger, job["organization"], job["request_id"], org, "remote-lead"
            )
            == result["results"]
        )
        with pytest.raises(WorkflowError):
            export_results(
                ledger, job["organization"], job["request_id"], org, "unmapped"
            )
    assert path.read_bytes() == before
    with WorkflowLedger(path) as ledger:
        assert (
            WorkflowExecutor(ledger, path, backend).prepare(
                job["organization"], job["request_id"], org, "coordinator", build
            )
            == result
        )
    assert backend.calls == calls
    unsigned = dict(result["results"])
    unsigned.pop("results_hash")
    assert digest(unsigned) == result["results"]["results_hash"]


def test_incomplete_results_cannot_be_exported(tmp_path):
    org, _, job, _, path, _ = system_fixture.__wrapped__(tmp_path)
    with (
        WorkflowLedger(path, readonly=True) as ledger,
        pytest.raises(WorkflowError, match="not complete"),
    ):
        export_results(
            ledger, job["organization"], job["request_id"], org, "coordinator"
        )


class Reader:
    def __init__(self, receipt):
        self.artifacts = {a["ref"]: a for a in receipt["artifacts"]}
        self.downloads, self.metadata_calls = [], []
        self.wrong_parent = False

    def metadata(self, ref):
        self.metadata_calls.append(ref)
        a = self.artifacts[ref]
        return {
            "id": ref.split("://")[1],
            "trashed": False,
            "parents": [
                "wrong" if self.wrong_parent else a["archive_ref"].split("://")[1]
            ],
            "size": str(a["size"]),
        }

    def read_reference(self, ref, **kwargs):
        self.downloads.append(ref)
        return FileContent.from_stream(
            io.BytesIO(self.artifacts[ref]["role"].encode())
        ), {"backend": "gdrive", "ref": ref.split("://")[1]}


@pytest.fixture
def store(tmp_path, prepared):
    if os.name != "posix":
        pytest.skip("Existing store filesystem operations require POSIX")
    org = prepared[0][0]
    root = tmp_path / "store"
    root.mkdir()
    save(
        str(root / "manifest.yaml"),
        empty_manifest(
            org["organization"]["id"],
            "docs",
            root_mac(org["organization"]["id"], "", "docs"),
        ),
    )
    org_file = tmp_path / "org.yaml"
    org_file.write_text(yaml.safe_dump(org))
    DocumentService(str(root), str(org_file), "coordinator").create_folder(
        name="project-reports",
        parent=None,
        category="category-2",
        owners=["custodian", "project-lead"],
    )
    return root, org_file


def consume(prepared, store, reader, receipt=None):
    org = prepared[0][0]
    path = prepared[0][4]
    with WorkflowLedger(path) as ledger:
        return index_archive(
            ledger,
            receipt or prepared[3],
            org,
            "coordinator",
            root=store[0],
            org_yaml=store[1],
            drive_reader=reader,
        )


def test_real_index_consumes_archive_ids_without_copy_or_rehash(
    prepared, store, monkeypatch
):
    reader = Reader(prepared[3])
    first = consume(prepared, store, reader)
    assert first["state"] == "indexed" and len(reader.downloads) == 2
    assert not (store[0] / "blobs").exists()

    def no_hash(*args):
        pytest.fail("retry must not hash document bytes")

    monkeypatch.setattr("phantomdocs.documents.content_hash", no_hash)
    second = consume(prepared, store, reader)
    assert all(a["unchanged"] for a in second["documents"])
    assert len(reader.downloads) == 2 and len(reader.metadata_calls) == 2
    docs = [
        n for n in load(str(store[0] / "manifest.yaml"))["nodes"] if n["kind"] == "doc"
    ]
    assert len(docs) == 2 and all(n["category"] == "category-2" for n in docs)
    assert all(set(n["owners"]) == {"custodian", "project-lead"} for n in docs)


@pytest.mark.parametrize(
    "change", ["role", "sha256", "archive_ref", "duplicate", "slug"]
)
def test_invalid_archive_receipt_refused_before_drive_or_index(prepared, store, change):
    receipt = deepcopy(prepared[3])
    a = receipt["artifacts"][0]
    if change == "duplicate":
        receipt["artifacts"].append(deepcopy(a))
    elif change == "sha256":
        a[change] = "0" * 64
    elif change == "slug":
        a[change] = "a..b"
    else:
        a[change] = "gdrive://other" if change == "archive_ref" else "other-role"
    reader = Reader(prepared[3])
    with pytest.raises(WorkflowError):
        consume(prepared, store, reader, receipt)
    assert not reader.downloads and not reader.metadata_calls


def test_wrong_actual_drive_parent_refused_before_index(prepared, store):
    reader = Reader(prepared[3])
    reader.wrong_parent = True
    with pytest.raises(WorkflowError, match="destination"):
        consume(prepared, store, reader)
    assert not reader.downloads
    assert all(
        n["kind"] == "folder" for n in load(str(store[0] / "manifest.yaml"))["nodes"]
    )


def test_index_commit_before_receipt_commit_recovers_without_redownload(
    prepared, store, monkeypatch
):
    reader = Reader(prepared[3])
    original = DocumentService.index_reference_once
    failed = []

    def lose_response(self, **kwargs):
        result = original(self, **kwargs)
        if not failed:
            failed.append(True)
            raise RuntimeError("response lost after index commit")
        return result

    monkeypatch.setattr(DocumentService, "index_reference_once", lose_response)
    with pytest.raises(RuntimeError):
        consume(prepared, store, reader)
    assert consume(prepared, store, reader)["state"] == "indexed"
    assert len(reader.downloads) == 2


def test_changed_archiver_intent_conflicts_on_retry(prepared, store):
    reader = Reader(prepared[3])
    consume(prepared, store, reader)
    changed = deepcopy(prepared[3])
    changed["artifacts"][0]["ref"] = "gdrive://different-id"
    with pytest.raises(WorkflowError, match="conflicting archive"):
        consume(prepared, store, reader, changed)


def test_unmatched_actual_bytes_do_not_enter_store(prepared, store):
    reader = Reader(prepared[3])
    reader.read_reference = lambda ref, **kwargs: (
        FileContent.from_stream(io.BytesIO(b"different content")),
        {"backend": "gdrive", "ref": ref.split("://")[1]},
    )
    with pytest.raises(DocumentError, match="artifact receipt"):
        consume(prepared, store, reader)
    assert all(
        n["kind"] == "folder" for n in load(str(store[0] / "manifest.yaml"))["nodes"]
    )


def test_results_cli_and_index_archive_cli(prepared, store, tmp_path, monkeypatch):
    org_file = store[1]
    ledger = prepared[0][4]
    runner = CliRunner()
    exported = runner.invoke(
        main,
        [
            "workflow",
            "results",
            "--request-id",
            "request-001",
            "--ledger",
            str(ledger),
            "--org-yaml",
            str(org_file),
            "--actor",
            "remote-lead",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output) == prepared[2]["results"]
    receipt = tmp_path / "archive.json"
    receipt.write_text(json.dumps(prepared[3]))
    token = tmp_path / "token"
    token.write_text("test-only")
    token.chmod(0o600)
    reader = Reader(prepared[3])
    monkeypatch.setattr(
        "phantomdocs.workflow_drive_reader.DriveArchiveReader", lambda *a, **kw: reader
    )
    imported = runner.invoke(
        main,
        [
            "workflow",
            "index-archive",
            "--receipt",
            str(receipt),
            "--ledger",
            str(ledger),
            "--org-yaml",
            str(org_file),
            "--actor",
            "coordinator",
            "--root",
            str(store[0]),
            "--drive-token-file",
            str(token),
            "--max-reference-bytes",
            "1024",
        ],
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.output)["state"] == "indexed"


def test_receipt_builder_uses_plan_and_results_without_inventing_ids(prepared):
    from phantomdocs.workflow_handover import build_archive_receipt

    org, _, job, _, path, _ = prepared[0]
    mapping = [
        {key: a[key] for key in ("role", "ref", "slug")}
        for a in prepared[3]["artifacts"]
    ]
    before = path.read_bytes()
    with WorkflowLedger(path, readonly=True) as ledger:
        receipt = build_archive_receipt(
            ledger, job["organization"], job["request_id"], org, "coordinator", mapping
        )
        with pytest.raises(WorkflowError):
            build_archive_receipt(
                ledger,
                job["organization"],
                job["request_id"],
                org,
                "coordinator",
                [{**mapping[0], "category": 1}],
            )
    assert receipt == prepared[3] and path.read_bytes() == before
