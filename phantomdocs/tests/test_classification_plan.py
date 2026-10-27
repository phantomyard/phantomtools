"""Synthetic, read-only classification proposals and their refusal cases."""

import copy
import hashlib
import json

import pytest
import yaml
from click.testing import CliRunner

from phantomdocs.classification import classification_plan
from phantomdocs.cli import main
from phantomdocs.identity import doc_version_mac_from_hash, root_mac
from phantomdocs.manifest import empty_manifest


@pytest.fixture
def model():
    org = {
        "version": 1,
        "policies": {
            "security_categories": {"category-2": {}, "category-4-sample": {}},
            "access_levels": {
                "both": {"categories": [2, "category-4-sample"]},
                "old": {"categories": [2]},
                "project": {"categories": ["category-4-sample"]},
            },
        },
        "roles": [
            {"id": "editor", "access_level": "both"},
            {"id": "old-reader", "access_level": "old"},
            {"id": "project-reader", "access_level": "project"},
        ],
        "actors": [
            {"id": "custodian", "role": "editor"},
            {"id": "organization-reader", "role": "old-reader"},
            {"id": "project-human", "role": "project-reader"},
        ],
    }
    data = empty_manifest("sample-org", "docs", root_mac("sample-org", "", "docs"))
    urn = "urn:sample-org:doc:record.txt"
    previous = None
    for content in [b"First fictional version", b"Second fictional version"]:
        digest = hashlib.sha256(content).hexdigest()
        mac = doc_version_mac_from_hash(
            data["manifest"]["rootMac"], previous, "record.txt", digest
        )
        data["nodes"].append(
            {
                "urn": urn,
                "mac": mac,
                "parentMac": data["manifest"]["rootMac"],
                "kind": "doc",
                "slug": "record.txt",
                "category": "category-2",
                "contentHash": digest,
                "size": len(content),
                "owners": ["editor"],
                "locations": [{"backend": "gdrive", "ref": "sample-reference"}],
                "meta": {},
                "relations": {},
                "previous": previous,
            }
        )
        previous = mac
    data["currentVersions"][urn] = previous
    data["manifest"]["headMac"] = previous
    args = {
        "ref": urn,
        "expected_mac": previous,
        "expected_category": "category-2",
        "target_category": "category-4-sample",
        "reason": "Project history review",
    }
    return data, org, args


def test_plan_describes_history_and_reader_impact_without_mutation(model):
    data, org, args = model
    before = copy.deepcopy(model)
    plan = classification_plan(data, org, "custodian", **args)
    assert model == before
    assert plan["status"] == "review-required"
    assert plan["execution_available"] is False
    assert plan["history_scope"] == "all-versions"
    assert len(plan["versions"]) == 2
    assert plan["reader_impact"] == {
        "gained": ["project-human"],
        "lost": ["organization-reader"],
        "retained": ["custodian"],
    }
    assert len(plan["expected_manifest_digest"]) == 64
    assert len(plan["expected_policy_hash"]) == 64
    assert plan["blockers"]


@pytest.mark.parametrize("actor", ["project-human", "organization-reader", "unknown"])
def test_plan_requires_write_access_on_both_sides(model, actor):
    data, org, args = model
    with pytest.raises(ValueError, match="denied"):
        classification_plan(data, org, actor, **args)


def test_plan_requires_ownership_even_when_both_categories_are_readable(model):
    data, org, args = model
    org["actors"].append({"id": "other-reader", "role": "editor"})
    for node in data["nodes"]:
        node["owners"] = ["custodian"]
    with pytest.raises(ValueError, match="denied"):
        classification_plan(data, org, "other-reader", **args)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("expected_mac", "0" * 64, "stale current"),
        ("expected_category", "category-1", "stale category"),
        ("target_category", "category-2", "must differ"),
        ("target_category", "category-99", "denied"),
        ("ref", "missing", "existing document"),
        ("reason", " ", "non-empty reason"),
        ("reason", "x" * 1025, "non-empty reason"),
    ],
)
def test_plan_refuses_invalid_intent(model, field, value, message):
    data, org, args = model
    args[field] = value
    with pytest.raises(ValueError, match=message):
        classification_plan(data, org, "custodian", **args)


@pytest.mark.parametrize(
    "field,value", [("category", "category-4-sample"), ("owners", ["other"])]
)
def test_mixed_history_requires_separate_review(model, field, value):
    data, org, args = model
    data["nodes"][0][field] = value
    with pytest.raises(ValueError, match="mixed historical"):
        classification_plan(data, org, "custodian", **args)


def test_duplicate_source_identity_is_not_silently_reclassified(model):
    data, org, args = model
    other = copy.deepcopy(data["nodes"][0])
    other["urn"] = "urn:sample-org:doc:other.txt"
    other["slug"] = "other.txt"
    other["mac"] = doc_version_mac_from_hash(
        other["parentMac"], None, other["slug"], other["contentHash"]
    )
    data["nodes"].append(other)
    data["currentVersions"][other["urn"]] = other["mac"]
    with pytest.raises(ValueError, match="multiple document identities"):
        classification_plan(data, org, "custodian", **args)


def test_broken_history_is_refused(model):
    data, org, args = model
    data["nodes"][-1]["previous"] = "0" * 64
    with pytest.raises(ValueError, match="manifest structure"):
        classification_plan(data, org, "custodian", **args)


@pytest.mark.parametrize("index", [0, -1])
def test_missing_historical_or_current_category_is_a_validation_error(model, index):
    data, org, args = model
    del data["nodes"][index]["category"]
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="manifest structure"):
        classification_plan(data, org, "custodian", **args)
    assert data == before


def test_nonmapping_node_is_a_validation_error(model):
    data, org, args = model
    data["nodes"][-1] = None
    with pytest.raises(ValueError, match="manifest structure"):
        classification_plan(data, org, "custodian", **args)


def test_unrelated_reference_change_invalidates_conservative_snapshot(model):
    data, org, args = model
    first = classification_plan(data, org, "custodian", **args)
    data["refs"]["unrelated-review-tag"] = data["nodes"][0]["mac"]
    second = classification_plan(data, org, "custodian", **args)
    assert second["expected_manifest_digest"] != first["expected_manifest_digest"]
    assert second["expected_current_mac"] == first["expected_current_mac"]
    assert second["expected_policy_hash"] == first["expected_policy_hash"]


@pytest.mark.parametrize("index", [0, -1])
def test_cli_missing_category_returns_a_clean_error(model, tmp_path, index):
    data, org, args = model
    del data["nodes"][index]["category"]
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    policy = tmp_path / "org.yaml"
    policy.write_text(yaml.safe_dump(org), encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            "classification-plan",
            args["ref"],
            "--expected-mac",
            args["expected_mac"],
            "--expected-category",
            "category-2",
            "--category",
            "category-4-sample",
            "--reason",
            args["reason"],
            "--root",
            str(tmp_path),
            "--org-yaml",
            str(policy),
            "--actor",
            "custodian",
        ],
    )
    assert result.exit_code != 0
    assert "missing required field 'category'" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_snapshot_guards_change_with_policy_or_manifest(model):
    data, org, args = model
    first = classification_plan(data, org, "custodian", **args)
    data["nodes"][0]["meta"]["title"] = "Updated metadata"
    second = classification_plan(data, org, "custodian", **args)
    assert second["expected_manifest_digest"] != first["expected_manifest_digest"]
    org["policies"]["security_categories"]["category-2"]["label"] = "Reviewed"
    third = classification_plan(data, org, "custodian", **args)
    assert third["expected_policy_hash"] != second["expected_policy_hash"]


def test_cli_only_prints_a_plan_and_preserves_store_bytes(model, tmp_path):
    data, org, args = model
    manifest = tmp_path / "manifest.yaml"
    policy = tmp_path / "org.yaml"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    policy.write_text(yaml.safe_dump(org), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = CliRunner().invoke(
        main,
        [
            "classification-plan",
            args["ref"],
            "--expected-mac",
            args["expected_mac"],
            "--expected-category",
            "category-2",
            "--category",
            "category-4-sample",
            "--reason",
            args["reason"],
            "--root",
            str(tmp_path),
            "--org-yaml",
            str(policy),
            "--actor",
            "custodian",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["execution_available"] is False
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
