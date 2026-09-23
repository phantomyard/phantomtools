"""A0 containment evidence tooling.

Read-only inventory collector (``collector``) plus schema and cross-check
validators (``validator``) for the contracts in ``docs/containment``.
"""

from __future__ import annotations

from .collector import CollectorError, collect_inventory, inventory_digest
from .validator import cross_check_errors, detect_kind, schema_errors

__all__ = [
    "CollectorError",
    "collect_inventory",
    "cross_check_errors",
    "detect_kind",
    "inventory_digest",
    "schema_errors",
]
