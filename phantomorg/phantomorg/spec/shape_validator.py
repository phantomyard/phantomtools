"""
org.yaml shape validation without depending on jsonschema. It checks the
same thing schema.json describes (section 5.2 of the spec) — required
fields present, basic types correct, non-empty lists where appropriate —
with pure Python. schema.json is kept in the repo as readable
documentation of the contract, and as a reference for migrating to
jsonschema/pydantic if the installation environment allows it in the
future.
"""

from __future__ import annotations

import re

VALID_MESSAGE_TYPES = {"REQUEST", "INFORM", "ESCALATE", "CONFIRM", "REJECT"}
VALID_SCOPES = {"actor", "role", "org"}

# NIP-19 bech32 npub (Nostr public key). Real format: "npub1" + 62 chars
# of the bech32 charset (32-byte x-only pubkey + 6-char checksum) = 63
# chars total. We validate charset, length AND the BIP-173 checksum, so a
# transposed or typo'd key is rejected at validation time instead of
# silently pointing at the wrong bot.
_NPUB_RE = re.compile(r"npub1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CHARSET_REV = {c: i for i, c in enumerate(_BECH32_CHARSET)}
_BECH32_GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]

# Single identifier grammar for every id in org.yaml: organization,
# department, role, actor, and the keys of access_levels /
# security_categories. These values are used as reference keys AND as
# filesystem path components (the compiler builds `out_dir / actor.id`),
# so they must be safe single path components — no separators, no "..",
# no whitespace, no leading dot, no trailing newline (a `$`-anchored
# pattern would match "ceo\n" and produce a directory/metadata mismatch).
# Mirrors schema.json's org.id pattern, extended with "_" (already used
# by real ids like chief_of_staff) and a leading-alnum rule (an id must
# never start with "-" or "_", which could collide with hidden/system
# dotfiles or flag-like names).
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Windows reserves these device names for paths; `mkdir out\con` fails
# with WinError 123 (and writes through `out\nul` go to the NUL device).
# Ids become directory names, so they must not collide.
_WINDOWS_RESERVED_NAMES = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# Comfortably under NAME_MAX (255 bytes) and the legacy Windows MAX_PATH
# (260 chars), leaving room for downstream "-" prefixes and suffixes.
_MAX_ID_LENGTH = 64


class ShapeError(Exception):
    """Shape error: a required field is missing or has the wrong type."""


def is_valid_identifier(value: object) -> bool:
    """True when ``value`` is a string matching the identifier grammar.

    The single source of truth for "what may be used as an id in
    org.yaml". Anything that will later be used as a filesystem path
    component, a reference key, or a lookup key must pass this check.
    Rejects Windows-reserved device names (con/aux/nul/...) and ids
    longer than 64 chars, both of which would fail at compile time with
    a cryptic OSError on some platforms.
    """
    return (
        isinstance(value, str)
        and bool(_IDENTIFIER_RE.fullmatch(value))
        and len(value) <= _MAX_ID_LENGTH
        and value.lower() not in _WINDOWS_RESERVED_NAMES
    )


def _bech32_polymod(values: list[int]) -> int:
    """BIP-173 bech32 checksum polymod (matches Bitcoin's reference)."""
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= _BECH32_GEN[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def is_valid_npub(value: object) -> bool:
    """True when ``value`` is a valid NIP-19 npub.

    Validates the bech32 charset, the exact 63-char shape and the
    BIP-173 checksum, so a typo'd or transposed key cannot pass.
    """
    if not isinstance(value, str) or not _NPUB_RE.fullmatch(value):
        return False
    hrp, _, data_part = value.partition("1")
    values = [_BECH32_CHARSET_REV[c] for c in data_part]
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + values)
    return polymod == 1


def _is_valid_telegram_handle(value: object) -> bool:
    """True when ``value`` is a plausible Telegram bot username.

    Telegram usernames: 5..32 chars, ``[A-Za-z0-9_]``, case-insensitive,
    preceded by '@'. This validates the *shape* only (catches typos like
    a missing '@' or spaces); it cannot know whether the handle actually
    exists — use ``po telegram-check`` for that (live getMe).
    """
    if not isinstance(value, str):
        return False
    m = re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value)
    return m is not None


def slugify_id(text: str) -> str:
    """Normalizes free text into an identifier-shaped string.

    Lowercases, strips accents, maps every non-alphanumeric character to
    "_", collapses repeated underscores, and strips leading/trailing
    ones. The result is NOT guaranteed to be a valid identifier (it can
    be empty when ``text`` had no alphanumerics, or still fail
    ``is_valid_identifier``) — callers must check with
    ``is_valid_identifier`` and handle failure explicitly.

    Mirrors the normalization the setup wizard applies to organization
    ids; import-audit uses it so a directory name like "Carla Gómez"
    becomes the valid id "carla_gomez" instead of being written verbatim
    (which `po validate` would immediately reject).
    """
    import unicodedata

    text = unicodedata.normalize("NFKD", text.lower().strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_")


def _require(d: dict, key: str, path: str) -> None:
    if key not in d or d[key] is None:
        raise ShapeError(f"{path}.{key}: required field missing")


def _require_type(value, expected_type, path: str) -> None:
    if expected_type is int and isinstance(value, bool):
        # bool is a subclass of int; version: true / max_hops: true must
        # not pass int checks (schema.json: "type": "integer").
        raise ShapeError(f"{path}: expected int, found bool")
    if not isinstance(value, expected_type):
        raise ShapeError(f"{path}: expected {expected_type}, found {type(value)}")


def _require_optional_type(value, expected_type, path: str) -> None:
    """Optional field: must be None or the expected type (when present)."""
    if value is not None and not isinstance(value, expected_type):
        raise ShapeError(
            f"{path}: expected {expected_type} or null, found {type(value)}"
        )


def _reject_unknown_keys(d: dict, allowed: set[str], path: str) -> None:
    """Rejects unknown/typo'd keys (mirrors schema.json's
    ``additionalProperties: false``). A typo'd optional field
    (``security_excpetions``) would otherwise be silently dropped."""
    extra = set(d) - allowed
    if extra:
        raise ShapeError(f"{path}: unknown field(s) {sorted(extra)}")


def _require_mapping(value, path: str) -> dict:
    """Requires a dict and returns it (typed)."""
    if not isinstance(value, dict):
        raise ShapeError(f"{path}: expected a mapping (dict), found {type(value)}")
    return value


def _require_list(value, path: str) -> list:
    """Requires a list and returns it (typed)."""
    if not isinstance(value, list):
        raise ShapeError(f"{path}: expected a list, found {type(value)}")
    return value


def _require_string_list(value, path: str) -> None:
    items = _require_list(value, path)
    for i, item in enumerate(items):
        _require_type(item, str, f"{path}[{i}]")


def _require_id(value, path: str) -> None:
    """An id must be a string matching the identifier grammar."""
    if not is_valid_identifier(value):
        raise ShapeError(
            f"{path}: invalid identifier {value!r} — ids must match "
            r"[a-z0-9][a-z0-9_-]* (lowercase letter/digit first, then "
            "lowercase letters, digits, '-' or '_'; no separators, no '..', "
            "no leading dot or dash, no trailing newline, max 64 chars, "
            "no Windows-reserved device name)"
        )


_ROOT_KEYS = {
    "version",
    "organization",
    "departments",
    "roles",
    "actors",
    "humans",
    "policies",
    "escalation_matrix",
    "communication",
}
_ORG_KEYS = {"id", "name", "sector", "languages", "default_language"}
_DEPARTMENT_KEYS = {"id", "name", "access_policy", "parent"}
_ROLE_KEYS = {
    "id",
    "name",
    "department",
    "access_level",
    "reports_to",
    "reports_to_human",
    "functions",
    "security_exceptions",
    "soul_line_budget",
    "description",
}
_ACTOR_KEYS = {
    "id",
    "role",
    "tools",
    "tools_excluded",
    "actor_exceptions",
    "telegram_bot",
    "tone",
    "npub",
}
_HUMAN_KEYS = {
    "id",
    "name",
    "role",
    "telegram_user_id",
    "npub",
}
_POLICIES_KEYS = {"access_levels", "security_categories"}
_ACCESS_LEVEL_KEYS = {"label", "categories"}
_SECURITY_CATEGORY_KEYS = {"label", "scope", "owner"}
_ESCALATION_KEYS = {"from", "to", "condition", "cross_department"}
_COMMUNICATION_KEYS = {
    "request_id_format",
    "message_types",
    "max_hops",
    "norm_version",
    "envelope",
    "channels",
}
_CHANNEL_HUMAN_KEYS = {"platform", "group", "chat_id"}
_CHANNEL_AGENT_KEYS = {
    "platform",
    "relay",
    "bridge_npub",
    "human_npubs",
    "public_relays",
}
_ENVELOPE_KEYS = {"marker", "ttl_hours"}


def validate_shape(raw: dict) -> None:
    """Raises ShapeError with the first problem found; otherwise returns nothing."""
    raw = _require_mapping(raw, "(root)")
    _reject_unknown_keys(raw, _ROOT_KEYS, "(root)")

    for key in [
        "version",
        "organization",
        "departments",
        "roles",
        "actors",
        "policies",
        "escalation_matrix",
        "communication",
    ]:
        _require(raw, key, "(root)")

    _require_type(raw["version"], int, "version")
    if raw["version"] != 1:
        raise ShapeError(f"version: expected 1, found {raw['version']}")

    org = _require_mapping(raw["organization"], "organization")
    _reject_unknown_keys(org, _ORG_KEYS, "organization")
    for key in ["id", "name", "sector", "languages"]:
        _require(org, key, "organization")
    _require_id(org["id"], "organization.id")
    _require_type(org["name"], str, "organization.name")
    _require_type(org["sector"], str, "organization.sector")
    _require_string_list(org["languages"], "organization.languages")
    if not org["languages"]:
        raise ShapeError("organization.languages: cannot be empty")
    _require_optional_type(
        org.get("default_language"), str, "organization.default_language"
    )

    departments = _require_list(raw["departments"], "departments")
    if not departments:
        raise ShapeError("departments: must have at least 1 entry")
    for i, d in enumerate(departments):
        d = _require_mapping(d, f"departments[{i}]")
        _reject_unknown_keys(d, _DEPARTMENT_KEYS, f"departments[{i}]")
        for key in ["id", "name", "access_policy"]:
            _require(d, key, f"departments[{i}]")
        _require_id(d["id"], f"departments[{i}].id")
        _require_type(d["name"], str, f"departments[{i}].name")
        _require_type(d["access_policy"], str, f"departments[{i}].access_policy")
        if "parent" not in d:
            raise ShapeError(
                f"departments[{i}].parent: required field missing (can be null)"
            )
        _require_optional_type(d["parent"], str, f"departments[{i}].parent")

    roles = _require_list(raw["roles"], "roles")
    if not roles:
        raise ShapeError("roles: must have at least 1 entry")
    for i, r in enumerate(roles):
        r = _require_mapping(r, f"roles[{i}]")
        _reject_unknown_keys(r, _ROLE_KEYS, f"roles[{i}]")
        for key in ["id", "name", "department", "access_level"]:
            _require(r, key, f"roles[{i}]")
        _require_id(r["id"], f"roles[{i}].id")
        _require_type(r["name"], str, f"roles[{i}].name")
        _require_type(r["department"], str, f"roles[{i}].department")
        _require_type(r["access_level"], str, f"roles[{i}].access_level")
        if "reports_to" not in r:
            raise ShapeError(
                f"roles[{i}].reports_to: required field missing (can be null)"
            )
        _require_optional_type(r["reports_to"], str, f"roles[{i}].reports_to")
        _require_optional_type(
            r.get("reports_to_human"), str, f"roles[{i}].reports_to_human"
        )
        _require_string_list(r.get("functions", []), f"roles[{i}].functions")
        _require_string_list(
            r.get("security_exceptions", []), f"roles[{i}].security_exceptions"
        )
        # soul_line_budget is optional (default 300), but an explicit null
        # is not an integer (schema.json: {"type": "integer", "minimum": 50})
        # and would crash check_soul_budget with a TypeError after a
        # successful build.
        if "soul_line_budget" in r:
            _require_type(r["soul_line_budget"], int, f"roles[{i}].soul_line_budget")
            if r["soul_line_budget"] < 50:
                raise ShapeError(
                    f"roles[{i}].soul_line_budget: must be >= 50, "
                    f"found {r['soul_line_budget']}"
                )
        _require_optional_type(r.get("description"), str, f"roles[{i}].description")

    actors = _require_list(raw["actors"], "actors")
    if not actors:
        raise ShapeError("actors: must have at least 1 entry")
    for i, a in enumerate(actors):
        a = _require_mapping(a, f"actors[{i}]")
        _reject_unknown_keys(a, _ACTOR_KEYS, f"actors[{i}]")
        for key in ["id", "role"]:
            _require(a, key, f"actors[{i}]")
        _require_id(a["id"], f"actors[{i}].id")
        _require_type(a["role"], str, f"actors[{i}].role")
        if "tools" not in a:
            raise ShapeError(f"actors[{i}].tools: required field missing")
        _require_string_list(a["tools"], f"actors[{i}].tools")
        _require_string_list(a.get("tools_excluded", []), f"actors[{i}].tools_excluded")
        _require_string_list(
            a.get("actor_exceptions", []), f"actors[{i}].actor_exceptions"
        )
        _require_optional_type(a.get("telegram_bot"), str, f"actors[{i}].telegram_bot")
        if a.get("telegram_bot") is not None and not _is_valid_telegram_handle(a["telegram_bot"]):
            raise ShapeError(
                f"actors[{i}].telegram_bot: invalid handle {a['telegram_bot']!r} — "
                "expected '@' + 5..32 chars of [A-Za-z0-9_] "
                "(the bot's REAL username, e.g. '@CEO_bot'; "
                "verify with `po telegram-check`)"
            )
        _require_optional_type(a.get("tone"), str, f"actors[{i}].tone")
        # npub is optional (bots may not have phantomchat configured yet),
        # but when present it must be a valid NIP-19 bech32 public key.
        _require_optional_type(a.get("npub"), str, f"actors[{i}].npub")
        if a.get("npub") is not None and not is_valid_npub(a["npub"]):
            raise ShapeError(
                f"actors[{i}].npub: invalid NIP-19 npub {a['npub']!r} — "
                "expected 'npub1' + 62 bech32 chars with a valid checksum"
            )

    # humans is an OPTIONAL registry of human counterparts (Board
    # president, treasurer, etc.). Unlike roles/actors it may be an
    # empty list (an org without a declared humans block is valid);
    # when entries exist, each must have a valid id, and
    # telegram_user_id / npub are nullable until the human has a real
    # token or key (npub, when set, must be a valid NIP-19 key).
    if "humans" in raw:
        humans = _require_list(raw["humans"], "humans")
        for i, h in enumerate(humans):
            h = _require_mapping(h, f"humans[{i}]")
            _reject_unknown_keys(h, _HUMAN_KEYS, f"humans[{i}]")
            _require(h, "id", f"humans[{i}]")
            _require_id(h["id"], f"humans[{i}].id")
            _require_optional_type(h.get("name"), str, f"humans[{i}].name")
            _require_optional_type(h.get("role"), str, f"humans[{i}].role")
            _require_optional_type(
                h.get("telegram_user_id"), int, f"humans[{i}].telegram_user_id"
            )
            _require_optional_type(h.get("npub"), str, f"humans[{i}].npub")
            if h.get("npub") is not None and not is_valid_npub(h["npub"]):
                raise ShapeError(
                    f"humans[{i}].npub: invalid NIP-19 npub {h['npub']!r} — "
                    "expected 'npub1' + 62 bech32 chars with a valid checksum"
                )

    policies = _require_mapping(raw["policies"], "policies")
    _reject_unknown_keys(policies, _POLICIES_KEYS, "policies")
    for key in ["access_levels", "security_categories"]:
        _require(policies, key, "policies")

    access_levels = _require_mapping(
        policies["access_levels"], "policies.access_levels"
    )
    for level_id, level in access_levels.items():
        _require_id(level_id, f"policies.access_levels.{level_id}")
        level = _require_mapping(level, f"policies.access_levels.{level_id}")
        _reject_unknown_keys(
            level, _ACCESS_LEVEL_KEYS, f"policies.access_levels.{level_id}"
        )
        _require(level, "label", f"policies.access_levels.{level_id}")
        _require_type(level["label"], str, f"policies.access_levels.{level_id}.label")
        if "categories" not in level:
            raise ShapeError(
                f"policies.access_levels.{level_id}.categories: required field missing"
            )
        cats = _require_list(
            level["categories"], f"policies.access_levels.{level_id}.categories"
        )
        for j, c in enumerate(cats):
            _require_type(c, int, f"policies.access_levels.{level_id}.categories[{j}]")

    security_categories = _require_mapping(
        policies["security_categories"], "policies.security_categories"
    )
    for cat_id, cat in security_categories.items():
        _require_id(cat_id, f"policies.security_categories.{cat_id}")
        cat = _require_mapping(cat, f"policies.security_categories.{cat_id}")
        _reject_unknown_keys(
            cat, _SECURITY_CATEGORY_KEYS, f"policies.security_categories.{cat_id}"
        )
        _require(cat, "label", f"policies.security_categories.{cat_id}")
        _require_type(cat["label"], str, f"policies.security_categories.{cat_id}.label")
        _require_optional_type(
            cat.get("scope"), str, f"policies.security_categories.{cat_id}.scope"
        )
        if (
            "scope" in cat
            and cat["scope"] is not None
            and cat["scope"] not in VALID_SCOPES
        ):
            raise ShapeError(
                f"policies.security_categories.{cat_id}.scope: '{cat['scope']}' is "
                f"not a valid scope (valid: {sorted(VALID_SCOPES)})"
            )
        _require_optional_type(
            cat.get("owner"), str, f"policies.security_categories.{cat_id}.owner"
        )

    escalation = _require_list(raw["escalation_matrix"], "escalation_matrix")
    for i, e in enumerate(escalation):
        e = _require_mapping(e, f"escalation_matrix[{i}]")
        _reject_unknown_keys(e, _ESCALATION_KEYS, f"escalation_matrix[{i}]")
        for key in ["from", "to", "condition"]:
            _require(e, key, f"escalation_matrix[{i}]")
        _require_type(e["from"], str, f"escalation_matrix[{i}].from")
        _require_type(e["to"], str, f"escalation_matrix[{i}].to")
        _require_type(e["condition"], str, f"escalation_matrix[{i}].condition")
        # cross_department is optional but must be a real bool when
        # present; an explicit null violates schema.json's
        # {"type": "boolean"} and silently distinguishes null from false.
        if "cross_department" in e:
            _require_type(
                e["cross_department"], bool, f"escalation_matrix[{i}].cross_department"
            )

    comm = _require_mapping(raw["communication"], "communication")
    _reject_unknown_keys(comm, _COMMUNICATION_KEYS, "communication")
    for key in ["request_id_format", "message_types", "max_hops"]:
        _require(comm, key, "communication")
    _require_type(comm["request_id_format"], str, "communication.request_id_format")
    message_types = _require_list(comm["message_types"], "communication.message_types")
    for mt in message_types:
        _require_type(mt, str, "communication.message_types")
        if mt not in VALID_MESSAGE_TYPES:
            raise ShapeError(
                f"communication.message_types: '{mt}' is not a valid type "
                f"(valid: {sorted(VALID_MESSAGE_TYPES)})"
            )
    _require_type(comm["max_hops"], int, "communication.max_hops")
    if comm["max_hops"] < 1:
        raise ShapeError(
            f"communication.max_hops: must be >= 1, found {comm['max_hops']}"
        )
    _require_optional_type(comm.get("norm_version"), str, "communication.norm_version")
    if "channels" in comm:
        channels = _require_mapping(comm["channels"], "communication.channels")
        _reject_unknown_keys(channels, {"human", "agent"}, "communication.channels")
        if "human" in channels:
            h = _require_mapping(channels["human"], "communication.channels.human")
            _reject_unknown_keys(h, _CHANNEL_HUMAN_KEYS, "communication.channels.human")
            _require(h, "platform", "communication.channels.human")
            _require_type(h["platform"], str, "communication.channels.human.platform")
        if "agent" in channels:
            a = _require_mapping(channels["agent"], "communication.channels.agent")
            _reject_unknown_keys(a, _CHANNEL_AGENT_KEYS, "communication.channels.agent")
            _require(a, "platform", "communication.channels.agent")
            _require_type(a["platform"], str, "communication.channels.agent.platform")
            if "relay" in a:
                _require_type(a["relay"], str, "communication.channels.agent.relay")
            if "bridge_npub" in a:
                _require_type(
                    a["bridge_npub"],
                    str,
                    "communication.channels.agent.bridge_npub",
                )
            for list_key in ("human_npubs", "public_relays"):
                if list_key in a:
                    _require_string_list(
                        a[list_key], f"communication.channels.agent.{list_key}"
                    )
    if "envelope" in comm:
        env = _require_mapping(comm["envelope"], "communication.envelope")
        _reject_unknown_keys(env, _ENVELOPE_KEYS, "communication.envelope")
        if "marker" in env:
            marker = env["marker"]
            _require_type(marker, str, "communication.envelope.marker")
            if marker != "[env]":
                raise ShapeError(
                    "communication.envelope.marker: constante de protocolo "
                    "fija (PhantomBridge la tiene hardcodeada); debe ser "
                    f"'[env]', found {marker!r}"
                )
        if "ttl_hours" in env:
            ttl = env["ttl_hours"]
            _require_type(ttl, int, "communication.envelope.ttl_hours")
            if ttl < 1:
                raise ShapeError(
                    f"communication.envelope.ttl_hours: must be >= 1, found {ttl}"
                )
