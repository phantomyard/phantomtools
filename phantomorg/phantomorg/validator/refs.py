"""
Cross-reference check: every id mentioned anywhere else in the spec must
exist. This is what prevents gap G4 detected in the Aquaponics United
audit (escalation paths to "Salvador" or "Fran" that didn't correspond to
any declared role).
"""

from __future__ import annotations

from ..spec.model import OrgSpec


class ReferenceError_(Exception):
    """Uses a different suffix so it doesn't clash with the builtin ReferenceError."""


def check_role_hierarchy_cycles(spec: OrgSpec) -> list[str]:
    """Detect cycles in the ``reports_to`` hierarchy (WHITE/GRAY/BLACK DFS).

    A role that (directly or transitively) reports to itself — ``A``
    reports to ``B`` and ``B`` reports to ``A``, or any longer loop —
    makes the standing org structure ambiguous (who is on top?) and
    breaks any upward walk over ``reports_to`` (escalation-to-human,
    scope derivation, hierarchy rendering).

    This is the role-hierarchy analogue of the escalation-matrix DAG
    check in ``graph.py``: the two graphs are independent (escalation is
    role→role on demand; ``reports_to`` is the standing org structure).
    ``reports_to`` is a single optional parent per role, so the graph is
    functional (out-degree ≤ 1) and its cycles are disjoint — each is
    reported exactly once.
    """
    edges: dict[str, str | None] = {r.id: r.reports_to for r in spec.roles}
    role_ids = set(edges)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in role_ids}
    stack: list[str] = []
    problems: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        parent = edges.get(node)
        if parent is not None:
            if parent in color:
                if color[parent] == GRAY:
                    start = stack.index(parent)
                    cycle = stack[start:] + [parent]
                    problems.append(
                        "roles: reports_to cycle detected: " + " -> ".join(cycle)
                    )
                elif color[parent] == WHITE:
                    visit(parent)
            # parent not in color => references a non-existent role, which
            # is a different error that check_references already reports
            # ("roles.<id>: reports_to '<x>' does not exist").
        stack.pop()
        color[node] = BLACK

    for node in sorted(role_ids):
        if color[node] == WHITE:
            visit(node)
    return problems


def check_references(spec: OrgSpec) -> list[str]:
    """Returns a list of problems found (empty if everything is fine)."""
    problems: list[str] = []

    # Duplicate ids: defense in depth. add_department/add_role/add_actor
    # already prevent this when adding (DuplicateIdError), but this covers
    # the case of someone editing org.yaml by hand.
    groups: list[tuple[str, list]] = [
        ("departments", spec.departments),
        ("roles", spec.roles),
        ("actors", spec.actors),
        ("humans", spec.humans),
    ]
    for label, items in groups:
        seen: dict[str, int] = {}
        for item in items:
            seen[item.id] = seen.get(item.id, 0) + 1
        for item_id, count in seen.items():
            if count > 1:
                problems.append(
                    f"{label}: id '{item_id}' appears {count} times (must be unique)"
                )

    # Global id uniqueness: an actor, role or department sharing the same id
    # would make hand-edited YAML ambiguous (which entity does 'ceo' mean?).
    # References resolve within their own group, so this is not a correctness
    # bug, but the spec treats ids as org-wide identifiers.
    all_ids: dict[str, str] = {}  # id -> first group that declared it
    for label, items in groups:
        for item in items:
            if item.id in all_ids and all_ids[item.id] != label:
                problems.append(
                    f"{label}: id '{item.id}' collides with "
                    f"{all_ids[item.id]} (ids must be unique org-wide)"
                )
            else:
                all_ids.setdefault(item.id, label)

    dept_ids = {d.id for d in spec.departments}
    role_ids = {r.id for r in spec.roles}
    access_level_ids = set(spec.policies.access_levels.keys())
    security_category_ids = set(spec.policies.security_categories.keys())

    # departments: parent must exist (or be null)
    for d in spec.departments:
        if d.parent is not None and d.parent not in dept_ids:
            problems.append(f"departments.{d.id}: parent '{d.parent}' does not exist")
        if d.access_policy not in access_level_ids:
            problems.append(
                f"departments.{d.id}: access_policy '{d.access_policy}' "
                f"is not in policies.access_levels"
            )

    # roles: department, reports_to, access_level, security_exceptions
    for r in spec.roles:
        if r.department not in dept_ids:
            problems.append(f"roles.{r.id}: department '{r.department}' does not exist")
        if r.reports_to is not None and r.reports_to not in role_ids:
            problems.append(f"roles.{r.id}: reports_to '{r.reports_to}' does not exist")
        if r.access_level not in access_level_ids:
            problems.append(
                f"roles.{r.id}: access_level '{r.access_level}' "
                f"is not in policies.access_levels"
            )
        for exc in r.security_exceptions:
            if exc not in security_category_ids:
                problems.append(
                    f"roles.{r.id}: security_exceptions '{exc}' "
                    f"is not in policies.security_categories"
                )

    # actors: role, actor_exceptions
    seen_bots: dict[str, str] = {}
    seen_npubs: dict[str, str] = {}
    for a in spec.actors:
        if a.role not in role_ids:
            problems.append(f"actors.{a.id}: role '{a.role}' does not exist")
        for exc in a.actor_exceptions:
            if exc not in security_category_ids:
                problems.append(
                    f"actors.{a.id}: actor_exceptions '{exc}' "
                    f"is not in policies.security_categories"
                )
        if a.telegram_bot:
            if a.telegram_bot in seen_bots:
                problems.append(
                    f"actors.{a.id}: telegram_bot '{a.telegram_bot}' "
                    f"duplicated with actors.{seen_bots[a.telegram_bot]}"
                )
            else:
                seen_bots[a.telegram_bot] = a.id
        if a.npub:
            if a.npub in seen_npubs:
                problems.append(
                    f"actors.{a.id}: npub duplicated with actors.{seen_npubs[a.npub]} "
                    f"(each actor must have its own Nostr identity)"
                )
            else:
                seen_npubs[a.npub] = a.id

    # escalation_matrix: from/to must exist (from allows '*') and pairs must
    # not be duplicated (the compiler emits one escalation path per entry, so
    # a repeated from->to would silently duplicate the path in the SOUL).
    seen_escalations: set[tuple[str, str]] = set()
    for e in spec.escalation_matrix:
        pair = (e.from_, e.to)
        if pair in seen_escalations:
            problems.append(
                f"escalation_matrix: duplicate entry {e.from_} -> {e.to} "
                f"(appears more than once)"
            )
        else:
            seen_escalations.add(pair)
        if e.from_ != "*" and e.from_ not in role_ids:
            problems.append(f"escalation_matrix: from '{e.from_}' does not exist")
        if e.to not in role_ids:
            problems.append(f"escalation_matrix: to '{e.to}' does not exist")
        if e.cross_department and e.from_ != "*":
            from_role = next((r for r in spec.roles if r.id == e.from_), None)
            to_role = next((r for r in spec.roles if r.id == e.to), None)
            if from_role and to_role and from_role.department == to_role.department:
                problems.append(
                    f"escalation_matrix: entry {e.from_} -> {e.to} is marked "
                    f"cross_department but both roles are in the same department"
                )

    problems.extend(check_role_hierarchy_cycles(spec))

    return problems


def suggest_role_level_exceptions(spec: OrgSpec) -> list[str]:
    """
    Anti 'role explosion' heuristic (section 5.5 of the spec): if all the
    actors occupying a role share the same actor_exceptions, suggest moving
    it to the role's security_exceptions instead of repeating it per actor.
    """
    suggestions: list[str] = []
    by_role: dict[str, list[set[str]]] = {}
    for a in spec.actors:
        by_role.setdefault(a.role, []).append(set(a.actor_exceptions))

    for role_id, exception_sets in by_role.items():
        if len(exception_sets) < 2:
            continue
        common = set.intersection(*exception_sets)
        if common:
            suggestions.append(
                f"roles.{role_id}: all its actors share the exception(s) "
                f"{sorted(common)} — consider moving it/them to security_exceptions "
                f"of the role instead of repeating it/them in each actor"
            )
    return suggestions
