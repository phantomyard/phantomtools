"""
`po new-org` — creates a minimal but valid organizations/<id>/org.yaml
against the schema. Without `--template`, it starts with a single
department ("Management") ready for add-department/add-role/add-actor to
complete it. With `--template <sector>`, it starts with the typical
departments of that sector (see templates.py).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..spec.shape_validator import is_valid_identifier
from .templates import departments_for


def new_org(
    org_id: str,
    name: str,
    sector: str,
    languages: list[str],
    base_dir: Path = Path("organizations"),
    template: str | None = None,
) -> Path:
    if not is_valid_identifier(org_id):
        raise ValueError(f"invalid org id {org_id!r}: must match ^[a-z0-9][a-z0-9_-]*$")
    org_dir = base_dir / org_id
    org_dir.mkdir(parents=True, exist_ok=True)
    org_path = org_dir / "org.yaml"

    if org_path.exists():
        raise FileExistsError(f"{org_path} already exists")

    departments = (
        departments_for(template)
        if template
        else [
            {
                "id": "direccion",
                "name": "Management",
                "parent": None,
                "access_policy": "level-3",
            }
        ]
    )

    doc = {
        "version": 1,
        "organization": {
            "id": org_id,
            "name": name,
            "sector": sector,
            "languages": languages,
            "default_language": languages[0] if languages else "en",
        },
        "departments": departments,
        "roles": [],
        "actors": [],
        "humans": [],
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
        "escalation_matrix": [],
        "communication": {
            "request_id_format": "{org_id}-{yyyymmdd}-{seq4}",
            "message_types": ["REQUEST", "INFORM", "ESCALATE", "CONFIRM", "REJECT"],
            "max_hops": 3,
        },
    }

    # note: roles/actors start empty, which makes the schema fail
    # (minItems: 1) until the first role/actor is added — this is
    # deliberate: `po validate` must fail on a half-created organization
    # and guide the user to complete add-role / add-actor before trying
    # to build.
    with open(org_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)

    return org_path
