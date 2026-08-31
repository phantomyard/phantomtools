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
The message signed is a **canonical mutation envelope** — a deterministic
serialization of the node MAC together with the authorization-relevant fields
(actor, action, category, owners, locations, urn) — domain-separated by a
fixed prefix so a signature can never be replayed across a different protocol.

Signing the envelope (rather than the bare MAC) is what makes the v2
boundary an *authorization* boundary and not merely a tamper seal: the MAC
covers only content identity, so a signature over the MAC alone cannot
prevent one declared actor from authenticating a mutation asserted as a
different actor, or a mutation whose category/owners/locations were swapped
underneath a still-valid MAC. Binding those fields into the signed message
means ``verify`` can reconstruct the exact envelope from the stored node and
reject a signature that does not cover the node as it currently stands.
"""

from __future__ import annotations

import hashlib
import json

import coincurve

# Domain separator so a phantomdocs mutation signature can never be confused
# with a signature over the same bytes in another protocol.
_SIGN_DOMAIN = b"phantomdocs-mutation-v1"

# Domain separator for the org's head seal (issue #70/#71): a signature over
# the namespace root + head commitment, never confused with a mutation sig.
_SEAL_DOMAIN = b"phantomdocs-seal-v1"

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


def _convertbits(
    data: list[int], frombits: int, tobits: int, pad: bool = True
) -> list[int] | None:
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


def mutation_envelope(
    *,
    mac: str,
    actor: str,
    action: str,
    category: str,
    owners: list[str] | None,
    locations: list[dict] | None,
    urn: str,
    ref: str | None = None,
    seq: int | None = None,
    prev_head: str | None = None,
) -> bytes:
    """The canonical bytes signed for a mutation (issue #30 v2, #73).

    Deterministic JSON (sorted keys, compact separators, ASCII-escaped) so
    ``verify`` can rebuild the exact message from the stored node fields and
    check that the signature covers the authorization-relevant metadata — not
    just the content-addressed MAC. Any field change (actor, action,
    category, owners, locations, urn) changes the signed message and
    invalidates the signature.

    ``ref`` is set only for ``tag`` mutations: the mutable ref name is bound
    into the envelope so a ref renamed or repointed after signing no longer
    verifies (issue #30 v2 / PR #38).

    ``seq`` and ``prev_head`` bind the mutation to a specific committed
    state (issue #73): ``seq`` is the monotonic mutation sequence and
    ``prev_head`` is the committed head MAC this mutation builds on. A
    mutation whose signature was produced for an older state (a replay, or a
    re-insertion after rollback) no longer verifies against the current
    state, because its ``seq``/``prev_head`` do not match the chain.
    """
    payload = {
        "actor": actor,
        "action": action,
        "category": category,
        "locations": list(locations) if locations else [],
        "mac": mac,
        "owners": list(owners) if owners else [],
        "urn": urn,
    }
    if ref is not None:
        payload["ref"] = ref
    if seq is not None:
        payload["seq"] = seq
    if prev_head is not None:
        payload["prev_head"] = prev_head
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def mutation_message(envelope: bytes) -> bytes:
    """The 32-byte message signed for a mutation: H(domain || envelope)."""
    return _sha256(_SIGN_DOMAIN + envelope)


def sign_mutation(nsec: str, envelope: bytes) -> str:
    """Schnorr-sign a mutation envelope with the actor's nsec. 128-hex."""
    secret = bytes.fromhex(nsec_to_secret_hex(nsec))
    return (
        coincurve.PrivateKey(secret)
        .sign_schnorr(mutation_message(envelope), None)
        .hex()
    )


def verify_mutation(pubkey_hex: str, signature_hex: str, envelope: bytes) -> bool:
    """Verify a mutation signature over ``envelope`` against an x-only pubkey."""
    try:
        pubkey = coincurve.PublicKeyXOnly(bytes.fromhex(pubkey_hex))
        signature = bytes.fromhex(signature_hex)
        return pubkey.verify(signature, mutation_message(envelope))
    except (ValueError, TypeError):
        return False


def seal_envelope(
    *,
    root_mac: str,
    head_seq: int,
    head_mac: str,
    audit_seq: int,
    audit_head: str | None,
) -> bytes:
    """The canonical bytes the org signs to seal the namespace head.

    Covers the root MAC **and** the monotonic head state (issues #70/#71), so
    a forged root, a deleted version, a rolled-back head, or a truncated audit
    log all change the envelope and invalidate the seal — which only the org
    (holder of the org key) can re-make.
    """
    payload = {
        "audit_head": audit_head or "",
        "audit_seq": audit_seq,
        "head_mac": head_mac,
        "head_seq": head_seq,
        "root_mac": root_mac,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def seal_message(envelope: bytes) -> bytes:
    """The 32-byte message signed for a head seal: H(domain || envelope)."""
    return _sha256(_SEAL_DOMAIN + envelope)


def sign_seal(nsec: str, envelope: bytes) -> str:
    """Schnorr-sign a head seal envelope with the org's nsec. 128-hex."""
    secret = bytes.fromhex(nsec_to_secret_hex(nsec))
    return coincurve.PrivateKey(secret).sign_schnorr(seal_message(envelope), None).hex()


def verify_seal(pubkey_hex: str, signature_hex: str, envelope: bytes) -> bool:
    """Verify a head seal signature over ``envelope`` against an x-only pubkey."""
    try:
        pubkey = coincurve.PublicKeyXOnly(bytes.fromhex(pubkey_hex))
        signature = bytes.fromhex(signature_hex)
        return pubkey.verify(signature, seal_message(envelope))
    except (ValueError, TypeError):
        return False
