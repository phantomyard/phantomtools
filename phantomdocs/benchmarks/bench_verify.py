#!/usr/bin/env python3
"""Benchmark `pd verify` scaling (audit #12).

Builds namespaces directly — manifest + audit log + blobs constructed in one
O(N) pass (no per-mutation load/save, no fsync) — and times full verification,
to confirm verify scales ~linearly with node / audit / version count rather
than quadratically.

Run: .venv/bin/python benchmarks/bench_verify.py
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import coincurve
from click.testing import CliRunner

from phantomdocs import identity, signing
from phantomdocs.cli import main
from phantomdocs.manifest import empty_manifest, save

ORG = """\
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-2:
      categories: [1, 2]
roles:
  - id: ceo
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    npub: NPUB_PLACEHOLDER
    actor_exceptions: []
"""


def _bech32_encode(hrp, data):
    from phantomdocs.signing import (
        _BECH32_CHARSET,
        _bech32_expand,
        _bech32_polymod,
        _convertbits,
    )

    values = _convertbits(list(data), 8, 5)
    checksum = [0] * 6
    poly = _bech32_polymod(_bech32_expand(hrp) + values + checksum) ^ 1
    for i in range(6):
        checksum[i] = (poly >> (5 * (5 - i))) & 31
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in values + checksum)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_namespace(root, items):
    """Build a signed namespace from ``items`` = list of ``(slug, content)``.

    Repeated slugs produce new *versions* of the same doc (chained via
    ``previous``). Returns build seconds. The org.yaml path is ``<root>/org.yaml``.
    """
    root = Path(root)
    actor_secret = coincurve.PrivateKey().secret.hex()
    actor_pubkey = signing.pubkey_from_nsec(actor_secret)
    org_pubkey = "ab" * 32
    org_id = "example-org"
    ns = "docs"

    root_mac = identity.root_mac(org_id, org_pubkey, ns)

    (root / "org.yaml").write_text(
        ORG.replace(
            "NPUB_PLACEHOLDER", _bech32_encode("npub", bytes.fromhex(actor_pubkey))
        ),
        encoding="utf-8",
    )
    nsec = root / "actor.nsec"
    nsec.write_text(actor_secret)
    os.chmod(nsec, 0o600)
    (root / "blobs").mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()

    # Audit log (in memory): the init entry, then one per mutation.
    init_entry = {
        "ts": _now(),
        "actor": "phantom",
        "action": "init",
        "urn": f"urn:{org_id}:namespace:{ns}",
        "mac": root_mac,
        "hash": None,
        "prev": None,
        "seq": 0,
    }
    init_line = json.dumps(init_entry, sort_keys=True) + "\n"
    audit_lines = [init_line]
    audit_prev = _sha256(init_line.encode())

    data = empty_manifest(org_id, ns, root_mac)
    data["manifest"]["auditSeq"] = 1
    data["manifest"]["auditHead"] = audit_prev

    prev_head = root_mac
    prev_by_urn: dict[str, str] = {}

    for i, (slug, content) in enumerate(items):
        ch = identity.content_hash(content)
        urn = f"urn:{org_id}:doc:{slug}"
        previous = prev_by_urn.get(urn)
        mac = identity.doc_version_mac(root_mac, previous, slug, content)

        # Blob (no fsync — benchmark only).
        blob_path = root / "blobs" / ch[:2] / ch
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(content)

        locations = [{"backend": "local", "path": str(blob_path)}]
        seq = i + 1
        ts = _now()
        env = signing.mutation_envelope(
            mac=mac,
            actor="paco",
            action="add" if previous is None else "version",
            category="category-1",
            owners=["ceo"],
            locations=locations,
            urn=urn,
            seq=seq,
            prev_head=prev_head,
            ts=ts,
        )
        sig = signing.sign_mutation(actor_secret, env)
        node = {
            "urn": urn,
            "mac": mac,
            "parentMac": root_mac,
            "kind": "doc",
            "slug": slug,
            "category": "category-1",
            "contentHash": ch,
            "size": len(content),
            "owners": ["ceo"],
            "locations": locations,
            "meta": {"title": slug},
            "relations": {},
            "previous": previous,
            "actor": "paco",
            "action": "add" if previous is None else "version",
            "seq": seq,
            "prevHead": prev_head,
            "ts": ts,
            "cryptoVersion": signing.CRYPTO_VERSION,
            "sig": sig,
            "sigPubkey": actor_pubkey,
        }
        data["nodes"].append(node)

        entry = {
            "ts": ts,
            "actor": "paco",
            "action": "add" if previous is None else "version",
            "urn": urn,
            "mac": mac,
            "hash": ch,
            "prev": audit_prev,
            "seq": seq,
            "sig": sig,
            "sigPubkey": actor_pubkey,
        }
        line = json.dumps(entry, sort_keys=True) + "\n"
        audit_lines.append(line)
        audit_prev = _sha256(line.encode())

        data["manifest"]["headSeq"] = seq
        data["manifest"]["headMac"] = mac
        data["manifest"]["auditSeq"] = i + 2
        data["manifest"]["auditHead"] = audit_prev

        prev_by_urn[urn] = mac
        prev_head = mac

    (root / "audit.log").write_text("".join(audit_lines), encoding="utf-8")
    save(str(root / "manifest.yaml"), data)
    return time.perf_counter() - t0


def time_verify(root, org_path):
    runner = CliRunner()
    t0 = time.perf_counter()
    r = runner.invoke(main, ["verify", "--org-yaml", org_path, "--root", root])
    dt = time.perf_counter() - t0
    assert r.exit_code == 0, r.output
    return dt


def bench():
    with tempfile.TemporaryDirectory() as td:
        build_namespace(td, [(f"d{i}.txt", f"hello {i}".encode()) for i in range(5)])
        print(f"  sanity: verify 5 nodes = {time_verify(td, str(Path(td) / 'org.yaml')):.3f}s")

    print("=== node count scaling (20-byte blobs) ===", flush=True)
    for n in (500, 1_000, 2_000, 5_000):
        with tempfile.TemporaryDirectory() as td:
            b = build_namespace(td, [(f"d{i}.txt", b"x" * 20 + str(i).encode()) for i in range(n)])
            v = time_verify(td, str(Path(td) / "org.yaml"))
            print(f"  nodes={n:>6}  build={b:6.2f}s  verify={v:6.3f}s")

    print("=== version depth (1 doc, N versions) ===", flush=True)
    for v in (100, 1_000):
        with tempfile.TemporaryDirectory() as td:
            b = build_namespace(td, [("doc.txt", f"version {i}".encode()) for i in range(v)])
            t = time_verify(td, str(Path(td) / "org.yaml"))
            print(f"  versions={v:>5}  build={b:6.2f}s  verify={t:6.3f}s")

    print("=== blob size scaling (1000 nodes) ===", flush=True)
    for size, label in ((1_000, "1KB"), (100_000, "100KB"), (1_000_000, "1MB")):
        with tempfile.TemporaryDirectory() as td:
            b = build_namespace(td, [(f"d{i}.txt", b"x" * size + str(i).encode()) for i in range(1_000)])
            t = time_verify(td, str(Path(td) / "org.yaml"))
            print(f"  blob={label:>5}  build={b:6.2f}s  verify={t:6.3f}s", flush=True)


if __name__ == "__main__":
    bench()
