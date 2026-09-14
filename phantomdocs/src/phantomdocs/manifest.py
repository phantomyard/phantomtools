"""Manifest — the single source of truth (spec §6).

A per-namespace YAML document mapping identity (urn + MAC) to location,
metadata, classification and relations. Version 1 is single-tenant.

Versioning: a URN may map to several nodes (one per version, each with its own
MAC). The *current* version is an explicit head ref — ``currentVersions[urn]``
(issue #99) — not an array position; earlier versions are linked via the
`previous` field (git commit-parent style), and the head ref is validated
against the MAC-chained lineage tip.

Concurrency: every mutating command performs a read-modify-write under an
inter-process lock (``manifest.lock``), and each write uses its own unique
``mkstemp`` file, so concurrent personas never clobber each other's updates
or rename the same temp file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml

from .fsutil import fsync_dir
from .identity import is_valid_hex64
from .signing import CRYPTO_VERSION

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.yaml"
LOCK_FILENAME = "manifest.lock"

# Node kinds accepted in the manifest schema.
VALID_KINDS = {"folder", "doc"}

# A security-category id: `category-<digits>` optionally followed by one or
# more `-<scope>` segments (`category-4-almaponia`). Kept in sync with the
# access resolver's notion of a category grant (access.py).
_CATEGORY_ID_RE = re.compile(r"^category-\d+(?:-[A-Za-z0-9][A-Za-z0-9_-]*)*$")


class ManifestError(ValueError):
    """Raised when a manifest fails validation."""


def empty_manifest(
    org: str, namespace: str, root_mac: str, require_signatures: bool = False
) -> dict[str, Any]:
    """A fresh, valid single-tenant manifest.

    The header carries a monotonic mutation head (``headSeq``, the number of
    committed mutations) and a structural node head (``headMac``, the MAC of
    the last committed node), plus an audit-log head (``auditSeq`` /
    ``auditHead``) so that `pd verify` can detect a mutation whose manifest
    commit and audit entry diverged (crash between the two) and an audit log
    that has been truncated or rolled back relative to the manifest (issues
    #71/#74).

    ``headSeq`` and ``auditHead`` advance on *every* mutation (including
    ``tag``); ``headMac`` advances only on node-producing mutations
    (``mkdir``/``add``/``version``/``rollback``), because a tag creates no
    node. The canonical identity of the last mutation is ``auditHead`` (the
    hash of its audit entry).
    """
    return {
        "manifest": {
            "version": MANIFEST_VERSION,
            "cryptoVersion": CRYPTO_VERSION,
            "org": org,
            "namespace": namespace,
            "tenant": "single",
            "rootMac": root_mac,
            "signedRootMac": None,
            "sealPubkey": None,
            "sealedHeadSeq": None,
            # Seal-key lifecycle (issue #104): the org identity key that anchors
            # the namespace, the seal keys it has authorized, and the
            # append-only history of seals made. See the module helpers below.
            "sealIdentityNpub": None,
            "sealKeys": [],
            "seals": [],
            "requireSignatures": require_signatures,
            "profileTransition": None,
            "headSeq": 0,
            "headMac": root_mac,
            "auditSeq": 0,
            "auditHead": None,
        },
        "refs": {},
        # Explicit per-URN head refs (issue #99): "current" is a field, not an
        # array position. ``currentVersions[urn]`` names the version MAC that
        # is current for that URN; the path/slug/urn resolvers read this map
        # instead of taking ``matches[-1]``. Manifests written before issue
        # #99 have no map and fall back to the MAC-chained lineage tip.
        "currentVersions": {},
        "nodes": [],
    }


def load(path: str) -> dict[str, Any]:
    """Load and validate a manifest from disk."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    errors = validate(data)
    if errors:
        raise ManifestError("; ".join(errors))
    return data


def save(path: str, data: dict[str, Any]) -> None:
    """Atomically write a manifest to disk using a unique temp file.

    The temp name is unique per call (``mkstemp``), so concurrent writers
    can never collide on the same temp path. The caller is expected to hold
    the inter-process lock (``manifest_lock``) across its read-modify-write,
    but the atomic replace is correct even without it.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manifest-", suffix=".tmp")
    fd_unclaimed = True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # fdopen has taken ownership. The context manager closes it
            # before an exception reaches the cleanup handler.
            fd_unclaimed = False
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(directory)
    except BaseException:
        if fd_unclaimed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def manifest_lock(path: str) -> Iterator[None]:
    """Hold an inter-process lock for ``path`` across a read-modify-write.

    POSIX uses ``fcntl.flock`` on a sidecar ``manifest.lock``; Windows falls
    back to an ``msvcrt`` range lock. The lock is advisory but serializes the
    mutating commands, so a concurrent ``add``/``tag``/``mkdir`` cannot lose
    the other's update.

    The lock is **host-local**: it serializes concurrent processes on the same
    host, not across hosts. PhantomDocs v1 supports a single authoritative
    writer host per namespace (Model A); two hosts writing the same namespace
    would fork the head (see SPEC §6.3).
    """
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(path)) or ".", LOCK_FILENAME
    )
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def seal_identity_npub(header: dict[str, Any]) -> str | None:
    """The npub of the org identity key recorded at the first seal (#104).

    The identity key is baked into ``root_mac`` and never rotates; ``verify
    --org-pubkey`` cross-checks this field against the externally supplied
    anchor, so a manifest that declares a different identity than the one the
    operator trusts is rejected.
    """
    value = header.get("sealIdentityNpub")
    return value if isinstance(value, str) and value else None


def seal_key_records(header: dict[str, Any]) -> list[dict[str, Any]]:
    """The authorized seal-key lifecycle records (issue #104)."""
    records = header.get("sealKeys")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def seal_key_record(header: dict[str, Any], npub: str) -> dict[str, Any] | None:
    """The lifecycle record for a seal key, or None when never authorized."""
    for rec in seal_key_records(header):
        if rec.get("npub") == npub:
            return rec
    return None


def upsert_seal_key_record(
    header: dict[str, Any],
    *,
    npub: str,
    valid_from: str,
    delegation: str,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """Record (or update) an org-authorized seal key (issue #104).

    The record carries the key's Nostr identity, its authorization window, and
    the org identity key's ``delegation`` signature over that window — the
    authorization that makes the entry trustworthy. A re-authorization updates
    the window and replaces the delegation, because the delegation covers the
    window it authorizes.

    Revocation is deliberately *not* recorded here. Overwriting an
    authorization in place would let anyone able to edit the manifest bring a
    revoked key back by restoring the older record: the org signed that window
    once and that signature never stops being valid. Revoking appends a
    permanent record to ``revocations`` instead (:func:`append_revocation`).
    """
    records = header.setdefault("sealKeys", [])
    for rec in records:
        if isinstance(rec, dict) and rec.get("npub") == npub:
            rec.update(
                {
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "delegation": delegation,
                }
            )
            return rec
    rec = {
        "npub": npub,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "delegation": delegation,
    }
    records.append(rec)
    return rec


def revocation_records(header: dict[str, Any]) -> list[dict[str, Any]]:
    """The append-only seal-key revocation chain (issue #104).

    Each record is ``{n, npub, revoked_at, prevHash, delegation}``: a 1-based
    generation ``n``, the revoked key, the effective timestamp, the hash of
    the chain *before* this record, and the org identity key's signature over
    all of it. The chain is never rewritten — that is what makes a revocation
    irreversible.
    """
    records = header.get("revocations")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def revocations_hash(records: list[dict[str, Any]]) -> str:
    """The canonical 64-hex hash of a revocation chain (or chain prefix).

    Both the chain itself (each record commits to its prefix via ``prevHash``)
    and a published checkpoint (which commits to a whole history) are built on
    this, so a verifier can tell a truncated or rewritten chain from the
    anchored one.
    """
    payload = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revocation_generation(header: dict[str, Any]) -> int:
    """The number of revocations recorded (the chain's high-water mark)."""
    return len(revocation_records(header))


def append_revocation(
    header: dict[str, Any], *, npub: str, revoked_at: str, delegation: str
) -> dict[str, Any]:
    """Append a permanent revocation to the chain (issue #104).

    Returns the new record. The chain is only ever appended to, so a
    revocation cannot be undone by restoring an older manifest: the restored
    copy loses the record, and that loss is exactly what the chain, the head
    seal or a published checkpoint detects.
    """
    records = header.setdefault("revocations", [])
    if not isinstance(records, list):
        raise TypeError("manifest.revocations must be a list")
    rec = {
        "n": len(records) + 1,
        "npub": npub,
        "revoked_at": revoked_at,
        "prevHash": revocations_hash(records),
        "delegation": delegation,
    }
    records.append(rec)
    return rec


def revoked_at_of(header: dict[str, Any], npub: str) -> str | None:
    """When ``npub`` was revoked, or None if it never was.

    Revocation is permanent, so the *earliest* revocation of a key governs: a
    later duplicate cannot restore validity.
    """
    stamps = [
        r["revoked_at"]
        for r in revocation_records(header)
        if r.get("npub") == npub and isinstance(r.get("revoked_at"), str)
    ]
    return min(stamps) if stamps else None


def revocation_chain_issues(header: dict[str, Any]) -> list[str]:
    """Chain-shape problems in the revocation history (issue #104).

    The chain must be contiguous (``n`` = 1..N), each record's ``prevHash``
    must equal the hash of the records before it, and each revoked key must
    still have its org authorization on file — so a deletion, reorder or edit
    anywhere in the history is reported instead of silently accepted.
    """
    issues: list[str] = []
    records = revocation_records(header)
    for index, rec in enumerate(records):
        prefix = f"manifest.revocations[{index}]"
        if rec.get("n") != index + 1:
            issues.append(f"{prefix}: generation {rec.get('n')!r} is not {index + 1}")
        if rec.get("prevHash") != revocations_hash(records[:index]):
            issues.append(f"{prefix}: prevHash does not chain to the previous record")
        if seal_key_record(header, str(rec.get("npub") or "")) is None:
            issues.append(
                f"{prefix}: revokes a key the org never authorized "
                "(its authorization record is missing)"
            )
    return issues


def seal_events(header: dict[str, Any]) -> list[dict[str, Any]]:
    """The append-only seal history (one event per ``pd seal``, #104)."""
    events = header.get("seals")
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)]


def latest_seal_event(
    header: dict[str, Any], *, head_seq: int | None = None
) -> dict[str, Any] | None:
    """The most recent seal event for ``head_seq`` (default: the live seal).

    History is append-only, so a re-seal at the same head (for example a
    rotation at the same commit sequence) leaves the earlier event in place
    and the *latest* matching one wins — the event whose signature is the one
    the manifest currently carries.
    """
    target = header.get("sealedHeadSeq") if head_seq is None else head_seq
    if target is None:
        return None
    for event in reversed(seal_events(header)):
        if event.get("cs") == target:
            return event
    return None


def record_seal_event(
    header: dict[str, Any],
    *,
    npub: str,
    ts: str,
    head_seq: int,
    head_mac: str,
    audit_seq: int,
    audit_head: str | None,
    sig: str,
    require_signatures: bool | None = None,
    crypto_version: int | None = CRYPTO_VERSION,
) -> dict[str, Any]:
    """Append a seal event to the history (issue #104).

    Each event records *what* was sealed (``cs`` = commit sequence, plus the
    head/audit anchors), *who* sealed it (``npub``), *when* (``ts``), the
    signing profile it was sealed under, and the signature — so a seal made
    under a key that has since rotated out or been revoked stays verifiable
    under the key that made it, even after a later profile transition.
    """
    event = {
        "npub": npub,
        "ts": ts,
        "cs": head_seq,
        "headMac": head_mac,
        "auditSeq": audit_seq,
        "auditHead": audit_head,
        "sig": sig,
        "requireSignatures": require_signatures,
        "cryptoVersion": crypto_version,
    }
    header.setdefault("seals", []).append(event)
    return event


def seal_key_valid_at(header: dict[str, Any], rec: dict[str, Any], ts: str) -> bool:
    """True iff a seal key was valid at ``ts`` (issue #104).

    Mirrors ``access.key_valid_at`` for actor keys (#76): the key must not be
    revoked at or before ``ts``, and ``ts`` must fall inside its
    ``valid_from``/``valid_until`` window. Timestamps are ISO-8601 UTC strings
    compared lexicographically.

    The revocation is read from the append-only chain, never from the
    authorization record, so replaying an older authorization cannot un-revoke
    a key.
    """
    revoked_at = revoked_at_of(header, str(rec.get("npub") or ""))
    if revoked_at and ts >= revoked_at:
        return False
    valid_from = rec.get("valid_from")
    if valid_from and ts < valid_from:
        return False
    valid_until = rec.get("valid_until")
    return not (valid_until and ts >= valid_until)


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty == valid).

    Validates the manifest header, the ``nodes`` list (required fields, field
    formats, kind/category shape, unique MACs, known parents, known version
    predecessors, unique versions) and the ``refs`` map. This is a *structural*
    schema check performed at load time; cryptographic integrity (recomputing
    MACs and content hashes) remains ``pd verify``'s job.
    """
    errors: list[str] = []

    # --- header ---
    m = data.get("manifest", {})
    if not isinstance(m, dict):
        errors.append("manifest.manifest must be a mapping")
        m = {}
    if m.get("version") != MANIFEST_VERSION:
        errors.append(f"unsupported manifest version: {m.get('version')!r}")
    if m.get("tenant") == "multi":
        errors.append("multi-tenancy is not supported in this version")
    if not m.get("org"):
        errors.append("manifest.org is required")
    if m.get("cryptoVersion") is not None and not isinstance(
        m.get("cryptoVersion"), int
    ):
        errors.append("manifest.cryptoVersion must be an integer")
    # Head/anchor fields are optional (older manifests lack them) but, when
    # present, must have the right shape so verify can trust them.
    if m.get("headSeq") is not None and not isinstance(m.get("headSeq"), int):
        errors.append("manifest.headSeq must be an integer")
    if m.get("auditSeq") is not None and not isinstance(m.get("auditSeq"), int):
        errors.append("manifest.auditSeq must be an integer")
    if m.get("sealedHeadSeq") is not None and not isinstance(
        m.get("sealedHeadSeq"), int
    ):
        errors.append("manifest.sealedHeadSeq must be an integer")
    if m.get("requireSignatures") is not None and not isinstance(
        m.get("requireSignatures"), bool
    ):
        errors.append("manifest.requireSignatures must be a boolean")
    profile_transition = m.get("profileTransition")
    if profile_transition is not None:
        if not isinstance(profile_transition, dict):
            errors.append("manifest.profileTransition must be a mapping")
        else:
            if profile_transition.get("seq") is not None and not isinstance(
                profile_transition.get("seq"), int
            ):
                errors.append("manifest.profileTransition.seq must be an integer")
            for field in ("actor", "ts", "policyHash"):
                value = profile_transition.get(field)
                if value is not None and not isinstance(value, str):
                    errors.append(
                        f"manifest.profileTransition.{field} must be a string"
                    )
            sig = profile_transition.get("sig")
            if sig is not None and (
                not isinstance(sig, str)
                or len(sig) != 128
                or any(c not in "0123456789abcdef" for c in sig)
            ):
                errors.append("manifest.profileTransition.sig must be 128-hex")
            for field in ("sigPubkey", "prevHead"):
                value = profile_transition.get(field)
                if value is not None and (
                    not isinstance(value, str) or not is_valid_hex64(value)
                ):
                    errors.append(
                        f"manifest.profileTransition.{field} must be a 64-hex string"
                    )
    for field in ("headMac", "sealPubkey"):
        value = m.get(field)
        if value is not None and (
            not isinstance(value, str) or not is_valid_hex64(value)
        ):
            errors.append(f"manifest.{field} must be a 64-hex string")

    # Seal-key lifecycle (issue #104). Shape only: the delegation signature
    # and the seal signatures are cryptographic and checked by `pd verify`;
    # load must stay tolerant so `pd verify` can report a bad seal history
    # instead of both being unable to open the manifest.
    identity_npub = m.get("sealIdentityNpub")
    if identity_npub is not None and (
        not isinstance(identity_npub, str) or not identity_npub.startswith("npub1")
    ):
        errors.append("manifest.sealIdentityNpub must be an npub string")
    seal_keys = m.get("sealKeys")
    if seal_keys is not None:
        if not isinstance(seal_keys, list):
            errors.append("manifest.sealKeys must be a list")
        else:
            for index, rec in enumerate(seal_keys):
                prefix = f"manifest.sealKeys[{index}]"
                if not isinstance(rec, dict):
                    errors.append(f"{prefix}: must be a mapping")
                    continue
                npub = rec.get("npub")
                if not isinstance(npub, str) or not npub.startswith("npub1"):
                    errors.append(f"{prefix}: npub is required")
                if not rec.get("valid_from"):
                    errors.append(f"{prefix}: valid_from is required")
                sig = rec.get("delegation")
                if (
                    not isinstance(sig, str)
                    or len(sig) != 128
                    or any(c not in "0123456789abcdef" for c in sig)
                ):
                    errors.append(f"{prefix}: delegation must be a 128-hex signature")
                for field in ("valid_from", "valid_until"):
                    value = rec.get(field)
                    if value is not None and not isinstance(value, str):
                        errors.append(f"{prefix}.{field} must be an ISO-8601 string")
                if rec.get("revoked_at") is not None:
                    errors.append(
                        f"{prefix}: revoked_at is not an authorization field; "
                        "revocations are append-only in manifest.revocations"
                    )
    revocations = m.get("revocations")
    if revocations is not None:
        if not isinstance(revocations, list):
            errors.append("manifest.revocations must be a list")
        else:
            for index, rec in enumerate(revocations):
                prefix = f"manifest.revocations[{index}]"
                if not isinstance(rec, dict):
                    errors.append(f"{prefix}: must be a mapping")
                    continue
                if not isinstance(rec.get("n"), int):
                    errors.append(f"{prefix}: n must be an integer")
                npub = rec.get("npub")
                if not isinstance(npub, str) or not npub.startswith("npub1"):
                    errors.append(f"{prefix}: npub is required")
                revoked_at = rec.get("revoked_at")
                if not isinstance(revoked_at, str) or not revoked_at:
                    errors.append(f"{prefix}: revoked_at is required")
                prev_hash = rec.get("prevHash")
                if not isinstance(prev_hash, str) or not is_valid_hex64(prev_hash):
                    errors.append(f"{prefix}: prevHash must be 64-hex")
                sig = rec.get("delegation")
                if (
                    not isinstance(sig, str)
                    or len(sig) != 128
                    or any(c not in "0123456789abcdef" for c in sig)
                ):
                    errors.append(f"{prefix}: delegation must be a 128-hex signature")
    seals = m.get("seals")
    if seals is not None:
        if not isinstance(seals, list):
            errors.append("manifest.seals must be a list")
        else:
            for index, event in enumerate(seals):
                prefix = f"manifest.seals[{index}]"
                if not isinstance(event, dict):
                    errors.append(f"{prefix}: must be a mapping")
                    continue
                npub = event.get("npub")
                if not isinstance(npub, str) or not npub.startswith("npub1"):
                    errors.append(f"{prefix}: npub is required")
                if not isinstance(event.get("ts"), str) or not event.get("ts"):
                    errors.append(f"{prefix}: ts is required")
                if not isinstance(event.get("cs"), int):
                    errors.append(f"{prefix}: cs must be an integer")
                sig = event.get("sig")
                if (
                    not isinstance(sig, str)
                    or len(sig) != 128
                    or any(c not in "0123456789abcdef" for c in sig)
                ):
                    errors.append(f"{prefix}: sig must be a 128-hex signature")
    root_mac = m.get("rootMac")
    if not root_mac:
        errors.append("manifest.rootMac is required")
    elif not isinstance(root_mac, str) or not is_valid_hex64(root_mac):
        errors.append(f"manifest.rootMac must be 64-hex, got {root_mac!r}")

    # --- nodes: per-node shape + MAC uniqueness ---
    nodes = data.get("nodes", [])
    if not isinstance(nodes, list):
        errors.append("manifest.nodes must be a list")
        nodes = []

    known_macs: set[str] = set()
    if isinstance(root_mac, str) and is_valid_hex64(root_mac):
        known_macs.add(root_mac)

    for index, node in enumerate(nodes):
        prefix = f"nodes[{index}]"
        if not isinstance(node, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        for field in ("urn", "mac", "parentMac", "kind", "slug", "category"):
            value = node.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"{prefix}: missing required field {field!r}")

        mac = node.get("mac")
        if mac is None:
            pass  # already flagged as missing
        elif not isinstance(mac, str) or not is_valid_hex64(mac):
            errors.append(f"{prefix}: mac must be a 64-hex string, got {mac!r}")
        elif mac in known_macs:
            errors.append(f"{prefix}: duplicate mac {mac!r}")
        else:
            known_macs.add(mac)

        parent_mac = node.get("parentMac")
        if parent_mac is not None and (
            not isinstance(parent_mac, str) or not is_valid_hex64(parent_mac)
        ):
            errors.append(f"{prefix}: parentMac must be 64-hex, got {parent_mac!r}")

        kind = node.get("kind")
        if kind not in VALID_KINDS:
            errors.append(
                f"{prefix}: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}"
            )

        category = node.get("category")
        if category is not None and not _CATEGORY_ID_RE.match(str(category)):
            errors.append(f"{prefix}: category {category!r} is not a 'category-...' id")

        owners = node.get("owners")
        if owners is not None and not isinstance(owners, list):
            errors.append(f"{prefix}: owners must be a list")

        if kind == "doc":
            ch = node.get("contentHash")
            if ch is None or (isinstance(ch, str) and not ch.strip()):
                errors.append(f"{prefix}: doc is missing contentHash")
            elif not isinstance(ch, str) or not is_valid_hex64(ch):
                errors.append(f"{prefix}: contentHash must be 64-hex, got {ch!r}")
            locations = node.get("locations")
            if locations is not None:
                if not isinstance(locations, list):
                    errors.append(f"{prefix}: locations must be a list")
                else:
                    for j, loc in enumerate(locations):
                        if not isinstance(loc, dict):
                            errors.append(f"{prefix}: locations[{j}] must be a mapping")
                        elif "ref" not in loc and "path" not in loc:
                            errors.append(
                                f"{prefix}: locations[{j}] needs 'ref' or 'path'"
                            )

        previous = node.get("previous")
        if previous is not None and (
            not isinstance(previous, str) or not is_valid_hex64(previous)
        ):
            errors.append(
                f"{prefix}: previous must be 64-hex or null, got {previous!r}"
            )

    # --- cross-node relations (need the full MAC set) ---
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        prefix = f"nodes[{index}]"
        parent_mac = node.get("parentMac")
        if (
            isinstance(parent_mac, str)
            and is_valid_hex64(parent_mac)
            and parent_mac not in known_macs
        ):
            errors.append(f"{prefix}: parentMac {parent_mac!r} is unknown")
        previous = node.get("previous")
        if (
            isinstance(previous, str)
            and is_valid_hex64(previous)
            and previous not in known_macs
        ):
            errors.append(f"{prefix}: previous {previous!r} is unknown")

    # --- duplicate versions: (urn, contentHash, previous) must be unique ---
    # The predecessor is part of the key (issue #44/#55): a rollback re-introduces
    # an earlier (urn, contentHash) pair chained off a *new* predecessor, which is
    # a distinct version — only the same content appended twice off the same
    # predecessor is a genuine duplicate.
    seen_versions: dict[tuple[str, str, str | None], int] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("kind") != "doc":
            continue
        ch = node.get("contentHash")
        if not isinstance(ch, str) or not is_valid_hex64(ch):
            continue
        key = (node.get("urn", ""), ch, node.get("previous"))
        if key in seen_versions:
            errors.append(
                f"nodes[{index}]: duplicate version of {key[0]!r} "
                f"(contentHash {ch!r} already present at nodes[{seen_versions[key]}])"
            )
        else:
            seen_versions[key] = index

    # --- refs ---
    refs = data.get("refs", {})
    if not isinstance(refs, dict):
        errors.append("manifest.refs must be a mapping")
        refs = {}
    for name, value in refs.items():
        mac = ref_target_mac(value)
        if mac is None:
            errors.append(f"refs[{name!r}]: missing or invalid target MAC")
        elif not isinstance(mac, str) or not is_valid_hex64(mac):
            errors.append(f"refs[{name!r}]: target MAC must be 64-hex, got {mac!r}")

    # --- current-version head refs (issue #99) ---
    # Shape only: whether a ref still names a version of its URN is a semantic
    # (verify-time) question — ``load`` must stay tolerant so ``pd verify`` can
    # *report* a dangling ref and ``pd recover`` can re-align a broken manifest
    # instead of both being unable to open it. ``structural_issues`` performs
    # that check.
    heads = data.get("currentVersions")
    if heads is not None:
        if not isinstance(heads, dict):
            errors.append("manifest.currentVersions must be a mapping")
        else:
            for urn, mac in heads.items():
                if not isinstance(mac, str) or not is_valid_hex64(mac):
                    errors.append(
                        f"currentVersions[{urn!r}]: must be a 64-hex version MAC"
                    )

    return errors


def urn_path(urn: str) -> str:
    """urn:<org>:<kind>:<path> -> <path>."""
    return urn.split(":", 3)[-1]


def _matches(data: dict[str, Any], predicate) -> list[dict[str, Any]]:
    return [n for n in data.get("nodes", []) if predicate(n)]


def current_versions(data: dict[str, Any]) -> dict[str, Any]:
    """The explicit ``urn -> current version MAC`` map (issue #99).

    This map is the *authority* for which version is current; the array order
    is an implementation detail. Manifests written before issue #99 carry no
    map (an empty dict is returned) and keep resolving through the
    MAC-chained lineage tip.
    """
    heads = data.get("currentVersions")
    return heads if isinstance(heads, dict) else {}


def lineage_tip_mac(versions: list[dict[str, Any]]) -> str | None:
    """The MAC of the version no other version names as its ``previous``.

    Derived from the explicit ``previous`` links — the MAC-chained,
    tamper-evident part of a version — never from array position. Returns
    ``None`` when the versions do not form a single chain (fork or cycle);
    :func:`structural_issues` reports that and the explicit head ref is
    checked against this value.
    """
    if not versions:
        return None
    referenced = {v.get("previous") for v in versions}
    tips = [v.get("mac") for v in versions if v.get("mac") not in referenced]
    return tips[0] if len(tips) == 1 else None


def node_by_urn(data: dict[str, Any], urn: str) -> dict[str, Any] | None:
    """The current node for a URN (explicit head ref, issue #99)."""
    matches = _matches(data, lambda n: n.get("urn") == urn)
    return _current_of(data, matches)


def node_by_path(data: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The current node for a logical path (explicit head ref, issue #99)."""
    matches = _matches(data, lambda n: urn_path(n.get("urn", "")) == path)
    return _current_of(data, matches)


def node_by_slug(data: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """The current node for a slug (explicit head ref, issue #99)."""
    matches = _matches(data, lambda n: n.get("slug") == slug)
    return _current_of(data, matches)


def _current_of(
    data: dict[str, Any], matches: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The current version among the versions a lookup matched (issue #99).

    A lookup can span several URNs — duplicate basenames across folders are
    valid, so a slug is not URN-unique — so the candidate URN is selected
    *first*, by the established cross-URN rule (the URN owning the last
    matching node in physical order), and only then is a version resolved
    within it. Inside the selected URN, resolution is deliberately independent
    of array position:

    1. ``currentVersions[urn]`` — the explicit head ref. A ref naming no
       version of this URN (dangling) is a structural error reported by
       :func:`structural_issues`; resolution then falls through.
    2. the MAC-chained lineage tip — the version no other version names as
       its ``previous`` (legacy manifests, and the value the explicit ref is
       checked against).
    3. the last entry in physical order — only reached by an already-
       structurally-broken manifest, so read paths stay total.
    """
    if not matches:
        return None
    urn = matches[-1].get("urn")
    versions = [n for n in matches if n.get("urn") == urn]
    pointer = current_versions(data).get(urn)
    if pointer is not None:
        for node in versions:
            if node.get("mac") == pointer:
                return node
    tip = lineage_tip_mac(versions)
    if tip is not None:
        for node in versions:
            if node.get("mac") == tip:
                return node
    return versions[-1]


def node_by_mac(data: dict[str, Any], mac: str) -> dict[str, Any] | None:
    """The node for a version MAC (exact MAC; never a current-version alias)."""
    matches = _matches(data, lambda n: n.get("mac") == mac)
    return matches[-1] if matches else None


def versions_of(data: dict[str, Any], urn: str) -> list[dict[str, Any]]:
    """All versions of a URN, oldest first.

    Physical order is retained for display only; "current" is resolved by the
    explicit ``currentVersions`` head ref, never by position (issue #99).
    """
    return _matches(data, lambda n: n.get("urn") == urn)


def structural_issues(data: dict[str, Any]) -> list[str]:
    """Graph-level integrity problems in the node structure (issue #54).

    Non-breaking, verify-time checks over the existing ``parentMac`` /
    ``previous`` graph (cryptographic MAC recomputation stays ``verify``'s
    job; this is the structural/semantic gate):

    - tree connectivity: walking ``parentMac`` must reach ``rootMac`` exactly
      once (no cycles);
    - version lineage: for each URN, versions form a strictly linear chain —
      the first version has no ``previous`` and every later version's
      ``previous`` is the immediately preceding version's MAC (no cross-URN
      links, no skips, no cycles);
    - tree-position stability: every version of a URN must share the same
      ``parentMac`` — a location change is a separate, explicit move, never a
      silent side effect of versioning.

    Returns one human-readable string per problem.
    """
    issues: list[str] = []
    root_mac = data["manifest"]["rootMac"]
    nodes = data.get("nodes", [])
    mac_to_node = {n["mac"]: n for n in nodes}

    # Current-version head refs (issue #99): "current" is an explicit field
    # (``currentVersions[urn]``), never array position. Each ref must name a
    # version of its URN and must agree with the MAC-chained lineage tip — the
    # version no other version names as its ``previous``. A dangling ref is
    # rejected here instead of being silently resolved by position, and a
    # lineage without exactly one tip (fork/cycle) is reported because
    # "current" would otherwise be ambiguous.
    by_urn_current: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node.get("kind") == "doc":
            by_urn_current.setdefault(node["urn"], []).append(node)
    for urn, versions in by_urn_current.items():
        tip = lineage_tip_mac(versions)
        if tip is None:
            issues.append(f"{urn}: version lineage does not have exactly one tip")
        pointer = current_versions(data).get(urn)
        if pointer is None:
            continue
        if pointer not in {v.get("mac") for v in versions}:
            issues.append(f"{urn}: currentVersions points at unknown version {pointer}")
        elif tip is not None and pointer != tip:
            issues.append(
                f"{urn}: currentVersions {pointer} is not the lineage tip {tip}"
            )

    # Tree connectivity: the parentMac chain must terminate at rootMac and
    # must not cycle.
    for node in nodes:
        seen: set[str] = set()
        current = node
        while True:
            parent = current.get("parentMac")
            if parent == root_mac:
                break
            if parent in seen:
                issues.append(
                    f"{node.get('urn', node.get('mac'))}: parentMac cycle at {parent}"
                )
                break
            if parent not in mac_to_node:
                break  # unknown parent is reported by verify itself
            seen.add(parent)
            current = mac_to_node[parent]

    # Version lineage: each URN's versions form a strictly linear chain.
    by_urn: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node.get("kind") == "doc":
            by_urn.setdefault(node["urn"], []).append(node)
    for urn, versions in by_urn.items():
        # Tree-position stability: all versions of a URN must share one
        # parentMac. A change of location is a separate, explicit move
        # operation, never a silent side effect of versioning.
        parent_macs = {v.get("parentMac") for v in versions}
        if len(parent_macs) > 1:
            issues.append(
                f"{urn}: versions disagree on parentMac — all versions must "
                "share one tree position"
            )
        for index, version in enumerate(versions):
            previous = version.get("previous")
            if index == 0:
                if previous is not None:
                    issues.append(f"{urn}: first version has a previous link")
            elif previous != versions[index - 1]["mac"]:
                issues.append(
                    f"{urn}: version {index + 1} previous does not point at "
                    "the immediately preceding version"
                )

    # Revocation chain (issue #104): the seal-key revocation history must be
    # append-only, contiguous and hash-chained, so a revocation cannot be
    # deleted or reordered without breaking the chain.
    issues.extend(revocation_chain_issues(data["manifest"]))
    return issues


def ref_target_mac(value: Any) -> str | None:
    """The target MAC for a refs-map value (bare MAC string or signed record).

    ``tag`` stores either a bare MAC (legacy, unsigned) or a signed record
    (``{"mac": ..., "sig": ...}``); both name the same target node.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("mac")
    return None


def mutation_sequence_issues(data: dict[str, Any]) -> list[str]:
    """Mutation-sequence integrity problems (issue #73), or empty.

    Each mutation binds a monotonic ``seq`` and a ``prevHead`` (the last
    committed node's MAC it builds on) into its signed envelope. verify
    checks:

    - node ``seq`` is strictly increasing (no replay, duplicate or reorder);
    - node ``prevHead`` equals the previous node's MAC (or ``rootMac`` for the
      first), so a node re-inserted after a rollback fails;
    - ``manifest.headMac`` equals the last node's MAC (the structural node
      head), so deleting the latest node is detected;
    - ``manifest.headSeq`` is not lower than the last node's ``seq``.

    Returns one human-readable string per problem.
    """
    issues: list[str] = []
    root_mac = data["manifest"]["rootMac"]
    nodes = data.get("nodes", [])

    # 1. Node seq must be strictly increasing.
    last_seq = 0
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        seq = node.get("seq")
        if seq is not None:
            if seq <= last_seq:
                issues.append(f"nodes[{index}]: seq {seq!r} is not strictly increasing")
            last_seq = max(last_seq, seq)

    # 2. prevHead chaining over the ordered node list.
    prev_mac = root_mac
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        prev_head = node.get("prevHead")
        if prev_head is None:
            # A node without prevHead (legacy) ends the chainable prefix;
            # keep walking but only report when the field is present.
            prev_mac = node.get("mac", prev_mac)
            continue
        if prev_head != prev_mac:
            issues.append(
                f"nodes[{index}]: prevHead {prev_head!r} does not match "
                f"previous head {prev_mac!r}"
            )
        prev_mac = node.get("mac", prev_mac)

    # 3. The structural node head must agree with the last node (so deleting
    # the latest node is detected even when the seal/audit anchors are intact).
    header = data["manifest"]
    head_mac = header.get("headMac")
    if head_mac and nodes:
        last = nodes[-1]
        last_mac = last.get("mac") if isinstance(last, dict) else None
        if last_mac and last_mac != head_mac:
            issues.append(
                f"manifest.headMac {head_mac!r} does not match last node {last_mac!r}"
            )

    # 4. headSeq must not lag behind the node history.
    head_seq = header.get("headSeq")
    if head_seq is not None and last_seq and head_seq < last_seq:
        issues.append(
            f"manifest.headSeq {head_seq!r} is lower than the last node seq "
            f"{last_seq!r}"
        )
    return issues


def resolve_node(data: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Resolve a node by urn, logical path, slug, or ref name (a MAC)."""
    node = node_by_urn(data, ref) or node_by_path(data, ref) or node_by_slug(data, ref)
    if node is None and ref in data.get("refs", {}):
        mac = ref_target_mac(data["refs"][ref])
        if mac is not None:
            node = node_by_mac(data, mac)
    return node


class ManifestRepository:
    """Typed boundary over the manifest dict (the YAML persistence DTO).

    Commands mutate the manifest through this repository instead of reaching
    into ``manifest["nodes"]`` / ``manifest["refs"]`` directly, so the YAML
    layout stays an implementation detail behind a stable, typed API (issue
    #46: "the manifest is both storage format and domain model"). The
    repository wraps the in-memory dict; persistence remains the caller's job
    (``load`` before, ``save`` after, under ``manifest_lock``).
    """

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @property
    def data(self) -> dict[str, Any]:
        """The underlying manifest dict (for ``save``)."""
        return self._data

    # -- header accessors --

    @property
    def org(self) -> str:
        return self._data["manifest"]["org"]

    @property
    def namespace(self) -> str:
        return self._data["manifest"]["namespace"]

    @property
    def tenant(self) -> str:
        return self._data["manifest"]["tenant"]

    @property
    def root_mac(self) -> str:
        return self._data["manifest"]["rootMac"]

    # -- collections --

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self._data.setdefault("nodes", [])

    @property
    def refs(self) -> dict[str, Any]:
        return self._data.setdefault("refs", {})

    # -- mutations --

    @property
    def current_versions(self) -> dict[str, Any]:
        """The explicit ``urn -> current version MAC`` head map (issue #99)."""
        return self._data.setdefault("currentVersions", {})

    def add_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """Append a node (a new folder, or a new document version).

        A document node becomes its URN's current version by pointing the
        explicit head map at it (issue #99): ``currentVersions[urn]`` is the
        authority for "current", so the append never rewrites an existing
        node — earlier versions stay byte-for-byte as committed (the manifest
        is append-only, which is what keeps the MAC chain and the audit log
        auditable).
        """
        if node.get("kind") == "doc":
            self.current_versions[node["urn"]] = node["mac"]
        self.nodes.append(node)
        return node

    def add_version(self, node: dict[str, Any]) -> dict[str, Any]:
        """Append a document version node (semantic alias of ``add_node``)."""
        return self.add_node(node)

    def set_ref(self, name: str, record: Any) -> None:
        """Point a mutable ref at a version MAC (bare MAC or signed record)."""
        self.refs[name] = record

    # -- lookups (delegated to the module-level resolvers) --

    def node_by_urn(self, urn: str) -> dict[str, Any] | None:
        return node_by_urn(self._data, urn)

    def node_by_path(self, path: str) -> dict[str, Any] | None:
        return node_by_path(self._data, path)

    def node_by_slug(self, slug: str) -> dict[str, Any] | None:
        return node_by_slug(self._data, slug)

    def node_by_mac(self, mac: str) -> dict[str, Any] | None:
        return node_by_mac(self._data, mac)

    def versions_of(self, urn: str) -> list[dict[str, Any]]:
        return versions_of(self._data, urn)

    def resolve_node(self, ref: str) -> dict[str, Any] | None:
        return resolve_node(self._data, ref)
