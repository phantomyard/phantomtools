"""
Data model of a PhantomForge organization, as pure dataclasses (no
pydantic, so we don't depend on an external library just to type a dict
that PyYAML already loaded). Shape validation lives in
shape_validator.py; these classes only type and offer resolution helpers
(department_by_id, role_by_id, etc.) used by validator/ and compiler/.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Organization:
    id: str
    name: str
    sector: str
    languages: list[str]
    default_language: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Organization:
        return cls(
            id=d["id"],
            name=d["name"],
            sector=d["sector"],
            languages=list(d["languages"]),
            default_language=d.get("default_language"),
        )


@dataclass
class Department:
    id: str
    name: str
    parent: str | None
    access_policy: str

    @classmethod
    def from_dict(cls, d: dict) -> Department:
        return cls(
            id=d["id"],
            name=d["name"],
            parent=d.get("parent"),
            access_policy=d["access_policy"],
        )


@dataclass
class Role:
    id: str
    name: str
    department: str
    reports_to: str | None = None
    reports_to_human: str | None = None
    functions: list[str] = field(default_factory=list)
    access_level: str = ""
    security_exceptions: list[str] = field(default_factory=list)
    soul_line_budget: int = 300
    description: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Role:
        # Explicit null means "use the default" (shape validation rejects
        # null before this point, but from_dict must never yield None for
        # an int-annotated field — check_soul_budget would TypeError).
        budget = d.get("soul_line_budget")
        return cls(
            id=d["id"],
            name=d["name"],
            department=d["department"],
            reports_to=d.get("reports_to"),
            reports_to_human=d.get("reports_to_human"),
            functions=list(d.get("functions", [])),
            access_level=d["access_level"],
            security_exceptions=list(d.get("security_exceptions", [])),
            soul_line_budget=300 if budget is None else budget,
            description=d.get("description"),
        )


@dataclass
class Actor:
    id: str
    role: str
    telegram_bot: str | None = None
    npub: str | None = None
    tools: list[str] = field(default_factory=list)
    tools_excluded: list[str] = field(default_factory=list)
    actor_exceptions: list[str] = field(default_factory=list)
    tone: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Actor:
        return cls(
            id=d["id"],
            role=d["role"],
            telegram_bot=d.get("telegram_bot"),
            npub=d.get("npub"),
            tools=list(d.get("tools", [])),
            tools_excluded=list(d.get("tools_excluded", [])),
            actor_exceptions=list(d.get("actor_exceptions", [])),
            tone=d.get("tone"),
        )


@dataclass
class Human:
    """A human counterpart registered in org.yaml (``humans:`` block).

    Unlike actors, humans are not compiled into persona directories; they
    are the org's external counterparts (Board president, treasurer,
    secretary...) that personas interact with over Telegram or Nostr.
    ``telegram_user_id`` / ``npub`` are nullable until the human has a
    real token/key registered.
    """

    id: str
    name: str | None = None
    role: str | None = None
    telegram_user_id: int | None = None
    npub: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Human:
        return cls(
            id=d["id"],
            name=d.get("name"),
            role=d.get("role"),
            telegram_user_id=d.get("telegram_user_id"),
            npub=d.get("npub"),
        )


@dataclass
class AccessLevel:
    label: str
    categories: list[int] = field(default_factory=list)


@dataclass
class SecurityCategory:
    label: str
    scope: str | None = None
    owner: str | None = None


@dataclass
class Policies:
    access_levels: dict[str, AccessLevel]
    security_categories: dict[str, SecurityCategory]

    @classmethod
    def from_dict(cls, d: dict) -> Policies:
        levels = {
            k: AccessLevel(label=v["label"], categories=list(v.get("categories", [])))
            for k, v in d.get("access_levels", {}).items()
        }
        cats = {
            k: SecurityCategory(
                label=v["label"], scope=v.get("scope"), owner=v.get("owner")
            )
            for k, v in d.get("security_categories", {}).items()
        }
        return cls(access_levels=levels, security_categories=cats)


@dataclass
class EscalationEntry:
    from_: str
    to: str
    condition: str
    cross_department: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> EscalationEntry:
        return cls(
            from_=d["from"],
            to=d["to"],
            condition=d["condition"],
            cross_department=d.get("cross_department", False),
        )


@dataclass
class HumanChannel:
    platform: str
    group: str | None = None
    chat_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> HumanChannel:
        return cls(
            platform=d["platform"],
            group=d.get("group"),
            chat_id=d.get("chat_id"),
        )


@dataclass
class AgentChannel:
    platform: str
    relay: str | None = None
    bridge_npub: str | None = None
    human_npubs: list[str] = field(default_factory=list)
    public_relays: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> AgentChannel:
        return cls(
            platform=d["platform"],
            relay=d.get("relay"),
            bridge_npub=d.get("bridge_npub"),
            human_npubs=list(d.get("human_npubs", []) or []),
            public_relays=list(d.get("public_relays", []) or []),
        )


@dataclass
class Envelope:
    """Protocol envelope (norma v1.3): metadata carried in routed messages.

    ``marker`` is a FIXED protocol constant (``[env]``): PhantomBridge
    hardcodes it, so org.yaml must not diverge (shape validator enforces
    ``marker == "[env]"``). ``ttl_hours`` is the TTL the bridge applies when
    the envelope has no ``expires`` — it must match the bridge's
    ``config.antiloop.expireMs`` (see /status and README).
    """

    marker: str = "[env]"
    ttl_hours: int = 6

    @classmethod
    def from_dict(cls, d: dict) -> Envelope:
        """Build from a validated shape dict.

        Strict on purpose (F2-11): no silent coercion of types. The shape
        validator guarantees ``marker: str`` and ``ttl_hours: int >= 1``
        before this runs (loader flow), so any type mismatch here is a
        programming error, not a YAML edge case.
        """
        d = d or {}
        marker = d.get("marker", "[env]")
        ttl_hours = d.get("ttl_hours", 6)
        if not isinstance(marker, str) or not marker.strip():
            raise ValueError(
                f"communication.envelope.marker must be a non-empty string, got {marker!r}"
            )
        if (
            isinstance(ttl_hours, bool)
            or not isinstance(ttl_hours, int)
            or ttl_hours < 1
        ):
            raise ValueError(
                "communication.envelope.ttl_hours must be an int >= 1, "
                f"got {ttl_hours!r}"
            )
        return cls(marker=marker, ttl_hours=ttl_hours)


@dataclass
class Communication:
    request_id_format: str
    message_types: list[str]
    max_hops: int = 3
    norm_version: str = "1.0"
    envelope: Envelope | None = None
    human_channel: HumanChannel | None = None
    agent_channel: AgentChannel | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Communication:
        channels = d.get("channels") or {}
        human_raw = channels.get("human") if isinstance(channels, dict) else None
        agent_raw = channels.get("agent") if isinstance(channels, dict) else None
        env_raw = d.get("envelope")
        return cls(
            request_id_format=d["request_id_format"],
            message_types=list(d["message_types"]),
            max_hops=d.get("max_hops", 3),
            norm_version=d.get("norm_version", "1.0"),
            envelope=Envelope.from_dict(env_raw) if env_raw else None,
            human_channel=(HumanChannel.from_dict(human_raw) if human_raw else None),
            agent_channel=(AgentChannel.from_dict(agent_raw) if agent_raw else None),
        )


@dataclass
class OrgSpec:
    """Complete (shape-level) validated representation of org.yaml."""

    version: int
    organization: Organization
    departments: list[Department]
    roles: list[Role]
    actors: list[Actor]
    humans: list[Human]
    policies: Policies
    escalation_matrix: list[EscalationEntry]
    communication: Communication

    @classmethod
    def from_dict(cls, d: dict) -> OrgSpec:
        return cls(
            version=d["version"],
            organization=Organization.from_dict(d["organization"]),
            departments=[Department.from_dict(x) for x in d["departments"]],
            roles=[Role.from_dict(x) for x in d["roles"]],
            actors=[Actor.from_dict(x) for x in d["actors"]],
            humans=[Human.from_dict(x) for x in d.get("humans", [])],
            policies=Policies.from_dict(d["policies"]),
            escalation_matrix=[
                EscalationEntry.from_dict(x) for x in d["escalation_matrix"]
            ],
            communication=Communication.from_dict(d["communication"]),
        )

    # -- resolution helpers, used by validator and compiler -----------

    def department_by_id(self, dept_id: str) -> Department:
        for d in self.departments:
            if d.id == dept_id:
                return d
        raise KeyError(f"Department not found: {dept_id}")

    def role_by_id(self, role_id: str) -> Role:
        for r in self.roles:
            if r.id == role_id:
                return r
        raise KeyError(f"Role not found: {role_id}")

    def actor_by_id(self, actor_id: str) -> Actor:
        for a in self.actors:
            if a.id == actor_id:
                return a
        raise KeyError(f"Actor not found: {actor_id}")

    def human_by_id(self, human_id: str) -> Human:
        for h in self.humans:
            if h.id == human_id:
                return h
        raise KeyError(f"Human not found: {human_id}")

    def subordinates_of(self, role_id: str) -> list[Role]:
        return [r for r in self.roles if r.reports_to == role_id]
