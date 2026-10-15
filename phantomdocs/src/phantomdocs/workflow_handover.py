"""Separate processing results, operator archive receipts and ref indexing.

This module never uploads, moves, renames or deletes external objects. An archive
receipt is trusted local operator input, not proof that Drive has operation IDs.
Actual bytes and folder membership are independently checked before indexing.
"""

from __future__ import annotations

import json
from pathlib import Path

from .access import policy_hash
from .documents import DocumentService
from .identity import is_valid_slug
from .workflow_executor import execution_lock, validate_artifacts
from .workflow_requests import utc_now, utc_text
from .workflows import WorkflowError, digest, validate_contract


def export_results(ledger, organization, request_id, org, caller):
    record = ledger.status(organization, request_id, org, caller)
    planned = record["plan"]
    exists = ledger.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_execution'"
    ).fetchone()
    if not exists:
        raise WorkflowError("processing results are not complete")
    artifacts = {}
    operations = [
        {"id": "$inputs", "outputs": [a["role"] for a in planned["artifacts"]]}
    ] + [s for s in planned["steps"] if s["capability"] != "documents.index"]
    for step in operations:
        row = ledger.connection.execute(
            "SELECT * FROM workflow_execution WHERE organization=? AND request_id=? AND operation=?",
            (organization, request_id, step["id"]),
        ).fetchone()
        if (
            row is None
            or row["state"] != "complete"
            or row["plan_hash"] != planned["plan_hash"]
        ):
            raise WorkflowError("processing results are not complete")
        receipt = json.loads(row["receipt"])
        if digest(receipt) != row["receipt_hash"]:
            raise WorkflowError("processing receipt hash mismatch")
        for artifact in validate_artifacts(receipt, step["outputs"]):
            artifacts[artifact["role"]] = artifact
    result = {
        "version": 1,
        "kind": "processed-document-results",
        "organization": organization,
        "request_id": request_id,
        "plan_hash": planned["plan_hash"],
        "artifacts": [artifacts[role] for role in planned["final_roles"]],
    }
    result["results_hash"] = digest(result)
    validate_contract(result, "results-manifest")
    return result


def validate_archive(receipt, results, planned):
    validate_contract(receipt, "archive-receipt")
    for field in ("organization", "request_id", "plan_hash", "results_hash"):
        if receipt[field] != results[field]:
            raise WorkflowError(
                "archive receipt is not bound to these processing results"
            )
    artifacts = receipt["artifacts"]
    roles = [a["role"] for a in artifacts]
    if len(set(roles)) != len(roles) or set(roles) != set(planned["final_roles"]):
        raise WorkflowError("archive receipt must contain exactly all final roles")
    if len({a["ref"] for a in artifacts}) != len(artifacts) or len(
        {a["slug"] for a in artifacts}
    ) != len(artifacts):
        raise WorkflowError("archive receipt contains duplicate refs or slugs")
    source = {a["role"]: a for a in results["artifacts"]}
    for artifact in artifacts:
        if not is_valid_slug(artifact["slug"]):
            raise WorkflowError("invalid archived document slug")
        if (
            not planned["destinations"]["archive_ref"]
            or artifact["archive_ref"] != planned["destinations"]["archive_ref"]
        ):
            raise WorkflowError(
                "archive destination does not match the authorized plan"
            )
        if any(artifact[f] != source[artifact["role"]][f] for f in ("sha256", "size")):
            raise WorkflowError("archive receipt changed processed artifact bytes")
    return artifacts


def index_archive(
    ledger, receipt, org, caller, *, root, org_yaml, drive_reader, nsec_file=None
):
    """Consume a local archiver receipt; read only Drive and index existing refs.

    drive_reader.metadata(ref) returns id/parents/size/trashed and optional MD5.
    drive_reader.read_reference(ref, streaming=True) supplies a bounded snapshot.
    MD5 is auxiliary; the document service verifies the expected SHA-256 before
    committing a new node. Identical retries skip Drive reads and hashing.
    """
    validate_contract(receipt, "archive-receipt")
    organization, request_id = receipt["organization"], receipt["request_id"]
    with execution_lock(ledger.path):
        results = export_results(ledger, organization, request_id, org, caller)
        planned = ledger.status(organization, request_id, org, caller)["plan"]
        ledger._authorize(planned, org, caller, True)
        if (
            caller != planned["processor"]["actor"]
            or policy_hash(org) != planned["pins"]["org"]
        ):
            raise WorkflowError(
                "archive consumer must match the pinned processor and policy"
            )
        artifacts = validate_archive(receipt, results, planned)
        service = DocumentService(str(root), str(org_yaml), caller, nsec_file)
        if (
            service.policy_hash != planned["pins"]["org"]
            or service._load_repo().org != organization
        ):
            raise WorkflowError(
                "document service authority or store organization mismatch"
            )
        store_identity = str(Path(root).resolve())
        db = ledger.connection
        db.execute("""CREATE TABLE IF NOT EXISTS workflow_archive_index (
            organization TEXT NOT NULL, request_id TEXT NOT NULL, role TEXT NOT NULL,
            archive_hash TEXT NOT NULL, store_identity TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('started','complete')),
            urn TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(organization,request_id,role))""")
        archive_hash = digest(receipt)
        rows = {}
        for artifact in artifacts:
            row = db.execute(
                "SELECT * FROM workflow_archive_index WHERE organization=? AND request_id=? AND role=?",
                (organization, request_id, artifact["role"]),
            ).fetchone()
            if row and (
                row["archive_hash"] != archive_hash
                or row["store_identity"] != store_identity
            ):
                raise WorkflowError("conflicting archive receipt or store identity")
            rows[artifact["role"]] = row
        # Check the entire unconfirmed set before changing document state.
        for artifact in artifacts:
            row = rows[artifact["role"]]
            if row and row["state"] == "complete":
                continue
            metadata = drive_reader.metadata(artifact["ref"])
            if (
                metadata.get("id") != artifact["ref"].split("://")[1]
                or metadata.get("trashed") is not False
                or artifact["archive_ref"].split("://")[1]
                not in metadata.get("parents", [])
                or str(metadata.get("size")) != str(artifact["size"])
                or (
                    "md5" in artifact and metadata.get("md5Checksum") != artifact["md5"]
                )
            ):
                raise WorkflowError(
                    "archived Drive object does not match its destination or receipt"
                )
        output = []
        for artifact in artifacts:
            row = rows[artifact["role"]]
            if row is None:
                db.execute(
                    "INSERT INTO workflow_archive_index VALUES (?,?,?,?,?,?,?,?)",
                    (
                        organization,
                        request_id,
                        artifact["role"],
                        archive_hash,
                        store_identity,
                        "started",
                        None,
                        utc_text(utc_now()),
                    ),
                )

            def no_read(*args, **kwargs):
                raise WorkflowError(
                    "completed archive reference is missing; review required"
                )

            result = service.index_reference_once(
                ref=artifact["ref"],
                slug=artifact["slug"],
                category=planned["classification"]["category"],
                owners=planned["classification"]["owners"],
                folder=planned["destinations"]["logical_folder"],
                expected_hash=artifact["sha256"],
                reader=no_read
                if row and row["state"] == "complete"
                else drive_reader.read_reference,
            )
            if row and row["state"] == "complete" and result["urn"] != row["urn"]:
                raise WorkflowError("indexed reference identity changed")
            if not row or row["state"] != "complete":
                db.execute(
                    "UPDATE workflow_archive_index SET state='complete',urn=?,updated_at=? WHERE organization=? AND request_id=? AND role=?",
                    (
                        result["urn"],
                        utc_text(utc_now()),
                        organization,
                        request_id,
                        artifact["role"],
                    ),
                )
            output.append(
                {
                    "role": artifact["role"],
                    "ref": artifact["ref"],
                    "urn": result["urn"],
                    "unchanged": bool(result.get("unchanged")),
                }
            )
        return {
            "version": 1,
            "state": "indexed",
            "organization": organization,
            "request_id": request_id,
            "plan_hash": planned["plan_hash"],
            "results_hash": results["results_hash"],
            "archive_hash": archive_hash,
            "documents": output,
        }


def build_archive_receipt(ledger, organization, request_id, org, caller, mapping):
    """Bind explicitly supplied archive IDs/slugs to authoritative results.

    This records operator intent in stdout only. It neither uploads nor proves
    actual folder membership; index_archive independently verifies that later.
    """
    results = export_results(ledger, organization, request_id, org, caller)
    planned = ledger.status(organization, request_id, org, caller)["plan"]
    ledger._authorize(planned, org, caller, True)
    if not isinstance(mapping, list) or not all(
        isinstance(entry, dict) for entry in mapping
    ):
        raise WorkflowError("archive mapping must be an explicit list")
    source = {entry["role"]: entry for entry in results["artifacts"]}
    artifacts = []
    for entry in mapping:
        if (
            not {"role", "ref", "slug"} <= set(entry)
            or not set(entry) <= {"role", "ref", "slug", "name", "md5"}
            or entry["role"] not in source
        ):
            raise WorkflowError("invalid archive mapping fields or role")
        original = source[entry["role"]]
        artifacts.append(
            {
                **entry,
                "archive_ref": planned["destinations"]["archive_ref"],
                "sha256": original["sha256"],
                "size": original["size"],
            }
        )
    receipt = {
        **{
            field: results[field]
            for field in (
                "version",
                "organization",
                "request_id",
                "plan_hash",
                "results_hash",
            )
        },
        "kind": "archived-document-results",
        "artifacts": artifacts,
    }
    validate_archive(receipt, results, planned)
    return receipt
