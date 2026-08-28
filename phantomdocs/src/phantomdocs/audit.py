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
    sig: str | None = None,
    sig_pubkey: str | None = None,
    seq: int | None = None,
) -> str:
    """Append one entry and return the SHA-256 of the raw line written.

    ``seq`` is the monotonic mutation sequence the entry belongs to; it lets
    ``verify`` detect a truncated or re-ordered tail even when the ``prev``
    chain itself survives intact (issue #71).
    """
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
    if seq is not None:
        entry["seq"] = seq
    if sig is not None:
        entry["sig"] = sig
    if sig_pubkey is not None:
        entry["sigPubkey"] = sig_pubkey
    line = json.dumps(entry, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return _sha256(line.encode("utf-8"))


def head(root: str) -> tuple[int, str | None]:
    """Return ``(entry_count, last_line_hash)`` for the audit log.

    ``last_line_hash`` is the SHA-256 of the last raw line (including its
    newline), or None when the log is empty/absent. This is the *head anchor*
    the manifest stores so `verify` can detect truncation and tail rollback.
    """
    path = os.path.join(root, AUDIT_FILENAME)
    if not os.path.exists(path):
        return 0, None
    with open(path, "rb") as f:
        raw = f.read().splitlines(keepends=True)
    count = sum(1 for line in raw if line.strip())
    if not raw:
        return 0, None
    return count, _sha256(raw[-1])


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


def entries(root: str) -> list[dict[str, Any]]:
    """All audit entries, oldest first (missing/empty log -> [])."""
    path = os.path.join(root, AUDIT_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def sequence_issues(root: str) -> list[str]:
    """Mutation-sequence contiguity over the audit log (issue #73).

    The audit log is the authoritative record of *every* mutation (add, mkdir,
    tag, rollback), each with a monotonic ``seq`` (``init`` is ``seq=0``). The
    invariant is "exactly one valid successor relation": entries carrying a
    ``seq`` must be exactly contiguous — the first such entry anchors the
    sequence and every later one must be exactly the previous + 1. Legacy
    entries without ``seq`` are skipped without resetting the counter.
    """
    problems: list[str] = []
    expected: int | None = None
    for index, entry in enumerate(entries(root), 1):
        seq = entry.get("seq")
        if seq is None:
            continue
        if not isinstance(seq, int) or isinstance(seq, bool):
            problems.append(f"audit line {index}: seq {seq!r} is not an integer")
            continue
        if expected is None:
            expected = seq
        elif seq != expected + 1:
            problems.append(
                f"audit line {index}: seq {seq!r} is not contiguous "
                f"(expected {expected + 1!r})"
            )
        expected = seq
    return problems


def max_seq(root: str) -> int | None:
    """The highest mutation ``seq`` in the audit log, or None if no entry has one.

    This is the authoritative global mutation counter; ``manifest.headSeq`` must
    agree with it (a node whose ``seq``/``headSeq`` drifted from the audit log
    is a divergence the composed-tree check catches).
    """
    best: int | None = None
    for entry in entries(root):
        seq = entry.get("seq")
        if (
            isinstance(seq, int)
            and not isinstance(seq, bool)
            and (best is None or seq > best)
        ):
            best = seq
    return best


def raw_lines(root: str) -> list[bytes]:
    """The non-blank raw audit-log lines (bytes, newlines included), oldest first."""
    path = os.path.join(root, AUDIT_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        return [line for line in f.read().splitlines(keepends=True) if line.strip()]


def truncate(root: str, keep: int) -> None:
    """Rewrite the audit log keeping only the first ``keep`` lines (recovery).

    This is the *recovery* operation for the audit-first transaction (issue #74):
    it discards orphaned audit entries that were appended after the manifest's
    last commit. The caller MUST have already verified that the kept prefix is
    intact and matches the manifest's recorded head; ``truncate`` itself does
    not re-verify. Raw line bytes are preserved so the kept prefix's hash chain
    stays valid.
    """
    path = os.path.join(root, AUDIT_FILENAME)
    lines = raw_lines(root)
    with open(path, "wb") as f:
        f.writelines(lines[:keep])
