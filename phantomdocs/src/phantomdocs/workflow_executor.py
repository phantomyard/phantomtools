"""Durable executor with explicit trusted adapters and crash reconciliation.

Adapters own format validation, immutable source snapshots and controlled tool
execution. Every operation receives a stable key; reconcile must retrieve its
committed result, never perform the operation again. No adapters are discovered
from jobs and no Drive writer is installed automatically.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .access import key_valid_at
from .workflow_requests import parse_time, utc_now, utc_text, verify_request
from .workflows import WorkflowError, digest, reference_is_safe


@contextmanager
def execution_lock(path):
    """Single authoritative runner per private ledger, released on process exit."""
    path = Path(str(path) + ".runner.lock")
    if path.is_symlink():
        raise WorkflowError("execution lock must not be symlinked")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise WorkflowError("another executor owns this ledger") from exc
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def validate_artifacts(artifacts, roles):
    if not isinstance(artifacts, list) or len(artifacts) != len(roles):
        raise WorkflowError("adapter must return exactly one artifact per role")
    if not all(isinstance(a, dict) for a in artifacts):
        raise WorkflowError("artifact receipts must be objects")
    if {a.get("role") for a in artifacts} != set(roles):
        raise WorkflowError("adapter returned unexpected artifact roles")
    for artifact in artifacts:
        if (
            set(artifact)
            != {"role", "ref", "media_type", "sha256", "size", "derived_from"}
            or not reference_is_safe(artifact["ref"])
            or not isinstance(artifact["size"], int)
            or isinstance(artifact["size"], bool)
            or artifact["size"] <= 0
            or not isinstance(artifact["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", artifact["sha256"])
            or not isinstance(artifact["media_type"], str)
            or not re.fullmatch(
                r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", artifact["media_type"]
            )
            or not isinstance(artifact["derived_from"], list)
            or not all(isinstance(x, str) for x in artifact["derived_from"])
        ):
            raise WorkflowError("invalid artifact validation receipt")
    if len({a["ref"] for a in artifacts}) != len(artifacts):
        raise WorkflowError("adapter returned duplicate artifact references")
    return deepcopy(artifacts)


class WorkflowExecutor:
    """A trusted adapter supplies preflight, perform and reconcile methods.

    preflight(plan) verifies actual tools, versions and adapter compatibility.
    perform(key, operation, inputs, plan) returns validated artifact receipts;
    index operations return {"indexed_refs": [...]} after idempotent ref indexing.
    reconcile(key, operation, inputs, plan) returns a durable receipt or None.
    None after a started operation requires manual review, not blind repetition.
    """

    def __init__(self, ledger, ledger_path, adapter):
        self.ledger, self.path, self.adapter = ledger, ledger_path, adapter
        db = ledger.connection
        db.execute("""CREATE TABLE IF NOT EXISTS workflow_execution (
            organization TEXT NOT NULL, request_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL, operation TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('started','complete')),
            receipt TEXT, receipt_hash TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(organization,request_id,operation))""")

    def _row(self, org, request_id, operation):
        return self.ledger.connection.execute(
            "SELECT * FROM workflow_execution WHERE organization=? AND request_id=? AND operation=?",
            (org, request_id, operation),
        ).fetchone()

    def _receipt(self, row):
        receipt = json.loads(row["receipt"])
        if digest(receipt) != row["receipt_hash"]:
            raise WorkflowError("execution receipt hash mismatch")
        return receipt

    def _validate(self, receipt, operation, inputs, plan):
        if operation == "$inputs":
            validated = validate_artifacts(receipt, [a["role"] for a in inputs])
            for original in inputs:
                result = next(a for a in validated if a["role"] == original["role"])
                if (
                    result["ref"] != original["ref"]
                    or result["media_type"] != original["media_type"]
                    or result["derived_from"]
                ):
                    raise WorkflowError(
                        "source validation must preserve its reference and format"
                    )
            return validated
        step = next(s for s in plan["steps"] if s["id"] == operation)
        refs = sorted(a["ref"] for a in inputs)
        if step["capability"] == "documents.index":
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"indexed_refs"}
                or receipt["indexed_refs"] != refs
            ):
                raise WorkflowError("index receipt must confirm all final references")
            return receipt
        validated = validate_artifacts(receipt, step["outputs"])
        existing_refs = {a["ref"] for a in plan["artifacts"]} | set(refs)
        for artifact in validated:
            if (
                artifact["ref"] in existing_refs
                or sorted(artifact["derived_from"]) != refs
            ):
                raise WorkflowError(
                    "derived artifacts must preserve source references and lineage"
                )
        return validated

    def _operation(self, operation, inputs, planned):
        organization, request_id = planned["organization"], planned["request_id"]
        row = self._row(organization, request_id, operation)
        if row and row["plan_hash"] != planned["plan_hash"]:
            raise WorkflowError("execution is pinned to a different plan")
        if row and row["state"] == "complete":
            return self._validate(self._receipt(row), operation, inputs, planned)
        key = digest({"plan": planned["plan_hash"], "operation": operation})
        if row:
            receipt = self.adapter.reconcile(
                key, operation, deepcopy(inputs), deepcopy(planned)
            )
            if receipt is None:
                raise WorkflowError(
                    "uncertain operation requires review; no automatic repeat"
                )
        else:
            self.ledger.connection.execute(
                "INSERT INTO workflow_execution VALUES (?,?,?,?,?,?,?,?)",
                (
                    organization,
                    request_id,
                    planned["plan_hash"],
                    operation,
                    "started",
                    None,
                    None,
                    utc_text(utc_now()),
                ),
            )
            # Persist started before external effects. Failures remain uncertain.
            receipt = self.adapter.perform(
                key, operation, deepcopy(inputs), deepcopy(planned)
            )
        receipt = self._validate(receipt, operation, inputs, planned)
        self.ledger.connection.execute(
            "UPDATE workflow_execution SET state='complete',receipt=?,receipt_hash=?,updated_at=? WHERE organization=? AND request_id=? AND operation=?",
            (
                json.dumps(receipt),
                digest(receipt),
                utc_text(utc_now()),
                organization,
                request_id,
                operation,
            ),
        )
        return receipt

    def run(self, organization, request_id, org, caller, rebuild_plan):
        with execution_lock(self.path):
            # Serialize admission/replanning while binding this execution to a plan.
            db = self.ledger.connection
            db.execute("BEGIN IMMEDIATE")
            try:
                record = self.ledger.status(organization, request_id, org, caller)
                planned = record["plan"]
                self.ledger._authorize(planned, org, caller, True)
                if caller != planned["processor"]["actor"]:
                    raise WorkflowError("only the pinned processor can execute")
                stored = self.ledger._find(organization, request_id)
                signed = json.loads(stored["request_json"])
                job = verify_request(signed, org, parse_time(signed["issued_at"]))
                if not key_valid_at(
                    org, job["requester"], signed["signer_pubkey"], utc_text(utc_now())
                ):
                    raise WorkflowError("requester key is no longer valid")
                current = rebuild_plan(job)
                if (
                    current["plan_hash"] != planned["plan_hash"]
                    or planned["status"] != "planned"
                ):
                    raise WorkflowError("stale or blocked workflow plan")
                if len({a["role"] for a in job["artifacts"]}) != len(job["artifacts"]):
                    raise WorkflowError(
                        "executor requires one source artifact per role"
                    )
                if self.adapter.preflight(deepcopy(planned)) is not True:
                    raise WorkflowError(
                        "adapter preflight did not verify execution compatibility"
                    )
                # Reserve source operation in the same transaction as pin checks.
                fresh = self._row(organization, request_id, "$inputs") is None
                if fresh:
                    db.execute(
                        "INSERT INTO workflow_execution VALUES (?,?,?,?,?,?,?,?)",
                        (
                            organization,
                            request_id,
                            planned["plan_hash"],
                            "$inputs",
                            "started",
                            None,
                            None,
                            utc_text(utc_now()),
                        ),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            if fresh:
                # This process created the reservation; invoke exactly once.
                inputs = self.adapter.perform(
                    digest({"plan": planned["plan_hash"], "operation": "$inputs"}),
                    "$inputs",
                    deepcopy(job["artifacts"]),
                    deepcopy(planned),
                )
                inputs = self._validate(inputs, "$inputs", job["artifacts"], planned)
                db.execute(
                    "UPDATE workflow_execution SET state='complete',receipt=?,receipt_hash=?,updated_at=? WHERE organization=? AND request_id=? AND operation='$inputs'",
                    (
                        json.dumps(inputs),
                        digest(inputs),
                        utc_text(utc_now()),
                        organization,
                        request_id,
                    ),
                )
            else:
                inputs = self._operation("$inputs", job["artifacts"], planned)
            artifacts = {a["role"]: a for a in inputs}
            for step in planned["steps"]:
                values = [artifacts[role] for role in step["inputs"]]
                result = self._operation(step["id"], values, planned)
                if step["capability"] != "documents.index":
                    if any(
                        a["ref"] in {v["ref"] for v in artifacts.values()}
                        for a in result
                    ):
                        raise WorkflowError(
                            "derived result reuses an existing artifact reference"
                        )
                    artifacts.update({a["role"]: a for a in result})
            return {
                "state": "indexed",
                "plan_hash": planned["plan_hash"],
                "artifacts": list(artifacts.values()),
                "execution_ready": True,
            }
