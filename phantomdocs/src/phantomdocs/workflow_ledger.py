"""Private, additive SQLite ledger for authenticated workflow intake.

Stores accepted plans, not processing-completion claims. No document-store or
Drive operations occur. CLI authorization is a guardrail for trusted processes;
direct filesystem/database access remains inside that deployment trust boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from copy import deepcopy
from pathlib import Path

from .access import can_read, can_write
from .workflow_requests import utc_now, utc_text, verify_request
from .workflows import WorkflowError, digest

LEDGER_VERSION = 1


class WorkflowLedger:
    def __init__(self, path, *, readonly=False):
        path = Path(path).absolute()
        self.path = path
        if path.is_symlink() or path.parent.resolve() != path.parent:
            raise WorkflowError("ledger paths must not be symlinked")
        if os.name == "posix" and stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
            raise WorkflowError("ledger directory must not be group/world writable")
        if not readonly:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise WorkflowError("ledger must be a regular file")
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise WorkflowError(
                "ledger must be owned by the caller with mode 0600 or stricter"
            )
        mode = "ro" if readonly else "rw"
        self.connection = sqlite3.connect(
            path.as_uri() + f"?mode={mode}", uri=True, timeout=10, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        try:
            if readonly:
                self._check_version()
            else:
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("BEGIN IMMEDIATE")
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                tables = self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if version == 0 and not tables:
                    self.connection.execute("""CREATE TABLE jobs (
                        organization TEXT NOT NULL, request_id TEXT NOT NULL,
                        requester TEXT NOT NULL, job_hash TEXT NOT NULL,
                        plan_hash TEXT NOT NULL, processor TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('validated','blocked')),
                        request_json TEXT NOT NULL, plan_json TEXT NOT NULL,
                        accepted_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY (organization, request_id))""")
                    self.connection.execute("""CREATE TABLE events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        organization TEXT NOT NULL, request_id TEXT NOT NULL,
                        event TEXT NOT NULL, actor TEXT NOT NULL,
                        plan_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")
                    self.connection.execute("PRAGMA user_version=1")
                else:
                    self._check_version()
                self.connection.execute("COMMIT")
        except Exception:
            self.connection.close()
            raise

    def _check_version(self):
        if (
            self.connection.execute("PRAGMA user_version").fetchone()[0]
            != LEDGER_VERSION
        ):
            raise WorkflowError("unsupported or unrelated workflow ledger")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def _find(self, organization, request_id):
        return self.connection.execute(
            "SELECT * FROM jobs WHERE organization=? AND request_id=?",
            (organization, request_id),
        ).fetchone()

    @staticmethod
    def _plan(row):
        planned = json.loads(row["plan_json"])
        expected = planned.pop("plan_hash")
        if digest(planned) != expected or row["plan_hash"] != expected:
            raise WorkflowError("stored workflow plan hash mismatch")
        planned["plan_hash"] = expected
        return planned

    @staticmethod
    def _authorize(planned, org, caller, write=False):
        if org.get("organization", {}).get("id") != planned["organization"]:
            raise WorkflowError("ledger organization mismatch")
        classification = planned["classification"]
        check = (
            can_write(org, caller, classification["category"], classification["owners"])
            if write
            else can_read(org, caller, classification["category"])
        )
        if (
            classification["category"]
            not in org.get("policies", {}).get("security_categories", {})
            or not check
        ):
            raise WorkflowError("caller is not authorized for this workflow record")

    def _result(self, row, org, caller, *, write=False, unchanged=False):
        planned = self._plan(row)
        self._authorize(planned, org, caller, write)
        events = [
            dict(e)
            for e in self.connection.execute(
                "SELECT seq,event,actor,plan_hash,created_at FROM events WHERE organization=? AND request_id=? ORDER BY seq",
                (row["organization"], row["request_id"]),
            )
        ]
        execution = []
        if self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_execution'"
        ).fetchone():
            execution = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT operation,state,receipt_hash,updated_at FROM workflow_execution WHERE organization=? AND request_id=? ORDER BY operation",
                    (row["organization"], row["request_id"]),
                )
            ]
        if self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_archive_index'"
        ).fetchone():
            archived = list(
                self.connection.execute(
                    "SELECT role,state,updated_at FROM workflow_archive_index WHERE organization=? AND request_id=?",
                    (row["organization"], row["request_id"]),
                )
            )
            if archived:
                complete = {
                    entry["role"] for entry in archived if entry["state"] == "complete"
                } == set(planned["final_roles"])
                index_step = next(
                    s["id"]
                    for s in planned["steps"]
                    if s["capability"] == "documents.index"
                )
                execution.append(
                    {
                        "operation": index_step,
                        "state": "complete" if complete else "started",
                        "receipt_hash": None,
                        "updated_at": max(entry["updated_at"] for entry in archived),
                    }
                )
        return {
            "version": LEDGER_VERSION,
            "organization": row["organization"],
            "request_id": row["request_id"],
            "requester": row["requester"],
            "state": row["state"],
            "accepted_at": row["accepted_at"],
            "updated_at": row["updated_at"],
            "unchanged": unchanged,
            "plan": planned,
            "events": events,
            "execution_ready": False,
            "execution": execution,
        }

    def status(self, organization, request_id, org, caller):
        row = self._find(organization, request_id)
        if row is None:
            raise WorkflowError("workflow record not found or inaccessible")
        return self._result(row, org, caller)

    def accept(self, request, org, caller, make_plan, *, replan=False, now=None):
        # Authenticate before a transaction or any insert. Identical jobs may
        # be freshly signed after envelope expiry without becoming duplicates.
        request = deepcopy(request)
        now = now or utc_now()
        job = verify_request(request, org, now)
        organization, request_id, job_hash = (
            job["organization"],
            job["request_id"],
            digest(job),
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._find(organization, request_id)
            if existing:
                self._authorize(self._plan(existing), org, caller, True)
                if existing["job_hash"] != job_hash:
                    raise WorkflowError(
                        "request id already exists with different intent"
                    )
                if not replan:
                    result = self._result(
                        existing, org, caller, write=True, unchanged=True
                    )
                    self.connection.execute("COMMIT")
                    return result
            if existing and replan:
                table = self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_execution'"
                ).fetchone()
                if (
                    table
                    and self.connection.execute(
                        "SELECT 1 FROM workflow_execution WHERE organization=? AND request_id=? LIMIT 1",
                        (organization, request_id),
                    ).fetchone()
                ):
                    raise WorkflowError(
                        "execution has started; replanning is forbidden"
                    )
            planned = make_plan(job)
            self._authorize(planned, org, caller, True)
            if (
                planned["organization"] != organization
                or planned["request_id"] != request_id
                or planned["pins"]["job"] != job_hash
                or planned["requester"] != job["requester"]
                or planned["execution_ready"]
            ):
                raise WorkflowError(
                    "workflow plan does not bind the authenticated request"
                )
            unsigned_plan = dict(planned)
            expected_hash = unsigned_plan.pop("plan_hash")
            if digest(unsigned_plan) != expected_hash:
                raise WorkflowError("workflow plan hash mismatch")
            if existing:
                if self._plan(existing)["classification"] != planned["classification"]:
                    raise WorkflowError("replanning cannot reclassify or change owners")
                if existing["plan_hash"] == expected_hash:
                    result = self._result(
                        existing, org, caller, write=True, unchanged=True
                    )
                    self.connection.execute("COMMIT")
                    return result
            timestamp = utc_text(now)
            state = "blocked" if planned["status"] == "blocked" else "validated"
            if existing:
                self.connection.execute(
                    """UPDATE jobs SET plan_hash=?,processor=?,state=?,
                    request_json=?,plan_json=?,updated_at=? WHERE organization=? AND request_id=?""",
                    (
                        expected_hash,
                        planned["processor"]["actor"],
                        state,
                        json.dumps(request),
                        json.dumps(planned),
                        timestamp,
                        organization,
                        request_id,
                    ),
                )
            else:
                self.connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        organization,
                        request_id,
                        job["requester"],
                        job_hash,
                        expected_hash,
                        planned["processor"]["actor"],
                        state,
                        json.dumps(request),
                        json.dumps(planned),
                        timestamp,
                        timestamp,
                    ),
                )
            self.connection.execute(
                "INSERT INTO events (organization,request_id,event,actor,plan_hash,created_at) VALUES (?,?,?,?,?,?)",
                (
                    organization,
                    request_id,
                    "replanned" if existing else "accepted",
                    caller,
                    expected_hash,
                    timestamp,
                ),
            )
            result = self._result(
                self._find(organization, request_id), org, caller, write=True
            )
            self.connection.execute("COMMIT")
            return result
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
