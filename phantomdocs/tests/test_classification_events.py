"""Closed classification signature contracts with synthetic identities only."""

import copy
import hashlib

import coincurve
import pytest

from phantomdocs.classification_events import (
    classification_event_envelope,
    classification_event_id,
    classification_history_digest,
    sign_classification_event,
    verify_classification_signature,
)
from phantomdocs.signing import sign_mutation, verify_mutation


@pytest.fixture
def payload():
    return {
        "version": 1,
        "kind": "reclassify",
        "org": "sample-org",
        "namespace": "docs",
        "root_mac": "a" * 64,
        "urn": "urn:sample-org:doc:record.txt",
        "actor": "custodian",
        "from_category": "category-2",
        "target_category": "category-4-sample",
        "owners": ["editor"],
        "history_scope": "all-versions",
        "expected_manifest_digest": "b" * 64,
        "expected_current_mac": "c" * 64,
        "policy_hash": "d" * 64,
        "history_digest": "e" * 64,
        "seq": 8,
        "audit_seq": 8,
        "prev_audit_head": "f" * 64,
        "previous_event": None,
        "reason": "Reviewed project audience",
        "issued_at": "2026-01-02T03:04:05Z",
    }


@pytest.fixture
def secret():
    return coincurve.PrivateKey().secret.hex()


def test_signing_roundtrip_preserves_input_and_uses_detached_payload(payload, secret):
    original = copy.deepcopy(payload)
    signed = sign_classification_event(secret, payload)
    assert payload == original
    assert verify_classification_signature(signed) is True
    assert len(classification_event_id(signed)) == 64
    payload["owners"].append("other")
    assert signed["payload"] == original


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"kind": "add"},
        {"history_scope": "current-version"},
        {"org": "other-org", "urn": "urn:other-org:doc:record.txt"},
        {"namespace": "other-store"},
        {"root_mac": "1" * 64},
        {"urn": "urn:sample-org:doc:other.txt"},
        {"actor": "other-actor"},
        {"from_category": "category-3"},
        {"target_category": "category-4-other"},
        {"owners": ["other-owner"]},
        {"expected_manifest_digest": "2" * 64},
        {"expected_current_mac": "3" * 64},
        {"policy_hash": "4" * 64},
        {"history_digest": "5" * 64},
        {"seq": 9},
        {"audit_seq": 9},
        {"prev_audit_head": "6" * 64},
        {"previous_event": "7" * 64},
        {"reason": "Another audience"},
        {"issued_at": "2026-01-02T03:04:06Z"},
    ],
)
def test_signature_binds_every_classification_field(payload, secret, changes):
    record = sign_classification_event(secret, payload)
    record["payload"].update(changes)
    assert verify_classification_signature(record) is False
    with pytest.raises(ValueError, match="invalid classification signature"):
        classification_event_id(record)


def test_signatures_cannot_cross_existing_mutation_domain(payload, secret):
    record = sign_classification_event(secret, payload)
    envelope = classification_event_envelope(payload)
    assert verify_mutation(record["sigPubkey"], record["sig"], envelope) is False
    record["sig"] = sign_mutation(secret, envelope)
    assert verify_classification_signature(record) is False


def test_wrong_key_and_corrupt_signature_are_refused(payload, secret):
    record = sign_classification_event(secret, payload)
    other = coincurve.PrivateKey().public_key.format()[1:].hex()
    wrong_key = {**record, "sigPubkey": other}
    assert verify_classification_signature(wrong_key) is False
    assert verify_classification_signature({**record, "sig": "0" * 128}) is False


def test_canonical_envelope_ignores_object_key_order(payload, secret):
    reordered = dict(reversed(list(payload.items())))
    assert classification_event_envelope(payload) == classification_event_envelope(
        reordered
    )
    record = sign_classification_event(secret, payload)
    record["payload"] = reordered
    assert verify_classification_signature(record) is True
    envelope = classification_event_envelope(payload)
    assert b'"version":1' in envelope
    assert b'"previous_event":null' in envelope
    assert b"\n" not in envelope


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("seq", True),
        ("seq", 0),
        ("audit_seq", 1.0),
        ("audit_seq", -1),
        ("root_mac", "A" * 64),
        ("policy_hash", "not-a-hash"),
        ("previous_event", "short"),
        ("owners", []),
        ("owners", ["editor", "editor"]),
        ("owners", ["z", "a"]),
        ("owners", [{}]),
        ("owners", "editor"),
        ("actor", ""),
        ("namespace", " docs"),
        ("reason", "x" * 1025),
        ("reason", "line\nbreak"),
        ("from_category", 2),
        ("target_category", "category-2"),
        ("issued_at", "2026-02-30T03:04:05Z"),
        ("issued_at", "2026-01-02T03:04:05+00:00"),
        ("prev_audit_head", None),
        ("urn", "urn:other-org:doc:record.txt"),
        ("urn", "urn:sample-org:doc:"),
    ],
)
def test_invalid_payload_is_refused_before_signing(payload, secret, field, value):
    payload[field] = value
    with pytest.raises(ValueError):
        sign_classification_event(secret, payload)


def test_closed_payload_and_signed_record_schemas(payload, secret):
    for field in payload:
        candidate = {k: v for k, v in payload.items() if k != field}
        with pytest.raises(ValueError, match="closed v1 schema"):
            classification_event_envelope(candidate)
    with pytest.raises(ValueError, match="closed v1 schema"):
        classification_event_envelope({**payload, "approved": True})
    record = sign_classification_event(secret, payload)
    assert verify_classification_signature({**record, "approved": True}) is False
    for candidate in [None, [], {}, {**record, "sig": None}, {**record, "payload": []}]:
        assert verify_classification_signature(candidate) is False


def test_first_audit_event_requires_no_audit_predecessor(payload, secret):
    payload.update(audit_seq=1, prev_audit_head=None)
    assert verify_classification_signature(sign_classification_event(secret, payload))
    payload["prev_audit_head"] = "f" * 64
    with pytest.raises(ValueError, match="audit predecessor"):
        sign_classification_event(secret, payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("contentHash", "new-hash"),
        ("category", "category-4-sample"),
        ("owners", ["another-owner"]),
        ("locations", [{"ref": "other-source"}]),
        ("sig", "different-signature"),
    ],
)
def test_history_digest_binds_complete_nodes_without_mutation(field, value):
    history = [
        {
            "mac": "a" * 64,
            "contentHash": "b" * 64,
            "category": "category-2",
            "owners": ["editor"],
            "locations": [{"ref": "sample-source"}],
            "sig": "sample",
        }
    ]
    original = copy.deepcopy(history)
    first = classification_history_digest(history)
    assert history == original
    history[0][field] = value
    assert classification_history_digest(history) != first


def test_history_digest_preserves_chain_order_and_refuses_invalid_containers():
    history = [{"mac": "a" * 64}, {"mac": "b" * 64}]
    assert classification_history_digest(history) != classification_history_digest(
        history[::-1]
    )
    for invalid in [None, [], {}, [None]]:
        with pytest.raises(ValueError):
            classification_history_digest(invalid)
    with pytest.raises(ValueError):
        classification_history_digest([{"value": float("nan")}])
    with pytest.raises(ValueError, match="canonical JSON values"):
        classification_history_digest([{"value": {"not-json"}}])


def test_event_identifier_binds_the_complete_signed_record(payload, secret):
    record = sign_classification_event(secret, payload)
    assert (
        classification_event_id(record)
        != hashlib.sha256(classification_event_envelope(payload)).hexdigest()
    )
    assert classification_event_id(record) == classification_event_id(
        dict(reversed(list(record.items())))
    )


def test_valid_signature_is_not_actor_authorization(payload, secret):
    # Pure cryptographic primitives cannot establish the signer's org binding.
    payload["actor"] = "a-different-claimed-actor"
    record = sign_classification_event(secret, payload)
    assert verify_classification_signature(record) is True
    # The future native service must reject an actor/key mismatch independently.
