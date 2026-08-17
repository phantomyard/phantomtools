"""
Interactive layers (click.prompt, no extra dependencies) over the pure
functions of new_org.py and mutations.py. The CLI (cli.py) decides
whether to call these functions (interactive mode, no flags) or directly
the pure functions (flag mode, for scripting/CI).

add-role and add-actor read the existing org.yaml and offer the
departments/roles already defined as `click.Choice`, instead of asking
for the id as free text. This prevents at the source the kind of broken
reference that previously only `pf validate` detected after saving (a
mistyped `department` or `reports_to`) — it is suggested from the
organization itself, not validated a posteriori.
"""

from __future__ import annotations

from pathlib import Path

import click
import yaml

from ..importer.audit import audit_persona_dir
from ..spec.shape_validator import is_valid_identifier
from .mutations import (
    DuplicateIdError,
    _mutation_lock,
    _save,
    add_actor,
    add_department,
    add_role,
)
from .new_org import new_org
from .setup import (
    PersonaPlan,
    SetupPlan,
    _slugify,
    build_org_yaml,
    find_personas_dirs,
)
from .templates import available_templates


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def _load_raw(org_path: Path) -> dict:
    with open(org_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _require_org_doc_structure(doc: dict, org_path: Path) -> None:
    """Validate the minimal org.yaml structure `pf setup` needs to reuse.

    A malformed org.yaml (missing ``organization.id``, a department/role
    entry without ``id``) would otherwise crash the wizard mid-flight
    with a raw ``KeyError`` traceback. Emit a friendly message instead
    (L2).
    """
    org = doc.get("organization")
    if not isinstance(org, dict) or not isinstance(org.get("id"), str) or not org["id"]:
        raise click.ClickException(
            f"{org_path} is not a valid org.yaml for `pf setup`: "
            "missing a non-empty organization.id. Fix the file or run "
            "`pf validate` for details."
        )
    for label, entries in (
        ("departments", doc.get("departments", [])),
        ("roles", doc.get("roles", [])),
    ):
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not entry["id"]
            ):
                raise click.ClickException(
                    f"{org_path} is not a valid org.yaml for `pf setup`: "
                    f"an entry in {label} is missing its id. Fix the file "
                    "or run `pf validate` for details."
                )


def run_new_org_wizard() -> Path:
    org_id = click.prompt("Organization ID (slug, e.g. aquaponics-united)")
    name = click.prompt("Organization name")
    sector = click.prompt("Sector (e.g. ngo, pyme, educacion)")
    langs = click.prompt("Languages (comma-separated, e.g. en,es)", default="en")

    template_choices = ["none"] + available_templates()
    template_choice = click.prompt(
        "Sector department template (see `pf templates`)",
        type=click.Choice(template_choices),
        default="none",
    )
    template = None if template_choice == "none" else template_choice

    return new_org(org_id, name, sector, _split_csv(langs), template=template)


def run_add_department_wizard(org_path: Path) -> None:
    doc = _load_raw(org_path)
    existing_depts = [d["id"] for d in doc.get("departments", [])]

    dept_id = click.prompt("Department ID")
    name = click.prompt("Department name")

    parent_choices = ["none"] + existing_depts
    parent_choice = click.prompt(
        "Parent department",
        type=click.Choice(parent_choices),
        default="none",
    )
    parent = None if parent_choice == "none" else parent_choice

    access_policy = click.prompt(
        "Access policy", type=click.Choice(["level-1", "level-2", "level-3"])
    )
    add_department(org_path, dept_id, name, parent, access_policy)


def run_add_role_wizard(org_path: Path) -> None:
    doc = _load_raw(org_path)
    existing_depts = [d["id"] for d in doc.get("departments", [])]
    existing_roles = [r["id"] for r in doc.get("roles", [])]

    if not existing_depts:
        click.secho(
            "This organization has no departments yet. "
            "Use `pf add-department` before adding a role.",
            fg="red",
        )
        raise SystemExit(1)

    role_id = click.prompt("Role ID")
    name = click.prompt("Role name")
    department = click.prompt("Department", type=click.Choice(existing_depts))

    reports_to_choices = ["none"] + existing_roles
    reports_to_choice = click.prompt(
        "Reports to (existing role; 'none' if it is the root)",
        type=click.Choice(reports_to_choices),
        default="none",
    )
    reports_to = None if reports_to_choice == "none" else reports_to_choice

    reports_to_human = (
        click.prompt(
            "Reports to (human, empty if not applicable)",
            default="",
            show_default=False,
        )
        or None
    )
    access_level = click.prompt(
        "Access level", type=click.Choice(["level-1", "level-2", "level-3"])
    )
    functions_raw = click.prompt(
        "Functions (comma-separated)", default="", show_default=False
    )
    add_role(
        org_path,
        role_id,
        name,
        department,
        reports_to,
        access_level,
        functions=_split_csv(functions_raw),
        reports_to_human=reports_to_human,
    )


def run_add_actor_wizard(org_path: Path) -> None:
    doc = _load_raw(org_path)
    existing_roles = [r["id"] for r in doc.get("roles", [])]

    if not existing_roles:
        click.secho(
            "This organization has no roles yet. "
            "Use `pf add-role` before adding an actor.",
            fg="red",
        )
        raise SystemExit(1)

    actor_id = click.prompt("Actor ID")
    role = click.prompt("Assigned role", type=click.Choice(existing_roles))
    telegram_bot = (
        click.prompt(
            "Telegram bot (empty if not applicable)", default="", show_default=False
        )
        or None
    )
    tools_raw = click.prompt("Tools (comma-separated)", default="", show_default=False)
    add_actor(
        org_path, actor_id, role, _split_csv(tools_raw), telegram_bot=telegram_bot
    )


def _default_personas_root() -> Path:
    return Path.home() / ".local/share/phantombot/personas"


def run_setup_wizard(
    personas_root: Path | None = None,
    org_path: Path | None = None,
    base_dir: Path = Path("organizations"),
) -> Path:
    """
    `pf setup` — guided installation over a phantombot personas root.

    Steps:
      1. Locate the phantombot personas directory (detected or asked).
      2. Decide the org source: reuse an existing org.yaml, or create a
         fresh one (id/name/sector + departments defined interactively).
      3. Reassign every EXISTING persona to a department + role (audit
         suggests both; the user confirms or overrides).
      4. Optionally add brand-new personas.
      5. Apply everything to the org.yaml, then offer build + deploy.

    Returns the org.yaml path that was created or updated.
    """
    # 1. personas root
    root = personas_root or _default_personas_root()
    if not root.is_dir():
        if personas_root is None:
            click.secho(
                f"No personas directory found at {root}. ",
                fg="yellow",
                nl=False,
            )
            root = Path(
                click.prompt(
                    "Phantombot personas directory (or empty to skip)",
                    default="",
                    show_default=False,
                )
            )
            if not str(root).strip():
                root = Path("__none__")
        if not root.is_dir():
            click.secho(
                "No phantombot personas directory available — proceeding with "
                "new personas only.",
                fg="yellow",
            )
            root = Path("__none__")

    existing_personas = find_personas_dirs(root) if root.is_dir() else []

    # 2. org source
    create_new = org_path is None and not click.confirm(
        "Do you already have an org.yaml for this organization?"
    )
    if org_path is None and not create_new:
        org_path = Path(
            click.prompt(
                "Path to your org.yaml",
                type=click.Path(exists=True, dir_okay=False, path_type=Path),  # type: ignore[type-var]
            )
        )

    if create_new:
        org_id = click.prompt("Organization ID (slug, e.g. aquaponics-united)")
        if not is_valid_identifier(org_id):
            click.secho(
                f"Invalid organization id {org_id!r}: must match "
                r"^[a-z0-9][a-z0-9_-]*$",
                fg="red",
            )
            raise SystemExit(1)
        name = click.prompt("Organization name")
        sector = click.prompt("Sector (e.g. ngo, pyme, educacion)", default="ngo")
        langs = click.prompt("Languages (comma-separated, e.g. en,es)", default="en")
        languages = _split_csv(langs)

        click.echo(
            "\nDefine departments now — empty name finishes. "
            "Access policy defaults to level-2."
        )
        departments: list[dict] = []
        while True:
            dept_name = click.prompt("Department name", default="", show_default=False)
            if not dept_name.strip():
                break
            dept_id = _slugify(dept_name) or f"dept{len(departments) + 1}"
            departments.append(
                {
                    "id": dept_id,
                    "name": dept_name.strip(),
                    "parent": None,
                    "access_policy": "level-2",
                }
            )
        if not departments:
            click.secho("At least one department is required.", fg="red")
            raise SystemExit(1)

        org_path = base_dir / org_id / "org.yaml"
        org_path.parent.mkdir(parents=True, exist_ok=True)
        plan = SetupPlan(
            org_path=org_path,
            org_id=org_id,
            org_name=name,
            sector=sector,
            languages=languages,
            departments=departments,
            personas=[],
            create_new_org=True,
        )
    else:
        # else-branch invariant: the user provided org_path above
        if org_path is None:
            raise click.ClickException("No org.yaml was provided")
        doc = _load_raw(org_path)
        _require_org_doc_structure(doc, org_path)
        plan = SetupPlan(
            org_path=org_path,
            org_id=doc["organization"]["id"],
            org_name=doc["organization"].get("name", doc["organization"]["id"]),
            sector=doc["organization"].get("sector", "ngo"),
            languages=doc["organization"].get(
                "languages", [doc["organization"].get("default_language", "en")]
            ),
            departments=doc.get("departments", []),
            personas=[],
            create_new_org=False,
            existing_roles={r["id"]: r["name"] for r in doc.get("roles", [])},
            existing_departments=[d["id"] for d in doc.get("departments", [])],
        )

    # Invariant: by this point org_path is always set — either the user
    # supplied one, or the create_new branch built it.
    if org_path is None:
        raise click.ClickException("No org.yaml was provided")

    dept_choices = [d["id"] for d in plan.departments]

    def ask_role(persona_id: str, suggested: str | None = None) -> str:
        """Ask for the role of one persona. Returns the role id.

        `suggested` is the role name guessed by import-audit (e.g. "Project
        Lead"); it is used as the display name of the new role so the org
        stays readable, and offered as the default id when it slugs well.
        """
        existing = list(plan.existing_roles.keys())
        suggested_id = _slugify(suggested) if suggested else None
        if not existing:
            return _slugify(
                click.prompt(
                    f"Role ID for {persona_id}",
                    default=suggested_id or f"{persona_id}_role",
                    show_default=True,
                )
            )
        role_choices = existing + ["<new role>"]
        choice = click.prompt(
            "Role (choose existing or create new)",
            type=click.Choice(role_choices),
            default="<new role>",
            show_default=False,
        )
        if choice == "<new role>":
            return _slugify(
                click.prompt(
                    f"New role ID for {persona_id}",
                    default=suggested_id or f"{persona_id}_role",
                    show_default=True,
                )
            )
        return choice

    # 3. reassign existing personas (priority)
    if existing_personas:
        click.echo(f"\nFound {len(existing_personas)} existing persona(s):")
        for persona_dir in existing_personas:
            actor_id = persona_dir.name
            click.secho(f"\n  -> {actor_id}", bold=True)

            findings = audit_persona_dir(persona_dir)
            guess_dept = (
                findings.department_guess
                if findings.department_guess in dept_choices
                else None
            )
            dept_id = click.prompt(
                "Department",
                type=click.Choice(dept_choices) if dept_choices else str,
                default=guess_dept,
                show_default=bool(guess_dept),
            )
            role_id = ask_role(actor_id, suggested=findings.role_name_guess)
            if findings.warnings:
                click.secho(
                    f"      (audit: {len(findings.warnings)} warning(s) — "
                    f"{findings.warnings[0]})",
                    fg="yellow",
                )
            plan.personas.append(
                PersonaPlan(
                    actor_id=actor_id,
                    department_id=dept_id,
                    role_id=role_id,
                    is_new=False,
                    suggested_role=findings.role_name_guess,
                )
            )
    else:
        click.secho(
            "\nNo existing personas found — only new ones can be added.",
            fg="yellow",
        )

    # 4. add new personas
    while click.confirm("\nAdd a new persona?"):
        actor_id = click.prompt("New persona ID (slug)")
        dept_id = click.prompt(
            "Department",
            type=click.Choice(dept_choices) if dept_choices else str,
        )
        role_id = ask_role(actor_id)
        plan.personas.append(
            PersonaPlan(
                actor_id=actor_id,
                department_id=dept_id,
                role_id=role_id,
                is_new=True,
            )
        )

    # 5. final review & confirmation — nothing is written until the
    # user explicitly approves the whole plan (rollback safety: the
    # user may have changed their mind or mis-assigned someone).
    click.secho("\n" + "─" * 56, bold=True)
    click.secho("Review before applying", bold=True)
    click.secho("─" * 56, bold=True)
    click.echo(f"Organization : {plan.org_id} ({plan.org_name})")
    if plan.create_new_org:
        click.echo(f"  new org.yaml: {plan.org_path}")
        click.echo(f"  departments : {', '.join(d['name'] for d in plan.departments)}")
    else:
        click.echo(f"  org.yaml    : {plan.org_path} (existing, will be backed up)")
    click.echo("")
    if plan.personas:
        click.echo("Personas:")
        for p in plan.personas:
            kind = "new" if p.is_new else "existing"
            click.echo(
                f"  - {p.actor_id:<20} {kind:<10} dept={p.department_id or '?'}  "
                f"role={p.role_id or '?'}"
            )
    else:
        click.echo("Personas: (none)")
    click.echo("")
    if not click.confirm(
        "Apply this plan? The existing org.yaml (if any) is backed up first.",
        default=False,
    ):
        click.secho("Cancelled — no changes were made.", fg="yellow")
        raise SystemExit(1)

    # 6. apply
    if plan.create_new_org:
        doc = build_org_yaml(plan)
        if org_path.exists():
            click.secho(
                f"WARNING: {org_path} already exists — backing it up before overwrite.",
                fg="yellow",
            )
        _save(org_path, doc)  # backup (if any) + atomic replace
    else:
        doc = _load_raw(org_path)
        with _mutation_lock(org_path):
            # Transactional batch apply: mutate the document fully in
            # memory (pre-validating every id against the doc AND against
            # the rest of the plan), then commit with a SINGLE _save —
            # one backup, one atomic replace. A mid-batch failure can no
            # longer leave a partially-mutated org.yaml (M1).
            departments = doc.setdefault("departments", [])
            roles = doc.setdefault("roles", [])
            actors = doc.setdefault("actors", [])
            existing_dep_ids = {d["id"] for d in departments}
            existing_role_ids = {r["id"] for r in roles}
            existing_actor_ids = {a["id"] for a in actors}

            for dept in plan.departments:
                if dept["id"] in plan.existing_departments:
                    continue  # already there, untouched
                if dept["id"] in existing_dep_ids:
                    raise DuplicateIdError(
                        f"A department with id '{dept['id']}' already exists"
                    )
                existing_dep_ids.add(dept["id"])
                departments.append(
                    {
                        "id": dept["id"],
                        "name": dept["name"],
                        "parent": dept.get("parent"),
                        "access_policy": dept["access_policy"],
                    }
                )

            for persona in plan.personas:
                if persona.role_id is None or persona.department_id is None:
                    continue  # defensive: the wizard always fills these
                chosen_role: str = persona.role_id
                if (
                    chosen_role not in plan.existing_roles
                    and chosen_role not in plan.created_roles
                ):
                    if chosen_role in existing_role_ids:
                        raise DuplicateIdError(
                            f"A role with id '{chosen_role}' already exists"
                        )
                    existing_role_ids.add(chosen_role)
                    roles.append(
                        {
                            "id": chosen_role,
                            "name": persona.suggested_role or chosen_role,
                            "department": persona.department_id,
                            "reports_to": None,
                            "reports_to_human": None,
                            "functions": [],
                            "access_level": "level-2",
                            "security_exceptions": [],
                        }
                    )
                    plan.created_roles[chosen_role] = (
                        persona.suggested_role or chosen_role
                    )
                if persona.actor_id in existing_actor_ids:
                    raise DuplicateIdError(
                        f"An actor with id '{persona.actor_id}' already exists"
                    )
                existing_actor_ids.add(persona.actor_id)
                actors.append(
                    {
                        "id": persona.actor_id,
                        "role": chosen_role,
                        "telegram_bot": None,
                        "tools": [],
                        "tools_excluded": [],
                        "actor_exceptions": [],
                        "tone": None,
                    }
                )

        _save(org_path, doc)  # one backup, one atomic replace

    click.secho(f"\nOrganization written: {org_path}", fg="green")
    click.echo("Next: `pf validate` and `pf build`, then `pf deploy`.")
    return org_path
