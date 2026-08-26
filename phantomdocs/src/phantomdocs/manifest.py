"""Manifest — the single source of truth (spec §6).

A per-namespace YAML document mapping identity (urn + MAC) to location,
metadata, classification and relations. Version 1 is single-tenant.

Versioning: a URN may map to several nodes (one per version, each with its own
MAC). The *current* version is the last node for that URN; earlier versions are
linked via the `previous` field (git commit-parent style).

Concurrency: every mutating command performs a read-modify-write under an
inter-process lock (``manifest.lock``), and each write uses its own unique
``mkstemp`` file, so concurrent personas never clobber each other's updates
or rename the same temp file.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.yaml"
LOCK_FILENAME = "manifest.lock"


class ManifestError(ValueError):
    """Raised when a manifest fails validation."""


def empty_manifest(org: str, namespace: str, root_mac: str) -> dict[str, Any]:
    """A fresh, valid single-tenant manifest."""
    return {
        "manifest": {
            "version": MANIFEST_VERSION,
            "org": org,
            "namespace": namespace,
            "tenant": "single",
            "rootMac": root_mac,
            "signedRootMac": None,
        },
        "refs": {},
        "nodes": [],
    }


def load(path: str) -> dict[str, Any]:
    """Load and validate a manifest from disk."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    errors = validate(data)
    if errors:
        raise ManifestError("; ".join(errors))
    return data


def save(path: str, data: dict[str, Any]) -> None:
    """Atomically write a manifest to disk using a unique temp file.

    The temp name is unique per call (``mkstemp``), so concurrent writers
    can never collide on the same temp path. The caller is expected to hold
    the inter-process lock (``manifest_lock``) across its read-modify-write,
    but the atomic replace is correct even without it.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def manifest_lock(path: str) -> Iterator[None]:
    """Hold an inter-process lock for ``path`` across a read-modify-write.

    POSIX uses ``fcntl.flock`` on a sidecar ``manifest.lock``; Windows falls
    back to an ``msvcrt`` range lock. The lock is advisory but serializes the
    mutating commands, so a concurrent ``add``/``tag``/``mkdir`` cannot lose
    the other's update.
    """
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(path)) or ".", LOCK_FILENAME
    )
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty == valid)."""
    errors: list[str] = []
    m = data.get("manifest", {})
    if m.get("version") != MANIFEST_VERSION:
        errors.append(f"unsupported manifest version: {m.get('version')!r}")
    if m.get("tenant") == "multi":
        errors.append("multi-tenancy is not supported in this version")
    if not m.get("org"):
        errors.append("manifest.org is required")
    if not m.get("rootMac"):
        errors.append("manifest.rootMac is required")
    return errors


def urn_path(urn: str) -> str:
    """urn:<org>:<kind>:<path> -> <path>."""
    return urn.split(":", 3)[-1]


def _matches(data: dict[str, Any], predicate) -> list[dict[str, Any]]:
    return [n for n in data.get("nodes", []) if predicate(n)]


def node_by_urn(data: dict[str, Any], urn: str) -> dict[str, Any] | None:
    """The current (latest) node for a URN."""
    matches = _matches(data, lambda n: n.get("urn") == urn)
    return matches[-1] if matches else None


def node_by_path(data: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The current (latest) node for a logical path."""
    matches = _matches(data, lambda n: urn_path(n.get("urn", "")) == path)
    return matches[-1] if matches else None


def node_by_slug(data: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """The current (latest) node for a slug."""
    matches = _matches(data, lambda n: n.get("slug") == slug)
    return matches[-1] if matches else None


def node_by_mac(data: dict[str, Any], mac: str) -> dict[str, Any] | None:
    """The node for a version MAC."""
    matches = _matches(data, lambda n: n.get("mac") == mac)
    return matches[-1] if matches else None


def versions_of(data: dict[str, Any], urn: str) -> list[dict[str, Any]]:
    """All versions of a URN, oldest first."""
    return _matches(data, lambda n: n.get("urn") == urn)


def ref_target_mac(value: Any) -> str | None:
    """The target MAC for a refs-map value (bare MAC string or signed record).

    ``tag`` stores either a bare MAC (legacy, unsigned) or a signed record
    (``{"mac": ..., "sig": ...}``); both name the same target node.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("mac")
    return None


def resolve_node(data: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Resolve a node by urn, logical path, slug, or ref name (a MAC)."""
    node = node_by_urn(data, ref) or node_by_path(data, ref) or node_by_slug(data, ref)
    if node is None and ref in data.get("refs", {}):
        mac = ref_target_mac(data["refs"][ref])
        if mac is not None:
            node = node_by_mac(data, mac)
    return node


class ManifestRepository:
    """Typed boundary over the manifest dict (the YAML persistence DTO).

    Commands mutate the manifest through this repository instead of reaching
    into ``manifest["nodes"]`` / ``manifest["refs"]`` directly, so the YAML
    layout stays an implementation detail behind a stable, typed API (issue
    #46: "the manifest is both storage format and domain model"). The
    repository wraps the in-memory dict; persistence remains the caller's job
    (``load`` before, ``save`` after, under ``manifest_lock``).
    """

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @property
    def data(self) -> dict[str, Any]:
        """The underlying manifest dict (for ``save``)."""
        return self._data

    # -- header accessors --

    @property
    def org(self) -> str:
        return self._data["manifest"]["org"]

    @property
    def namespace(self) -> str:
        return self._data["manifest"]["namespace"]

    @property
    def tenant(self) -> str:
        return self._data["manifest"]["tenant"]

    @property
    def root_mac(self) -> str:
        return self._data["manifest"]["rootMac"]

    # -- collections --

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self._data.setdefault("nodes", [])

    @property
    def refs(self) -> dict[str, Any]:
        return self._data.setdefault("refs", {})

    # -- mutations --

    def add_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """Append a node (a new folder, or a new document version)."""
        self.nodes.append(node)
        return node

    def add_version(self, node: dict[str, Any]) -> dict[str, Any]:
        """Append a document version node (semantic alias of ``add_node``)."""
        return self.add_node(node)

    def set_ref(self, name: str, record: Any) -> None:
        """Point a mutable ref at a version MAC (bare MAC or signed record)."""
        self.refs[name] = record

    # -- lookups (delegated to the module-level resolvers) --

    def node_by_urn(self, urn: str) -> dict[str, Any] | None:
        return node_by_urn(self._data, urn)

    def node_by_path(self, path: str) -> dict[str, Any] | None:
        return node_by_path(self._data, path)

    def node_by_slug(self, slug: str) -> dict[str, Any] | None:
        return node_by_slug(self._data, slug)

    def node_by_mac(self, mac: str) -> dict[str, Any] | None:
        return node_by_mac(self._data, mac)

    def versions_of(self, urn: str) -> list[dict[str, Any]]:
        return versions_of(self._data, urn)

    def resolve_node(self, ref: str) -> dict[str, Any] | None:
        return resolve_node(self._data, ref)
