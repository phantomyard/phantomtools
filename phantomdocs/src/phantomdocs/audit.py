"""Append-only audit log (spec §12).

Every mutating operation appends one JSON line:
``{ts, actor, action, urn, mac, hash}``. The log is opened in append mode only
— it never truncates, so history is preserved by construction.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

AUDIT_FILENAME = "audit.log"


def append(
    root: str,
    actor: str,
    action: str,
    urn: str,
    mac: str,
    content_hash: str | None,
) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor,
        "action": action,
        "urn": urn,
        "mac": mac,
        "hash": content_hash,
    }
    path = os.path.join(root, AUDIT_FILENAME)
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
