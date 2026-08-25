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

Two failure modes are guarded at deploy time:

1. **Misfile** — the invoked binary's own config resolves ``personasDir`` /
   ``memoryDbPath``, uncorrelated with the deploy ``--target``. Before filing
   for an actor, ``_verify_persona`` probes ``phantombot memory today
   --persona <id>`` (exit 0 only when the binary resolves the persona) and,
   when the target is known, checks the printed daily-file path lives under
   ``<target>/<actor_id>``. A mismatch records an error and skips filing
   instead of writing rows into the wrong database.

2. **Stale rows** — rows are content-addressed and never removed, so a norm
   that changes or is dropped keeps briefing the judge alongside the current
   one for up to a year. The lines filed each deploy are persisted (keyed by
   content hash) in ``.phantomorg-norms.json`` next to the session manifest,
   and the NEXT deploy diffs that ledger against the current manifest to
   surface rows that were filed before but are gone now (``superseded``).

KNOWN GAP (upstream): the CLI ``--file`` flag does not yet accept
``--origin`` / ``--weight`` / ``--supersedes``, so rows filed from here land
with the CLI defaults (origin ``"cli"``, source ``"self"``) — the judge
cannot distinguish scaffold norms from operator-filed rows until phantombot
grows those flags. The manifest records ``origin: "phantomorg"`` as the
intent; the persisted ledger above is what lets us surface staleness despite
that gap.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404 — fixed arg list, no shell (see _run_binary)
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..compiler.build import NORMS_FILENAME

DEFAULT_TIMEOUT = 20.0

# Persisted ledger of filed scaffold norms, next to `.phantomorg-manifest.json`
# in the personas-archive root: actor id -> {content_sha256: line text}. The
# drawer's UNIQUE key is content-addressed, so the hash is a stable identity
# for diffing "what we filed last time" against the current manifest.
NORMS_STATE_FILENAME = ".phantomorg-norms.json"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass
class NormFilingResult:
    """Outcome of filing scaffold norms as drawer rows."""

    filed: dict[str, int] = field(default_factory=dict)
    """actor id -> number of norm rows filed (or reaffirmed)."""
    errors: list[str] = field(default_factory=list)
    """Non-fatal per-actor failures (missing/old binary, non-zero exit, ...)."""
    superseded: dict[str, list[str]] = field(default_factory=dict)
    """actor id -> norm lines filed by a PREVIOUS deploy that are no longer
    in the current manifest. Their rows still decay in the judge (365-day
    half-life) — surfaced so the operator knows to expect both."""
    filed_lines: dict[str, list[str]] = field(default_factory=dict)
    """actor id -> the norm lines actually filed (or reaffirmed) this run."""

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


def _hash_line(line: str) -> str:
    """Stable content identity for one norm line (matches the drawer's
    content-addressed ``UNIQUE`` key)."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _verify_persona(
    bin_path: str, actor_id: str, run: Runner, target: Path | None
) -> str | None:
    """Return an error message when the binary does NOT resolve ``actor_id``
    as a persona in the expected location; None when it is safe to file.

    ``phantombot memory today --persona <id>`` prints the persona's daily-file
    path (``<personasDir>/<id>/memory/<date>.md``) and exits 0 only when the
    binary's own config resolves the persona. A non-zero exit means the binary
    does not know this persona — filing would write rows into the wrong
    database. When ``target`` is given, the printed path is also checked to
    live under ``<target>/<actor_id>``, so a same-named persona in a different
    installation is still refused.
    """
    try:
        proc = run(["memory", "today", "--persona", actor_id])
    except (OSError, subprocess.SubprocessError, TimeoutError) as e:
        return f"{actor_id}: could not run {bin_path}: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        msg = f"{actor_id}: persona not resolvable by {bin_path}"
        if detail:
            msg += f" ({detail})"
        return msg + " — refusing to file norms into the wrong database"
    if target is not None:
        printed = (proc.stdout or "").strip().splitlines()
        if printed:
            try:
                resolved = Path(printed[0]).resolve()
            except OSError:
                resolved = None
            target_actor = (Path(target) / actor_id).resolve()
            if (
                resolved is not None
                and resolved != target_actor
                and target_actor not in resolved.parents
            ):
                return (
                    f"{actor_id}: {bin_path} resolves the persona at "
                    f"{resolved}, not the deploy target {target_actor} — "
                    "refusing to file norms into the wrong database"
                )
    return None


def file_norms(
    compiled_dir: Path,
    phantombot_bin: str = "phantombot",
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    previous: dict[str, dict[str, str]] | None = None,
    target: Path | None = None,
) -> NormFilingResult:
    """File every compiled actor's scaffold norms as drawer rows.

    For each norm line of each compiled actor, shells out to
    ``<phantombot_bin> memory drawers --kind norms --file "<line>" --persona
    <actor_id>``. Non-fatal by design: a missing or too-old binary (or a
    non-zero exit) is recorded in ``errors`` and the next actor is still
    attempted — never a silent skip.

    Before filing an actor, ``_verify_persona`` confirms the binary resolves
    that actor as a persona (guarding against filing into a database unrelated
    to the deploy target). ``previous`` is the persisted filed-line ledger from
    the last deploy; lines in it that are no longer in the current manifest
    are reported in ``superseded`` (stale rows still decaying in the judge).
    """
    result = NormFilingResult()
    run = runner or (lambda args: _run_binary(phantombot_bin, args, timeout))

    # Supersession detection: rows are content-addressed and never removed,
    # so a changed/dropped norm leaves the old row briefing the judge. Diff
    # the persisted ledger against the current manifest per actor. Scoped to
    # the actors of THIS build: an actor that is not part of the current
    # build (e.g. an org skipped by deploy-all) is left out — we cannot judge
    # whether its norms changed without seeing its manifest.
    previous = previous or {}
    current_actors = {d.name for d in compiled_dir.iterdir() if d.is_dir()}
    for actor_id in sorted(previous):
        if actor_id not in current_actors:
            continue
        prev_entries = previous.get(actor_id) or {}
        current_hashes = {
            _hash_line(ln) for ln in _read_norm_lines(compiled_dir / actor_id)
        }
        stale = sorted({text for h, text in prev_entries.items() if h not in current_hashes})
        if stale:
            result.superseded[actor_id] = stale

    for actor_dir in sorted(d for d in compiled_dir.iterdir() if d.is_dir()):
        actor_id = actor_dir.name
        lines = _read_norm_lines(actor_dir)
        if not lines:
            continue
        err = _verify_persona(phantombot_bin, actor_id, run, target)
        if err is not None:
            result.errors.append(err)
            continue
        filed = []
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
                result.errors.append(f"{actor_id}: could not run {phantombot_bin}: {e}")
                break
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                result.errors.append(
                    f"{actor_id}: norm filing failed ({proc.returncode}): {err}"
                )
                break
            filed.append(line)
        if filed:
            result.filed[actor_id] = len(filed)
            result.filed_lines[actor_id] = filed
    return result


def load_norms_state(state_path: Path) -> dict[str, dict[str, str]]:
    """Best-effort load of the filed-norm ledger (actor id -> {hash: text}).

    Returns {} when the ledger is absent or unreadable — a missing ledger
    simply means "no previous deploy filed norms" (nothing to supersede).
    """
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for actor_id, entries in data.items():
        if isinstance(entries, dict):
            out[str(actor_id)] = {str(h): str(t) for h, t in entries.items()}
    return out


def save_norms_state(state_path: Path, state: dict[str, dict[str, str]]) -> None:
    """Persist the filed-norm ledger atomically (tmp + os.replace)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, state_path)


def next_norms_state(
    previous: dict[str, dict[str, str]],
    result: NormFilingResult,
    built_actors: set[str],
) -> dict[str, dict[str, str]]:
    """Compute the next filed-norm ledger after a deploy.

    - Actors that filed lines this deploy record their current lines.
    - On a CLEAN deploy, actors in ``built_actors`` that did NOT file (their
      norms were dropped — empty manifest) drop their entries: their
      supersession was already reported this round, and re-reporting every
      deploy is noise. Actors NOT in ``built_actors`` (a skipped org, or an
      actor absent from this build) keep their entries untouched — their DB
      rows are unchanged, so the ledger must still match reality.
    - On a deploy WITH errors, previously-recorded actors that did not file
      keep their entries, so a future deploy can still surface their eventual
      supersession (filing failed, so the DB is unchanged from the ledger).
    """
    nxt = dict(previous)
    for actor_id, lines in result.filed_lines.items():
        nxt[actor_id] = {_hash_line(ln): ln for ln in lines}
    if not result.errors:
        for actor_id in list(nxt):
            if actor_id in built_actors and actor_id not in result.filed_lines:
                nxt.pop(actor_id, None)
    return nxt
