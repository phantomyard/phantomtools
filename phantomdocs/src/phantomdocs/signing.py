"""Mutation signing (issue #30 v2): bind authorship to the actor's Nostr key.

PhantomDocs' integrity guarantee (chained MACs) detects *tampering* but not
*who* wrote a node: a process with filesystem write access can add a node
with a valid MAC. The v2 authorization boundary binds each mutation to the
actor that made it, using the persona's existing Nostr identity
(``npub`` in org.yaml, ``nsec`` in the persona's vault):

  - a mutating command MAY sign the node MAC with the actor's nsec
    (``PHANTOMDOCS_NSEC`` env or ``--nsec-file``);
  - the signature + x-only pubkey are recorded on the node and in the audit
    entry;
  - ``pd verify --org-yaml`` then checks that a signed node's signature
    verifies against the actor's declared ``npub``, so an unauthorized write
    (signed with the wrong key, or unsigned where the namespace requires
    signatures) leaves a detectable trace.

Nostr uses BIP-340 Schnorr over secp256k1 with x-only (32-byte) public keys.
The message signed is the node's MAC (32 bytes) — already a content-addressed
identity — domain-separated by a fixed prefix so a signature can never be
replayed across a different protocol.
"""

from __future__ import annotations

import hashlib

import coincurve

# Domain separator so a phantomdocs mutation signature can never be confused
# with a signature over the same bytes in another protocol.
_SIGN_DOMAIN = b"phantomdocs-mutation-v1"

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generators[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def bech32_decode(bech: str) -> tuple[str, bytes]:
    """Decode a bech32 string to ``(hrp, data bytes)``.

    Minimal, strict implementation for the Nostr ``npub``/``nsec`` encodings
    (no length limit beyond the checksum). Raises ``ValueError`` on any
    malformed input.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        raise ValueError("invalid bech32 characters")
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        raise ValueError("invalid bech32 separator or length")
    hrp = bech[:pos]
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise ValueError("invalid hrp")
    data = [_BECH32_CHARSET.find(c) for c in bech[pos + 1 :]]
    if any(x == -1 for x in data):
        raise ValueError("invalid data character")
    if _bech32_polymod(_bech32_expand(hrp) + data) != 1:
        raise ValueError("invalid bech32 checksum")
    payload = _convertbits(data[:-6], 5, 8, pad=False)
    if payload is None:
        raise ValueError("invalid bech32 payload")
    return hrp, bytes(payload)


def npub_to_pubkey_hex(npub: str) -> str:
    """Decode a Nostr ``npub1...`` to the 32-byte x-only pubkey as hex."""
    hrp, data = bech32_decode(npub)
    if hrp != "npub":
        raise ValueError(f"expected npub, got hrp {hrp!r}")
    if len(data) != 32:
        raise ValueError("npub must encode 32 bytes")
    return data.hex()


def nsec_to_secret_hex(nsec: str) -> str:
    """Decode a Nostr ``nsec1...`` (or a bare 64-char hex secret) to hex.

    ``PHANTOMDOCS_NSEC`` may hold either form; a raw 32-byte hex secret is
    accepted unchanged for operators who keep hex keys rather than bech32.
    """
    if nsec.startswith("nsec1"):
        hrp, data = bech32_decode(nsec)
        if hrp != "nsec":
            raise ValueError(f"expected nsec, got hrp {hrp!r}")
        if len(data) != 32:
            raise ValueError("nsec must encode 32 bytes")
        return data.hex()
    secret = nsec.strip().lower()
    if len(secret) == 64 and all(c in "0123456789abcdef" for c in secret):
        return secret
    raise ValueError("nsec must be nsec1... or 64 hex chars")


def pubkey_from_nsec(nsec: str) -> str:
    """The x-only pubkey hex for a nsec (``nsec1...`` or 64 hex chars)."""
    secret_hex = nsec_to_secret_hex(nsec)
    secret = bytes.fromhex(secret_hex)
    return coincurve.PublicKeyXOnly.from_valid_secret(secret).format().hex()


def mutation_message(mac: str) -> bytes:
    """The 32-byte message signed for a mutation: H(domain || mac)."""
    return _sha256(_SIGN_DOMAIN + bytes.fromhex(mac))


def sign_mutation(nsec: str, mac: str) -> str:
    """Schnorr-sign the mutation MAC with the actor's nsec. Returns 128-hex."""
    secret = bytes.fromhex(nsec_to_secret_hex(nsec))
    return coincurve.PrivateKey(secret).sign_schnorr(mutation_message(mac), None).hex()


def verify_mutation(pubkey_hex: str, signature_hex: str, mac: str) -> bool:
    """Verify a mutation signature against an x-only pubkey (hex)."""
    try:
        pubkey = coincurve.PublicKeyXOnly(bytes.fromhex(pubkey_hex))
        signature = bytes.fromhex(signature_hex)
        return pubkey.verify(signature, mutation_message(mac))
    except (ValueError, TypeError):
        return False
