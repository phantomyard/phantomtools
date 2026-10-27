"""Classification event signature primitives, not an authorization or write API."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

import coincurve

from .signing import nsec_to_secret_hex, pubkey_from_nsec

_EVENT_DOMAIN = b"phantomdocs-classification-event-v1\x00"
_RECORD_DOMAIN = b"phantomdocs-classification-record-v1\x00"
_HISTORY_DOMAIN = b"phantomdocs-classification-history-v1\x00"
_FIELDS = {
    "version",
    "kind",
    "org",
    "namespace",
    "root_mac",
    "urn",
    "actor",
    "from_category",
    "target_category",
    "owners",
    "history_scope",
    "expected_manifest_digest",
    "expected_current_mac",
    "policy_hash",
    "history_digest",
    "seq",
    "audit_seq",
    "prev_audit_head",
    "previous_event",
    "reason",
    "issued_at",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")
_CATEGORY = re.compile(r"category-\d+(?:-[A-Za-z0-9][A-Za-z0-9_-]*)*")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and not any(ord(char) < 32 for char in value)
    )


def _hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def classification_event_envelope(payload: dict[str, Any]) -> bytes:
    """Validate the closed v1 payload and return its canonical UTF-8 bytes.

    Valid syntax does not establish actor identity, current ACL authority,
    history integrity, freshness, audit continuity or approval to execute.
    """
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("classification event fields must match the closed v1 schema")
    if (
        type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["kind"] != "reclassify"
        or payload["history_scope"] != "all-versions"
    ):
        raise ValueError("unsupported classification event version, kind or scope")
    for field in ("org", "namespace", "actor"):
        if not _text(payload[field], 1024):
            raise ValueError(f"invalid classification event {field}")
    if (
        not _text(payload["urn"], 4096)
        or not payload["urn"].startswith(f"urn:{payload['org']}:doc:")
        or payload["urn"] == f"urn:{payload['org']}:doc:"
    ):
        raise ValueError(
            "classification event requires a document URN in its organization"
        )
    for field in (
        "root_mac",
        "expected_manifest_digest",
        "expected_current_mac",
        "policy_hash",
        "history_digest",
    ):
        if not _hex(payload[field]):
            raise ValueError(f"classification event {field} must be lowercase 64-hex")
    for field in ("previous_event", "prev_audit_head"):
        if payload[field] is not None and not _hex(payload[field]):
            raise ValueError(f"invalid classification event {field}")
    for field in ("seq", "audit_seq"):
        if type(payload[field]) is not int or payload[field] < 1:
            raise ValueError(f"classification event {field} must be a positive integer")
    if (payload["audit_seq"] == 1) != (payload["prev_audit_head"] is None):
        raise ValueError(
            "classification event audit predecessor does not match its sequence"
        )
    for field in ("from_category", "target_category"):
        if not isinstance(payload[field], str) or not _CATEGORY.fullmatch(
            payload[field]
        ):
            raise ValueError(f"invalid classification event {field}")
    if payload["from_category"] == payload["target_category"]:
        raise ValueError("classification event must change category")
    owners = payload["owners"]
    if (
        not isinstance(owners, list)
        or not owners
        or not all(_text(owner, 1024) for owner in owners)
        or owners != sorted(set(owners))
    ):
        raise ValueError(
            "classification event owners must be sorted, unique and non-empty"
        )
    if not _text(payload["reason"], 1024):
        raise ValueError("invalid classification event reason")
    stamp = payload["issued_at"]
    if not isinstance(stamp, str) or not _STAMP.fullmatch(stamp):
        raise ValueError(
            "classification event issued_at must be a UTC second timestamp"
        )
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError as exc:
        raise ValueError("invalid classification event timestamp") from exc
    return _canonical(payload)


def classification_history_digest(history: list[dict[str, Any]]) -> str:
    """Bind complete retained nodes in validated oldest-to-newest chain order.

    This hashes supplied data; callers must independently verify the store,
    signatures and lineage. It does not load originals or alter stored nodes.
    """
    if (
        not isinstance(history, list)
        or not history
        or not all(isinstance(node, dict) for node in history)
    ):
        raise ValueError("a non-empty history of node mappings is required")
    try:
        encoded = _canonical(history)
    except (TypeError, ValueError) as exc:
        raise ValueError("history must contain canonical JSON values") from exc
    return hashlib.sha256(_HISTORY_DOMAIN + encoded).hexdigest()


def sign_classification_event(nsec: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Sign candidate bytes only; the future native service must authorize them."""
    envelope = classification_event_envelope(payload)
    secret = bytes.fromhex(nsec_to_secret_hex(nsec))
    message = hashlib.sha256(_EVENT_DOMAIN + envelope).digest()
    return {
        "payload": json.loads(envelope),
        "sigPubkey": pubkey_from_nsec(nsec),
        "sig": coincurve.PrivateKey(secret).sign_schnorr(message, None).hex(),
    }


def verify_classification_signature(record: dict[str, Any]) -> bool:
    """Check signature integrity only, never actor identity or write permission."""
    if not isinstance(record, dict) or set(record) != {"payload", "sigPubkey", "sig"}:
        return False
    if (
        not _hex(record["sigPubkey"])
        or not isinstance(record["sig"], str)
        or not _HEX128.fullmatch(record["sig"])
    ):
        return False
    try:
        envelope = classification_event_envelope(record["payload"])
        message = hashlib.sha256(_EVENT_DOMAIN + envelope).digest()
        return coincurve.PublicKeyXOnly(bytes.fromhex(record["sigPubkey"])).verify(
            bytes.fromhex(record["sig"]), message
        )
    except (ValueError, TypeError):
        return False


def classification_event_id(record: dict[str, Any]) -> str:
    """Digest a valid signed record for predecessor binding, not authorization."""
    if not verify_classification_signature(record):
        raise ValueError("cannot identify an invalid classification signature")
    return hashlib.sha256(_RECORD_DOMAIN + _canonical(record)).hexdigest()
