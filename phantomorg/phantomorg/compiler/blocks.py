"""
Block-based merge, not whole-file merge.

A generated file (SOUL.md, IDENTITY.md, tools.md) contains sections
delimited like this:

    <!-- FORJA:BEGIN security -->
    ...content derived from org.yaml...
    <!-- FORJA:END security -->

Merge rule on regeneration:
- Everything INSIDE a FORJA:BEGIN/END block is always replaced by the
  freshly rendered version — it is content derived from the spec, you
  don't hand-edit inside it.
- Everything OUTSIDE the blocks (before, between, or after them) is
  preserved exactly as it was in the existing file — that is where any
  manual annotation lives.
- If the existing file contains NO FORJA block at all (for example,
  someone deliberately deleted all the markers), it is interpreted as
  "this file has opted out of automatic generation" and is not touched
  at all.
- If org.yaml changes so that a new block appears that didn't exist in
  the file, it is appended at the end. If a block no longer makes sense
  (very rare, but possible), it is replaced by the empty/current version.

This replaces the previous "whole file frozen by [FORJA:manual]"
mechanism, which also froze the spec-derived sections
(security/escalation/comms) — the real gap reported after the pilot on
the United Capital Group VPS.
"""

from __future__ import annotations

import re
import warnings

# NOTE: `\r?\n?` (instead of a bare `\n`) makes the markers tolerant to
# CRLF line endings (F5): a file edited on Windows or rewritten by
# another tool must not silently stop matching, which would freeze the
# spec-derived content forever.
_BLOCK_RE = re.compile(
    r"<!-- FORJA:BEGIN (?P<name>[\w:.-]+) -->\r?\n?"
    r"(?P<body>.*?)"
    r"<!-- FORJA:END (?P=name) -->\r?\n?",
    re.DOTALL,
)

_BEGIN_RE = re.compile(r"<!-- FORJA:BEGIN (?P<name>[\w:.-]+) -->\r?\n?")
_END_RE = re.compile(r"<!-- FORJA:END (?P<name>[\w:.-]+) -->\r?\n?")
# Matches either marker, capturing the kind (BEGIN/END) and the name. Used
# by the stack-based nesting check (_structurally_sound). CRLF-tolerant so
# a Windows-edited file is validated exactly like an LF file.
_MARKER_RE = re.compile(
    r"<!-- FORJA:(?P<kind>BEGIN|END) (?P<name>[\w:.-]+) -->\r?\n?"
)


def extract_blocks(content: str) -> dict[str, str]:
    """Returns {block_name: body} for all the FORJA blocks in a text."""
    return {m.group("name"): m.group("body") for m in _BLOCK_RE.finditer(content)}


def has_blocks(content: str) -> bool:
    """True if the text contains at least one FORJA:BEGIN marker."""
    return _BEGIN_RE.search(content) is not None


def _structurally_sound(content: str) -> bool:
    """Stack-based check that BEGIN/END markers nest correctly.

    Catches crossed/interleaved nesting (``BEGIN a, BEGIN b, END a,
    END b``) that the name-list comparison in ``merge_content`` cannot
    see (the ordered lists of names are equal in the crossed case). Also
    catches a stray END (stack underflow) and an unclosed BEGIN (non-empty
    stack at the end). A well-formed file returns True; anything else
    means the merge is ambiguous and must be preserved whole.
    """
    stack: list[str] = []
    for m in _MARKER_RE.finditer(content):
        if m.group("kind") == "BEGIN":
            stack.append(m.group("name"))
        else:  # END
            if not stack:
                return False  # stray END (underflow)
            if stack[-1] != m.group("name"):
                return False  # crossed / interleaved names
            stack.pop()
    return not stack  # False when a BEGIN was never closed


def _find_block(content: str, name: str) -> tuple[int, int] | None:
    """Span (start, end) of the block named ``name`` in ``content``.

    Uses the LAST END marker after the first BEGIN, so a literal
    ``<!-- FORJA:END <name> -->`` inside a generated body (F6) is not
    mistaken for the real closing marker and the block is never
    truncated.
    """
    begin = f"<!-- FORJA:BEGIN {name} -->"
    end = f"<!-- FORJA:END {name} -->"
    start = content.find(begin)
    if start < 0:
        return None
    end_pos = content.rfind(end, start + len(begin))
    if end_pos < 0:
        return None
    finish = end_pos + len(end)
    if finish < len(content) and content[finish] == "\n":
        finish += 1
    return start, finish


def _merge_ambiguous(existing: str) -> str:
    """F4: duplicate or unbalanced markers make the merge ambiguous.

    A manual annotation quoting a well-formed FORJA pair is legitimate
    content; replacing every marker match would silently destroy it.
    When we cannot tell the real generated blocks apart from quoted
    ones, preserve the whole file and warn instead of guessing.
    """
    warnings.warn(
        "existing file has duplicate or unbalanced FORJA:BEGIN/END markers — "
        "preserving it whole instead of merging (no spec changes applied; "
        "fix the markers or remove quoted FORJA pairs from manual notes)",
        stacklevel=3,
    )
    return existing


def merge_content(existing: str, new: str) -> str:
    """
    Combines the content on disk with the freshly rendered content,
    applying the block rule described above. Returns the final content
    to write (it may be equal to `existing` if there are no changes).
    """
    if not has_blocks(existing):
        # No recognizable blocks: the file opted out of automatic
        # generation. It is preserved whole, untouched.
        return existing

    # F5: CRLF files must merge exactly like LF files. Normalize for the
    # merge decision; the written result is LF (consistent output).
    existing = existing.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")

    begin_names = [m.group("name") for m in _BEGIN_RE.finditer(existing)]
    end_names = [m.group("name") for m in _END_RE.finditer(existing)]
    if (
        len(begin_names) != len(end_names)
        or begin_names != end_names
        or len(set(begin_names)) != len(begin_names)
        or not _structurally_sound(existing)
    ):
        return _merge_ambiguous(existing)

    merged = existing
    for name in begin_names:
        old_span = _find_block(merged, name)
        new_span = _find_block(new, name)
        if old_span is None:
            # Defensive: a balanced name list guarantees the span, but
            # never crash on a malformed input.
            return _merge_ambiguous(existing)
        if new_span is None:
            # The block no longer exists in the current template:
            # remove it (documented behavior).
            merged = merged[: old_span[0]] + merged[old_span[1] :]
        else:
            merged = (
                merged[: old_span[0]]
                + new[new_span[0] : new_span[1]]
                + merged[old_span[1] :]
            )

    existing_names = set(begin_names)
    new_names = {m.group("name") for m in _BEGIN_RE.finditer(new)}
    for name in new_names - existing_names:
        span = _find_block(new, name)
        if span:
            merged = merged.rstrip("\n") + "\n\n" + new[span[0] : span[1]]

    return merged
