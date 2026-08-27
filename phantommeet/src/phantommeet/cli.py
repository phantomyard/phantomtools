"""PhantomMeet command line interface."""

from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

import click
import yaml

from . import __version__
from .apply import apply_manifest, unapply_manifest
from .derive import derive_manifest as _derive_manifest
from .infra import run_checks
from .manifest import ManifestError, load_manifest


@click.group()
@click.version_option(__version__, prog_name="phantommeet")
def cli() -> None:
    """PhantomMeet — agnostic meeting capabilities for PhantomOrg personas."""


@cli.command()
@click.option(
    "--manifest",
    "-m",
    "manifest_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the PhantomMeet YAML manifest.",
)
@click.option(
    "--target",
    "-t",
    required=True,
    type=click.Path(file_okay=False),
    help="Root of the persona installation (directory containing persona subdirs).",
)
@click.option(
    "--dry-run", is_flag=True, help="Report changes without writing anything."
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Also list unchanged (skipped) entries."
)
@click.option(
    "--ask-roles",
    is_flag=True,
    help="Interactively ask the operator who may schedule meetings (persisted as invite.roles).",
)
@click.option(
    "--invite-roles",
    help="Comma-separated persona ids allowed to schedule meetings (skips the interactive prompt).",
)
@click.option(
    "--ask-card",
    is_flag=True,
    help="Interactively ask the operator for the announcement card format (persisted as invite.card).",
)
@click.option(
    "--card-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read the announcement card from a file (one-shot; not persisted).",
)
def apply(
    manifest_path: str,
    target: str,
    dry_run: bool,
    verbose: bool,
    ask_roles: bool,
    invite_roles: str | None,
    ask_card: bool,
    card_file: str | None,
) -> None:
    """Apply the meeting capability update to personas."""
    invite_roles_list: list[str] | None = None
    if invite_roles:
        invite_roles_list = [r.strip() for r in invite_roles.split(",") if r.strip()]
    try:
        result = apply_manifest(
            manifest_path,
            target,
            dry_run=dry_run,
            verbose=verbose,
            invite_roles=invite_roles_list,
            ask_roles=ask_roles,
            ask_card=ask_card,
            card_file=card_file,
        )
    except ManifestError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    for change in result.changes:
        if change.action == "skip" and not verbose:
            continue
        click.echo(str(change))

    if result.errors:
        for err in result.errors:
            click.echo(f"error: {err}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(
            f"\nDry run: {len(result.pending)} change(s) would be applied, "
            f"{len(result.skipped)} up to date."
        )
    else:
        click.echo(
            f"\nApplied {len(result.pending)} change(s); "
            f"{len(result.skipped)} already up to date."
        )


@cli.command()
@click.option(
    "--manifest",
    "-m",
    "manifest_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the PhantomMeet YAML manifest.",
)
@click.option(
    "--target",
    "-t",
    required=True,
    type=click.Path(file_okay=False),
    help="Root of the persona installation (directory containing persona subdirs).",
)
def unapply(manifest_path: str, target: str) -> None:
    """Reverse PhantomMeet's owned changes (phantomchat relay, Meetings.md
    block, MEMORY.md section) without touching unrelated configuration."""
    try:
        result = unapply_manifest(manifest_path, target)
    except ManifestError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    for change in result.changes:
        click.echo(str(change))

    if result.errors:
        for err in result.errors:
            click.echo(f"error: {err}", err=True)
        sys.exit(1)

    click.echo(
        f"\nReversed {len(result.pending)} change(s); "
        f"{len(result.skipped)} already reversed."
    )


@cli.command()
@click.option(
    "--target",
    "-t",
    required=True,
    type=click.Path(file_okay=False),
    help="Root of the persona installation (directory containing persona subdirs).",
)
@click.option(
    "--org",
    "org_path",
    type=click.Path(dir_okay=False),
    multiple=True,
    help="Path(s) to a PhantomOrg org model (org.yaml); auto-detected when omitted.",
)
def discover(target: str, org_path: tuple[str, ...]) -> None:
    """Discover installed personas and the PhantomOrg org model (read-only).

    Scans ``--target`` for persona directories (identity.json / SOUL.md /
    phantomchat.json) and cross-references them with a PhantomOrg org
    model when one is found. The output is what the installer uses to ask
    the operator who may schedule meetings.
    """
    from .discovery import discover as run_discovery

    d = run_discovery(target, list(org_path) if org_path else None)
    click.echo(d.render())


@cli.command()
@click.option(
    "--org",
    "org_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a PhantomOrg org model (org.yaml).",
)
@click.option(
    "--base",
    "base_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="PhantomMeet base manifest (bridge/rooms/storage/derive rules).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    help="Write the derived manifest to this file (default: print to stdout).",
)
def derive_manifest(org_path: str, base_path: str, out_path: str | None) -> None:
    """Derive a PhantomMeet manifest from a PhantomOrg org model."""
    try:
        manifest, warnings = _derive_manifest(org_path, base_path)
    except ManifestError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    for w in warnings:
        click.echo(f"warning: {w}", err=True)

    text = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        click.echo(f"derived manifest written to {out_path}")
        try:
            load_manifest(out_path)
            click.echo("validation: OK")
        except ManifestError as exc:
            click.echo(f"validation FAILED: {exc}", err=True)
            sys.exit(1)
    else:
        click.echo(text)


@cli.command()
@click.option(
    "--manifest",
    "-m",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the PhantomMeet YAML manifest.",
)
@click.option(
    "--target",
    "-t",
    type=click.Path(file_okay=False),
    help="Root of the persona installation; enables per-persona applied-state checks.",
)
@click.option(
    "--host",
    default="any",
    type=click.Choice(["any", "local", "vps", "macbook", "server"]),
    help="Current machine identity; checks declaring this host run, others are SKIPped (default: any).",
)
@click.option(
    "--log",
    "log_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the report to this file (default: ~/.local/state/phantommeet/check-infra.log).",
)
@click.option(
    "--no-log",
    is_flag=True,
    default=False,
    help="Do not write a log file (overrides the default log path).",
)
def check_infra(
    manifest: str, target: str | None, host: str, log_file: str | None, no_log: bool
) -> None:
    """Check required infrastructure is reachable and personas are up to date.

    Read-only: runs the probes declared in the manifest's ``infra`` section
    (http/ws/command/file/env) and, when ``--target`` is given, verifies each
    persona is fully applied (Meetings.md, MEMORY markers, phantomchat patch).

    The report is printed to the screen AND appended to a log file
    (``--log FILE``, or ``~/.local/state/phantommeet/check-infra.log`` by
    default; disable with ``--no-log``). This is the **deployment
    prerequisites check**: run it on each host before deploying to verify the
    third-party software PhantomMeet needs is available, and again after any
    infrastructure change.
    """
    try:
        m = load_manifest(manifest)
    except ManifestError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    results = run_checks(m, Path(target) if target else None, host=host)
    lines = [r.render() for r in results]
    for line in lines:
        click.echo(line)

    ok_count = sum(1 for r in results if r.state == "ok")
    skip_count = sum(1 for r in results if r.state == "skip")
    summary = (
        f"\n{ok_count}/{len(results)} checks passed"
        + (f" ({skip_count} skipped for host {host!r})" if skip_count else "")
        + "."
    )
    click.echo(summary)

    # Persistent log: default XDG state path, overridable with --log, off with --no-log.
    if not no_log:
        if log_file:
            log_path = Path(log_file).expanduser()
        else:
            state_home = os.environ.get("XDG_STATE_HOME") or str(
                Path.home() / ".local/state"
            )
            log_path = Path(state_home) / "phantommeet" / "check-infra.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n--- check-infra {stamp} (manifest {manifest}, host {host!r}) ---\n"
                )
                fh.write("\n".join(lines))
                fh.write(summary + "\n")
            click.echo(f"log: {log_path}")
        except OSError as exc:
            click.echo(f"warning: could not write log {log_path}: {exc}", err=True)

    if any(r.state == "fail" for r in results):
        sys.exit(1)


@cli.command()
@click.option(
    "--manifest", "-m", required=True, type=click.Path(exists=True, dir_okay=False)
)
def validate(manifest: str) -> None:
    """Validate a manifest and print a summary."""
    try:
        m = load_manifest(manifest)
    except ManifestError as exc:
        click.echo(f"invalid: {exc}", err=True)
        sys.exit(1)

    click.echo(f"org:        {m['org']}")
    click.echo(f"language:   {m['language']}")
    click.echo(f"version:    {m.get('version', '?')}")
    click.echo(f"roles:      {', '.join(f'{p}={r}' for p, r in m['roles'].items())}")
    click.echo(f"full:       {', '.join(m['permissions'].get('full', []))}")
    click.echo("OK")


def main() -> None:
    cli(prog_name="phantommeet")


if __name__ == "__main__":
    main()
