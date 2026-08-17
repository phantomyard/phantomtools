"""
Python 3.10 compatibility guard.

`pyproject.toml` declares ``requires-python = ">=3.10"``. CI only runs
3.12, so a 3.11+-only API can slip back in unnoticed. This test pins the
known offenders at source level.

Regression: compiler F1 — ``build.py`` used ``datetime.UTC`` (added in
Python 3.11), which raises AttributeError on import/runtime under 3.10.
Fixed to ``datetime.timezone.utc`` (available since 3.2).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# Production code only: tests run on the CI interpreter, so a 3.11+-only
# API in tests cannot break a 3.10 install of the package.
SOURCE_DIRS = ("phantomforge",)

# 3.11+-only stdlib names that must never appear in source while
# requires-python is ">=3.10". Add offenders here as they are found.
FORBIDDEN_311_PLUS = (
    # datetime.UTC alias was added in Python 3.11
    "datetime.UTC",
)


def _iter_source_files():
    for dirname in SOURCE_DIRS:
        base = REPO_ROOT / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_no_311_plus_apis_in_source():
    offenders: list[tuple[Path, str]] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_311_PLUS:
            if name in text:
                offenders.append((path, name))
    assert not offenders, [
        f"{path}: {name} is Python 3.11+ only (requires-python >= 3.10)"
        for path, name in offenders
    ]


def test_requires_python_still_supports_310():
    """Guard that the declared floor is still 3.10 — if it ever moves up,
    this test must move with it (and the forbidden list can shrink)."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
