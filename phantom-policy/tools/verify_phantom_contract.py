"""Consumer-pinned verifier. Never import code from the candidate archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
import time
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"phantom-policy-contract-release/v1\x00"
AUTHORITY = "phantomyard/phantomtools"
VERSIONS = {
    "contract_schema": "1.0.0",
    "fixture_suite": "1.0.0",
    "reference_evaluator": "0.1.0",
    "signature_envelope": 1,
}
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_CONTENT = 64 * 1024 * 1024
MAX_ENTRIES = 4096
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class VerificationError(ValueError):
    """The contract cannot be accepted."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise VerificationError(reason)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_limited(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "input exceeds size limit")
    return data


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def parse(data: bytes) -> dict:
    value = json.loads(data, object_pairs_hook=unique_object)
    require(isinstance(value, dict), "expected JSON object")
    return value


def fields(value: dict, expected: set[str]) -> None:
    require(set(value) == expected, "missing or unsupported fields")


def safe_path(name: str) -> bool:
    # A portable, deliberately restricted namespace, including Windows aliases.
    if not isinstance(name, str) or len(name) > 240:
        return False
    for part in name.split("/"):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part):
            return False
        if part.endswith("."):
            return False
        stem = part.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[0-9]", stem
        ):
            return False
    return True


def verify_archive(data: bytes, files: dict) -> None:
    require(isinstance(files, dict) and bool(files), "empty or invalid inventory")
    require(len(files) <= MAX_ENTRIES, "too many files")
    for name, checksum in files.items():
        require(safe_path(name), "unsafe inventory path")
        require(
            isinstance(checksum, str) and bool(DIGEST.fullmatch(checksum)),
            "invalid file digest",
        )
    # Avoid case-insensitive aliases and file/directory collisions on consumers.
    folded = {name.casefold() for name in files}
    require(len(folded) == len(files), "case-colliding paths")
    for name in folded:
        parts = name.split("/")
        require(
            all("/".join(parts[:i]) not in folded for i in range(1, len(parts))),
            "file/directory collision",
        )
    seen = set()
    total = 0
    # Bound decompression before tarfile interprets extension headers, which can
    # otherwise consume arbitrary memory before yielding an ordinary member.
    expanded_limit = MAX_CONTENT + MAX_ENTRIES * 1024 + 10240
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(expanded_limit + 1)
    require(len(expanded) <= expanded_limit, "expanded tar exceeds size limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            require(len(seen) < MAX_ENTRIES, "too many archive entries")
            require(
                member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
                and not member.pax_headers
                and member.sparse is None,
                "only ordinary files are allowed",
            )
            require(safe_path(member.name), "unsafe archive path")
            require(member.name not in seen, "duplicate archive path")
            require(member.name in files, "undeclared archive file")
            require(0 <= member.size <= MAX_CONTENT, "invalid file size")
            total += member.size
            require(total <= MAX_CONTENT, "expanded archive exceeds size limit")
            stream = archive.extractfile(member)
            require(stream is not None, "missing file data")
            content = stream.read(MAX_CONTENT + 1)
            require(len(content) == member.size, "truncated file")
            require(digest(content) == files[member.name], "file digest mismatch")
            seen.add(member.name)
    require(seen == set(files), "missing archive files")


def verify(
    archive: Path,
    manifest: Path,
    signature: Path,
    lock: Path,
    anchor: Path,
    *,
    now: int,
) -> dict:
    """Verify against consumer-owned inputs and an operator-trusted UTC clock.

    Does not install files, advance a lock, establish clock trust, or authorize
    resource access. The reviewed lock supplies the persistent sequence floor.
    """
    pinned = parse(read_limited(lock, 65536))
    fields(
        pinned,
        {
            "format_version",
            "authority",
            "source_commit",
            "consumer_commit",
            "verifier_commit",
            "versions",
            "assets",
            "manifest_digest",
            "archive_digest",
            "anchor_fingerprint",
            "anchor_provisioning",
            "owner_review",
            "minimum_sequence",
        },
    )
    require(
        type(pinned["format_version"]) is int and pinned["format_version"] == 1,
        "unsupported lock version",
    )
    require(pinned["authority"] == AUTHORITY, "non-upstream authority")
    require(pinned["versions"] == VERSIONS, "unsupported version tuple")
    require(
        type(pinned["versions"]["signature_envelope"]) is int,
        "invalid envelope version",
    )
    for key in ("source_commit", "consumer_commit", "verifier_commit"):
        require(
            isinstance(pinned[key], str) and bool(COMMIT.fullmatch(pinned[key])),
            "full commit required",
        )
    require(
        pinned["anchor_provisioning"] == "producer_under_owner_review",
        "unsupported anchor provisioning",
    )
    require(
        isinstance(pinned["owner_review"], str)
        and bool(pinned["owner_review"].strip()),
        "owner review reference required",
    )
    prefix = f"https://github.com/{AUTHORITY}/releases/download/phantom-policy-v1.0.0/"
    require(
        pinned["assets"]
        == {
            "archive": prefix + "phantom-policy-1.0.0.tar.gz",
            "manifest": prefix + "contract-manifest.json",
            "signature": prefix + "contract-manifest.sig",
        },
        "noncanonical asset coordinates",
    )
    public = read_limited(anchor, 32)
    require(
        len(public) == 32 and digest(public) == pinned["anchor_fingerprint"],
        "anchor fingerprint mismatch",
    )
    raw = read_limited(manifest, 1024 * 1024)
    require(digest(raw) == pinned["manifest_digest"], "manifest digest mismatch")
    signed = read_limited(signature, 64)
    require(len(signed) == 64, "invalid signature size")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signed, DOMAIN + raw)
    except InvalidSignature as exc:
        raise VerificationError("invalid release signature") from exc
    release = parse(raw)
    fields(
        release,
        {
            "format_version",
            "authority",
            "source_commit",
            "versions",
            "assets",
            "archive_digest",
            "key_id",
            "sequence",
            "issued_at",
            "expires_at",
            "files",
        },
    )
    require(
        type(release["format_version"]) is int and release["format_version"] == 1,
        "unsupported manifest version",
    )
    for key in ("authority", "source_commit", "versions", "assets", "archive_digest"):
        require(release[key] == pinned[key], "manifest differs from consumer lock")
    require(
        type(release["versions"]["signature_envelope"]) is int,
        "invalid envelope version",
    )
    require(release["key_id"] == pinned["anchor_fingerprint"], "wrong signing key id")
    for value in (
        now,
        release["sequence"],
        pinned["minimum_sequence"],
        release["issued_at"],
        release["expires_at"],
    ):
        require(type(value) is int and value >= 0, "invalid sequence or time")
    require(release["sequence"] >= pinned["minimum_sequence"], "release rollback")
    require(release["issued_at"] <= now < release["expires_at"], "release not current")
    data = read_limited(archive, MAX_ARCHIVE)
    require(digest(data) == pinned["archive_digest"], "archive digest mismatch")
    verify_archive(data, release["files"])
    return {
        "ok": True,
        "versions": VERSIONS,
        "source_commit": release["source_commit"],
        "manifest_digest": digest(raw),
        "archive_digest": digest(data),
        "sequence": release["sequence"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "manifest", "signature", "lock", "anchor"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(**vars(args), now=int(time.time()))
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        tarfile.TarError,
        EOFError,
        zlib.error,
        RecursionError,
    ) as exc:
        # No candidate text/paths in diagnostics: files may contain private data.
        reason = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        print(json.dumps({"ok": False, "reason": reason}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
