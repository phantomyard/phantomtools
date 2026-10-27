"""Short-lived dashboard grants issued to verified Nostr/PhantomChat identities."""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from . import core

LIFETIME = 5 * 60
REQUEST_WINDOW = 60


def public_origin(value, port):
    """Accept an origin only, never credentials, paths, fragments or cleartext WAN."""
    if value is None:
        return f"http://127.0.0.1:{port}"
    try:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and parsed.hostname != "127.0.0.1")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or value != f"{parsed.scheme}://{parsed.netloc}"
            or "\\" in value
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise core.Invalid(
            "Public origin must be an HTTPS origin or loopback HTTP origin"
        ) from exc
    return value


def _signer():
    try:
        from coincurve import PrivateKey
    except ImportError as exc:
        raise core.Invalid(
            "Install phantomblog[access] to sign access requests"
        ) from exc
    return PrivateKey


def require_verifier():
    """Fail before opening a dashboard listener if its access extra is missing."""
    try:
        from coincurve import PublicKeyXOnly
    except ImportError as exc:
        raise core.Invalid(
            "Install phantomblog[access] to verify signed access requests: "
            "python3 -m pip install 'phantomblog[access]' "
            "(or python3 -m pip install '.[access]' from the phantomblog directory)"
        ) from exc
    return PublicKeyXOnly


def _secret_bytes(value):
    """Decode runtime hex or NIP-19 nsec with strict checksum and padding."""
    if not isinstance(value, str):
        raise TypeError
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return bytes.fromhex(value)
    if value != value.lower() or not value.startswith("nsec1") or len(value) != 63:
        raise ValueError
    alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    data = [alphabet.index(c) for c in value[5:]]
    hrp = "nsec"
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    checksum = 1
    for digit in expanded + data:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ digit
        for bit, generator in enumerate(
            (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
        ):
            if (top >> bit) & 1:
                checksum ^= generator
    if checksum != 1:
        raise ValueError
    result = bytearray()
    accumulator = bits = 0
    for digit in data[:-6]:
        accumulator = (accumulator << 5) | digit
        bits += 5
        if bits >= 8:
            bits -= 8
            result.append((accumulator >> bits) & 255)
    if len(result) != 32 or bits >= 5 or ((accumulator << (8 - bits)) & 255):
        raise ValueError
    return bytes(result)


def persona_event(persona, persona_dir, port):
    """Read only the selected store/<id>/identity.json; never create or mutate it."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise core.Invalid("Dashboard port must be between 1 and 65535")
    signer = _signer()
    if not isinstance(persona, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", persona):
        raise core.Invalid("Invalid persona id")
    store = persona_dir or os.environ.get("PHANTOMBLOG_PERSONA_DIR")
    if not store:
        raise core.Invalid(
            "Supply --persona-dir or PHANTOMBLOG_PERSONA_DIR (persona store)"
        )
    try:
        root = Path(store).resolve(strict=True)
        identity = core.contained(root, persona + "/identity.json", True)
        with identity.open("rb") as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError
        value = core.decode(raw)
        key = signer(_secret_bytes(value["nsec"]))
    except (OSError, ValueError, TypeError, KeyError, core.Invalid) as exc:
        raise core.Invalid("Persona identity is unavailable or invalid") from exc
    event = {
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": int(time.time()),
        "kind": 27235,
        "tags": [
            ["u", public_origin(None, port) + "/api/access-link"],
            ["method", "POST"],
            ["payload", hashlib.sha256(b"{}").hexdigest()],
            ["nonce", secrets.token_hex(16)],
        ],
        "content": "",
    }
    event["id"] = event_digest(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def event_digest(event):
    """NIP-01 canonical event identifier; never includes the signature."""
    value = [
        0,
        event["pubkey"],
        event["created_at"],
        event["kind"],
        event["tags"],
        event["content"],
    ]
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class AccessLinks:
    def __init__(self, identities=(), clock=time.monotonic, wall_clock=time.time):
        self.identities = frozenset(identities)
        if any(
            not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
            for key in self.identities
        ):
            raise core.Invalid(
                "Access identities must be lowercase 64-character Nostr public keys"
            )
        self.clock = clock
        self.wall_clock = wall_clock
        self.lock = threading.Lock()
        self.grants = {}
        self.requests = {}
        self.public_key = None
        if self.identities:
            self.public_key = require_verifier()

    def issue(self, authorization, origin, delivery_origin=None):
        """Verify a fresh, single-use NIP-98 POST authorization before minting."""
        try:
            if (
                not self.identities
                or not isinstance(authorization, str)
                or not authorization.startswith("Nostr ")
                or len(authorization) > 8192
            ):
                raise ValueError
            event = core.decode(base64.b64decode(authorization[6:], validate=True))
            if not isinstance(event, dict) or set(event) != {
                "id",
                "pubkey",
                "created_at",
                "kind",
                "tags",
                "content",
                "sig",
            }:
                raise ValueError
            if (
                event["pubkey"] not in self.identities
                or type(event["kind"]) is not int
                or event["kind"] != 27235
            ):
                raise ValueError
            if (
                type(event["created_at"]) is not int
                or abs(self.wall_clock() - event["created_at"]) > REQUEST_WINDOW
            ):
                raise ValueError
            tags = event["tags"]
            if (
                event["content"] != ""
                or not isinstance(tags, list)
                or not all(
                    isinstance(t, list) and all(isinstance(v, str) for v in t)
                    for t in tags
                )
            ):
                raise ValueError
            expected = {
                "u": origin + "/api/access-link",
                "method": "POST",
                "payload": hashlib.sha256(b"{}").hexdigest(),
            }
            for key, value in expected.items():
                if [t for t in tags if t and t[0] == key] != [[key, value]]:
                    raise ValueError
            if (
                not isinstance(event["id"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", event["id"])
                or event_digest(event) != event["id"]
            ):
                raise ValueError
            if not isinstance(event["sig"], str) or not re.fullmatch(
                r"[0-9a-f]{128}", event["sig"]
            ):
                raise ValueError
            if not self.public_key(bytes.fromhex(event["pubkey"])).verify(
                bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, core.Invalid) as exc:
            raise core.Invalid("Signed access request refused") from exc
        with self.lock:
            now = self.clock()
            self.grants = {
                key: expires for key, expires in self.grants.items() if expires > now
            }
            self.requests = {
                key: expires for key, expires in self.requests.items() if expires > now
            }
            if (
                event["id"] in self.requests
                or len(self.grants) >= 1000
                or len(self.requests) >= 1000
            ):
                raise core.Invalid("Signed access request refused")
            code = secrets.token_urlsafe(32)
            self.grants[hashlib.sha256(code.encode()).digest()] = now + LIFETIME
            self.requests[event["id"]] = now + 2 * REQUEST_WINDOW + 1
            return (delivery_origin or origin) + "/#c=" + code

    def redeem(self, code, create_session):
        """Consume only after session creation succeeds, atomically across threads."""
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            raise core.Invalid("Access link is invalid, expired or already used")
        digest = hashlib.sha256(code.encode()).digest()
        with self.lock:
            if self.grants.get(digest, 0) <= self.clock():
                self.grants.pop(digest, None)
                raise core.Invalid("Access link is invalid, expired or already used")
            session = create_session()
            del self.grants[digest]
            return session


def request_link(event, port, origin=None):
    """Send an already-signed event to the fixed loopback issuer, without redirects."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise core.Invalid("Dashboard port must be between 1 and 65535")
    origin = public_origin(origin, port)
    authorization = "Nostr " + base64.b64encode(core.encoded(event)).decode("ascii")
    if len(authorization) > 8192:
        raise core.Invalid("Signed access request is too large")
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            "/api/access-link",
            b"{}",
            {"Content-Type": "application/json", "Authorization": authorization},
        )
        response = connection.getresponse()
        value = core.decode(response.read(8193))
        if response.status != 200 or not isinstance(value, dict):
            raise core.Invalid("Signed access request refused")
        link = value.get("link")
        if not isinstance(link, str) or not re.fullmatch(
            re.escape(origin) + r"/#c=[A-Za-z0-9_-]{43}", link
        ):
            raise core.Invalid("Invalid access link response")
        return link
    finally:
        connection.close()
