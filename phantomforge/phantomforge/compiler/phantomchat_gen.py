"""
Phantomchat generation: compile phantomchat.json from the org model.

Contrast with ``phantomchat.py`` (verification-only): this module GENERATES
the runtime ``phantomchat.json`` for every actor that declares a Nostr npub.
It is the "compile" layer of the phantomchat plan — org.yaml declares the
channels (private relay, bridge npub, human npubs) and the actor identities
(``actors[].npub``), and each persona gets a ready-to-deploy phantomchat.json
with:

- ``relays``: the org private relay first, then the optional public relays.
- ``allowed_npubs``: every OTHER actor's npub (no self), then the org's
  human npubs, then the bridge npub.
- ``greeted``: same set, ordered humans + bridge first, then the other
  actors (this matches what phantombot's phantomchat greeting does).

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
        return json.dumps(
            {
                "relays": self.relays,
                "allowed_npubs": self.allowed_npubs,
                "greeted": self.greeted,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n"


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

    other_npubs = [a.npub for a in spec.actors if a.id != actor.id and a.npub]
    human_npubs = list(channel.human_npubs or [])
    bridge_npub = channel.bridge_npub

    relays = [channel.relay] + list(channel.public_relays or [])

    allowed: list[str] = []
    allowed.extend(other_npubs)
    allowed.extend(human_npubs)
    if bridge_npub:
        allowed.append(bridge_npub)

    # greeted: humans + bridge first, then the other actors (same set).
    greeted: list[str] = []
    greeted.extend(human_npubs)
    if bridge_npub:
        greeted.append(bridge_npub)
    greeted.extend(other_npubs)

    return PhantomchatConfig(relays=relays, allowed_npubs=allowed, greeted=greeted)
