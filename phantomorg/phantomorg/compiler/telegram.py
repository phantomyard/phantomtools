"""
Telegram verification: contrast declared bot usernames against the runtime.

org.yaml declares a ``telegram_bot`` handle (e.g. ``@marco_bot``) for
every actor — the username citizens use to reach that persona in the
"chain of personas" (communication norm). The handle is *declared*
state (org.yaml), but the authoritative value lives in the runtime:
phantombot's config.toml holds the bot token for the default persona
(``[channels.telegram].token``) and for each sub-persona
(``[channels.telegram.personas.<id>].token``), and Telegram itself is the
source of truth for what username each token resolves to (``getMe``).

This module verifies the declared handle against the real one by reading
config.toml and calling the public Telegram Bot API (``getMe``) — it
NEVER writes, generates, or modifies anything. It is the Telegram
counterpart of the phantomchat verification: run it before/after a
deploy to know exactly which personas have a working Telegram bot and
whether org.yaml matches reality.

Note: ``getMe`` works from any host with internet access to
api.telegram.org — no need to run it on the phantombot host itself.

Statuses (per actor)
--------------------
``ok``                 declared handle matches the real bot username.
``mismatch``           declared handle does NOT match the real username
                       (org.yaml is out of sync with the deployed bot).
``no-token``           the actor is declared with a telegram_bot but has
                       no token in config.toml (no bot configured for
                       that persona).
``not-declared``       org.yaml declares no telegram_bot for this actor —
                       nothing to compare; reported for completeness.
``error``              getMe failed (network, timeout, invalid token) —
                       status unknown.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..spec.model import OrgSpec

# Config keys (phantombot config.toml)
DEFAULT_PERSONA_KEY = "default_persona"
TELEGRAM_SECTION = "channels.telegram"
# The literal config key name, not a credential value (bandit B105
# false positive).
TOKEN_KEY = "token"  # nosec B105
PERSONAS_PREFIX = f"{TELEGRAM_SECTION}.personas."

# Optional: path to state.json whose default_persona overrides config.toml
# at runtime (phantombot persists the active persona there).
STATE_DEFAULT_PERSONA_KEY = "default_persona"

MANIFEST_FORMAT_VERSION = 1

DEFAULT_TIMEOUT = 15.0
TELEGRAM_API = "https://api.telegram.org"

# Statuses (sorted for deterministic manifests)
OK = "ok"
MISMATCH = "mismatch"
# A status string, not a credential value (bandit B105 false positive).
NO_TOKEN = "no-token"  # nosec B105
NOT_DECLARED = "not-declared"
ERROR = "error"

_STATUSES = (
    OK,
    MISMATCH,
    NO_TOKEN,
    NOT_DECLARED,
    ERROR,
)


class TelegramError(ValueError):
    """Raised for invalid inputs (unknown status, bad config shape)."""


@dataclass
class ActorCheck:
    """Result of verifying one actor's Telegram bot."""

    actor_id: str
    status: str
    declared_bot: str | None = None
    real_bot: str | None = None
    token_source: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "declared_bot": self.declared_bot,
            "real_bot": self.real_bot,
            "token_source": self.token_source,
            "detail": self.detail,
        }


@dataclass
class TelegramManifest:
    """Structured report of a Telegram verification run."""

    org_id: str
    config_path: str
    checked_at: str
    checks: list[ActorCheck] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s: 0 for s in _STATUSES}
        for c in self.checks:
            counts[c.status] += 1
        return counts

    @property
    def ok(self) -> bool:
        """True when every actor with a declared telegram_bot verified OK
        (or had none declared). NOT_DECLARED counts as OK; ERROR/
        MISMATCH/NO_TOKEN make the verification fail."""
        return all(c.status in (OK, NOT_DECLARED) for c in self.checks) and bool(
            self.checks
        )

    def as_dict(self) -> dict:
        return {
            "format_version": MANIFEST_FORMAT_VERSION,
            "org": self.org_id,
            "config_path": self.config_path,
            "checked_at": self.checked_at,
            "summary": self.summary(),
            "checks": {c.actor_id: c.as_dict() for c in self.checks},
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


def _normalize_handle(handle: str | None) -> str | None:
    """Strip a leading '@', whitespace, and normalize case; None stays None."""
    if handle is None:
        return None
    return handle.strip().lstrip("@").strip().lower()


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _load_toml(path: Path) -> dict:
    """Load a TOML file (tomllib, py3.11+)."""
    import tomllib  # stdlib since 3.11

    with path.open("rb") as f:
        return tomllib.load(f)


def _load_json(path: Path) -> dict:
    """Best-effort JSON load; empty dict on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _token_for_actor(
    config: dict, state: dict, actor_id: str
) -> tuple[str, str] | None:
    """Return (token, source) for an actor, or None when no token exists.

    Resolution order:
      1. ``[channels.telegram.personas.<id>].token`` — sub-persona token.
      2. main ``[channels.telegram].token`` when the actor is the runtime
         default persona (state.json wins over config.toml, mirroring
         phantombot's runtime behaviour).
    """
    personas = config.get("channels", {}).get("telegram", {}).get("personas", {})
    if isinstance(personas, dict):
        sub = personas.get(actor_id)
        if (
            isinstance(sub, dict)
            and isinstance(sub.get(TOKEN_KEY), str)
            and sub[TOKEN_KEY]
        ):
            return sub[TOKEN_KEY], f"personas.{actor_id}"

    default_persona = state.get(STATE_DEFAULT_PERSONA_KEY)
    if not isinstance(default_persona, str):
        default_persona = config.get(DEFAULT_PERSONA_KEY)
    if default_persona == actor_id:
        main_token = config.get("channels", {}).get("telegram", {}).get(TOKEN_KEY)
        if isinstance(main_token, str) and main_token:
            return main_token, "main (default persona)"

    return None


def _getme(token: str, timeout: float) -> tuple[str | None, str]:
    """Call Telegram ``getMe``. Returns (username, detail).

    ``username`` is the bot's username WITHOUT the leading '@'. On any
    failure returns (None, reason).
    """
    url = f"{TELEGRAM_API}/bot{token}/getMe"
    req = urllib.request.Request(url, method="GET")
    try:
        # Fixed https URL to Telegram's public API — not attacker-controlled
        # and no custom schemes are reachable from ``token`` (a Telegram bot
        # token, constrained to [A-Za-z0-9:_-]).
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return None, f"getMe failed: {e}"
    if not payload.get("ok"):
        desc = payload.get("description", "unknown error")
        return None, f"getMe failed: {desc}"
    result = payload.get("result") or {}
    username = result.get("username")
    if not isinstance(username, str) or not username:
        return None, "getMe returned no username"
    return username, ""


def verify_telegram(
    spec: OrgSpec,
    config_path: Path,
    state_path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    now: Callable[[], str] = _utc_now,
) -> TelegramManifest:
    """Verify every actor's declared telegram_bot against the live bot.

    Non-invasive: reads config.toml (and optionally state.json) and calls
    the public Telegram ``getMe`` endpoint. Never writes anything.
    """
    declared = {a.id: a.telegram_bot for a in spec.actors}
    try:
        config = _load_toml(config_path)
    except OSError as e:
        raise TelegramError(f"cannot read config: {e}") from e
    except Exception as e:
        raise TelegramError(f"cannot parse config {config_path}: {e}") from e

    state: dict = {}
    if state_path is not None:
        state = _load_json(state_path)

    checks: list[ActorCheck] = []
    for actor in spec.actors:
        actor_id = actor.id
        declared_bot = declared.get(actor_id)

        if not declared_bot:
            checks.append(
                ActorCheck(
                    actor_id=actor_id,
                    status=NOT_DECLARED,
                    declared_bot=None,
                    detail="no telegram_bot declared in org.yaml",
                )
            )
            continue

        tok = _token_for_actor(config, state, actor_id)
        if tok is None:
            checks.append(
                ActorCheck(
                    actor_id=actor_id,
                    status=NO_TOKEN,
                    declared_bot=declared_bot,
                    detail=(
                        "no token in config.toml for this persona "
                        "(neither [channels.telegram.personas.<id>] nor "
                        "main token when default persona)"
                    ),
                )
            )
            continue
        token, source = tok

        real_username, err = _getme(token, timeout)
        if real_username is None:
            checks.append(
                ActorCheck(
                    actor_id=actor_id,
                    status=ERROR,
                    declared_bot=declared_bot,
                    token_source=source,
                    detail=err,
                )
            )
            continue

        if _normalize_handle(declared_bot) == real_username.lower():
            status = OK
            detail = "declared telegram_bot matches the live bot username"
        else:
            status = MISMATCH
            detail = (
                "declared telegram_bot differs from the live bot username "
                f"(getMe reports @{real_username}) — org.yaml is out of sync"
            )
        checks.append(
            ActorCheck(
                actor_id=actor_id,
                status=status,
                declared_bot=declared_bot,
                real_bot=f"@{real_username}",
                token_source=source,
                detail=detail,
            )
        )

    return TelegramManifest(
        org_id=spec.organization.id,
        config_path=str(config_path),
        checked_at=now(),
        checks=checks,
    )
