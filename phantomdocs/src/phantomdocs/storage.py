"""Storage adapters (spec §8).

Every node is a blob (bytes) + its content hash; adapters only move bytes.
The OS is irrelevant because everything flows through this layer.

Backends are addressed by URI:

    local://<root>                  content-addressed filesystem store
    ssh://[user@]host[:port]/<base> remote content-addressed store over SSH
    gdrive://                       delegates to the persona's workspace.py
"""

from __future__ import annotations

import os
import shutil

# subprocess is required to shell out to ssh / the persona's workspace.py.
# Commands are built as argument lists (no shell=True) and inputs validated.
import subprocess  # nosec B404
import tempfile
from urllib.parse import urlparse

from .identity import content_hash as _content_hash
from .identity import is_valid_hex64


class StorageError(Exception):
    """Raised when a backend cannot read/write a blob."""


def _require_hash(content_hash: str) -> None:
    if not is_valid_hex64(content_hash):
        raise StorageError(f"invalid content hash: {content_hash!r}")


def _run_checked(args: list[str], *, stdin: bytes | None = None, text: bool = False):
    """Run a subprocess without a shell.

    Args are an explicit list (no shell=True) and any remote command is built
    from validated 64-hex hashes, so there is no injection surface. The
    returncode is checked by the caller.
    """
    return subprocess.run(  # nosec B603
        args, input=stdin, capture_output=True, text=text, check=False
    )


class LocalBackend:
    """Content-addressed store: <root>/blobs/<aa>/<full-sha256-hex>."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root or ".")

    def blob_path(self, content_hash: str) -> str:
        _require_hash(content_hash)
        return os.path.join(self.root, "blobs", content_hash[:2], content_hash)

    def put(self, content_hash: str, data: bytes) -> str:
        path = self.blob_path(content_hash)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return path

    def get(self, content_hash: str) -> bytes:
        path = self.blob_path(content_hash)
        if not os.path.isfile(path):
            raise StorageError(f"blob not found: {content_hash}")
        with open(path, "rb") as f:
            data = f.read()
        # Content-addressed store: verify the bytes against the requested
        # hash on read, so a mutated blob is refused (integrity on the read
        # path, not only under `pd verify`).
        if _content_hash(data) != content_hash:
            raise StorageError(f"content hash mismatch for {content_hash}")
        return data

    def has(self, content_hash: str) -> bool:
        return os.path.isfile(self.blob_path(content_hash))


class SshBackend:
    """Remote content-addressed store over SSH (spec §8).

    Same layout as LocalBackend, on the remote host: ``<base>/blobs/<aa>/<hash>``.
    Content hashes are validated as 64-hex before being embedded in a remote
    command, so there is no injection surface from the hash. ``base`` is
    trusted operator configuration.
    """

    def __init__(
        self,
        host: str,
        user: str | None = None,
        port: int = 22,
        base: str = "/var/phantomdocs",
        key: str | None = None,
    ):
        self.host = host
        self.user = user
        self.port = int(port)
        self.base = base.rstrip("/") or "/"
        self.key = key

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _ssh_args(self) -> list[str]:
        args = [
            "ssh",
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
        ]
        if self.key:
            args += ["-i", self.key]
        args.append(self.target)
        return args

    def remote_path(self, content_hash: str) -> str:
        _require_hash(content_hash)
        return f"{self.base}/blobs/{content_hash[:2]}/{content_hash}"

    def put(self, content_hash: str, data: bytes) -> str:
        remote = self.remote_path(content_hash)
        parent = os.path.dirname(remote)
        proc = _run_checked(
            self._ssh_args() + [f"mkdir -p {parent} && cat > {remote}"], stdin=data
        )
        if proc.returncode != 0:
            raise StorageError(self._err(proc, "ssh put failed"))
        return f"ssh://{self.target}:{self.port}{remote}"

    def get(self, content_hash: str) -> bytes:
        remote = self.remote_path(content_hash)
        proc = _run_checked(self._ssh_args() + [f"cat {remote}"])
        if proc.returncode != 0:
            raise StorageError(self._err(proc, "ssh get failed (blob not found?)"))
        data = proc.stdout
        # Integrity on the read path: verify the bytes against the hash.
        if _content_hash(data) != content_hash:
            raise StorageError(f"content hash mismatch for {content_hash}")
        return data

    def has(self, content_hash: str) -> bool:
        remote = self.remote_path(content_hash)
        proc = _run_checked(self._ssh_args() + [f"test -f {remote}"])
        return proc.returncode == 0

    @staticmethod
    def _err(proc: subprocess.CompletedProcess, prefix: str) -> str:
        detail = proc.stderr.decode(errors="replace").strip()
        return f"{prefix}: {detail}" if detail else prefix


class GdriveBackend:
    """Google Drive adapter (spec §8) — delegates to the persona's workspace
    tooling; PhantomDocs never manages Google credentials itself (same pattern
    as PhantomMeet SPEC §6.1).

    ``put`` delegates to ``workspace.py drive-upload``; ``get``/``has`` are
    left to the persona's ``workspace.py drive`` tooling.
    """

    def __init__(self, workspace_py: str = "workspace.py"):
        self.workspace_py = workspace_py

    def _require_tool(self) -> None:
        if shutil.which(self.workspace_py) is None:
            raise StorageError(
                f"gdrive:// requires {self.workspace_py!r} on PATH "
                "(the persona's Google Drive tooling)"
            )

    def put(self, content_hash: str, data: bytes) -> str:
        _require_hash(content_hash)
        self._require_tool()
        with tempfile.NamedTemporaryFile(prefix="pd-", delete=False) as f:
            tmp = f.name
            f.write(data)
        try:
            proc = _run_checked(
                [self.workspace_py, "drive-upload", tmp, "--folder", "phantomdocs"],
                text=True,
            )
        finally:
            os.unlink(tmp)
        if proc.returncode != 0:
            detail = proc.stderr.strip()
            raise StorageError(
                f"drive upload failed: {detail}" if detail else "drive upload failed"
            )
        return proc.stdout.strip() or f"gdrive://phantomdocs/{content_hash}"

    def get(self, content_hash: str) -> bytes:
        _require_hash(content_hash)
        raise StorageError(
            "gdrive:// get is delegated to the persona's `workspace.py drive`"
        )

    def has(self, content_hash: str) -> bool:
        _require_hash(content_hash)
        raise StorageError(
            "gdrive:// has is delegated to the persona's `workspace.py drive`"
        )


def resolve_backend(uri: str):
    """Build a backend from a URI (``local://``, ``ssh://``, ``gdrive://``).

    A path without a scheme is treated as a local root.
    """
    if "://" not in uri:
        return LocalBackend(uri)
    parsed = urlparse(uri)
    scheme = parsed.scheme
    if scheme == "local":
        # ``local://<root>`` puts the root in netloc; ``local:///abs`` puts it
        # in path. Recombine so the documented two-slash form never silently
        # resolves to the current directory.
        root = (parsed.netloc or "") + (parsed.path or "")
        return LocalBackend(root or ".")
    if scheme == "ssh":
        return SshBackend(
            host=parsed.hostname or "",
            user=parsed.username or None,
            port=parsed.port or 22,
            base=parsed.path or "/var/phantomdocs",
        )
    if scheme == "gdrive":
        return GdriveBackend()
    raise StorageError(f"unknown backend scheme: {scheme!r}")
