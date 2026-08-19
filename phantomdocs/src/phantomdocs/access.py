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

# The PhantomOrg org.yaml schema version PhantomDocs resolves access from.
# PhantomOrg's own model declares ``version: 1`` (top-level int) and validates
# it as ``Organization.version``. PhantomDocs does not assume forward
# compatibility: an unknown or missing version is refused, not tolerated.
REQUIRED_ORG_SCHEMA_VERSION = 1


def validate_org_schema(org: dict[str, Any]) -> None:
    """Fail-closed: the org.yaml must declare the schema version PhantomDocs
    resolves access from.

    A missing or different ``version`` is refused (rather than assumed
    compatible) so that a future PhantomOrg schema bump surfaces loudly here
    instead of silently mis-resolving access.
    """
    version = org.get("version")
    if version != REQUIRED_ORG_SCHEMA_VERSION:
        raise ValueError(
            f"org.yaml schema version must be {REQUIRED_ORG_SCHEMA_VERSION} "
            f"(got {version!r}); PhantomDocs resolves access from PhantomOrg's "
            "version-1 org model and refuses unknown schema versions "
            "(fail-closed)"
        )


def load_org(org_yaml_path: str) -> dict[str, Any]:
    with open(org_yaml_path, "r", encoding="utf-8") as f:
        org = yaml.safe_load(f) or {}
    validate_org_schema(org)
    return org


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


def _actor_role_id(org: dict[str, Any], actor_id: str) -> str | None:
    """The role id an actor holds, or None if the actor is unknown."""
    for a in org.get("actors", []):
        if a.get("id") == actor_id:
            return a.get("role")
    return None


def can_read(org: dict[str, Any], actor_id: str, category: int) -> bool:
    """True iff the actor's resolved access covers the given category."""
    return category in resolved_categories(org, actor_id)


def can_write(
    org: dict[str, Any], actor_id: str, category: int, owners: list[str] | None = None
) -> bool:
    """True iff the actor may write a node of ``category`` (spec §9).

    Write requires, in order (fail-closed, "no rule -> denied"):

    1. An explicit, non-empty ``owners`` list. PhantomDocs v1 does NOT
       re-derive PhantomOrg's reporting-chain default write scope (§9's
       "otherwise the actors in the same reporting chain"); it requires
       owners to be declared and denies when they are not. This avoids a
       second, drifting implementation of PhantomOrg's scope policy.
    2. Read access to the category (an actor who cannot read cannot write).
    3. Membership in ``owners`` by actor id OR by role id — PhantomOrg's
       ``owners`` field accepts both role ids and actor ids, so a role-owned
       node must be writable by every actor holding that role.
    """
    owners = list(owners or [])
    if not owners:
        return False
    if not can_read(org, actor_id, category):
        return False
    if actor_id in owners:
        return True
    role_id = _actor_role_id(org, actor_id)
    return bool(role_id) and role_id in owners
