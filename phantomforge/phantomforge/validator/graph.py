"""
Check that escalation_matrix is a directed acyclic graph (DAG).

A cycle (A escalates to B, B escalates to A, directly or transitively) is
exactly the failure CrewAI documents in production when hierarchical
delegation has no limit: the agent enters a loop without resolution.
PhantomForge detects it at `validate` time, before it can end up in a
generated SOUL.md.
"""

from __future__ import annotations

from ..spec.model import OrgSpec


class EscalationCycleError(Exception):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__("Cycle detected in escalation_matrix: " + " -> ".join(cycle))


def _edges(spec: OrgSpec) -> dict[str, list[str]]:
    """Graph role -> [roles it escalates to]. Ignores the wildcard '*'."""
    graph: dict[str, list[str]] = {r.id: [] for r in spec.roles}
    for entry in spec.escalation_matrix:
        if entry.from_ == "*":
            continue
        graph.setdefault(entry.from_, []).append(entry.to)
    return graph


def check_no_cycles(spec: OrgSpec) -> None:
    """Raises EscalationCycleError if there is a cycle; otherwise returns nothing."""
    graph = _edges(spec)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    path: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in color:
                # Reference to a non-existent role: not a cycle, but a
                # different error that refs.py must catch; skip it here.
                continue
            if color[neighbor] == GRAY:
                cycle_start = path.index(neighbor)
                raise EscalationCycleError(path[cycle_start:] + [neighbor])
            if color[neighbor] == WHITE:
                visit(neighbor)
        path.pop()
        color[node] = BLACK

    for node in list(graph.keys()):
        if color[node] == WHITE:
            visit(node)


def max_hops_to_root(spec: OrgSpec, role_id: str) -> int:
    """
    Length of the longest escalation path from role_id to a root role
    (reports_to is None) or to a hop marked with reports_to_human.
    Used to warn if a chain could exceed communication.max_hops.
    """
    graph = _edges(spec)
    seen: set[str] = set()
    hops = 0
    current = role_id
    while graph.get(current):
        if current in seen:
            # a cycle should already have been detected before; we
            # protect ourselves anyway
            break
        seen.add(current)
        current = graph[current][0]
        hops += 1
    return hops
