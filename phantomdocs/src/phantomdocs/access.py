"""Access resolution from a PhantomOrg org.yaml (spec §9).

PhantomDocs does NOT define its own ACL: it consumes PhantomOrg's
`policies.access_levels` + `policies.security_categories` and the role/actor
exception fields, and checks whether an actor's resolved access covers a
node's category. Fail-closed: no rule -> denied.
"""

from __future__ import annotations

import warnings
from typing import Any

import yaml


def load_org(org_yaml_path: str) -> dict[str, Any]:
    with open(org_yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolved_categories(org: dict[str, Any], actor_id: str) -> list[int]:
    """Categories an actor may read, after RBAC base + role/actor exceptions."""
    actors = {a.get("id"): a for a in org.get("actors", [])}
    roles = {r.get("id"): r for r in org.get("roles", [])}
    levels = org.get("policies", {}).get("access_levels", {})

    actor = actors.get(actor_id)
    if not actor:
        return []
    role = roles.get(actor.get("role"), {})

    categories: set[int] = set()
    level = levels.get(role.get("access_level"), {})
    categories.update(level.get("categories", []))

    for exc in list(role.get("security_exceptions", [])) + list(
        actor.get("actor_exceptions", [])
    ):
        if isinstance(exc, str) and exc.startswith("category-"):
            num = exc[len("category-") :]
            if num.isdigit():
                categories.add(int(num))
            else:
                warnings.warn(
                    f"actor {actor_id!r}: exception {exc!r} does not parse as "
                    "'category-<N>' and is ignored (fail-closed)",
                    stacklevel=2,
                )
        elif exc:
            # Free-text / structured exceptions are NOT category grants in
            # PhantomOrg's resolver; they are carried as prose. Fail-closed:
            # don't grant anything from them, but don't fail silently either.
            warnings.warn(
                f"actor {actor_id!r}: exception {exc!r} is not a 'category-<N>' "
                "grant and is ignored (fail-closed)",
                stacklevel=2,
            )
    return sorted(categories)


def can_read(org: dict[str, Any], actor_id: str, category: int) -> bool:
    """True iff the actor's resolved access covers the given category."""
    return category in resolved_categories(org, actor_id)


def can_write(
    org: dict[str, Any], actor_id: str, category: int, owners: list[str] | None = None
) -> bool:
    """True iff the actor may write a node of ``category``.

    Write requires read access to the category AND, when the node declares
    explicit ``owners``, membership in that list. No rule -> denied.
    """
    if not can_read(org, actor_id, category):
        return False
    if owners:
        return actor_id in owners
    return True
