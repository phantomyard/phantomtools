"""Tests for the A0 containment collector and validators.

Every fixture here is synthetic: no real hostnames, addresses, accounts,
person names, npubs, emails or tokens. The collector is exercised against a
throwaway temp directory that stands in for a host.
"""

import json
import os
import socket
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.containment.collector import (
    CollectorError,
    collect_inventory,
    inventory_digest,
)
from phantomorg.containment.validator import (
    cross_check_errors,
    detect_kind,
    schema_errors,
)

FIXTURES = Path(__file__).parent / "fixtures" / "containment"
CONTRACTS = Path(__file__).parents[1] / "docs" / "containment"
EXAMPLES = CONTRACTS / "examples"

STAMP = "2026-09-21T10:00:00Z"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _plan(base: Path) -> dict:
    """A synthetic plan whose probes point inside ``base``."""
    return {
        "inventory_id": "inventory-2026-09-21-lab-a",
        "collector": "synthetic-operator",
        "hosts": [
            {
                "host_id": "lab-a",
                "workloads": [
                    {
                        "workload_id": "project-lead",
                        "runtime": "phantombot",
                        "os_identity": _current_account(),
                        "candidate_boundary": "project-a",
                        "service_units": ["phantombot-project-lead.service"],
                        "probe": "os_identity",
                    },
                    {
                        "workload_id": "retired-agent",
                        "runtime": "phantombot",
                        "os_identity": "svc-does-not-exist",
                        "candidate_boundary": "project-a",
                        "probe": "os_identity",
                    },
                ],
                "stores": [
                    {
                        "store_id": "project-a-persona-files",
                        "kind": "persona_files",
                        "location": str(base / "personas" / "project-lead"),
                        "disposition": "authoritative",
                        "candidate_boundary": "project-a",
                        "allowed_workloads": ["project-lead"],
                        "probe": "path",
                    },
                    {
                        "store_id": "lab-a-archive",
                        "kind": "backup",
                        "location": str(base / "missing-archive"),
                        "disposition": "retained_copy",
                        "candidate_boundary": None,
                        "allowed_workloads": [],
                        "probe": "path",
                    },
                ],
                "surfaces": [
                    {
                        "surface_id": "maintenance-gateway",
                        "interface": str(base / "maintenance.sock"),
                        "privileged_effect": "yes",
                        "authentication": "operator-owned credential and socket ACL",
                        "probe": "unix_socket",
                    }
                ],
            }
        ],
    }


def _current_account() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _short_temp_root() -> str:
    """AF_UNIX caps ``sun_path`` at ~107 bytes, so keep the probe base short."""
    return "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()


@pytest.fixture()
def probed_host():
    """A temp dir with one observable store, one observable socket, one miss."""
    with tempfile.TemporaryDirectory(prefix="pgt-", dir=_short_temp_root()) as tmp:
        base = Path(tmp)
        (base / "personas" / "project-lead").mkdir(parents=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(base / "maintenance.sock"))
            yield base
        finally:
            server.close()


# --- collector ---------------------------------------------------------------


def test_collector_emits_schema_valid_inventory(probed_host):
    inventory = collect_inventory(_plan(probed_host), collected_at=STAMP)

    assert schema_errors("inventory", inventory) == []
    assert detect_kind(inventory) == "inventory"
    assert inventory_digest(inventory).startswith("sha256:")
    assert inventory["collected_at"] == STAMP


def test_collector_marks_missing_probe_targets_unreachable(probed_host):
    host = collect_inventory(_plan(probed_host), collected_at=STAMP)["hosts"][0]
    stores = {store["store_id"]: store for store in host["stores"]}
    workloads = {w["workload_id"]: w for w in host["workload_identities"]}
    surfaces = {s["surface_id"]: s for s in host["maintenance_surfaces"]}

    assert stores["project-a-persona-files"]["evidence"]["status"] == "observed"
    assert stores["lab-a-archive"]["evidence"]["status"] == "unreachable"
    assert surfaces["maintenance-gateway"]["evidence"]["status"] == "observed"
    assert workloads["project-lead"]["evidence"]["status"] == "observed"
    assert workloads["retired-agent"]["evidence"]["status"] == "unreachable"
    # Every miss must be surfaced as an unknown, never silently dropped.
    assert any("lab-a-archive" in item for item in host["unknowns"])
    assert any("retired-agent" in item for item in host["unknowns"])


def test_collector_rejects_unknown_probe(probed_host):
    plan = _plan(probed_host)
    plan["hosts"][0]["stores"][0]["probe"] = "teleport"
    with pytest.raises(CollectorError):
        collect_inventory(plan, collected_at=STAMP)


def test_collector_host_filter_and_unknown_host(probed_host):
    plan = _plan(probed_host)
    assert (
        len(collect_inventory(plan, host_id="lab-a", collected_at=STAMP)["hosts"]) == 1
    )
    with pytest.raises(CollectorError):
        collect_inventory(plan, host_id="lab-z", collected_at=STAMP)


# --- schema validation -------------------------------------------------------


def test_schema_validates_shipped_doc_examples():
    for name in ("inventory.synthetic.json", "boundary-declaration.synthetic.json"):
        doc = _load(EXAMPLES / name)
        assert schema_errors(detect_kind(doc), doc) == []


def test_schema_reports_a_violation():
    inventory = _load(EXAMPLES / "inventory.synthetic.json")
    del inventory["hosts"][0]["evidence"]["status"]
    errors = schema_errors("inventory", inventory)
    assert errors and "status" in errors[0]


# --- cross-check -------------------------------------------------------------


def test_shipped_declaration_digest_matches_inventory():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    assert declaration["inventory_digest"] == inventory_digest(inventory)


def test_cross_check_accepts_coherent_fixtures():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    assert cross_check_errors(inventory, declaration) == []


def test_cross_check_flags_unknown_host():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["host_id"] = "lab-z"
    assert any(
        "lab-z" in message for message in cross_check_errors(inventory, declaration)
    )


def test_cross_check_flags_missing_store_id():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["boundaries"][0]["store_ids"] = ["project-b-persona-files"]
    errors = cross_check_errors(inventory, declaration)
    assert any("project-b-persona-files" in message for message in errors)


def test_cross_check_flags_missing_workload_id():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["boundaries"][0]["workloads"] = [
        {"workload_id": "project-deputy", "os_identity": "svc-project-deputy"}
    ]
    errors = cross_check_errors(inventory, declaration)
    assert any("project-deputy" in message for message in errors)


def test_cross_check_flags_stale_digest():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["inventory_digest"] = "sha256:" + "0" * 64
    errors = cross_check_errors(inventory, declaration)
    assert any("inventory_digest" in message for message in errors)


def test_cross_check_flags_uncovered_unreachable_store():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["exclusions"] = [{"scope": "some-other-scope", "reason": "unrelated"}]
    errors = cross_check_errors(inventory, declaration)
    assert any("lab-a-archive" in message for message in errors)


def test_cross_check_flags_unknowns_without_exclusions():
    inventory = _load(FIXTURES / "inventory.synthetic.json")
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["exclusions"] = []
    errors = cross_check_errors(inventory, declaration)
    assert any("unknowns" in message for message in errors)


# --- CLI ---------------------------------------------------------------------


def test_cli_collect_then_validate(tmp_path):
    plan_path = tmp_path / "plan.json"
    out_path = tmp_path / "inventory.json"
    (tmp_path / "personas").mkdir()
    plan = _plan(tmp_path)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    runner = CliRunner()
    collected = runner.invoke(
        main,
        [
            "containment-collect",
            "--plan",
            str(plan_path),
            "--out",
            str(out_path),
            "--now",
            STAMP,
        ],
    )
    assert collected.exit_code == 0, collected.output
    assert "inventory_digest: sha256:" in collected.output

    inventory = _load(out_path)
    assert schema_errors("inventory", inventory) == []

    declaration_path = tmp_path / "declaration.json"
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["inventory_digest"] = inventory_digest(inventory)
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")

    checked = runner.invoke(
        main,
        [
            "containment-validate",
            str(declaration_path),
            "--inventory",
            str(out_path),
        ],
    )
    assert checked.exit_code == 0, checked.output
    assert "cross-check against" in checked.output


def test_cli_validate_fails_on_mismatch(tmp_path):
    inventory_path = FIXTURES / "inventory.synthetic.json"
    declaration = _load(FIXTURES / "boundary-declaration.synthetic.json")
    declaration["inventory_digest"] = "sha256:" + "0" * 64
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "containment-validate",
            str(declaration_path),
            "--inventory",
            str(inventory_path),
        ],
    )
    assert result.exit_code == 1
    assert "mismatch" in result.output


def test_cli_collect_fails_cleanly_on_unreadable_plan(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{not json", encoding="utf-8")
    out_path = tmp_path / "inventory.json"

    result = CliRunner().invoke(
        main,
        [
            "containment-collect",
            "--plan",
            str(plan_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 1
    assert "Collection failed" in result.output
    assert not out_path.exists()
