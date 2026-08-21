"""
Phantomchat generation: compile phantomchat.json from the org model.

Contrast with ``phantomchat.py`` (verification-only): this module GENERATES
the runtime ``phantomchat.json`` for every actor that declares a Nostr npub.
It is the "compile" layer of the phantomchat plan — org.yaml declares the
channels (private relay, bridge npub, human npubs) and the actor identities
(``actors[].npub``), and each persona gets a ready-to-deploy phantomchat.json
with:

- ``relays``: the org private relay first, then the optional public relays.
- ``allowed_npubs``: explicitly designated PRINCIPALS only (``principal_npubs``).
  The shared human group identity (``human_npubs``), relays, the bridge and
  peer actors are delivery endpoints, never promoted to principal trust.
- ``greeted`` is intentionally empty: onboarding must remain visible to the
  operator and must not silently establish trust.

The file is derived state (like the norma): fully regenerated on every
build, written with write_plain_if_changed, never block-merged. Actors
without an npub simply get no phantomchat.json (build() warns about them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..spec.model import Actor, AgentChannel, OrgSpec

PHANTOMCHAT_FILENAME = "phantomchat.json"


@dataclass
class PhantomchatConfig:
    """One persona's phantomchat.json content (serializable)."""

    relays: list[str] = field(default_factory=list)
    allowed_npubs: list[str] = field(default_factory=list)
    greeted: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "relays": self.relays,
                    "allowed_npubs": self.allowed_npubs,
                    "greeted": self.greeted,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )


def phantomchat_config(
    spec: OrgSpec, actor: Actor, channel: AgentChannel | None = None
) -> PhantomchatConfig | None:
    """Build the phantomchat.json content for ``actor``, or None if the
    actor (or the org) has no Nostr identity to configure."""
    if channel is None:
        channel = spec.communication.agent_channel
    if channel is None or not channel.relay:
        return None
    if not actor.npub:
        return None

    principal_npubs = list(channel.principal_npubs or [])

    relays = [channel.relay] + list(channel.public_relays or [])

    # ``allowed_npubs`` is a PRINCIPAL trust grant, not a delivery list. Only
    # the explicitly designated principal keys (``principal_npubs``) are
    # trusted; the shared human group identity (``human_npubs``), the bridge,
    # relays and peer actors remain screened by the threat judge. Nothing is
    # auto-promoted from delivery capability to principal authority.
    allowed = principal_npubs.copy()

    # Do not pre-seed greeted. The runtime onboarding signal must remain
    # observable rather than silently converting delivery into trust.
    return PhantomchatConfig(relays=relays, allowed_npubs=allowed, greeted=[])
