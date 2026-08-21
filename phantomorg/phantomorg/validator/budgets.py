"""
Size budget check on already generated files.
It runs after the compile, not on the spec itself.

- SOUL.md: per-role budget (role.soul_line_budget, default 300 lines).
- MEMORY.md: <2 KB, following the limit documented by persona loaders
  (e.g., the Phantombot loader: "keep under a few KB").
"""

from __future__ import annotations

from pathlib import Path

MEMORY_MAX_BYTES = 2 * 1024


def check_soul_budget(soul_path: Path, line_budget: int) -> str | None:
    lines = soul_path.read_text(encoding="utf-8").splitlines()
    if len(lines) > line_budget:
        return (
            f"{soul_path.name}: {len(lines)} lines, exceeds the budget "
            f"of {line_budget} for this role"
        )
    return None


def check_memory_budget(memory_path: Path) -> str | None:
    size = memory_path.stat().st_size
    if size > MEMORY_MAX_BYTES:
        return (
            f"{memory_path.name}: {size} bytes, exceeds the limit of "
            f"{MEMORY_MAX_BYTES} bytes (~2 KB)"
        )
    return None
