"""Workflow planning, intake, explicit processing and archive handover."""

from __future__ import annotations

import json
import sqlite3

import click

from .access import load_org
from .documents import DocumentError, _signing_key
from .workflow_ledger import WorkflowLedger
from .workflow_requests import sign_request, verify_request
from .workflows import (
    WorkflowError,
    dependency_graph,
    digest,
    load_registry,
    observed_dependency_graph,
    plan,
    read_json,
    resolve_processor,
    validate_contract,
    validate_job,
)


@click.group()
def workflow():
    """Plan, accept, prepare and index explicitly configured workflows."""


@workflow.command("validate")
@click.argument(
    "kind",
    type=click.Choice(
        [
            "job",
            "registry",
            "installation",
            "results-manifest",
            "archive-receipt",
            "processing",
        ]
    ),
)
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def validate(kind, path):
    """Validate a local contract; does not authenticate its sender."""
    try:
        if kind == "registry":
            load_registry(path)
        elif kind == "job":
            validate_job(read_json(path))
        else:
            validate_contract(read_json(path), kind)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException("workflow contract validation failed") from exc
    click.echo(f"valid {kind} contract")


@workflow.command("plan")
@click.option("--job", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--installation", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--registry", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--actor", default=None, help="Local planner actor; not remote authentication."
)
@click.option(
    "--phantombot-config", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--phantombot-state", default=None, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--graph",
    is_flag=True,
    help="Export declared dependencies for an observatory adapter.",
)
def make_plan(
    job,
    installation,
    registry,
    org_yaml,
    actor,
    phantombot_config,
    phantombot_state,
    graph,
):
    """Emit a pinned read-only plan. Missing tools return exit 2."""
    from .cli import _require_acl

    try:
        caller, _ = _require_acl(org_yaml, actor)
        processor = resolve_processor(phantombot_config, phantombot_state)
        result = plan(
            read_json(job),
            read_json(installation),
            load_org(org_yaml),
            load_registry(registry),
            processor,
            caller,
        )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException("cannot load workflow inputs") from exc
    click.echo(
        json.dumps(
            dependency_graph(result) if graph else result, indent=2, ensure_ascii=False
        )
    )
    if result["status"] == "blocked":
        raise click.exceptions.Exit(2)


@workflow.command("sign-request")
@click.option("--job", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
@click.option(
    "--nsec-file", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--expires-at", required=True, help="Explicit UTC expiry, YYYY-MM-DDTHH:MM:SSZ."
)
def sign_job(job, org_yaml, actor, nsec_file, expires_at):
    """Sign a job using the caller's existing declared private identity key."""
    from .cli import _require_acl

    try:
        caller, org = _require_acl(org_yaml, actor)
        job_data = read_json(job)
        validate_job(job_data)
        if job_data.get("requester") != caller:
            raise WorkflowError("caller cannot sign as a different requester")
        secret = _signing_key(nsec_file)
        if not secret:
            raise WorkflowError("a private signing key is required")
        result = sign_request(job_data, org, secret, expires_at)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, DocumentError) as exc:
        raise click.ClickException("cannot sign workflow request") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@workflow.command("accept")
@click.option("--request", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--ledger", required=True, type=click.Path(dir_okay=False))
@click.option("--installation", required=True, type=click.Path(dir_okay=False))
@click.option("--registry", default=None, type=click.Path(dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
@click.option("--phantombot-config", required=True, type=click.Path(dir_okay=False))
@click.option("--phantombot-state", default=None, type=click.Path(dir_okay=False))
@click.option(
    "--replan",
    is_flag=True,
    help="Explicitly refresh this accepted plan; preserve classification and owners.",
)
def accept_job(
    request,
    ledger,
    installation,
    registry,
    org_yaml,
    actor,
    phantombot_config,
    phantombot_state,
    replan,
):
    """Verify a signed request and atomically retain its plan in a private ledger."""
    from .cli import _require_acl

    try:
        caller, org = _require_acl(org_yaml, actor)
        signed = read_json(request)
        verify_request(signed, org)  # Reject bad signatures before creating a database.

        def build(job):
            return plan(
                job,
                read_json(installation),
                org,
                load_registry(registry),
                resolve_processor(phantombot_config, phantombot_state),
                caller,
            )

        with WorkflowLedger(ledger) as store:
            result = store.accept(signed, org, caller, build, replan=replan)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException("cannot accept workflow request") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result["state"] == "blocked":
        raise click.exceptions.Exit(2)


@workflow.command("status")
@click.option("--request-id", required=True)
@click.option("--ledger", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
@click.option(
    "--graph",
    is_flag=True,
    help="Export declared dependencies with ledger observations.",
)
def job_status(request_id, ledger, org_yaml, actor, graph):
    """Read an authorized ledger record without updating its state or plans."""
    from .cli import _require_acl

    try:
        caller, org = _require_acl(org_yaml, actor)
        with WorkflowLedger(ledger, readonly=True) as store:
            result = store.status(org["organization"]["id"], request_id, org, caller)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException("cannot read workflow record") from exc
    click.echo(
        json.dumps(
            observed_dependency_graph(result) if graph else result,
            indent=2,
            ensure_ascii=False,
        )
    )


@workflow.command("results")
@click.option("--request-id", required=True)
@click.option("--ledger", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
def results_manifest(request_id, ledger, org_yaml, actor):
    """Export completed processing results for an independent archiver."""
    from .cli import _require_acl
    from .workflow_handover import export_results

    try:
        caller, org = _require_acl(org_yaml, actor)
        with WorkflowLedger(ledger, readonly=True) as store:
            result = export_results(
                store, org["organization"]["id"], request_id, org, caller
            )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException("cannot export processing results") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@workflow.command("index-archive")
@click.option("--receipt", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--ledger", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
@click.option("--root", required=True, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--drive-token-file", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--max-reference-bytes", required=True, type=click.IntRange(min=1))
@click.option("--nsec-file", default=None, type=click.Path(exists=True, dir_okay=False))
def index_archived(
    receipt,
    ledger,
    org_yaml,
    actor,
    root,
    drive_token_file,
    max_reference_bytes,
    nsec_file,
):
    """Verify an archiver receipt and index its existing Drive objects by reference."""
    from .cli import _require_acl
    from .workflow_drive_reader import DriveArchiveReader
    from .workflow_handover import index_archive

    try:
        caller, org = _require_acl(org_yaml, actor)
        reader = DriveArchiveReader(drive_token_file, max_bytes=max_reference_bytes)
        with WorkflowLedger(ledger) as store:
            result = index_archive(
                store,
                read_json(receipt),
                org,
                caller,
                root=root,
                org_yaml=org_yaml,
                drive_reader=reader,
                nsec_file=nsec_file,
            )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        DocumentError,
    ) as exc:
        raise click.ClickException("cannot index archive receipt") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@workflow.command("archive-receipt")
@click.option("--request-id", required=True)
@click.option("--mapping", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--ledger", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
def archive_receipt(request_id, mapping, ledger, org_yaml, actor):
    """Bind archiver-supplied Drive IDs to completed results; no upload or rename."""
    from .cli import _require_acl
    from .workflow_handover import build_archive_receipt

    try:
        caller, org = _require_acl(org_yaml, actor)
        with WorkflowLedger(ledger, readonly=True) as store:
            result = build_archive_receipt(
                store,
                org["organization"]["id"],
                request_id,
                org,
                caller,
                read_json(mapping),
            )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException("cannot build archive receipt") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@workflow.command("prepare")
@click.option("--request-id", required=True)
@click.option("--ledger", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--installation", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--registry", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--org-yaml", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--actor", default=None)
@click.option(
    "--phantombot-config", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--phantombot-state", default=None, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--drive-token-file", required=True, type=click.Path(exists=True, dir_okay=False)
)
def prepare_job(
    request_id,
    ledger,
    installation,
    registry,
    org_yaml,
    actor,
    phantombot_config,
    phantombot_state,
    drive_token_file,
):
    """Run explicitly pinned processing; leave publication/indexing to the archiver."""
    from .cli import _require_acl
    from .workflow_drive_reader import DriveArchiveReader
    from .workflow_executor import WorkflowExecutor
    from .workflow_processing import ProcessingAdapter

    try:
        caller, org = _require_acl(org_yaml, actor)
        data = read_json(installation)
        validate_contract(data, "installation")

        def build(job):
            return plan(
                job,
                data,
                org,
                load_registry(registry),
                resolve_processor(phantombot_config, phantombot_state),
                caller,
            )

        with WorkflowLedger(ledger) as store:
            planned = store.status(org["organization"]["id"], request_id, org, caller)[
                "plan"
            ]
            store._authorize(planned, org, caller, True)
            if caller != planned["processor"]["actor"]:
                raise WorkflowError("only the pinned processor can prepare results")
            if (
                digest(data) != planned["pins"]["installation"]
                or "processing" not in data
            ):
                raise WorkflowError(
                    "preparation requires the pinned processing installation"
                )
            reader = DriveArchiveReader(
                drive_token_file, max_bytes=data["processing"]["max_input_bytes"]
            )
            with ProcessingAdapter(data, reader) as adapter:
                result = WorkflowExecutor(store, store.path, adapter).prepare(
                    org["organization"]["id"], request_id, org, caller, build
                )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException("cannot prepare workflow results") from exc
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
