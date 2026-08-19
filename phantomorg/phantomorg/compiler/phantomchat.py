"""
Phantomchat verification: contrast declared npubs against the runtime.

The org model can declare a Nostr identity (``actors[].npub``) for every
actor — the public key other bots use to DM that persona over phantomchat
(Nostr NIP-17, phantombot's bot<->bot layer). The npub is *declared*
state (org.yaml), but the authoritative identity lives in the runtime:
each persona directory holds ``identity.json`` (the nsec keypair) and
``phantomchat.json`` (relays + allowed/greeted npubs), and phantombot
derives the real npub from the nsec.

This module verifies the declared npub against the real one, purely by
reading files and invoking phantombot's own (non-invasive) ``phantomchat``
subcommand — it NEVER writes, generates, or modifies anything. It is the
"build verification" layer of the phantomchat plan: run it before/after a
deploy to know exactly which personas have a working phantomchat identity
and whether org.yaml matches reality.

Statuses (per actor)
--------------------
``ok``                 declared npub matches the real identity npub.
``mismatch``           declared npub does NOT match the real npub
                       (org.yaml is out of sync with the runtime).
``missing-identity``   persona dir has no identity.json (no keypair yet:
                       run ``phantombot phantomchat --persona X`` once).
``missing-phantomchat`` identity.json exists but phantomchat.json is
                       missing (relays/allowed_npubs never configured).
``not-declared``       org.yaml declares no npub for this actor — nothing
                       to compare; reported for completeness.
``error``              the phantomchat binary could not be run (not
                       installed, timeout, crash) — status unknown.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess  # nosec B404 — fixed arg list, no shell (see _run_binary)
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..spec.model import OrgSpec

IDENTITY_FILENAME = "identity.json"
PHANTOMCHAT_FILENAME = "phantomchat.json"

# Real NIP-19 npub: "npub1" + 58 bech32 data/checksum chars.
_NPUB_RE = re.compile(r"npub1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")

MANIFEST_FORMAT_VERSION = 1

DEFAULT_TIMEOUT = 20.0

# Statuses (sorted for deterministic manifests)
OK = "ok"
MISMATCH = "mismatch"
MISSING_IDENTITY = "missing-identity"
MISSING_PHANTOMCHAT = "missing-phantomchat"
NOT_DECLARED = "not-declared"
ERROR = "error"

_STATUSES = (
    OK,
    MISMATCH,
    MISSING_IDENTITY,
    MISSING_PHANTOMCHAT,
    NOT_DECLARED,
    ERROR,
)


class PhantomchatError(ValueError):
    """Raised for invalid inputs (unknown status, wrong manifest shape)."""


@dataclass
class ActorCheck:
    """Result of verifying one actor's phantomchat identity."""

    actor_id: str
    status: str
    declared_npub: str | None = None
    real_npub: str | None = None
    identity_exists: bool = False
    phantomchat_exists: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "declared_npub": self.declared_npub,
            "real_npub": self.real_npub,
            "identity_exists": self.identity_exists,
            "phantomchat_exists": self.phantomchat_exists,
            "detail": self.detail,
        }


@dataclass
class PhantomchatManifest:
    """Structured report of a phantomchat verification run."""

    org_id: str
    personas_dir: str
    phantomchat_bin: str
    checked_at: str
    checks: list[ActorCheck] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s: 0 for s in _STATUSES}
        for c in self.checks:
            counts[c.status] += 1
        return counts

    @property
    def ok(self) -> bool:
        """True when every actor with a declared npub verified OK (or had
        none declared). NOT_DECLARED counts as OK; ERROR/MISMATCH/missing
        states make the verification fail."""
        return all(c.status in (OK, NOT_DECLARED) for c in self.checks) and bool(
            self.checks
        )

    def as_dict(self) -> dict:
        return {
            "format_version": MANIFEST_FORMAT_VERSION,
            "org": self.org_id,
            "personas_dir": self.personas_dir,
            "phantomchat_bin": self.phantomchat_bin,
            "checked_at": self.checked_at,
            "summary": self.summary(),
            "checks": {c.actor_id: c.as_dict() for c in self.checks},
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run_binary(
    bin_path: str, args: list[str], timeout: float
) -> subprocess.CompletedProcess:
    """Run phantombot with a fixed arg list (no shell)."""
    # Fixed arg list, no shell — safe by construction (bandit B603/B607).
    return subprocess.run(  # nosec B603 B607
        [bin_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _extract_npub(text: str) -> str | None:
    """First npub-looking token in ``text`` (TUI output may wrap it)."""
    m = _NPUB_RE.search(text)
    return m.group(0) if m else None


def _read_json_keys(path: Path) -> set[str]:
    """Best-effort top-level keys of a JSON file; empty set on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    return set(data.keys())


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def verify_phantomchat(
    spec: OrgSpec,
    personas_dir: Path,
    phantomchat_bin: str = "phantombot",
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    now: Callable[[], str] = _utc_now,
) -> PhantomchatManifest:
    """Verify every actor's declared npub against the runtime identity.

    Non-invasive: reads persona files, and for each actor invokes
    ``<bin> phantomchat --persona <id>`` (phantombot's own subcommand,
    which only PRINTS the npub when an identity exists — it never
    generates or modifies anything in that case).

    ``runner`` is injectable for tests; by default it shells out to the
    real binary. ``now`` is injectable for deterministic manifests.
    """
    declared = {a.id: a.npub for a in spec.actors}
    run = runner or (lambda args: _run_binary(phantomchat_bin, args, timeout))

    checks: list[ActorCheck] = []
    for actor in spec.actors:
        actor_dir = personas_dir / actor.id
        identity_exists = (actor_dir / IDENTITY_FILENAME).is_file()
        phantomchat_exists = (actor_dir / PHANTOMCHAT_FILENAME).is_file()
        declared_npub = declared.get(actor.id)

        # Identity sanity: identity.json must actually contain a nsec key.
        if identity_exists:
            keys = _read_json_keys(actor_dir / IDENTITY_FILENAME)
            if "nsec" not in keys:
                identity_exists = False  # present but unusable — treat as missing

        if identity_exists and not phantomchat_exists:
            checks.append(
                ActorCheck(
                    actor_id=actor.id,
                    status=MISSING_PHANTOMCHAT,
                    declared_npub=declared_npub,
                    identity_exists=True,
                    phantomchat_exists=False,
                    detail="phantomchat.json is missing; deploy the generated runtime configuration",
                )
            )
            continue

        if not identity_exists:
            checks.append(
                ActorCheck(
                    actor_id=actor.id,
                    status=(
                        NOT_DECLARED if declared_npub is None else MISSING_IDENTITY
                    ),
                    declared_npub=declared_npub,
                    identity_exists=False,
                    phantomchat_exists=phantomchat_exists,
                    detail=(
                        "no npub declared in org.yaml"
                        if declared_npub is None
                        else "identity.json missing or has no nsec key — "
                        "run `phantombot phantomchat --persona "
                        f"{actor.id}` once to generate it"
                    ),
                )
            )
            continue

        # Real npub via the binary (source of truth: nsec -> npub).
        try:
            proc = run(["phantomchat", "--persona", actor.id])
        except (OSError, subprocess.SubprocessError, TimeoutError) as e:
            checks.append(
                ActorCheck(
                    actor_id=actor.id,
                    status=ERROR,
                    declared_npub=declared_npub,
                    identity_exists=True,
                    phantomchat_exists=phantomchat_exists,
                    detail=f"could not run {phantomchat_bin}: {e}",
                )
            )
            continue
        real_npub = _extract_npub(proc.stdout) if proc.returncode == 0 else None
        if real_npub is None:
            checks.append(
                ActorCheck(
                    actor_id=actor.id,
                    status=ERROR,
                    declared_npub=declared_npub,
                    identity_exists=True,
                    phantomchat_exists=phantomchat_exists,
                    detail=(
                        f"{phantomchat_bin} phantomchat exited rc={proc.returncode} "
                        "without printing an npub"
                        if proc.returncode != 0
                        else "no npub found in phantomchat output"
                    ),
                )
            )
            continue

        if declared_npub is None:
            checks.append(
                ActorCheck(
                    actor_id=actor.id,
                    status=NOT_DECLARED,
                    declared_npub=None,
                    real_npub=real_npub,
                    identity_exists=True,
                    phantomchat_exists=phantomchat_exists,
                    detail=("runtime identity exists but org.yaml declares no npub"),
                )
            )
            continue

        if declared_npub == real_npub:
            status = OK
            detail = "declared npub matches runtime identity"
        else:
            status = MISMATCH
            detail = (
                "declared npub differs from runtime identity — org.yaml "
                "is out of sync (update it, or fix the persona identity)"
            )
        checks.append(
            ActorCheck(
                actor_id=actor.id,
                status=status,
                declared_npub=declared_npub,
                real_npub=real_npub,
                identity_exists=True,
                phantomchat_exists=phantomchat_exists,
                detail=detail,
            )
        )

    return PhantomchatManifest(
        org_id=spec.organization.id,
        personas_dir=str(personas_dir),
        phantomchat_bin=phantomchat_bin,
        checked_at=now(),
        checks=checks,
    )
