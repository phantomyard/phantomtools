"""Authenticated workflow requests using the actor's existing Nostr identity.

Uses existing BIP-340 primitives with a workflow-specific canonical envelope.
No remote transport, private-key generation or credential storage is provided.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

from .access import key_valid_at, validate_org_schema
from .signing import CRYPTO_VERSION, pubkey_from_nsec, sign_mutation, verify_mutation
from .workflows import WorkflowError, digest, validate_contract, validate_job

REQUEST_DOMAIN = b"phantomdocs-workflow-request-v1\x00"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_text(value):
    if value.tzinfo is None:
        raise WorkflowError("a timezone-aware clock is required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError) as exc:
        raise WorkflowError("invalid workflow request timestamp") from exc


def request_envelope(request):
    payload = {
        "kind": request["kind"],
        "version": request["version"],
        "crypto_version": request["crypto_version"],
        "job_hash": digest(request["job"]),
        "organization": request["job"]["organization"],
        "request_id": request["job"]["request_id"],
        "requester": request["job"]["requester"],
        "issued_at": request["issued_at"],
        "expires_at": request["expires_at"],
        "signer_pubkey": request["signer_pubkey"],
    }
    return (
        REQUEST_DOMAIN
        + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    )


def verify_request(request, org, now=None):
    validate_contract(request, "signed-request")
    validate_job(request["job"])
    validate_org_schema(org)
    if org.get("organization", {}).get("id") != request["job"]["organization"]:
        raise WorkflowError("request organization mismatch")
    now = now or utc_now()
    issued, expiry = parse_time(request["issued_at"]), parse_time(request["expires_at"])
    if now.tzinfo is None or not issued <= now < expiry:
        raise WorkflowError(
            "workflow request is expired, future-dated or has an invalid validity window"
        )
    actor, pubkey = request["job"]["requester"], request["signer_pubkey"]
    if not (
        key_valid_at(org, actor, pubkey, request["issued_at"])
        and key_valid_at(org, actor, pubkey, utc_text(now))
    ):
        raise WorkflowError(
            "request signer is not a currently valid declared requester key"
        )
    if not verify_mutation(pubkey, request["signature"], request_envelope(request)):
        raise WorkflowError("invalid workflow request signature")
    return deepcopy(request["job"])


def sign_request(job, org, secret, expires_at, now=None):
    validate_job(job)
    now = now or utc_now()
    request = {
        "kind": "signed-document-job",
        "version": 1,
        "crypto_version": CRYPTO_VERSION,
        "job": deepcopy(job),
        "issued_at": utc_text(now),
        "expires_at": expires_at,
        "signer_pubkey": pubkey_from_nsec(secret),
    }
    request["signature"] = sign_mutation(secret, request_envelope(request))
    verify_request(request, org, now)
    return request
