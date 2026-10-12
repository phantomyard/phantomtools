"""Additive read-only workflow CLI. No file/Drive/store mutations."""

from __future__ import annotations

import json

import click

from .access import load_org
from .workflows import (
    WorkflowError,
    dependency_graph,
    load_registry,
    plan,
    read_json,
    resolve_processor,
    validate_contract,
    validate_job,
)


@click.group()
def workflow():
    """Validate and plan versioned document processing; never execute tools."""


@workflow.command("validate")
@click.argument("kind", type=click.Choice(["job", "registry", "installation"]))
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
