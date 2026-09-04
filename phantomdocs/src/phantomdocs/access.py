"""Access resolution from a PhantomOrg org.yaml (spec §9).

PhantomDocs does NOT define its own ACL: it consumes PhantomOrg's
`policies.access_levels` + `policies.security_categories` and the role/actor
exception fields, and checks whether an actor's resolved access covers a
node's category. Fail-closed: no rule -> denied.

Categories are hierarchical (issue #45): the hierarchy is declared in
``policies.security_categories``, where each category carries an optional
``scope`` and ``owner``. A category grants its declared descendants (the
longest declared ``-``-prefix parent links declared categories); the prefix
is no longer a blind grant on arbitrary strings. An undeclared category is
denied (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import warnings
from typing import Any

import yaml

from .signing import npub_to_pubkey_hex

# The PhantomOrg org.yaml schema version PhantomDocs resolves access from.
# PhantomOrg's own model declares ``version: 1`` (top-level int) and validates
# it as ``Organization.version``. PhantomDocs does not assume forward
# compatibility: an unknown or missing version is refused, not tolerated.
REQUIRED_ORG_SCHEMA_VERSION = 1

# A category exception id: `category-<digits>` optionally followed by one or
# more `-<scope>` segments (`category-4-almaponia`). Anything that does not
# match is NOT a category grant and is ignored fail-closed.
_CATEGORY_ID_RE = re.compile(r"^category-\d+(?:-[A-Za-z0-9][A-Za-z0-9_-]*)*$")


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


def policy_hash(org: dict[str, Any]) -> str:
    """Canonical digest of the org model that authorizes a mutation (audit #7).

    Deterministic JSON (sorted keys, compact separators, ASCII-escaped) of the
    parsed org model, SHA-256 hashed. Two org models that differ in any
    authorization-relevant way (roles, actors, keys, categories, exceptions)
    produce different digests, so a node records *which* policy version
    authorized it — a mutation carries ``policyHash = Y`` and an auditor can
    later prove provenance without trusting the current org model.
    """
    canonical = json.dumps(
        org, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def actor_key_records(org: dict[str, Any], actor_id: str) -> list[dict[str, Any]]:
    """The actor's declared signing keys as lifecycle records (issue #76).

    Each record: ``{npub, valid_from, valid_until, revoked_at}``, where the
    timestamps are ISO-8601 UTC strings (or None). The actor's ``npub`` is the
    active key; optional ``keys`` entries add rotation windows and revocations
    (key A valid T0–T1, key B valid from T1, key X revoked at T).
    """
    actor = next((a for a in org.get("actors", []) if a.get("id") == actor_id), None)
    if not actor:
        return []
    records: list[dict[str, Any]] = []
    key_map: dict[str, dict[str, Any]] = {}
    for k in actor.get("keys") or []:
        if isinstance(k, dict) and k.get("npub"):
            key_map[k["npub"]] = {
                "npub": k["npub"],
                "valid_from": k.get("valid_from"),
                "valid_until": k.get("valid_until"),
                "revoked_at": k.get("revoked_at"),
            }
    npub = actor.get("npub")
    if npub and isinstance(npub, str) and npub.strip():
        if npub in key_map:
            records.append(key_map.pop(npub))
        else:
            records.append(
                {
                    "npub": npub,
                    "valid_from": None,
                    "valid_until": None,
                    "revoked_at": None,
                }
            )
    # Historical keys (rotation/revocation) that are not the active npub.
    records.extend(key_map.values())
    return records


def key_valid_at(org: dict[str, Any], actor_id: str, pubkey_hex: str, ts: str) -> bool:
    """True iff ``pubkey_hex`` is a declared key for ``actor_id`` that was
    valid at ``ts`` — not yet revoked, within its ``valid_from``/``valid_until``
    window. ``ts`` is an ISO-8601 UTC string (lexicographic comparison)."""
    for rec in actor_key_records(org, actor_id):
        try:
            if npub_to_pubkey_hex(rec["npub"]) != pubkey_hex:
                continue
        except ValueError:
            continue
        revoked_at = rec.get("revoked_at")
        if revoked_at and ts >= revoked_at:
            return False
        valid_from = rec.get("valid_from")
        if valid_from and ts < valid_from:
            continue
        valid_until = rec.get("valid_until")
        if valid_until and ts >= valid_until:
            continue
        return True
    return False


def key_valid_now(org: dict[str, Any], actor_id: str, pubkey_hex: str) -> bool:
    """True iff the key is a currently-valid (non-revoked) key for the actor."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return key_valid_at(org, actor_id, pubkey_hex, now)


def normalize_category(category: int | str) -> str:
    """Canonical ``category-...`` id for a category reference.

    Accepts an int (``2``), a numeric string (``"2"``) or an explicit id
    (``"category-2"``, ``"category-4-almaponia"``). Anything else is returned
    verbatim (it will simply never match a resolved id).
    """
    if isinstance(category, int):
        return f"category-{category}"
    text = str(category).strip()
    if text.startswith("category-"):
        return text
    if text.isdigit():
        return f"category-{text}"
    return text


def _exception_category_id(exc: Any) -> str | None:
    """The category id a role/actor exception grants, or None.

    A grant is a ``category-...`` id (``category-3``, ``category-4``,
    ``category-4-almaponia``). Bare integers are also accepted. Free-text /
    structured exceptions (e.g. prose) are NOT category grants.
    """
    if isinstance(exc, str):
        exc = exc.strip()
        if _CATEGORY_ID_RE.match(exc):
            return exc
    elif isinstance(exc, int):
        return f"category-{exc}"
    return None


def resolved_categories(org: dict[str, Any], actor_id: str) -> list[str]:
    """Category ids an actor may read, after RBAC base + role/actor exceptions."""
    actors = {a.get("id"): a for a in org.get("actors", [])}
    roles = {r.get("id"): r for r in org.get("roles", [])}
    levels = org.get("policies", {}).get("access_levels", {})

    actor = actors.get(actor_id)
    if not actor:
        return []
    role = roles.get(actor.get("role"), {})

    categories: set[str] = set()
    level = levels.get(role.get("access_level"), {})
    cats = level.get("categories")
    if not isinstance(cats, list):
        cats = []  # non-list categories (None/string/...) grant nothing — fail closed
    for cat in cats:
        categories.add(normalize_category(cat))

    for exc in list(role.get("security_exceptions") or []) + list(
        actor.get("actor_exceptions") or []
    ):
        cat_id = _exception_category_id(exc)
        if cat_id:
            categories.add(cat_id)
        elif exc:
            # Not a category grant; carried as prose, not an access grant.
            warnings.warn(
                f"actor {actor_id!r}: exception {exc!r} is not a 'category-...' "
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


def root_role_ids(org: dict[str, Any]) -> list[str]:
    """Role ids at the top of the reporting hierarchy (the org's root).

    A role whose ``reports_to`` is unset/null is the root (top of the
    hierarchy, e.g. ``ceo``). These are the only roles authorized to
    administer the namespace security profile. Fail-closed: an org model
    that declares no root role authorizes nobody.
    """
    roots: list[str] = []
    for r in org.get("roles", []):
        if isinstance(r, dict) and r.get("id") and not r.get("reports_to"):
            roots.append(r["id"])
    return roots


def can_administer_namespace(org: dict[str, Any], actor_id: str) -> bool:
    """True iff ``actor_id`` is authorized to administer the namespace profile.

    Administration (changing the namespace-wide signing profile) is restricted
    to the root role(s) — the top of the reporting hierarchy. Authorization is
    distinct from authentication: a declared actor holding a valid key is
    *authenticated*, but only a root-role actor is *authorized* to change the
    signing profile (audit #1). Fail-closed: an unknown actor, an actor with
    no role, or a non-root role is denied.
    """
    role_id = _actor_role_id(org, actor_id)
    if not role_id:
        return False
    return role_id in root_role_ids(org)


def _security_categories(org: dict[str, Any]) -> dict[str, Any]:
    """The declared ``policies.security_categories`` map (id -> spec)."""
    return org.get("policies", {}).get("security_categories", {}) or {}


def _category_parents(org: dict[str, Any]) -> dict[str, str | None]:
    """Map each declared category id to its declared parent id.

    The parent is the longest declared proper ``-``-prefix, so the prefix only
    links *declared* categories (issue #45): the hierarchy is an explicit
    relation over ``security_categories`` (which carries ``scope``/``owner``),
    not a blind string match. An undeclared category has no parent.
    """
    declared = set(_security_categories(org))
    tree: dict[str, str | None] = {}
    for cid in declared:
        candidates = [d for d in declared if d != cid and cid.startswith(d + "-")]
        tree[cid] = max(candidates, key=len) if candidates else None
    return tree


def can_read(org: dict[str, Any], actor_id: str, category: int | str) -> bool:
    """True iff the actor's resolved access covers ``category``.

    A direct grant covers the category itself. Otherwise the category is
    granted when some resolved category is a *declared ancestor* of it — the
    hierarchy comes from ``policies.security_categories`` (issue #45), so an
    undeclared category is denied (fail-closed).
    """
    cat = normalize_category(category)
    resolved = set(resolved_categories(org, actor_id))
    if cat in resolved:
        return True
    tree = _category_parents(org)
    if cat not in tree:
        return False
    current = tree.get(cat)
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        if current in resolved:
            return True
        current = tree.get(current)
    return False


def can_write(
    org: dict[str, Any],
    actor_id: str,
    category: int | str,
    owners: list[str] | None = None,
) -> bool:
    """True iff the actor may write a node of ``category`` (spec §9).

    Write requires, in order (fail-closed, "no rule -> denied"):

    1. An explicit, non-empty ``owners`` list. PhantomDocs v1 does NOT
       re-derive PhantomOrg's reporting-chain default write scope (§9's
       "otherwise the actors in the same reporting chain"); it requires
       owners to be declared and denies when they are not.
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
