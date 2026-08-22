"""File scaffold norms as drawer ROWS at deploy time (phantombot >= 1.1.282).

``memory/norms.md`` is a deprecated read path: since phantombot
#412/#415/#416/#418 the five drawers are rows in ``memory.sqlite``
(``drawer_entries``), ranked by ``weight * 2^(-ageDays / halfLifeDays)``
(norms: 365-day half-life), and the threat judge is briefed from ranked
rows — never from the markdown file. PhantomOrg's build emits ``norms.json``
(one plain-text line per scaffold norm, see ``compiler.build.NORMS_FILENAME``);
this module files each line as a row via the phantombot CLI::

    phantombot memory drawers --kind norms --file "<line>" --persona <actor_id>

Re-filing the same text is idempotent (content-hash + ``UNIQUE``), so
re-deploys reaffirm instead of duplicating, and the decay clock resets.

KNOWN GAP (upstream): the CLI ``--file`` flag does not yet accept
``--origin`` / ``--weight`` / ``--supersedes``, so rows filed from here land
with the CLI defaults (origin ``"cli"``). The manifest records
``origin: "phantomorg"`` as the intent; once phantombot grows the flags this
module should pass ``--origin phantomorg``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..compiler.build import NORMS_FILENAME

DEFAULT_TIMEOUT = 20.0

Runner = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass
class NormFilingResult:
    """Outcome of filing scaffold norms as drawer rows."""

    filed: dict[str, int] = field(default_factory=dict)
    """actor id -> number of norm rows filed (or reaffirmed)."""
    errors: list[str] = field(default_factory=list)
    """Non-fatal per-actor failures (missing/old binary, non-zero exit, ...)."""

    @property
    def ok(self) -> bool:
        return not self.errors


def _run_binary(
    bin_path: str, args: list[str], timeout: float
) -> subprocess.CompletedProcess:
    """Run phantombot with a fixed arg list (no shell)."""
    return subprocess.run(  # nosec B603 B607
        [bin_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_norm_lines(actor_dir: Path) -> list[str]:
    """Read one compiled actor's norms manifest; [] on absence/unreadable."""
    manifest = actor_dir / NORMS_FILENAME
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    return [str(e) for e in entries if isinstance(e, str) and str(e).strip()]


def file_norms(
    compiled_dir: Path,
    phantombot_bin: str = "phantombot",
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> NormFilingResult:
    """File every compiled actor's scaffold norms as drawer rows.

    For each norm line of each compiled actor, shells out to
    ``<phantombot_bin> memory drawers --kind norms --file "<line>" --persona
    <actor_id>``. Non-fatal by design: a missing or too-old binary (or a
    non-zero exit) is recorded in ``errors`` and the next actor is still
    attempted — never a silent skip.
    """
    result = NormFilingResult()
    run = runner or (lambda args: _run_binary(phantombot_bin, args, timeout))
    for actor_dir in sorted(d for d in compiled_dir.iterdir() if d.is_dir()):
        actor_id = actor_dir.name
        lines = _read_norm_lines(actor_dir)
        if not lines:
            continue
        filed = 0
        failed = False
        for line in lines:
            try:
                proc = run(
                    [
                        "memory",
                        "drawers",
                        "--kind",
                        "norms",
                        "--file",
                        line,
                        "--persona",
                        actor_id,
                    ]
                )
            except (OSError, subprocess.SubprocessError, TimeoutError) as e:
                result.errors.append(
                    f"{actor_id}: could not run {phantombot_bin}: {e}"
                )
                failed = True
                break
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                result.errors.append(
                    f"{actor_id}: norm filing failed ({proc.returncode}): {err}"
                )
                failed = True
                break
            filed += 1
        if filed:
            result.filed[actor_id] = filed
    return result
