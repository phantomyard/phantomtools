"""Storage adapters (spec §8).

Every node is a blob (bytes) + its content hash; adapters only move bytes.
The OS is irrelevant because everything flows through this layer.
"""

from __future__ import annotations

import os


class StorageError(Exception):
    """Raised when a backend cannot read/write a blob."""


class LocalBackend:
    """Content-addressed store: <root>/blobs/<aa>/<full-sha256-hex>."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def blob_path(self, content_hash: str) -> str:
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
            return f.read()

    def has(self, content_hash: str) -> bool:
        return os.path.isfile(self.blob_path(content_hash))


class SshBackend:
    """Remote host over SSH. Not implemented in v0.1 (scaffold)."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("ssh:// backend is not implemented in v0.1")


class GdriveBackend:
    """Google Drive via OAuth2. Not implemented in v0.1 (scaffold)."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("gdrive:// backend is not implemented in v0.1")
