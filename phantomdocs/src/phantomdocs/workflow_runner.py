"""Explicit, hash-pinned local transformation runner; no upload or indexing.

Use only protected installation descriptors and private staging snapshots.
This is a timeout-bound subprocess boundary, not an OS/network sandbox.
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404 - trusted operator bindings, argv only
from pathlib import Path

from .workflow_commands import build_command
from .workflows import WorkflowError, validate_contract


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_binding(binding):
    validate_contract(binding, "tool")
    for field, hash_field in (("entrypoint", "sha256"), ("script", "script_sha256")):
        if field not in binding:
            continue
        path = Path(binding[field])
        # Interpreter links are normal; pins cover the resolved target bytes.
        if not path.is_absolute() or not path.is_file() or hash_field not in binding:
            raise WorkflowError(
                "execution requires absolute paths and verified binary/script hashes"
            )
        if file_hash(path) != binding[hash_field]:
            raise WorkflowError("installed tool bytes do not match their execution pin")


def validate_output(path, media_type, max_bytes):
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= max_bytes
    ):
        raise WorkflowError("missing, linked, empty or oversized transformation output")
    with path.open("rb") as stream:
        header = stream.read(1024)
        if media_type == "application/pdf":
            if not header.startswith(b"%PDF-"):
                raise WorkflowError("renderer did not produce a PDF")
            stream.seek(max(0, path.stat().st_size - 1024))
            if b"%%EOF" not in stream.read():
                raise WorkflowError("renderer produced an incomplete PDF")
        elif media_type in ("text/plain", "text/markdown"):
            stream.seek(0)
            # Bounded by max_bytes; only these explicitly supported formats run.
            try:
                text = stream.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkflowError("text output is not valid UTF-8") from exc
            if not text.strip() or "\x00" in text:
                raise WorkflowError("invalid text transformation output")
        else:
            raise WorkflowError("output format has no installed validator")
    return {
        "sha256": file_hash(path),
        "size": path.stat().st_size,
        "media_type": media_type,
    }


def run_transform(
    binding,
    artifact_role,
    input_path,
    output_path,
    *,
    media_type,
    timeout_seconds,
    max_output_bytes,
):
    """Execute an explicit compatible binding in a fresh private staging directory.

    The caller validates the input format, supplies an immutable source snapshot,
    and owns credentials, OS resource limits and durable operation reconciliation.
    A successful return alone is not a publication or index-completion receipt.
    """
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise WorkflowError("positive timeout and output limit required")
    source, target = Path(input_path).absolute(), Path(output_path).absolute()
    if (
        source.is_symlink()
        or not source.is_file()
        or target.exists()
        or target.is_symlink()
    ):
        raise WorkflowError("regular staging source and fresh output required")
    if (
        target.parent.resolve() != target.parent
        or source.resolve().parent != target.parent
    ):
        raise WorkflowError(
            "source and output must share an unlinked staging directory"
        )
    verify_binding(binding)
    before = file_hash(source)
    command = build_command(binding, artifact_role, source, target)
    # Discard tool diagnostics instead of leaking document text or credentials.
    try:
        result = subprocess.run(  # nosec B603 - pinned trusted executable, no shell
            command,
            cwd=target.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(
            "transformation timed out; reconcile before retrying"
        ) from exc
    if file_hash(source) != before:
        raise WorkflowError("tool modified its staging source")
    if result.returncode != 0:
        raise WorkflowError("transformation failed; reconcile before retrying")
    return validate_output(target, media_type, max_output_bytes)
