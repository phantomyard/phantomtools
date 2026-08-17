"""
`pf setup` — guided installation of PhantomForge over an existing (or new)
phantombot installation.

The wizard answers two questions once, then it is done:

1. Where do the personas live?  (detected or asked)
2. How is the organization structured?  (departments, then every persona
   gets a department + role)

Existing personas are reassigned first (that is the priority); afterwards
the user may add brand-new personas. Works for installations with one or
many personas — the user decides.

This module holds the PURE logic (no click): `collect_setup` produces a
`SetupPlan` that the CLI can either apply directly (flag mode) or feed
into an interactive prompt loop. Keeping it pure makes the whole flow
testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _slugify(text: str) -> str:
    """Normalizes free text into a valid identifier (see slugify_id)."""
    from ..spec.shape_validator import slugify_id

    return slugify_id(text)


def find_personas_dirs(personas_root: Path) -> list[Path]:
    """Subdirectories of a phantombot personas root that look like a persona
    (contain SOUL.md or IDENTITY.md, or are non-hidden with content)."""
    if not personas_root.is_dir():
        return []
    found = []
    for child in sorted(personas_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "SOUL.md").is_file() or (child / "IDENTITY.md").is_file():
            found.append(child)
    return found


@dataclass
class PersonaPlan:
    """One persona to place into the organization."""

    actor_id: str
    suggested_role: str | None = None
    suggested_department: str | None = None
    role_id: str | None = None  # chosen by the user during setup
    department_id: str | None = None  # chosen by the user during setup
    is_new: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class SetupPlan:
    """Everything needed to write the org.yaml and deploy."""

    org_path: Path
    org_id: str
    org_name: str
    sector: str
    languages: list[str]
    departments: list[dict]  # [{id, name, parent, access_policy}]
    personas: list[PersonaPlan]
    create_new_org: bool  # True -> write a fresh org.yaml; False -> mutate existing
    # Pre-existing roles to reuse (id -> name) when the user accepts the suggestion.
    existing_roles: dict[str, str] = field(default_factory=dict)
    existing_departments: list[str] = field(default_factory=list)
    # Roles created during this setup run (id -> role name), so that
    # several personas sharing a suggested role reuse it instead of
    # creating duplicates.
    created_roles: dict[str, str] = field(default_factory=dict)

    @property
    def pending_personas(self) -> list[PersonaPlan]:
        return [p for p in self.personas if not p.department_id or not p.role_id]


def build_org_yaml(plan: SetupPlan) -> dict:
    """Turn a SetupPlan into the org.yaml document (roles created per
    persona, actors linked). Reuses the same structure `pf new-org`
    writes, so `pf validate` accepts it immediately."""
    roles: list[dict] = []
    actors: list[dict] = []

    for persona in plan.personas:
        role_id = persona.role_id or f"{persona.actor_id}_role"
        # Reuse an existing role when the user accepted the suggestion, or a
        # role already created during this same setup run (several personas
        # sharing a suggested role).
        if role_id in plan.existing_roles or role_id in plan.created_roles:
            role_ref = role_id
        else:
            role_name = persona.suggested_role or role_id
            roles.append(
                {
                    "id": role_id,
                    "name": role_name,
                    "department": persona.department_id,
                    "reports_to": None,
                    "reports_to_human": None,
                    "functions": [],
                    "access_level": "level-2",
                    "security_exceptions": [],
                }
            )
            plan.created_roles[role_id] = role_name
            role_ref = role_id
        actors.append(
            {
                "id": persona.actor_id,
                "role": role_ref,
                "telegram_bot": None,
                "tools": [],
                "tools_excluded": [],
                "actor_exceptions": [],
                "tone": None,
            }
        )

    return {
        "version": 1,
        "organization": {
            "id": plan.org_id,
            "name": plan.org_name,
            "sector": plan.sector,
            "languages": plan.languages,
            "default_language": plan.languages[0] if plan.languages else "en",
        },
        "departments": list(plan.departments),
        "roles": roles,
        "actors": actors,
        "policies": {
            "access_levels": {
                "level-3": {"label": "Executive", "categories": [1, 2, 3]},
                "level-2": {"label": "Operational", "categories": [1, 2]},
                "level-1": {"label": "Restricted", "categories": [1]},
            },
            "security_categories": {
                "category-1": {"label": "Public / low internal"},
                "category-2": {"label": "Confidential"},
                "category-3": {"label": "Credentials / sensitive financial"},
            },
        },
        "humans": [],
        "escalation_matrix": [],
        "communication": {
            "request_id_format": "{org_id}-{yyyymmdd}-{seq4}",
            "message_types": ["REQUEST", "INFORM", "ESCALATE", "CONFIRM", "REJECT"],
            "max_hops": 3,
        },
    }
