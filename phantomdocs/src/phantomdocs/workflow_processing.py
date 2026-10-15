"""Installed processing adapter: private snapshots, pinned tools and receipts.

Reads authorized intake objects and produces local results only. No upload,
archive naming, Drive mutation, indexing, credential discovery or persona change.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess  # nosec B404
import tempfile
import wave
from copy import deepcopy
from pathlib import Path

from .fsutil import fsync_dir
from .workflow_commands import build_command
from .workflow_executor import execution_lock
from .workflow_runner import file_hash, run_transform, validate_output, verify_binding
from .workflows import WorkflowError, digest, validate_contract


def private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_dir():
        raise WorkflowError(
            "processing directory must be existing, absolute and unlinked"
        )
    info = path.stat()
    if os.name == "posix" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise WorkflowError(
            "processing directory must be caller-owned and private (0700)"
        )
    return path


def validate_input(path, media_type, specification, limits):
    if media_type not in specification["media_types"]:
        raise WorkflowError("source media type has no declared processing validator")
    if media_type in ("text/plain", "text/markdown", "application/pdf"):
        return validate_output(path, media_type, limits["max_input_bytes"])
    if media_type in ("audio/wav", "audio/x-wav"):
        try:
            with wave.open(str(path), "rb") as audio:
                frames = audio.getnframes()
                if (
                    frames <= 0
                    or audio.getnchannels() <= 0
                    or audio.getframerate() <= 0
                ):
                    raise WorkflowError("empty or invalid WAV recording")
                expected = frames * audio.getnchannels() * audio.getsampwidth()
                if expected > limits["max_input_bytes"]:
                    raise WorkflowError("WAV header exceeds the input byte limit")
                frame_bytes = audio.getnchannels() * audio.getsampwidth()
                frame_chunk = max(1, min(16384, 1048576 // frame_bytes))
                total = 0
                while chunk := audio.readframes(frame_chunk):
                    total += len(chunk)
                if total != expected:
                    raise WorkflowError("incomplete WAV recording")
        except (wave.Error, EOFError) as exc:
            raise WorkflowError("invalid WAV recording") from exc
    elif media_type.startswith(("audio/", "video/")):
        binding = specification.get("probe")
        if not binding or not specification.get("formats"):
            raise WorkflowError(
                "media validation requires a pinned probe and declared containers"
            )
        verify_binding(binding)
        command = build_command(
            binding, "probe", path, path.parent / "unused-probe-output"
        )
        before = file_hash(path)
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(  # nosec B603
                    command,
                    cwd=path.parent,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=limits["timeout_seconds"],
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise WorkflowError("media probe timed out") from exc
            output.seek(0)
            raw = output.read(65537)
        if result.returncode or len(raw) > 65536 or file_hash(path) != before:
            raise WorkflowError("media probe failed or changed its input")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("format"), dict)
            or not isinstance(value["format"].get("format_name"), str)
            or not isinstance(value.get("streams"), list)
            or not all(isinstance(stream, dict) for stream in value["streams"])
        ):
            raise WorkflowError("media probe returned an invalid format/stream report")
        formats = set(value.get("format", {}).get("format_name", "").split(","))
        streams = {s.get("codec_type") for s in value.get("streams", [])}
        if (
            not formats.intersection(specification["formats"])
            or media_type.split("/")[0] not in streams
        ):
            raise WorkflowError(
                "recording container or stream does not match its declared type"
            )
    else:
        raise WorkflowError("source format has no installed processing validator")
    return {
        "sha256": file_hash(path),
        "size": path.stat().st_size,
        "media_type": media_type,
    }


class ProcessingAdapter:
    def __init__(self, installation, source_reader):
        validate_contract(installation, "installation")
        if "processing" not in installation:
            raise WorkflowError("installation has no explicit processing profile")
        self.installation = deepcopy(installation)
        self.profile = self.installation["processing"]
        self.root = private_directory(self.profile["work_root"])
        self.reader = source_reader
        cache = self.root / "processing.sqlite"
        if cache.is_symlink():
            raise WorkflowError("processing cache must not be symlinked")
        try:
            fd = os.open(cache, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = cache.stat()
        if not stat.S_ISREG(info.st_mode) or (
            os.name == "posix"
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077)
        ):
            raise WorkflowError("processing cache must be a private regular file")
        self.db = sqlite3.connect(cache, isolation_level=None, timeout=10)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if version == 0 and not tables:
                self.db.execute(
                    "CREATE TABLE receipts (key TEXT PRIMARY KEY, plan_hash TEXT, operation TEXT, input_hash TEXT, receipt TEXT, receipt_hash TEXT)"
                )
                self.db.execute(
                    "CREATE TABLE snapshots (ref TEXT, sha256 TEXT, path TEXT, PRIMARY KEY(ref,sha256))"
                )
                self.db.execute("PRAGMA user_version=1")
            elif version != 1 or {t[0] for t in tables} != {"receipts", "snapshots"}:
                raise WorkflowError("unrelated or unsupported processing cache")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def preflight(self, planned):
        if digest(self.installation) != planned["pins"]["installation"]:
            raise WorkflowError(
                "processing profile does not match the pinned installation"
            )
        private_directory(self.root)
        initial = {a["role"] for a in planned["artifacts"]}
        if set(self.profile["inputs"]) != initial:
            raise WorkflowError("processing input validators do not match source roles")
        outputs = set()
        for step in planned["steps"]:
            if step["capability"] == "documents.index":
                continue
            if (
                step["capability"]
                not in (
                    "documents.transcribe",
                    "documents.summarize",
                    "documents.brand",
                )
                or len(step["inputs"]) != 1
                or len(step["outputs"]) != 1
            ):
                raise WorkflowError(
                    "processing adapter requires supported one-input/one-output steps"
                )
            binding = self.installation["tools"][step["capability"]]
            verify_binding(binding)
            if not binding.get("arguments") and not binding.get("variants"):
                raise WorkflowError("processing binding has no explicit argv")
            outputs.update(step["outputs"])
        for specification in self.profile["outputs"].values():
            expected = {
                "text/plain": "txt",
                "text/markdown": "md",
                "application/pdf": "pdf",
            }[specification["media_type"]]
            if specification["extension"] != expected:
                raise WorkflowError(
                    "processing output extension does not match its format"
                )
        if outputs != set(self.profile["outputs"]):
            raise WorkflowError(
                "processing output validators do not match protocol roles"
            )
        for specification in self.profile["inputs"].values():
            if specification.get("probe"):
                verify_binding(specification["probe"])
        for resource in self.profile["resources"]:
            path = Path(resource["path"])
            if (
                not path.is_absolute()
                or not path.is_file()
                or file_hash(path) != resource["sha256"]
            ):
                raise WorkflowError(
                    "processing resource does not match its declared pin"
                )
        return True

    def _directory(self, key):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise WorkflowError("invalid local processing operation key")
        path = self.root / key
        path.mkdir(mode=0o700, exist_ok=True)
        return private_directory(path)

    def _snapshot(self, artifact):
        row = self.db.execute(
            "SELECT path FROM snapshots WHERE ref=? AND sha256=?",
            (artifact["ref"], artifact["sha256"]),
        ).fetchone()
        if row is None:
            raise WorkflowError("processing input has no recorded private snapshot")
        path = Path(row[0])
        if (
            not path.is_relative_to(self.root)
            or path.resolve() != path
            or not path.is_file()
            or path.stat().st_size != artifact["size"]
            or file_hash(path) != artifact["sha256"]
        ):
            raise WorkflowError(
                "processing snapshot changed or escaped its private root"
            )
        return path

    def _commit(self, key, operation, inputs, planned, receipt, snapshots):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for artifact, path in snapshots:
                with Path(path).open("r+b") as stream:
                    os.fsync(stream.fileno())
                fsync_dir(Path(path).parent)
                self.db.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                    (artifact["ref"], artifact["sha256"], str(path)),
                )
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                (
                    key,
                    planned["plan_hash"],
                    operation,
                    digest(inputs),
                    json.dumps(receipt),
                    digest(receipt),
                ),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return deepcopy(receipt)

    def reconcile(self, key, operation, inputs, planned):
        row = self.db.execute(
            "SELECT plan_hash,operation,input_hash,receipt,receipt_hash FROM receipts WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row[:3] != (planned["plan_hash"], operation, digest(inputs)):
            raise WorkflowError("processing receipt identity conflict")
        value = json.loads(row[3])
        if digest(value) != row[4]:
            raise WorkflowError("processing receipt hash mismatch")
        return value

    def perform(self, key, operation, inputs, planned):
        with execution_lock(self.root / "processing-cache"):
            return self._perform_locked(key, operation, inputs, planned)

    def _perform_locked(self, key, operation, inputs, planned):
        previous = self.reconcile(key, operation, inputs, planned)
        if previous is not None:
            return previous
        directory = self._directory(key)
        if any(directory.iterdir()):
            raise WorkflowError(
                "unconfirmed processing files require review; no overwrite"
            )
        if operation == "$inputs":
            metadata = []
            for artifact in inputs:
                specification = self.profile["inputs"][artifact["role"]]
                value = self.reader.metadata(artifact["ref"])
                if (
                    not artifact["ref"].startswith("gdrive://")
                    or value.get("id") != artifact["ref"].split("://")[1]
                    or value.get("trashed") is not False
                    or not isinstance(value.get("parents"), list)
                    or planned["intake_ref"].split("://")[1] not in value["parents"]
                    or value.get("mimeType") != artifact["media_type"]
                    or artifact["media_type"] not in specification["media_types"]
                    or not 0
                    < int(value.get("size", 0))
                    <= self.profile["max_input_bytes"]
                ):
                    raise WorkflowError(
                        "source does not match the authorized intake and media policy"
                    )
                metadata.append(value)
            receipt, snapshots = [], []
            for number, artifact in enumerate(inputs):
                spec = self.profile["inputs"][artifact["role"]]
                path = directory / f"source-{number}.{spec['extension']}"
                content, _ = self.reader.read_reference(artifact["ref"], streaming=True)
                try:
                    if len(content) != int(metadata[number]["size"]):
                        raise WorkflowError("source size changed during download")
                    with path.open("xb") as destination:
                        content.copy_to(destination)
                finally:
                    content.close()
                os.chmod(path, 0o600)
                measured = validate_input(
                    path, artifact["media_type"], spec, self.profile
                )
                value = {
                    "role": artifact["role"],
                    "ref": artifact["ref"],
                    **measured,
                    "derived_from": [],
                }
                receipt.append(value)
                snapshots.append((value, path))
            return self._commit(key, operation, inputs, planned, receipt, snapshots)
        step = next(s for s in planned["steps"] if s["id"] == operation)
        if step["capability"] == "documents.index":
            raise WorkflowError(
                "processing adapter never indexes; use prepare and the archiver handover"
            )
        self.preflight(planned)
        original = self._snapshot(inputs[0])
        source = directory / ("input" + original.suffix)
        with original.open("rb") as stream, source.open("xb") as destination:
            shutil.copyfileobj(stream, destination)
        os.chmod(source, 0o600)
        role = step["outputs"][0]
        spec = self.profile["outputs"][role]
        target = directory / ("result." + spec["extension"])
        measured = run_transform(
            self.installation["tools"][step["capability"]],
            inputs[0]["role"],
            source,
            target,
            media_type=spec["media_type"],
            timeout_seconds=self.profile["timeout_seconds"],
            max_output_bytes=self.profile["max_output_bytes"],
        )
        os.chmod(target, 0o600)
        value = {
            "role": role,
            "ref": target.as_uri(),
            **measured,
            "derived_from": [inputs[0]["ref"]],
        }
        return self._commit(key, operation, inputs, planned, [value], [(value, target)])
