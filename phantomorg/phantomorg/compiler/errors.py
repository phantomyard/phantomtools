"""Compiler error types."""

from __future__ import annotations


class CompileError(ValueError):
    """Base error for build-time failures.

    Raised when ``build()`` refuses to write — a symlink in the output
    tree (file-level or a path component), or an actor id that would
    escape the requested output directory. Subclasses ValueError so the
    existing ``assertRaises(ValueError)`` tests keep passing, while the
    CLI can catch it distinctly from a plain programming error.
    """
