"""
Entry point of the `phantomorg` / `pf` CLI (see section 7 of the spec:
exact command contract).

Each command supports flag mode (for scripting/CI) and, if the main flags
are omitted, falls back to the interactive wizard (click.prompt).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from .compiler import CompileError
from .compiler import build as compiler_build
from .compiler.phantomchat import verify_phantomchat
from .compiler.telegram import TelegramError, verify_telegram
from .deploy.session import (
    ManifestError,
    RollbackError,
    _transaction_lock,
    begin_session,
    commit_session,
    execute_rollback,
    plan_rollback,
    remove_abandoned_archive_root,
    sessions_for_target,
)
from .deploy.target import (
    DeployCollisionError,
    DeployError,
    archives_dir,
    default_personas_dir,
)
from .deploy.target import deploy as deploy_target
from .importer import audit_persona_dir, render_org_yaml_fragment, resolve_against_org
from .spec.loader import OrgSpecError
from .updater import run_update as run_updater
from .validator import validate_compiled_output, validate_org
from .wizard.mutations import DuplicateIdError, RemovalBlockedError
from .wizard.mutations import add_actor as add_actor_fn
from .wizard.mutations import add_department as add_department_fn
from .wizard.mutations import add_role as add_role_fn
from .wizard.mutations import add_role_and_actor as add_role_and_actor_fn
from .wizard.mutations import remove_actor as remove_actor_fn
from .wizard.mutations import remove_department as remove_department_fn
from .wizard.mutations import remove_role as remove_role_fn
from .wizard.mutations import rename_actor as rename_actor_fn
from .wizard.mutations import rename_department as rename_department_fn
from .wizard.mutations import rename_role as rename_role_fn
from .wizard.new_org import new_org as new_org_fn


class _ExpandUserPath(click.Path):
    """``click.Path`` that expands a leading ``~`` before validating.

    click (as of 8.3) does not expand ``~`` in path options, so
    ``--target ~/personas`` silently creates a literal ``~/personas``
    directory under the CWD. ``resolve_path`` would fix it but also
    resolves symlinks, which the deploy layer specifically treats as
    attack surface — expanding only the user prefix keeps every other
    click.Path guarantee (exists / file_okay / dir_okay) intact.
    """

    def __init__(
        self,
        *args: Any,
        path_type: type | None = None,
        **kwargs: Any,
    ) -> None:
        # types-click's stub constrains path_type to Type[str] | Type[bytes],
        # but click's runtime accepts any callable (we pass pathlib.Path).
        super().__init__(*args, path_type=path_type, **kwargs)  # type: ignore[type-var,misc]

    def convert(self, value, param, ctx):
        if isinstance(value, str):
            value = os.path.expanduser(value)
        return super().convert(value, param, ctx)


def _org_yaml_hashes(org_yaml_paths):
    """Compute (path, sha256) for each org.yaml, for spec-drift warnings."""
    import hashlib

    out = []
    for p in org_yaml_paths:
        try:
            digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        except OSError:
            continue
        out.append((Path(p), digest))
    return out


def _meta_org_id(meta_path: Path) -> str | None:
    """Best-effort read of the organization_id from a .phantomorg.yaml.
    Returns None when the file is unreadable or has no such key."""
    import yaml as _yaml

    try:
        data = _yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — metadata is best-effort
        return None
    if not isinstance(data, dict):
        return None
    org = data.get("organization_id")
    return str(org) if org else None


def _compiled_org_ids(compiled_dir: Path) -> list[str]:
    """Unique organization ids present in a compiled build's metadata."""
    orgs: set[str] = set()
    if not compiled_dir.is_dir():
        return []
    for d in sorted(compiled_dir.iterdir()):
        meta = d / ".phantomorg.yaml"
        if not meta.is_file():
            continue
        org = _meta_org_id(meta)
        if org:
            orgs.add(org)
    return sorted(orgs)


def _first_org_id(compiled_dirs: list[Path]) -> str | None:
    """Organization id of the first compiled actor that has metadata."""
    for d in compiled_dirs:
        meta = d / ".phantomorg.yaml"
        if not meta.is_file():
            continue
        org = _meta_org_id(meta)
        if org:
            return org
    return None


def _reject_unresolved_in_progress(target: Path) -> None:
    """Refuse to start a new deploy while an interrupted deploy session
    is still recorded: its target/archive state is unknown, so deploying
    on top of it would bury the unresolved attempt. The user must resolve
    it with `po rollback` first."""
    archive_root = archives_dir(target)
    try:
        pending = [
            s
            for s in sessions_for_target(archive_root, target)
            if s.get("state") == "in_progress"
        ]
    except ManifestError as e:
        from .deploy.session import _quarantine_corrupt_manifest

        try:
            preserved = _quarantine_corrupt_manifest(archive_root)
            hint = f"Preserved at {preserved}."
        except Exception:  # noqa: BLE001 — best-effort quarantine
            hint = "The manifest was left in place."
        click.secho(
            f"Cannot deploy: the session manifest is corrupt and would be "
            f"overwritten. {hint} Resolve it (or delete it if you accept "
            f"losing the rollback history) before deploying again. ({e})",
            fg="red",
        )
        raise SystemExit(1)
    if pending:
        ids = ", ".join(str(s.get("id", "?")) for s in pending)
        click.secho(
            f"There is an interrupted deploy session recorded ({ids}). "
            "Its target/archive state is unknown — run `po rollback` to "
            "revert it before deploying again.",
            fg="red",
        )
        raise SystemExit(1)


def _version():
    """Return the project version, preferring the repo's pyproject.toml
    (single source of truth) and falling back to the installed metadata."""
    repo_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if repo_pyproject.is_file():
        try:
            with repo_pyproject.open("rb") as fh:
                return tomllib.load(fh).get("project", {}).get("version", "unknown")
        except (tomllib.TOMLDecodeError, OSError):
            pass
    try:
        from importlib.metadata import version

        return version("phantomorg")
    except Exception:  # noqa: BLE001 — defensive fallback: any metadata lookup failure yields "unknown"
        return "unknown"


def _run_wizard(fn, *args, **kwargs):
    """Run an interactive wizard; on Ctrl+C / abort, or on predictable
    user-facing errors (duplicate ids, existing org file), exit cleanly
    with the message instead of dumping a raw traceback (L3)."""
    try:
        return fn(*args, **kwargs)
    except click.exceptions.Abort:
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise click.exceptions.Exit(1)
    except (DuplicateIdError, FileExistsError) as e:
        click.secho(str(e), fg="red")
        raise click.exceptions.Exit(1)


@click.group()
@click.version_option(version=_version(), message="%(prog)s, version %(version)s")
def main():
    """PhantomOrg — generates AI agent organizations from a spec."""


@main.command("new-org")
@click.option("--id", "org_id", required=False, help="Organization slug")
@click.option("--name", required=False, help="Organization name")
@click.option("--sector", required=False, help="Sector (e.g. ngo, pyme, educacion)")
@click.option(
    "--lang",
    "languages",
    multiple=True,
    help="Language (repeatable: --lang es --lang en)",
)
@click.option(
    "--template",
    required=False,
    default=None,
    help="Sector department template (see `po templates`)",
)
def new_org_cmd(org_id, name, sector, languages, template):
    """Creates a minimal organizations/<id>/org.yaml."""
    if not (org_id and name):
        from .wizard.interactive import run_new_org_wizard

        path = _run_wizard(run_new_org_wizard)
    else:
        # `--sector` / `--lang` / `--template` are optional conveniences:
        # defaults are a generic sector and English.
        path = new_org_fn(
            org_id,
            name,
            sector or "general",
            list(languages) or ["en"],
            template=template,
        )
    click.secho(f"Created {path}", fg="green")


@main.command("templates")
def templates_cmd():
    """Lists the sector templates available for `new-org --template`."""
    from .wizard.templates import available_templates, departments_for

    for name in available_templates():
        depts = ", ".join(d["name"] for d in departments_for(name))
        click.echo(f"  - {name}: {depts}")


@main.command("setup")
@click.option(
    "--phantombot-dir",
    "personas_root",
    required=False,
    default=None,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Phantombot personas directory (default: ~/.local/share/phantombot/personas)",
)
@click.option(
    "--org",
    "org_path",
    required=False,
    default=None,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
    help="Existing org.yaml to reuse instead of creating a new one",
)
@click.option(
    "--base",
    "base_dir",
    required=False,
    default=Path("organizations"),
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Base directory for a newly created organization (default: organizations)",
)
def setup_cmd(personas_root, org_path, base_dir):
    """
    Guided installation over a phantombot installation: detects the
    existing personas, reassigns them to departments + roles (audit
    suggests, you confirm), lets you add new personas, and writes the
    org.yaml. One pass — afterwards use `po validate` / `po build` /
    `po deploy`.
    """
    from .wizard.interactive import run_setup_wizard

    path = _run_wizard(run_setup_wizard, personas_root, org_path, base_dir)
    click.secho(f"\nSetup complete. Organization: {path}", fg="green")


@main.command("add-department")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "dept_id", required=False)
@click.option("--name", required=False)
@click.option("--parent", required=False, default=None)
@click.option("--access-policy", required=False)
def add_department_cmd(org_path, dept_id, name, parent, access_policy):
    """Adds a department to an existing org.yaml."""
    if not (dept_id and name and access_policy):
        from .wizard.interactive import run_add_department_wizard

        _run_wizard(run_add_department_wizard, org_path)
    else:
        try:
            add_department_fn(org_path, dept_id, name, parent, access_policy)
        except DuplicateIdError as e:
            click.secho(str(e), fg="red")
            raise SystemExit(1)
    click.secho(f"Department added to {org_path}", fg="green")


@main.command("add-role")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "role_id", required=False)
@click.option("--name", required=False)
@click.option("--department", required=False)
@click.option("--reports-to", required=False, default=None)
@click.option("--reports-to-human", required=False, default=None)
@click.option("--access-level", required=False)
@click.option("--function", "functions", multiple=True)
def add_role_cmd(
    org_path,
    role_id,
    name,
    department,
    reports_to,
    reports_to_human,
    access_level,
    functions,
):
    """Adds a role to an existing org.yaml."""
    if not (role_id and name and department and access_level):
        from .wizard.interactive import run_add_role_wizard

        _run_wizard(run_add_role_wizard, org_path)
    else:
        try:
            add_role_fn(
                org_path,
                role_id,
                name,
                department,
                reports_to,
                access_level,
                functions=list(functions),
                reports_to_human=reports_to_human,
            )
        except DuplicateIdError as e:
            click.secho(str(e), fg="red")
            raise SystemExit(1)
    click.secho(f"Role added to {org_path}", fg="green")


@main.command("add-actor")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "actor_id", required=False)
@click.option("--role", required=False)
@click.option("--telegram-bot", required=False, default=None)
@click.option("--tool", "tools", multiple=True)
def add_actor_cmd(org_path, actor_id, role, telegram_bot, tools):
    """Adds an actor to an existing org.yaml."""
    if not (actor_id and role):
        from .wizard.interactive import run_add_actor_wizard

        _run_wizard(run_add_actor_wizard, org_path)
    else:
        try:
            add_actor_fn(
                org_path, actor_id, role, list(tools), telegram_bot=telegram_bot
            )
        except DuplicateIdError as e:
            click.secho(str(e), fg="red")
            raise SystemExit(1)
    click.secho(f"Actor added to {org_path}", fg="green")


@main.command("validate")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
def validate_cmd(org_path):
    """Validates shape + escalation cycles + cross-references + suggestions."""
    try:
        spec, result = validate_org(org_path)
    except Exception as e:  # noqa: BLE001 — surface any load error as a friendly CLI message
        click.secho(f"Load error: {e}", fg="red")
        raise SystemExit(1)

    if result.errors:
        click.secho(f"{len(result.errors)} error(s):", fg="red")
        for err in result.errors:
            click.echo(f"  - {err}")
    if result.warnings:
        click.secho(f"{len(result.warnings)} suggestion(s):", fg="yellow")
        for w in result.warnings:
            click.echo(f"  - {w}")

    if result.ok:
        click.secho(
            f"✓ {org_path} is valid "
            f"({len(spec.actors)} actors, {len(spec.roles)} roles, "
            f"{len(spec.departments)} departments)",
            fg="green",
        )
    else:
        raise SystemExit(1)


@main.command("phantomchat-check")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--personas-dir",
    "personas_dir",
    required=False,
    default=None,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Personas directory to verify against (default: $PHANTOMORG_TARGET_DIR or ~/.local/share/phantombot/personas)",
)
@click.option(
    "--bin",
    "phantomchat_bin",
    required=False,
    default="phantombot",
    show_default=True,
    help="phantombot binary used to read each persona's real npub",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the full verification manifest as JSON",
)
def phantomchat_check_cmd(org_path, personas_dir, phantomchat_bin, as_json):
    """Verifies each actor's declared npub against the runtime identity.

    Non-invasive: reads each persona's identity.json/phantomchat.json and
    invokes `phantombot phantomchat --persona X` (which only prints the
    npub when an identity exists). Never writes or modifies anything.
    """
    try:
        spec, result = validate_org(org_path)
    except OrgSpecError as e:
        click.secho(f"Cannot verify: {e}", fg="red")
        raise SystemExit(1)
    if not result.ok:
        click.secho("Cannot verify: the organization is not valid.", fg="red")
        click.echo("Run `po validate` for details.")
        raise SystemExit(1)

    target = Path(personas_dir) if personas_dir else default_personas_dir()

    manifest = verify_phantomchat(spec, target, phantomchat_bin=phantomchat_bin)

    if as_json:
        click.echo(manifest.to_json())
        raise SystemExit(0 if manifest.ok else 1)

    summary = manifest.summary()
    click.echo(
        f"phantomchat verification for {spec.organization.name} "
        f"({manifest.checked_at}):"
    )
    click.echo(f"  personas dir : {target}")
    click.echo(f"  bin          : {phantomchat_bin}")
    for c in manifest.checks:
        icon = {
            "ok": "✓",
            "mismatch": "✗",
            "missing-identity": "⚠",
            "missing-phantomchat": "⚠",
            "not-declared": "·",
            "error": "✗",
        }[c.status]
        click.echo(f"  {icon} {c.actor_id}: {c.status}")
        if c.declared_npub or c.real_npub:
            click.echo(f"      declared: {c.declared_npub or '(none)'}")
            click.echo(f"      real     : {c.real_npub or '(unknown)'}")
        if c.detail:
            click.echo(f"      {c.detail}")
    click.echo(
        f"  summary: {summary['ok']} ok, {summary['mismatch']} mismatch, "
        f"{summary['missing-identity']} missing identity, "
        f"{summary['missing-phantomchat']} missing phantomchat, "
        f"{summary['not-declared']} not declared, "
        f"{summary['error']} error"
    )
    if manifest.ok:
        click.secho("✓ All declared npubs match the runtime.", fg="green")
    else:
        click.secho("Some actors need attention (see above).", fg="yellow")
        raise SystemExit(1)


@main.command("build")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
)
@click.option(
    "--only", "only_actor", required=False, default=None, help="Compile only this actor"
)
@click.option(
    "--scope-rule",
    "scope_rule",
    required=False,
    default="chain",
    show_default=True,
    type=click.Choice(["chain", "department"], case_sensitive=False),
    help="Visibility rule for the derived scopes.json (chain | department)",
)
def build_cmd(org_path, out_dir, only_actor, scope_rule):
    """Compiles the organization (or a single actor) to IDENTITY/SOUL/tools/MEMORY."""
    try:
        spec, result = validate_org(org_path)
    except OrgSpecError as e:
        click.secho(f"Cannot compile: {e}", fg="red")
        raise SystemExit(1)
    if not result.ok:
        click.secho("Cannot compile: the organization is not valid.", fg="red")
        click.echo("Run `po validate` for details.")
        raise SystemExit(1)

    if only_actor is not None and not any(a.id == only_actor for a in spec.actors):
        click.secho(
            f"Cannot compile: no actor with id {only_actor!r} in this org.", fg="red"
        )
        click.echo(f"Actors: {', '.join(a.id for a in spec.actors)}")
        raise SystemExit(1)

    try:
        written = compiler_build(
            spec, Path(out_dir), only=only_actor, scope_rule=scope_rule
        )
    except CompileError as e:
        click.secho(f"Cannot compile: {e}", fg="red")
        raise SystemExit(1)
    except OSError as e:
        click.secho(f"Cannot compile: filesystem error: {e}", fg="red")
        raise SystemExit(1)

    click.echo(f"Build of {spec.organization.name}:")
    for actor_id, files in written.items():
        if actor_id in ("__scopes__", "__warnings__"):
            continue
        estado = f"{len(files)} file(s) written" if files else "no changes"
        click.echo(f"  - {actor_id}: {estado}")

    for w in written.get("__warnings__", []):
        click.secho(f"⚠ {w['message']}", fg="yellow")

    budget_result = validate_compiled_output(spec, Path(out_dir))
    for w in budget_result.warnings:
        click.secho(f"⚠ {w}", fg="yellow")


@main.command("telegram-check")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--config",
    "config_path",
    required=False,
    default=None,
    type=_ExpandUserPath(dir_okay=False, path_type=Path),
    help="phantombot config.toml to read tokens from "
    "(default: ~/.config/phantombot/config.toml)",
)
@click.option(
    "--state",
    "state_path",
    required=False,
    default=None,
    type=_ExpandUserPath(dir_okay=False, path_type=Path),
    help="phantombot state.json whose default_persona overrides config.toml "
    "(default: ~/.local/share/phantombot/state.json)",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the full verification manifest as JSON",
)
def telegram_check_cmd(org_path, config_path, state_path, as_json):
    """Verifies each actor's declared telegram_bot against the live bot.

    Non-invasive: reads config.toml (and optionally state.json) and calls
    Telegram's public getMe endpoint. Never writes or modifies anything.
    """
    try:
        spec, result = validate_org(org_path)
    except OrgSpecError as e:
        click.secho(f"Cannot verify: {e}", fg="red")
        raise SystemExit(1)
    if not result.ok:
        click.secho("Cannot verify: the organization is not valid.", fg="red")
        click.echo("Run `po validate` for details.")
        raise SystemExit(1)

    config = (
        Path(config_path)
        if config_path
        else Path.home() / ".config" / "phantombot" / "config.toml"
    )
    state = (
        Path(state_path)
        if state_path
        else Path.home() / ".local" / "share" / "phantombot" / "state.json"
    )

    try:
        manifest = verify_telegram(
            spec, config, state_path=state if state.exists() else None
        )
    except TelegramError as e:
        click.secho(f"Cannot verify: {e}", fg="red")
        raise SystemExit(1)

    if as_json:
        click.echo(manifest.to_json())
        raise SystemExit(0 if manifest.ok else 1)

    summary = manifest.summary()
    click.echo(
        f"telegram verification for {spec.organization.name} ({manifest.checked_at}):"
    )
    click.echo(f"  config path: {config}")
    if state.exists():
        click.echo(f"  state      : {state}")
    for c in manifest.checks:
        icon = {
            "ok": "✓",
            "mismatch": "✗",
            "no-token": "⚠",
            "not-declared": "·",
            "error": "✗",
        }[c.status]
        click.echo(f"  {icon} {c.actor_id}: {c.status}")
        if c.declared_bot or c.real_bot:
            click.echo(f"      declared: {c.declared_bot or '(none)'}")
            click.echo(f"      real     : {c.real_bot or '(unknown)'}")
        if c.token_source:
            click.echo(f"      token    : {c.token_source}")
        if c.detail:
            click.echo(f"      {c.detail}")
    click.echo(
        f"  summary: {summary['ok']} ok, {summary['mismatch']} mismatch, "
        f"{summary['no-token']} no token, "
        f"{summary['not-declared']} not declared, "
        f"{summary['error']} error"
    )
    if manifest.ok:
        click.secho(
            "✓ All declared telegram_bot handles match the live bots.", fg="green"
        )
    else:
        click.secho("Some actors need attention (see above).", fg="yellow")
        raise SystemExit(1)


@main.command("deploy")
@click.option(
    "--from",
    "compiled_dir",
    required=True,
    type=_ExpandUserPath(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--target",
    required=False,
    default=None,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Target personas directory (default: $PHANTOMORG_TARGET_DIR or ~/.local/share/phantombot/personas)",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite even if the actor belongs to another organization",
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help="Removes from the target actors of this same organization that are no longer in the current build",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the final confirmation (for scripting/CI)",
)
def deploy_cmd(compiled_dir, target, force, prune, assume_yes):
    """Deploys the compiled output into the runtime's personas directory.

    Deploy is strictly ADDITIVE: it writes only the files PhantomOrg owns,
    in place, preserving identity.json, the vault, accumulated memory and
    the KB. Files being overwritten are backed up to personas-archive/
    (per-file) so `po rollback` can restore them. A fresh persona is a
    runtime-owned lifecycle operation, never a compiler deploy.
    """
    target_path = Path(target) if target else None
    effective_target = target_path or default_personas_dir()

    # Pre-flight summary + confirmation: nothing is written until the
    # user explicitly approves (or passes --yes for scripting).
    compiled_dir_path = Path(compiled_dir)
    if not compiled_dir_path.is_dir():
        click.secho(
            f"Cannot deploy: compiled output not found at "
            f"{compiled_dir_path}. Run `po build` (or `po build-all`) first.",
            fg="red",
        )
        raise SystemExit(1)
    actor_ids = sorted(d.name for d in compiled_dir_path.iterdir() if d.is_dir())
    click.echo("Deploy plan:")
    click.echo(f"  target     : {effective_target}")
    click.echo(f"  actors     : {', '.join(actor_ids) if actor_ids else '(none)'}")
    if prune:
        click.echo(
            "  --prune    : actors of this org no longer in the build will be archived"
        )
    click.echo(
        "  overwrites : files this tool owns are backed up to personas-archive/ "
        "first (per-file)"
    )
    if not assume_yes and not click.confirm("Apply this deployment?", default=False):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)

    with _transaction_lock(effective_target):
        _reject_unresolved_in_progress(effective_target)
        # Compute the journal plan UNDER the transaction lock: the
        # planned state must reflect the target exactly as this deploy
        # is about to mutate it. A pre-lock snapshot can go stale (a
        # concurrent deploy finishing between the snapshot and the lock
        # would leave e.g. planned_created naming a persona that now
        # exists) — an interrupted rollback would then misclassify that
        # persona as "created by this deploy" and discard its archive.
        archive_root_pre_existed = archives_dir(effective_target).is_dir()
        target_pre_existed = effective_target.is_dir()
        compiled_dirs = [d for d in sorted(compiled_dir_path.iterdir()) if d.is_dir()]
        planned_archived = [
            d.name for d in compiled_dirs if (effective_target / d.name).exists()
        ]
        planned_created = [
            d.name for d in compiled_dirs if not (effective_target / d.name).exists()
        ]
        planned_pruned: list[str] = []
        if prune:
            compiled_org_id = _first_org_id(compiled_dirs)
            compiled_ids = {d.name for d in compiled_dirs}
            if compiled_org_id and effective_target.is_dir():
                from .deploy.target import _org_id_of

                planned_pruned = [
                    d.name
                    for d in sorted(effective_target.iterdir())
                    if d.is_dir()
                    and d.name not in compiled_ids
                    and _org_id_of(d) == compiled_org_id
                ]
        journal = begin_session(
            effective_target,
            command="deploy",
            orgs=_compiled_org_ids(compiled_dir_path),
            planned_archived=planned_archived,
            planned_created=planned_created,
            planned_pruned=planned_pruned,
            archive_root_pre_existed=archive_root_pre_existed,
            target_pre_existed=target_pre_existed,
        )
        try:
            result = deploy_target(
                compiled_dir_path,
                effective_target,
                force=force,
                prune=prune,
            )
        except DeployCollisionError as e:
            # Collisions are detected in preflight, BEFORE anything is
            # mutated: nothing was written, so the journal entry has
            # nothing to reconcile — drop it instead of leaving a phantom
            # in_progress that would block the next deploy.
            click.secho(str(e), fg="red")
            from .deploy.session import discard_session

            try:
                discard_session(
                    effective_target, journal["id"], archive_root_pre_existed
                )
            except DeployError as de:
                click.secho(str(de), fg="red")
            click.secho(
                "No changes were made — resolve the collision and retry.",
                fg="yellow",
            )
            raise SystemExit(1)
        except DeployError as e:
            click.secho(
                f"Deployment failed: {e}\n"
                + "The deploy session is recorded as in_progress — run "
                "`po rollback` to revert the partial deploy.",
                fg="red",
            )
            raise SystemExit(1)
        except OSError as e:
            # The journal entry (in_progress) is the trace that target/
            # archive were modified: rollback can now reconcile it instead
            # of leaving an orphaned archive with no record.
            click.secho(
                f"Deployment failed with a filesystem error: {e}\n"
                + "The deploy session is recorded as in_progress — run "
                "`po rollback` to revert the partial deploy (it will "
                "restore anything already archived and clean up what was "
                "created).",
                fg="red",
            )
            raise SystemExit(1)
        except KeyboardInterrupt:
            # Ctrl+C mid-mutation: the session was already recorded as
            # in_progress, so the user needs to know it is reconcilable
            # instead of being left with a silent partial state.
            click.secho(
                "Aborted — the deploy session is recorded as in_progress; "
                "run `po rollback` to clean up.",
                fg="yellow",
            )
            raise SystemExit(1)
        try:
            commit_session(effective_target, journal["id"], result)
        except (DeployError, OSError) as e:
            # F5: the deploy ITSELF succeeded — only the manifest write
            # failed (disk full, permissions...). Say so explicitly
            # instead of a raw traceback: the session stays in_progress,
            # and `po rollback` would revert a deploy that actually
            # worked. The user must decide: fix the manifest issue and
            # re-run, or roll back.
            click.secho(
                f"Deployment SUCCEEDED but the session could not be "
                f"committed: {e}\n"
                f"The session is recorded as in_progress — either fix "
                f"the manifest issue and re-run `po deploy`, or run "
                f"`po rollback` to revert this deploy.",
                fg="red",
            )
            raise SystemExit(1)

    if not archive_root_pre_existed and archives_dir(effective_target).is_dir():
        click.secho(
            f"Backup archive created: {archives_dir(effective_target)} "
            "(restore any persona with `phantombot import-persona`)",
            fg="cyan",
        )
    click.secho(f"Deployed to {result.target}", fg="green")
    for actor_id in result.deployed:
        click.echo(f"  - {actor_id}")
    if result.archived:
        click.secho(
            f"{len(result.archived)} previous persona(s) archived (rollback available):",
            fg="cyan",
        )
        for name, archive_dir in result.archived:
            click.echo(f"  - {name} -> {archive_dir}")
    if result.pruned:
        click.secho(
            f"{len(result.pruned)} actor(s) pruned (no longer in the spec):",
            fg="yellow",
        )
        for a in result.pruned:
            click.echo(f"  - {a}")
    if result.deployed or result.pruned or result.archived:
        click.secho(
            "Rollback available: `po rollback` (restores the pre-deploy state)",
            fg="cyan",
        )
    if result.scopes_written:
        click.echo("  - scopes.json written (derived visibility scopes)")


@main.command("list-orgs")
@click.option(
    "--base",
    "base_dir",
    required=False,
    default="organizations",
    type=_ExpandUserPath(file_okay=False, path_type=Path),
)
def list_orgs_cmd(base_dir):
    """Lists the managed organizations under a directory (default ./organizations)."""
    base_dir = Path(base_dir)
    if not base_dir.exists():
        click.secho(f"{base_dir} does not exist", fg="red")
        raise SystemExit(1)

    found = False
    for org_dir in sorted(base_dir.iterdir()):
        org_yaml = org_dir / "org.yaml"
        if not org_yaml.exists():
            continue
        found = True
        try:
            spec, result = validate_org(org_yaml)
            estado = "✓ valid" if result.ok else f"✗ {len(result.errors)} error(s)"
            click.echo(
                f"  - {spec.organization.id} ({spec.organization.name}): "
                f"{len(spec.actors)} actor(s), {len(spec.departments)} dept(s) — {estado}"
            )
        except Exception as e:  # noqa: BLE001 — one broken org must not stop the rest
            click.echo(f"  - {org_dir.name}: load error ({e})")

    if not found:
        click.echo(f"No organizations found under {base_dir}")


def _iter_org_yaml_paths(base_dir: Path):
    for org_dir in sorted(base_dir.iterdir()):
        org_yaml = org_dir / "org.yaml"
        if org_yaml.exists():
            yield org_dir.name, org_yaml


@main.command("build-all")
@click.option(
    "--base",
    "base_dir",
    required=False,
    default="organizations",
    type=_ExpandUserPath(file_okay=False, path_type=Path),
)
@click.option(
    "--out",
    "out_base",
    required=True,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Base output directory; each organization is compiled into out_base/<org_id>/",
)
def build_all_cmd(base_dir, out_base):
    """Compiles all organizations under --base, one per subfolder in --out."""
    base_dir, out_base = Path(base_dir), Path(out_base)
    if not base_dir.exists():
        click.secho(f"{base_dir} does not exist", fg="red")
        raise SystemExit(1)

    ok, failed = 0, 0
    for org_id, org_yaml in _iter_org_yaml_paths(base_dir):
        try:
            spec, result = validate_org(org_yaml)
        except OrgSpecError as e:
            click.secho(f"✗ {org_id}: load error, skipping ({e})", fg="red")
            failed += 1
            continue
        if not result.ok:
            click.secho(
                f"✗ {org_id}: not valid, skipping ({len(result.errors)} error(s))",
                fg="red",
            )
            failed += 1
            continue
        try:
            written = compiler_build(spec, out_base / org_id)
        except CompileError as e:
            click.secho(f"✗ {org_id}: compile error, skipping ({e})", fg="red")
            failed += 1
            continue
        except OSError as e:
            click.secho(f"✗ {org_id}: filesystem error, skipping ({e})", fg="red")
            failed += 1
            continue
        total_files = sum(len(f) for f in written.values())
        if org_id != spec.organization.id:
            click.secho(
                f"⚠ {org_yaml}: directory name {org_id!r} != organization.id "
                f"{spec.organization.id!r}; deploy-all ownership matching "
                f"will be wrong",
                fg="yellow",
            )
        click.secho(
            f"✓ {org_id}: {len(written)} actor(s), {total_files} file(s) written",
            fg="green",
        )
        ok += 1

    click.echo(
        f"\n{ok} organization(s) compiled, {failed} skipped due to validation error."
    )
    if failed:
        raise SystemExit(1)


@main.command("deploy-all")
@click.option(
    "--base",
    "base_dir",
    required=False,
    default="organizations",
    type=_ExpandUserPath(file_okay=False, path_type=Path),
)
@click.option(
    "--dist-base",
    required=True,
    type=_ExpandUserPath(exists=True, file_okay=False, path_type=Path),
    help="Root directory already compiled by build-all (with one subfolder per org_id)",
)
@click.option(
    "--target",
    required=False,
    default=None,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Target personas directory (default: $PHANTOMORG_TARGET_DIR or ~/.local/share/phantombot/personas)",
)
@click.option("--force", is_flag=True, default=False)
@click.option("--prune", is_flag=True, default=False)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the final confirmation (for scripting/CI)",
)
def deploy_all_cmd(base_dir, dist_base, target, force, prune, assume_yes):
    """Deploys all organizations under --base using what was compiled in --dist-base."""
    base_dir, dist_base = Path(base_dir), Path(dist_base)
    target_path = Path(target) if target else None
    effective_target = target_path or default_personas_dir()

    if not base_dir.is_dir():
        click.secho(
            f"Cannot deploy-all: base directory not found at {base_dir}. "
            f"Pass --base with the organizations directory.",
            fg="red",
        )
        raise SystemExit(1)
    if not dist_base.is_dir():
        click.secho(
            f"Cannot deploy-all: compiled output not found at {dist_base}. "
            f"Run `po build-all` first (or pass --dist-base).",
            fg="red",
        )
        raise SystemExit(1)

    org_ids = [org_id for org_id, _ in _iter_org_yaml_paths(base_dir)]
    click.echo("Deploy plan (all organizations):")
    click.echo(f"  target : {effective_target}")
    click.echo(f"  orgs   : {', '.join(org_ids) if org_ids else '(none)'}")
    click.echo("  existing personas are archived to personas-archive/ before overwrite")
    if not assume_yes and not click.confirm("Apply this deployment?", default=False):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)

    ok, failed, collided = 0, 0, 0
    org_yaml_paths = [p for _, p in _iter_org_yaml_paths(base_dir)]
    orgs_done: list[str] = []
    merged_deployed: list[str] = []
    merged_created: list[str] = []
    merged_pruned: list[str] = []
    merged_archived: list[tuple[str, str]] = []
    # Data-dir backup info aggregated across orgs. Keep the FIRST
    # pre-overwrite backup seen per file (the true pre-session state,
    # matching the archive-dedup semantics: the first archive per name is
    # the pre-session version) and OR the created flags (if any org saw
    # the file absent, the deploy created it for the session as a whole).
    merged_scopes_backup: str | None = None
    merged_scopes_created = False
    merged_humans_backup: str | None = None
    merged_humans_created = False

    with _transaction_lock(effective_target):
        _reject_unresolved_in_progress(effective_target)
        # Compute the journal plan UNDER the transaction lock: the
        # planned state must reflect the target exactly as this deploy
        # is about to mutate it. A pre-lock snapshot can go stale (a
        # concurrent deploy finishing between the snapshot and the lock
        # would leave e.g. planned_created naming a persona that now
        # exists) — an interrupted rollback would then misclassify that
        # persona as "created by this deploy" and discard its archive.
        archive_root_pre_existed = archives_dir(effective_target).is_dir()
        target_pre_existed = effective_target.is_dir()
        # Planned personas across ALL orgs (for the durable journal).
        compiled_dirs_all: list[Path] = []
        for org_id in org_ids:
            cd = dist_base / org_id
            if cd.is_dir():
                compiled_dirs_all.extend(sorted(d for d in cd.iterdir() if d.is_dir()))
        planned_archived = [
            d.name for d in compiled_dirs_all if (effective_target / d.name).exists()
        ]
        planned_created = [
            d.name
            for d in compiled_dirs_all
            if not (effective_target / d.name).exists()
        ]
        planned_pruned: list[str] = []
        if prune and effective_target.is_dir():
            from .deploy.target import _org_id_of

            compiled_ids = {d.name for d in compiled_dirs_all}
            # F12: the preflight prune criterion is the METADATA org id
            # (_preflight/deploy_target prune by _org_id_of), not the
            # organizations/ folder name. If an org.yaml's
            # organization.id differs from its folder name, matching by
            # folder name would record a planned_pruned list that
            # diverges from what actually gets pruned.
            org_ids_set = {oid for d in compiled_dirs_all if (oid := _org_id_of(d))}
            planned_pruned = [
                d.name
                for d in sorted(effective_target.iterdir())
                if d.is_dir()
                and d.name not in compiled_ids
                and (_org_id_of(d) or "") in org_ids_set
            ]
        journal = begin_session(
            effective_target,
            command="deploy-all",
            orgs=org_ids,
            planned_archived=planned_archived,
            planned_created=planned_created,
            planned_pruned=planned_pruned,
            archive_root_pre_existed=archive_root_pre_existed,
            target_pre_existed=target_pre_existed,
            org_yamls=_org_yaml_hashes(org_yaml_paths),
        )
        mutation_failed = False
        for org_id, _org_yaml in _iter_org_yaml_paths(base_dir):
            compiled_dir = dist_base / org_id
            if not compiled_dir.exists():
                click.secho(
                    f"✗ {org_id}: no build at {compiled_dir}, skipping (run build-all first)",
                    fg="red",
                )
                failed += 1
                continue
            try:
                result = deploy_target(
                    compiled_dir, effective_target, force=force, prune=prune
                )
            except DeployCollisionError as e:
                # Preflight rejects the org BEFORE it mutates anything, so
                # skipping it leaves no partial state: only the other orgs
                # contribute to the session.
                click.secho(f"✗ {org_id}: collision, skipping —\n{e}", fg="red")
                collided += 1
                continue
            except DeployError as e:
                mutation_failed = True
                click.secho(
                    f"✗ {org_id}: deploy error — {e}\n"
                    "  The deploy session remains recorded as in_progress — "
                    "run `po rollback` after this to reconcile the partial "
                    "deploy (restore what was archived, clean what was created).",
                    fg="red",
                )
                failed += 1
                continue
            except OSError as e:
                mutation_failed = True
                click.secho(
                    f"✗ {org_id}: filesystem error — {e}\n"
                    "  The deploy session remains recorded as in_progress — "
                    "run `po rollback` after this to reconcile the partial "
                    "deploy (restore what was archived, clean what was created).",
                    fg="red",
                )
                failed += 1
                continue
            except KeyboardInterrupt:
                # Ctrl+C mid-mutation: earlier orgs may already be
                # deployed and the aggregated session is in_progress.
                mutation_failed = True
                click.secho(
                    "Aborted — the deploy-all session is recorded as "
                    "in_progress; run `po rollback` to clean up.",
                    fg="yellow",
                )
                raise SystemExit(1)
            click.secho(
                f"✓ {org_id}: {len(result.deployed)} actor(s) deployed", fg="green"
            )
            if result.pruned:
                click.echo(f"    pruned (archived): {result.pruned}")
            for name, archive_dir in result.archived:
                click.echo(f"    archived: {name} -> {archive_dir}")
            if result.deployed or result.pruned or result.archived:
                orgs_done.append(org_id)
                merged_deployed.extend(result.deployed)
                merged_created.extend(result.created)
                merged_pruned.extend(result.pruned)
                merged_archived.extend(result.archived)
                if merged_scopes_backup is None:
                    merged_scopes_backup = result.scopes_backup
                if merged_humans_backup is None:
                    merged_humans_backup = result.humans_backup
                merged_scopes_created = merged_scopes_created or result.scopes_created
                merged_humans_created = merged_humans_created or result.humans_created
            ok += 1

        # One aggregated session for the whole deploy-all invocation: a single
        # `po rollback` undoes everything this command changed.
        # NOTE: `archived` keeps its original order AND any duplicates (the
        # same actor archived twice — e.g. two orgs sharing an actor id). The
        # FIRST archive per name is the pre-session version and is restored;
        # later archives of the same name are in-session artifacts and are
        # discarded by plan_rollback (restoring both in order would clobber
        # the freshly restored pre-session version with the in-session one).
        if mutation_failed:
            click.secho(
                "Some organizations failed with a filesystem error — the "
                f"deploy-all session ({journal['id']}) is left as in_progress. "
                "Run `po rollback` to reconcile the partial deploy.",
                fg="yellow",
            )
        elif merged_deployed or merged_pruned or merged_archived:
            from .deploy.target import DeployResult

            merged = DeployResult(
                target=effective_target,
                deployed=sorted(set(merged_deployed)),
                created=sorted(set(merged_created)),
                pruned=sorted(set(merged_pruned)),
                archived=merged_archived,
                scopes_written=merged_scopes_backup is not None
                or merged_scopes_created,
                scopes_backup=merged_scopes_backup,
                scopes_created=merged_scopes_created,
                humans_written=merged_humans_backup is not None
                or merged_humans_created,
                humans_backup=merged_humans_backup,
                humans_created=merged_humans_created,
            )
            try:
                commit_session(effective_target, journal["id"], merged)
            except (DeployError, OSError) as e:
                click.secho(
                    f"Deployment SUCCEEDED but the deploy-all session could "
                    f"not be committed: {e}\n"
                    f"The session is recorded as in_progress — either fix "
                    f"the manifest issue and re-run `po deploy-all`, or run "
                    f"`po rollback` to revert this deploy-all.",
                    fg="red",
                )
                raise SystemExit(1)
        else:
            # Nothing was deployed anywhere: drop the journal entry (it
            # records no mutation, so nothing to reconcile).
            from .deploy.session import discard_session

            try:
                discard_session(
                    effective_target, journal["id"], archive_root_pre_existed
                )
            except (DeployError, OSError) as e:
                click.secho(
                    f"The deploy-all session could not be discarded: {e}",
                    fg="red",
                )
                raise SystemExit(1)

    if not archive_root_pre_existed and archives_dir(effective_target).is_dir():
        click.secho(
            f"Backup archive created: {archives_dir(effective_target)} "
            "(restore any persona with `phantombot import-persona`)",
            fg="cyan",
        )
    click.secho(
        "Rollback available: `po rollback` (restores the pre-deploy state)",
        fg="cyan",
    )
    click.echo(
        f"\n{ok} organization(s) deployed, {collided} with collision, {failed} without build."
    )
    # Exit non-zero whenever ANY organization failed or collided — even
    # when others succeeded. Automation treats exit 0 as "everything
    # deployed"; a partial rollout must not report as complete.
    if mutation_failed or failed or collided:
        raise SystemExit(1)


@main.command("rollback")
@click.option(
    "--target",
    required=False,
    default=None,
    type=_ExpandUserPath(file_okay=False, path_type=Path),
    help="Target personas directory (default: $PHANTOMORG_TARGET_DIR or ~/.local/share/phantombot/personas)",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    default=False,
    help="List recorded deploy sessions without rolling back",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the final confirmation (for scripting/CI)",
)
def rollback_cmd(target, list_only, assume_yes):
    """Undoes the last deploy: restores archived personas, removes
    personas the deploy created, and deletes the backups — the system
    returns to exactly the state it was in before that deploy.

    Stack-based: run it once per deploy you want to undo.
    """
    target_path = Path(target) if target else default_personas_dir()
    archive_root = archives_dir(target_path)

    try:
        mine = sessions_for_target(archive_root, target_path)
    except ManifestError as e:
        click.secho(
            "Cannot roll back: the session manifest is unreadable or corrupt "
            f"({e}). The archived personas in {archive_root} are still there "
            "and can be restored manually (move them back into the target), "
            "but the rollback history is unavailable.",
            fg="red",
        )
        raise SystemExit(1)

    if not mine:
        # A rollback that dropped its session can still crash in the final
        # best-effort removal of an empty personas-archive/. If the root
        # is genuinely empty (no sessions, no archives, no trash content),
        # remove it so the system returns to exactly its pre-deploy state.
        removed_root = remove_abandoned_archive_root(archive_root)
        mp = archive_root / ".phantomorg-manifest.json"
        if mp.is_file():
            click.secho(
                "The session manifest exists but no readable session was found "
                f"({mp}). It may be corrupt — the archived personas in "
                f"{archive_root} are still there and can be restored manually "
                "(mv them back into the target), but `po rollback` cannot help.\n",
                fg="red",
            )
        click.secho(
            "Nothing to roll back — no deploy session recorded for this target.",
            fg="yellow",
        )
        if removed_root:
            click.secho(
                "  (removed the empty personas-archive/ left by an interrupted "
                "rollback)",
                fg="yellow",
            )
        raise SystemExit(1)

    if list_only:
        click.echo(f"Deploy sessions for {target_path.resolve()}:")
        for i, session in enumerate(mine, 1):
            archived_n = len(session.get("archived", []))
            created_n = len(session.get("created", []))
            orgs = ", ".join(session.get("orgs", [])) or "-"
            click.echo(
                f"  {i}. {session.get('id')}  "
                f"{session.get('command', '?'):10} orgs: {orgs}  "
                f"{len(session.get('deployed', []))} deployed, "
                f"{archived_n} archived, {created_n} created"
            )
        raise SystemExit(0)

    try:
        with _transaction_lock(target_path):
            plan = plan_rollback(archive_root, target_path)
    except RollbackError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)

    session = plan.session
    click.echo(f"Rollback of session {plan.session_id} ({session.get('command', '?')})")
    if session.get("state") == "in_progress":
        click.secho(
            "  reconciling: this session is an INTERRUPTED deploy (no "
            "committed result). Rolling back will restore whatever the "
            "attempt already archived and discard whatever it created — "
            "returning the target to its state before the attempt.",
            fg="yellow",
        )
    if plan.cleanup_only:
        click.secho(
            "  cleanup: this rollback was already applied but never finished "
            "(the archived personas are gone). It will only remove what the "
            "deploy created and finish the cleanup.",
            fg="yellow",
        )
    if plan.restore:
        click.echo(
            "  restore : "
            + ", ".join(name for name, _ in plan.restore)
            + " (from personas-archive)"
        )
    if plan.remove_created:
        click.echo(
            "  remove  : "
            + ", ".join(plan.remove_created)
            + " (created by that deploy)"
        )
    if plan.discard:
        click.secho(
            "  replace: "
            + ", ".join(plan.discard)
            + " (current versions moved to a trash dir, deleted only if the rollback succeeds)",
            fg="yellow",
        )
    if plan.unexpected:
        click.secho(
            "  left untouched: "
            + ", ".join(name for name, _ in plan.unexpected)
            + " (not created by this deploy — foreign archives in "
            "personas-archive/, kept as-is)",
            fg="yellow",
        )
    if not session.get("archive_root_pre_existed", False):
        click.echo(
            "  personas-archive/: will be deleted (did not exist before that deploy)"
        )
    if not session.get("target_pre_existed", False):
        click.echo(
            f"  {session['target']}: will be deleted if empty (did not exist before)"
        )
    for drift in plan.spec_drift:
        click.secho(f"⚠ spec changed since that deploy: {drift}", fg="yellow")
    if not assume_yes and not click.confirm(
        "Roll back to the pre-deploy state?", default=False
    ):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)

    # Re-acquire the transaction lock for the mutation itself, and
    # re-plan under it: if another deploy happened while the user was
    # confirming, the plan we showed is stale — refuse instead of
    # executing a plan whose session is no longer the latest.
    with _transaction_lock(target_path):
        fresh = plan_rollback(archive_root, target_path)
        if fresh.session_id != plan.session_id:
            click.secho(
                "The deploy session changed while you were confirming (a new "
                "deploy was recorded). Nothing was changed — run `po rollback` "
                "again to see the current session.",
                fg="red",
            )
            raise SystemExit(1)
        try:
            result = execute_rollback(fresh)
        except RollbackError as e:
            click.secho(str(e), fg="red")
            raise SystemExit(1)
        except KeyboardInterrupt:
            click.secho(
                "Aborted — the rollback may be partially applied; run "
                "`po rollback` again to finish the cleanup.",
                fg="yellow",
            )
            raise SystemExit(1)
    click.secho(f"Rolled back to the state before {result.session_id}.", fg="green")
    if result.restored:
        click.echo(f"  restored : {', '.join(result.restored)}")
    if result.discarded:
        click.echo(
            "  discarded: "
            + ", ".join(result.discarded)
            + " (post-deploy versions replaced by the pre-deploy ones)"
        )
    if result.removed_created:
        click.echo(f"  removed  : {', '.join(result.removed_created)}")
    if result.restored_data:
        click.echo(
            "  data-restored: "
            + ", ".join(Path(p).name for p in result.restored_data)
            + " (data-dir files returned to their pre-deploy state)"
        )
    if result.removed_data:
        click.echo(
            "  data-removed : "
            + ", ".join(Path(p).name for p in result.removed_data)
            + " (data-dir files created by that deploy)"
        )
    if result.data_skipped:
        click.echo(
            "  data-skipped : scopes.json/HUMANS.md were NOT restored "
            "(their pre-deploy state is unknown: interrupted deploy or "
            "pre-backup session)."
        )
    if result.discarded_archives:
        click.echo(
            "  discarded: "
            + ", ".join(result.discarded_archives)
            + " (in-session archives of an interrupted deploy, not restored)"
        )
    if result.archive_root_deleted:
        click.echo("  personas-archive/: deleted (was created by that deploy)")
    elif not session.get("archive_root_pre_existed", False):
        click.echo("  personas-archive/: kept (it still contains other content)")
    else:
        click.echo("  personas-archive/: kept (it existed before that deploy)")
    if result.target_deleted:
        click.echo(f"  {session['target']}: deleted (was created by that deploy)")


@main.command("remove-department")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "dept_id", required=True)
@click.option(
    "--cascade",
    is_flag=True,
    default=False,
    help="Promotes child departments to root instead of blocking",
)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def remove_department_cmd(org_path, dept_id, cascade, yes):
    """Removes a department. Blocks if it has assigned roles (never cascades them)."""
    if not yes and not click.confirm(f"Remove department '{dept_id}' from {org_path}?"):
        click.echo("Cancelled.")
        return
    try:
        cascade_actions = remove_department_fn(org_path, dept_id, cascade=cascade)
    except (KeyError, RemovalBlockedError) as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Department '{dept_id}' removed from {org_path}", fg="green")
    for action in cascade_actions:
        click.echo(f"  [cascade] {action}")


@main.command("remove-role")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "role_id", required=True)
@click.option(
    "--cascade",
    is_flag=True,
    default=False,
    help="Promotes subordinates to root and cleans escalation_matrix (never deletes actors)",
)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def remove_role_cmd(org_path, role_id, cascade, yes):
    """Removes a role. Always blocks if actors are assigned (use remove-actor first)."""
    if not yes and not click.confirm(f"Remove role '{role_id}' from {org_path}?"):
        click.echo("Cancelled.")
        return
    try:
        cascade_actions = remove_role_fn(org_path, role_id, cascade=cascade)
    except (KeyError, RemovalBlockedError) as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Role '{role_id}' removed from {org_path}", fg="green")
    for action in cascade_actions:
        click.echo(f"  [cascade] {action}")


@main.command("remove-actor")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--id", "actor_id", required=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def remove_actor_cmd(org_path, actor_id, yes):
    """Removes an actor from org.yaml. Does not delete its already compiled/deployed folder."""
    if not yes and not click.confirm(f"Remove actor '{actor_id}' from {org_path}?"):
        click.echo("Cancelled.")
        return
    try:
        remove_actor_fn(org_path, actor_id)
    except KeyError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Actor '{actor_id}' removed from {org_path}", fg="green")
    click.secho(
        "Note: this only edits org.yaml. If it's already compiled/deployed, "
        "delete its folder manually in the `po deploy` target.",
        fg="yellow",
    )


@main.command("rename-department")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--old-id", required=True)
@click.option("--new-id", required=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def rename_department_cmd(org_path, old_id, new_id, yes):
    """Renames a department and updates every cross-reference."""
    if not yes and not click.confirm(
        f"Rename department '{old_id}' -> '{new_id}' in {org_path}?"
    ):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)
    try:
        updated = rename_department_fn(org_path, old_id, new_id)
    except (KeyError, ValueError) as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Department '{old_id}' -> '{new_id}' in {org_path}", fg="green")
    for ref in updated:
        click.echo(f"  [updated] {ref}")


@main.command("rename-role")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--old-id", required=True)
@click.option("--new-id", required=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def rename_role_cmd(org_path, old_id, new_id, yes):
    """Renames a role and updates every cross-reference (reports_to, actors, escalation_matrix)."""
    if not yes and not click.confirm(
        f"Rename role '{old_id}' -> '{new_id}' in {org_path}?"
    ):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)
    try:
        updated = rename_role_fn(org_path, old_id, new_id)
    except (KeyError, ValueError) as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Role '{old_id}' -> '{new_id}' in {org_path}", fg="green")
    for ref in updated:
        click.echo(f"  [updated] {ref}")


@main.command("rename-actor")
@click.option(
    "--org",
    "org_path",
    required=True,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--old-id", required=True)
@click.option("--new-id", required=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Don't ask for confirmation"
)
def rename_actor_cmd(org_path, old_id, new_id, yes):
    """Renames an actor. Does not move anything on disk on the runtime side (build+deploy afterwards)."""
    if not yes and not click.confirm(
        f"Rename actor '{old_id}' -> '{new_id}' in {org_path}?"
    ):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)
    try:
        rename_actor_fn(org_path, old_id, new_id)
    except (KeyError, ValueError) as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1)
    click.secho(f"Actor '{old_id}' -> '{new_id}' in {org_path}", fg="green")
    click.secho(
        "Note: the compiled/deployed directory is still named "
        f"'{old_id}' until the next `po build` + `po deploy`.",
        fg="yellow",
    )


@main.command("import-audit")
@click.option(
    "--persona-dir",
    required=True,
    type=_ExpandUserPath(exists=True, file_okay=False, path_type=Path),
)
@click.option("--role-id", required=True, help="Proposed role ID for this actor")
@click.option(
    "--department",
    required=False,
    default=None,
    help="Department ID; optional if --against-org can suggest it",
)
@click.option(
    "--against-org",
    required=False,
    default=None,
    type=_ExpandUserPath(exists=True, dir_okay=False, path_type=Path),
    help="Target org.yaml: resolves 'reports to' against its real roles/actors",
)
@click.option("--access-level", default="level-2", show_default=True)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Applies the fragment directly to --against-org (add-role + add-actor), instead of just showing it",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Don't ask for confirmation when applying",
)
def import_audit_cmd(
    persona_dir, role_id, department, against_org, access_level, apply_, yes
):
    """
    Analyzes an existing persona folder (not generated by PhantomOrg)
    and proposes an org.yaml fragment for manual review. Without --apply
    it writes nothing.

    Without --against-org, 'reports to' stays as the raw detected text
    (it is not resolved to a real id). With --against-org, the text is
    resolved against the roles/actors of that organization; if it is
    ambiguous (several roles match) or matches nothing, the reason is
    left explicit.

    --apply requires --against-org (it is the file it writes to) and asks
    for confirmation unless --yes. If the 'reports to' ambiguity was not
    resolved, it applies with reports_to: null (same as in the fragment) —
    --apply never picks a random candidate among several.
    """
    if apply_ and not against_org:
        click.secho(
            "--apply requires --against-org (it is the file it writes to).", fg="red"
        )
        raise SystemExit(1)

    findings = audit_persona_dir(Path(persona_dir))

    resolved = None
    if against_org:
        spec, result = validate_org(against_org)
        if not result.ok:
            click.secho(
                "The target organization (--against-org) is not valid:", fg="red"
            )
            for e in result.errors:
                click.echo(f"  - {e}")
            raise SystemExit(1)
        resolved = resolve_against_org(findings, spec)

    department_id = department or (
        resolved.suggested_department_id if resolved else None
    )
    if not department_id:
        click.secho(
            "Could not determine the department: pass it with --department "
            "or use --against-org to have it suggested from the resolved superior.",
            fg="red",
        )
        raise SystemExit(1)

    fragment = render_org_yaml_fragment(
        findings, role_id, department_id, access_level, resolved=resolved
    )
    click.echo(fragment)

    if resolved and resolved.resolution_notes:
        click.secho("Resolution against the target organization:", fg="cyan")
        for note in resolved.resolution_notes:
            click.echo(f"  - {note}")

    if findings.warnings:
        click.secho(
            f"{len(findings.warnings)} warning(s) — review before accepting:",
            fg="yellow",
        )
        for w in findings.warnings:
            click.echo(f"  - {w}")

    if not apply_:
        return

    reports_to = resolved.resolved_reports_to_role_id if resolved else None
    reports_to_human = (
        resolved.unmatched_candidates[0]
        if resolved and resolved.unmatched_candidates
        else None
    )
    if not yes and not click.confirm(
        f"\nApply this fragment to {against_org} "
        f"(add-role '{role_id}' + add-actor '{findings.actor_id}')?"
    ):
        click.echo("Cancelled, nothing was applied.")
        return

    try:
        # Single atomic load-mutate-save: both ids are pre-checked
        # (existence + identifier grammar) BEFORE anything is written, so
        # a rejected apply can never leave a half-applied role.
        add_role_and_actor_fn(
            against_org,
            role_id=role_id,
            role_name=findings.role_name_guess or role_id,
            department=department_id,
            reports_to=reports_to,
            access_level=access_level,
            reports_to_human=reports_to_human,
            functions=[],
            actor_id=findings.actor_id,
            tools=findings.tools_guess,
            telegram_bot=findings.telegram_bot,
        )
    except (DuplicateIdError, ValueError) as e:
        click.secho(f"Not applied: {e}", fg="red")
        raise SystemExit(1)

    click.secho(f"Applied to {against_org}. Run `po validate` to confirm.", fg="green")


@main.command("update")
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Print whether an update is available without installing. Exit 0 if up to date, 2 if available, 1 on error.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip the install confirmation (use from cron).",
)
@click.option(
    "--repo",
    "repo_override",
    required=False,
    default=None,
    help="Override the GitHub repo (owner/name). Default: remote.origin or $PHANTOMORG_UPDATE_REPO.",
)
def update_cmd(check, force, repo_override):
    """
    Self-update: fetch the latest PhantomOrg release from GitHub and
    fast-forward this checkout to it (phantombot-style update cycle).

    The repo checkout is the source of truth (install.sh symlinks bin/po
    into PATH), so an update is a git fast-forward to the released tag,
    followed by a venv dependency refresh when the repo has a .venv.

    Exit codes (cron-alertable): 0 updated or already latest, 1 error,
    2 update available (with --check). Set GITHUB_TOKEN for private
    repos or higher rate caps; PHANTOMORG_UPDATE_REPO overrides the
    repo to check.
    """
    raise SystemExit(run_updater(check=check, force=force, repo_override=repo_override))


if __name__ == "__main__":
    main()
