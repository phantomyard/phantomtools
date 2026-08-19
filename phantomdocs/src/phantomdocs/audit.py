"""Append-only audit log (spec §12).

Every mutating operation appends one JSON line:
``{ts, actor, action, urn, mac, hash, prev}``. Each entry carries a
``prev`` field = SHA-256 of the previous entry's line (including its newline),
so the log is a hash chain: ``pd verify`` can walk it exactly like the node
chain and detect a deleted or reordered middle entry.

The log is opened in append mode only — it never truncates. ``prev`` makes
that property *checkable* rather than merely conventional.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

AUDIT_FILENAME = "audit.log"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def append(
    root: str,
    actor: str,
    action: str,
    urn: str,
    mac: str,
    content_hash: str | None,
) -> None:
    path = os.path.join(root, AUDIT_FILENAME)
    prev: str | None = None
    if os.path.exists(path):
        with open(path, "rb") as f:
            lines = f.read().splitlines(keepends=True)
        if lines:
            prev = _sha256(lines[-1])
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor,
        "action": action,
        "urn": urn,
        "mac": mac,
        "hash": content_hash,
        "prev": prev,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def read(root: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the last `limit` entries, oldest first."""
    path = os.path.join(root, AUDIT_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return entries[-limit:]


def verify_chain(root: str) -> list[str]:
    """Walk the ``prev`` hash chain and return a list of problems (empty == OK).

    Each entry's ``prev`` must equal the SHA-256 of the preceding raw line.
    A missing/incorrect link, or a non-JSON line, is reported so a deleted or
    reordered middle entry leaves a trace.
    """
    path = os.path.join(root, AUDIT_FILENAME)
    if not os.path.exists(path):
        return []  # no audit log is not a failure
    with open(path, "rb") as f:
        raw_lines = f.read().splitlines(keepends=True)

    problems: list[str] = []
    previous_hash: str | None = None
    for index, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        line = raw.decode("utf-8", "replace")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"audit line {index}: not valid JSON")
            previous_hash = None
            continue
        prev = entry.get("prev")
        if previous_hash is not None and prev != previous_hash:
            problems.append(
                f"audit line {index}: prev {prev!r} != {previous_hash!r} (chain broken)"
            )
        previous_hash = _sha256(raw)
    return problems
