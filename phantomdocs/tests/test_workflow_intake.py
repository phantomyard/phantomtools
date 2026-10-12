"""Real signatures and durable intake without media/Drive/store execution."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import coincurve
import pytest
import test_workflows as fixtures
import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.signing import npub_encode, pubkey_from_nsec, sign_mutation
from phantomdocs.workflow_ledger import WorkflowLedger
from phantomdocs.workflow_requests import (
    sign_request,
    utc_now,
    utc_text,
    verify_request,
)
from phantomdocs.workflows import WorkflowError

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
make_plan = fixtures.make_plan


@pytest.fixture
def model():
    return fixtures.model.__wrapped__()


@pytest.fixture
def installation():
    return fixtures.installation.__wrapped__()


@pytest.fixture
def job():
    return fixtures.job.__wrapped__()


@pytest.fixture
def signed_system(model, job):
    # Generate ephemeral test keys; never add deployment identities or secrets.
    secret = coincurve.PrivateKey().secret.hex()
    model["actors"][1]["npub"] = npub_encode(pubkey_from_nsec(secret))
    signed = sign_request(job, model, secret, utc_text(NOW + timedelta(hours=1)), NOW)
    return secret, signed


def test_request_authenticates_the_declared_remote_requester(model, job, signed_system):
    _, signed = signed_system
    assert verify_request(signed, model, NOW) == job
    assert "secret" not in signed and "nsec" not in signed


@pytest.mark.parametrize(
    "change", ["requester", "reference", "organization", "expiry", "signature", "suite"]
)
def test_tampered_envelopes_fail_closed(model, signed_system, change):
    _, signed = signed_system
    signed = deepcopy(signed)
    if change == "requester":
        signed["job"]["requester"] = "coordinator"
    elif change == "reference":
        signed["job"]["artifacts"][0]["ref"] = "gdrive://different-intent"
    elif change == "organization":
        signed["job"]["organization"] = "other-org"
    elif change == "expiry":
        signed["expires_at"] = utc_text(NOW + timedelta(days=1))
    elif change == "suite":
        signed["crypto_version"] = 2
    else:
        signed["signature"] = "0" * 128
    with pytest.raises(WorkflowError):
        verify_request(signed, model, NOW)


def test_expired_future_and_revoked_requests_are_refused(model, signed_system):
    _, signed = signed_system
    for clock in [NOW - timedelta(seconds=1), NOW + timedelta(hours=1)]:
        with pytest.raises(WorkflowError, match="validity"):
            verify_request(signed, model, clock)
    model["actors"][1]["keys"] = [
        {"npub": model["actors"][1]["npub"], "revoked_at": utc_text(NOW)}
    ]
    with pytest.raises(WorkflowError, match="declared requester key"):
        verify_request(signed, model, NOW)


def test_mutation_signatures_cannot_be_replayed_as_requests(model, signed_system):
    secret, signed = signed_system
    signed = deepcopy(signed)
    signed["signature"] = sign_mutation(secret, b"a different protocol envelope")
    with pytest.raises(WorkflowError, match="signature"):
        verify_request(signed, model, NOW)


def test_durable_idempotent_acceptance_pins_original_processor(
    tmp_path, model, installation, signed_system
):
    _, signed = signed_system
    path = tmp_path / "jobs.sqlite"
    with WorkflowLedger(path) as store:
        result = store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=NOW,
        )
        assert result["state"] == "validated" and len(result["events"]) == 1

    # A retry after restart must not rebuild from a changed/missing default.
    def forbidden(*args):
        raise AssertionError("idempotent intake must preserve the accepted plan")

    with WorkflowLedger(path) as store:
        repeated = store.accept(signed, model, "coordinator", forbidden, now=NOW)
        assert repeated["unchanged"] and len(repeated["events"]) == 1
        assert repeated["plan"]["processor"]["actor"] == "coordinator"
    before = path.read_bytes()
    with WorkflowLedger(path, readonly=True) as store:
        assert (
            store.status("example-org", "request-001", model, "remote-lead")["state"]
            == "validated"
        )
        with pytest.raises(WorkflowError):
            store.status("example-org", "request-001", model, "unmapped")
    assert path.read_bytes() == before


def test_same_id_different_intent_is_atomic_conflict(
    tmp_path, model, installation, job, signed_system
):
    secret, signed = signed_system
    path = tmp_path / "jobs.sqlite"
    with WorkflowLedger(path) as store:
        store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=NOW,
        )
        changed = deepcopy(job)
        changed["artifacts"][0]["ref"] = "gdrive://new-content"
        changed_request = sign_request(
            changed, model, secret, utc_text(NOW + timedelta(hours=1)), NOW
        )
        with pytest.raises(WorkflowError, match="different intent"):
            store.accept(
                changed_request,
                model,
                "coordinator",
                lambda j: make_plan(j, installation, model),
                now=NOW,
            )
        assert (
            len(
                store.status("example-org", "request-001", model, "coordinator")[
                    "events"
                ]
            )
            == 1
        )


def test_invalid_plan_rolls_back_without_an_accepted_record(
    tmp_path, model, installation, signed_system
):
    _, signed = signed_system

    def invalid(j):
        planned = make_plan(j, installation, model)
        planned["processor"]["actor"] = "forged"
        return planned

    with WorkflowLedger(tmp_path / "jobs.sqlite") as store:
        with pytest.raises(WorkflowError, match="plan hash"):
            store.accept(signed, model, "coordinator", invalid, now=NOW)
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert (
            store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        )


def test_concurrent_duplicate_intake_commits_one_job_and_event(
    tmp_path, model, installation, signed_system
):
    _, signed = signed_system
    path = tmp_path / "jobs.sqlite"

    def accept(_):
        with WorkflowLedger(path) as store:
            return store.accept(
                signed,
                model,
                "coordinator",
                lambda j: make_plan(j, installation, model),
                now=NOW,
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(accept, range(4)))
    assert sum(not r["unchanged"] for r in results) == 1
    with WorkflowLedger(path, readonly=True) as store:
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert (
            store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        )


def test_explicit_replan_unblocks_without_changing_classification(
    tmp_path, model, installation, signed_system
):
    _, signed = signed_system
    tools = deepcopy(installation["tools"])
    installation["tools"] = {}
    with WorkflowLedger(tmp_path / "jobs.sqlite") as store:
        blocked = store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=NOW,
        )
        assert blocked["state"] == "blocked"
        installation["tools"] = tools
        assert (
            store.accept(
                signed,
                model,
                "coordinator",
                lambda j: make_plan(j, installation, model),
                now=NOW,
            )["state"]
            == "blocked"
        )
        refreshed = store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            replan=True,
            now=NOW,
        )
        assert refreshed["state"] == "validated" and len(refreshed["events"]) == 2
        assert not refreshed["execution_ready"]
        model["roles"].append({"id": "finance-lead", "access_level": "internal"})
        model["documents"]["naming"]["domains"][0]["owners"].append("finance-lead")
        with pytest.raises(WorkflowError, match="replanning cannot"):
            store.accept(
                signed,
                model,
                "coordinator",
                lambda j: make_plan(j, installation, model),
                replan=True,
                now=NOW,
            )


def test_fresh_signature_resumes_an_existing_job_after_envelope_expiry(
    tmp_path, model, installation, signed_system
):
    secret, signed = signed_system
    with WorkflowLedger(tmp_path / "jobs.sqlite") as store:
        store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=NOW,
        )
        later = NOW + timedelta(hours=2)
        renewed = sign_request(
            signed["job"], model, secret, utc_text(later + timedelta(hours=1)), later
        )
        result = store.accept(
            renewed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=later,
        )
        assert result["unchanged"] and len(result["events"]) == 1


def test_default_processor_changes_require_explicit_replan(
    tmp_path, model, installation, signed_system
):
    _, signed = signed_system
    with WorkflowLedger(tmp_path / "jobs.sqlite") as store:
        store.accept(
            signed,
            model,
            "coordinator",
            lambda j: make_plan(j, installation, model),
            now=NOW,
        )
        model["actors"][1]["tools"] = ["documents"]

        def changed_default(j):
            return make_plan(
                j, installation, model, processor=("remote-lead", "phantombot-state")
            )

        duplicate = store.accept(signed, model, "coordinator", changed_default, now=NOW)
        assert duplicate["plan"]["processor"]["actor"] == "coordinator"
        reassigned = store.accept(
            signed, model, "coordinator", changed_default, replan=True, now=NOW
        )
        assert reassigned["plan"]["processor"]["actor"] == "remote-lead"
        assert reassigned["events"][-1]["event"] == "replanned"


def test_unrelated_database_is_not_taken_over(tmp_path):
    path = tmp_path / "other.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    os.chmod(path, 0o600)
    with pytest.raises(WorkflowError, match="unrelated"):
        WorkflowLedger(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("unrelated",)]


def test_cli_sign_accept_and_status_without_media_execution(
    tmp_path, model, installation, job, signed_system, monkeypatch
):
    secret, _ = signed_system
    job_path, org_path, key, config, settings, request_path = [
        tmp_path / f
        for f in [
            "job.json",
            "org.yaml",
            "key",
            "config.toml",
            "installation.json",
            "request.json",
        ]
    ]
    job_path.write_text(json.dumps(job), encoding="utf-8")
    org_path.write_text(yaml.safe_dump(model), encoding="utf-8")
    key.write_text(secret, encoding="utf-8")
    os.chmod(key, 0o600)
    config.write_text('default_persona="coordinator"\n', encoding="utf-8")
    settings.write_text(json.dumps(installation), encoding="utf-8")
    runner = CliRunner()
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "remote-lead")
    signed = runner.invoke(
        main,
        [
            "workflow",
            "sign-request",
            "--job",
            str(job_path),
            "--org-yaml",
            str(org_path),
            "--nsec-file",
            str(key),
            "--expires-at",
            utc_text(utc_now() + timedelta(hours=1)),
        ],
    )
    assert signed.exit_code == 0, signed.output
    assert secret not in signed.output
    request_path.write_text(signed.output, encoding="utf-8")
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "coordinator")

    def forbidden(*args, **kwargs):
        raise AssertionError("intake must not run tools or read media")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("phantomdocs.cli.read_reference", forbidden)
    ledger = tmp_path / "ledger.sqlite"
    args = [
        "workflow",
        "accept",
        "--request",
        str(request_path),
        "--ledger",
        str(ledger),
        "--installation",
        str(settings),
        "--org-yaml",
        str(org_path),
        "--phantombot-config",
        str(config),
    ]
    accepted = runner.invoke(main, args)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.output)["state"] == "validated"
    assert json.loads(runner.invoke(main, args).output)["unchanged"]
    status = runner.invoke(
        main,
        [
            "workflow",
            "status",
            "--request-id",
            job["request_id"],
            "--ledger",
            str(ledger),
            "--org-yaml",
            str(org_path),
        ],
    )
    assert status.exit_code == 0 and not json.loads(status.output)["execution_ready"]
    altered = json.loads(signed.output)
    altered["signature"] = "0" * 128
    request_path.write_text(json.dumps(altered), encoding="utf-8")
    other = tmp_path / "invalid.sqlite"
    args[args.index(str(ledger))] = str(other)
    assert runner.invoke(main, args).exit_code == 1
    assert not other.exists()
