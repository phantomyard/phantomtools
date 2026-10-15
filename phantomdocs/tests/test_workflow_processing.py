"""Actual staged subprocess chain with fake authorized Drive inputs."""

import io
import json
import sys
import wave
from datetime import timedelta

import coincurve
import pytest
import test_workflows as fixtures
import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.content import FileContent
from phantomdocs.signing import npub_encode, pubkey_from_nsec
from phantomdocs.workflow_executor import WorkflowExecutor
from phantomdocs.workflow_ledger import WorkflowLedger
from phantomdocs.workflow_processing import ProcessingAdapter
from phantomdocs.workflow_requests import sign_request, utc_now, utc_text
from phantomdocs.workflow_runner import file_hash
from phantomdocs.workflows import WorkflowError, load_registry, plan


@pytest.fixture
def staged(tmp_path):
    org, install, job = (
        fixtures.model.__wrapped__(),
        fixtures.installation.__wrapped__(),
        fixtures.job.__wrapped__(),
    )
    secret = coincurve.PrivateKey().secret.hex()
    org["actors"][1]["npub"] = npub_encode(pubkey_from_nsec(secret))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    worker = tmp_path / "worker.py"
    log = tmp_path / "calls.jsonl"
    worker.write_text(
        "import pathlib,sys,json\nmode=sys.argv[1]\nargs=sys.argv[2:]\nsource=pathlib.Path(args[0])\ntarget=pathlib.Path(args[args.index('--out')+1] if '--out' in args else args[1])\nwith pathlib.Path("
        + repr(str(log))
        + ").open('a') as f: f.write(json.dumps({'mode':mode,'args':args})+'\\n')\ntarget.write_bytes(b'%PDF-1.7\\nBranded output\\n%%EOF' if mode=='brand' else b'Transcript or summary')\n"
    )

    def binding(mode):
        return {
            "id": mode,
            "version": "1",
            "entrypoint": sys.executable,
            "sha256": file_hash(sys.executable),
            "script": str(worker),
            "script_sha256": file_hash(worker),
        }

    install["tools"].update(
        {
            "documents.transcribe": {
                **binding("transcribe"),
                "arguments": [
                    "transcribe",
                    "{input}",
                    "--model",
                    "turbo",
                    "--out",
                    "{output}",
                ],
            },
            "documents.summarize": {
                **binding("summarize"),
                "arguments": ["summarize", "{input}", "--out", "{output}"],
            },
            "documents.brand": {
                **binding("brand"),
                "variants": {
                    role: ["brand", "{input}", "{output}", "--clase", literal]
                    for role, literal in [
                        ("transcription", "transcripcion"),
                        ("summary", "resumen"),
                    ]
                },
            },
        }
    )
    install["routes"][0].update(
        protocol="meeting-processing",
        branding_profile="test-brand",
        archive_ref="gdrive://archive",
    )
    install["processing"] = {
        "work_root": str(work),
        "timeout_seconds": 5,
        "max_input_bytes": 4096,
        "max_output_bytes": 4096,
        "inputs": {"recording": {"media_types": ["audio/wav"], "extension": "wav"}},
        "outputs": {
            role: {"media_type": mime, "extension": ext}
            for role, mime, ext in [
                ("transcription", "text/plain", "txt"),
                ("summary", "text/markdown", "md"),
                ("branded-transcription", "application/pdf", "pdf"),
                ("branded-summary", "application/pdf", "pdf"),
            ]
        },
        "resources": [{"path": str(worker), "sha256": file_hash(worker)}],
    }
    job["artifacts"][0].update(role="recording", media_type="audio/wav")
    sound = io.BytesIO()
    with wave.open(sound, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 160)

    class Source:
        wrong = False
        downloads = 0
        data = sound.getvalue()

        def metadata(self, ref):
            return {
                "id": "document-001",
                "parents": ["other" if self.wrong else "shared-intake"],
                "trashed": False,
                "size": str(len(self.data)),
                "mimeType": "audio/wav",
            }

        def read_reference(self, ref, **kwargs):
            self.downloads += 1
            return FileContent.from_stream(io.BytesIO(self.data)), {
                "backend": "gdrive",
                "ref": "document-001",
            }

    source = Source()

    def build(value):
        return plan(
            value,
            install,
            org,
            load_registry(),
            ("coordinator", "phantombot-config"),
            "coordinator",
        )

    path = tmp_path / "ledger.sqlite"
    signed = sign_request(job, org, secret, utc_text(utc_now() + timedelta(hours=1)))
    with WorkflowLedger(path) as ledger:
        ledger.accept(signed, org, "coordinator", build)
    return org, install, job, path, source, build, log


def prepare(staged):
    org, install, job, path, source, build, _ = staged
    with WorkflowLedger(path) as ledger, ProcessingAdapter(install, source) as adapter:
        return WorkflowExecutor(ledger, path, adapter).prepare(
            job["organization"], job["request_id"], org, "coordinator", build
        )


def test_real_processing_produces_branded_files_without_drive_writes(staged):
    result = prepare(staged)
    assert result["state"] == "awaiting-archive"
    assert {a["role"] for a in result["results"]["artifacts"]} == {
        "branded-transcription",
        "branded-summary",
    }
    assert all(
        a["media_type"] == "application/pdf" and a["ref"].startswith("file://")
        for a in result["results"]["artifacts"]
    )
    calls = [json.loads(line) for line in staged[6].read_text().splitlines()]
    assert [c["mode"] for c in calls] == ["transcribe", "brand", "summarize", "brand"]
    assert "--summarize" not in calls[0]["args"]
    assert calls[1]["args"][-1] == "transcripcion" and calls[3]["args"][-1] == "resumen"
    assert staged[4].downloads == 1
    assert prepare(staged) == result
    assert len(staged[6].read_text().splitlines()) == 4 and staged[4].downloads == 1


def test_wrong_intake_and_fake_wav_are_refused(staged):
    staged[4].wrong = True
    with pytest.raises(WorkflowError, match="authorized intake"):
        prepare(staged)
    assert staged[4].downloads == 0 and not staged[6].exists()


def test_invalid_recording_bytes_are_not_transcribed(staged):
    staged[4].data = b"not really a WAV"
    with pytest.raises(WorkflowError, match="WAV"):
        prepare(staged)
    assert not staged[6].exists()


def test_changed_resource_pin_refused_before_download(staged):
    staged[1]["processing"]["resources"][0]["sha256"] = "0" * 64
    # The changed installation itself is no longer the accepted plan.
    with pytest.raises(WorkflowError, match="stale"):
        prepare(staged)
    assert staged[4].downloads == 0


def test_cached_backend_receipt_recovers_lost_journal_commit(staged, monkeypatch):
    original = WorkflowExecutor._validate
    failed = []

    def lose(self, receipt, operation, inputs, planned):
        if operation == "transcribe" and not failed:
            failed.append(True)
            raise RuntimeError("stopped before journal receipt commit")
        return original(self, receipt, operation, inputs, planned)

    monkeypatch.setattr(WorkflowExecutor, "_validate", lose)
    with pytest.raises(RuntimeError):
        prepare(staged)
    assert prepare(staged)["state"] == "awaiting-archive"
    assert [
        json.loads(line)["mode"] for line in staged[6].read_text().splitlines()
    ].count("transcribe") == 1


def test_prepare_cli_uses_pinned_default_and_returns_manifest(
    staged, tmp_path, monkeypatch
):
    org, install, _, path, source, _, _ = staged
    model = tmp_path / "org.yaml"
    model.write_text(yaml.safe_dump(org))
    configuration = tmp_path / "installation.json"
    configuration.write_text(json.dumps(install))
    bot = tmp_path / "bot.toml"
    bot.write_text('default_persona="coordinator"')
    token = tmp_path / "token"
    token.write_text("test-only")
    token.chmod(0o600)
    monkeypatch.setattr(
        "phantomdocs.workflow_drive_reader.DriveArchiveReader", lambda *a, **kw: source
    )
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "prepare",
            "--request-id",
            "request-001",
            "--ledger",
            str(path),
            "--installation",
            str(configuration),
            "--org-yaml",
            str(model),
            "--actor",
            "coordinator",
            "--phantombot-config",
            str(bot),
            "--drive-token-file",
            str(token),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "awaiting-archive"


def test_snapshot_tampering_stops_next_step(staged, monkeypatch):
    from pathlib import Path

    original = ProcessingAdapter._commit
    changed = []

    def alter(self, key, operation, inputs, planned, receipt, snapshots):
        value = original(self, key, operation, inputs, planned, receipt, snapshots)
        if operation == "$inputs" and not changed:
            changed.append(True)
            Path(snapshots[0][1]).write_bytes(b"changed")
        return value

    monkeypatch.setattr(ProcessingAdapter, "_commit", alter)
    with pytest.raises(WorkflowError, match="snapshot changed"):
        prepare(staged)
    assert not staged[6].exists()


def test_probe_checks_declared_container_and_stream(tmp_path):
    from phantomdocs.workflow_processing import validate_input

    file = tmp_path / "recording.mp4"
    file.write_bytes(b"synthetic media")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json;print(json.dumps({'format':{'format_name':'mov,mp4'},'streams':[{'codec_type':'video'}]}))"
    )
    descriptor = {
        "id": "test-probe",
        "version": "1",
        "entrypoint": sys.executable,
        "sha256": file_hash(sys.executable),
        "script": str(probe),
        "script_sha256": file_hash(probe),
        "arguments": ["{input}"],
    }
    spec = {
        "media_types": ["video/mp4"],
        "extension": "mp4",
        "probe": descriptor,
        "formats": ["mp4"],
    }
    limits = {"timeout_seconds": 5, "max_input_bytes": 1024}
    assert validate_input(file, "video/mp4", spec, limits)["sha256"] == file_hash(file)
    spec["formats"] = ["unexpected"]
    with pytest.raises(WorkflowError, match="container"):
        validate_input(file, "video/mp4", spec, limits)
