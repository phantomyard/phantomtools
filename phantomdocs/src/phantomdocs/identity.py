"""Chained identity headers (MAC) — the cryptographic identity scheme.

Spec §4: every node's identifier inherits its parent's header and appends its
own component, forming a hash chain:

    root MAC  = H(org_pubkey || namespace)
    node MAC  = H(parent_MAC || component)
    component(folder) = slug
    component(doc)    = slug || H(content)

Identifiers are self-describing (multihash / RFC 6920 style), spec §4.2:

    full    -> "sha2-256-256:<64 hex>"   (verification)
    display -> "sha2-256-128:<32 hex>"   (128-bit truncation floor)
"""

from __future__ import annotations

import hashlib

DISPLAY_BYTES = 16  # 128-bit truncation floor (RFC 6920 "sha-256-128")


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw content bytes."""
    return _sha256(data).hex()


def root_mac(org_pubkey: str, namespace: str) -> str:
    """The namespace root MAC: H(org_pubkey || namespace)."""
    return _sha256(f"{org_pubkey}{namespace}".encode()).hex()


def component_for_folder(slug: str) -> bytes:
    """Folder component = slug (folders are structural)."""
    return slug.encode("utf-8")


def component_for_doc(slug: str, content: bytes) -> bytes:
    """Document component = slug || H(content) (identity bound to content)."""
    return slug.encode("utf-8") + _sha256(content)


def node_mac(parent_mac: str, component: bytes) -> str:
    """H(parent_MAC || component) — the child inherits the parent's header."""
    return _sha256(bytes.fromhex(parent_mac) + component).hex()


def full_id(mac: str) -> str:
    """Self-describing full form: sha2-256-256:<64 hex>."""
    return f"sha2-256-256:{mac}"


def display_id(mac: str) -> str:
    """Self-describing display form: sha2-256-128:<32 hex>."""
    return f"sha2-256-128:{mac[: DISPLAY_BYTES * 2]}"
