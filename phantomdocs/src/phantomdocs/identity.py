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
import re

DISPLAY_BYTES = 16  # 128-bit truncation floor (RFC 6920 "sha-256-128")


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def is_valid_hex64(value: str) -> bool:
    """True iff value is a 64-char lowercase hex string (a SHA-256 digest)."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def is_valid_slug(slug: str) -> bool:
    """True iff ``slug`` is a valid SPEC §7 name segment.

    A slug is a single, non-empty, lowercase/kebab ASCII segment: lowercase
    letters, digits, hyphen and dot, with no spaces, slashes, accents, or
    parent-traversal (``..``).
    """
    return bool(slug) and ".." not in slug and _SLUG_RE.match(slug) is not None


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw content bytes."""
    return _sha256(data).hex()


def root_mac(org_id: str, org_pubkey: str, namespace: str) -> str:
    """The namespace root MAC: H(len(org_id) || org_id || len(pubkey) ||
    pubkey || len(namespace) || namespace).

    Every field is length-prefixed so the root is domain-separated: two orgs
    with the documented defaults (empty pubkey, namespace "docs") can never
    collide, because the validated org id is part of the preimage. The empty
    ``--org-pubkey`` default is therefore safe rather than a foot-gun."""

    def field(value: str) -> bytes:
        raw = value.encode()
        return len(raw).to_bytes(4, "big") + raw

    return _sha256(field(org_id) + field(org_pubkey) + field(namespace)).hex()


def component_for_folder(slug: str) -> bytes:
    """Folder component = len(slug) || slug (folders are structural)."""
    raw = slug.encode()
    return len(raw).to_bytes(4, "big") + raw


def component_for_doc(slug: str, content: bytes) -> bytes:
    """Document component = len(slug) || slug || H(content). The slug is
    length-prefixed so a slug boundary can never be ambiguous."""
    raw = slug.encode()
    return len(raw).to_bytes(4, "big") + raw + _sha256(content)


def node_mac(parent_mac: str, component: bytes) -> str:
    """H(parent_MAC || component) — the child inherits the parent's header."""
    return _sha256(bytes.fromhex(parent_mac) + component).hex()


def doc_version_mac(
    parent_mac: str, previous_mac: str | None, slug: str, content: bytes
) -> str:
    """The identity of a document version (issue #44).

    Version identity binds the predecessor, so the version history is
    cryptographically chained — not just the tree:

        v1 = H(parentMac || slug || H(content))
        vN = H(v_{N-1} || slug || H(content))    (N > 1)

    ``parent_mac`` is the *tree* parent (folder or root) and is used only for
    the first version; later versions chain off their predecessor instead.
    This makes a rollback to older content yield a distinct identity (a
    different predecessor) and makes tampering with ``previous`` detectable
    by MAC recomputation.
    """
    base = previous_mac if previous_mac else parent_mac
    return node_mac(base, component_for_doc(slug, content))


def full_id(mac: str) -> str:
    """Self-describing full form: sha2-256-256:<64 hex>."""
    return f"sha2-256-256:{mac}"


def display_id(mac: str) -> str:
    """Self-describing display form: sha2-256-128:<32 hex>."""
    return f"sha2-256-128:{mac[: DISPLAY_BYTES * 2]}"
