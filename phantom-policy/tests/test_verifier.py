"""Synthetic adversarial release fixtures; no production keys or records."""

import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).parents[1] / "tools" / "verify_phantom_contract.py"
spec = importlib.util.spec_from_file_location("verifier", SCRIPT)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def packed(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for name, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = 2 if kind == tarfile.REGTYPE else 0
            member.linkname = (
                "outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            )
            tar.addfile(member, io.BytesIO(b"{}") if member.size else None)
    return buffer.getvalue()


@pytest.fixture
def release(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    paths = {
        name: tmp_path / name
        for name in ("archive", "manifest", "signature", "lock", "anchor")
    }
    paths["anchor"].write_bytes(public)
    paths["archive"].write_bytes(packed([("fixtures/basic.json", tarfile.REGTYPE)]))
    prefix = (
        f"https://github.com/{v.AUTHORITY}/releases/download/phantom-policy-v1.0.0/"
    )
    manifest = {
        "format_version": 1,
        "authority": v.AUTHORITY,
        "source_commit": "a" * 40,
        "versions": v.VERSIONS.copy(),
        "assets": {
            "archive": prefix + "phantom-policy-1.0.0.tar.gz",
            "manifest": prefix + "contract-manifest.json",
            "signature": prefix + "contract-manifest.sig",
        },
        "archive_digest": v.digest(paths["archive"].read_bytes()),
        "key_id": v.digest(public),
        "sequence": 7,
        "issued_at": 1000,
        "expires_at": 2000,
        "files": {"fixtures/basic.json": v.digest(b"{}")},
    }
    lock = {
        k: manifest[k]
        for k in (
            "format_version",
            "authority",
            "source_commit",
            "versions",
            "assets",
            "archive_digest",
        )
    }
    lock.update(
        consumer_commit="b" * 40,
        verifier_commit="c" * 40,
        anchor_fingerprint=v.digest(public),
        anchor_provisioning="producer_under_owner_review",
        owner_review="synthetic-review",
        minimum_sequence=7,
    )

    def seal():
        raw = encoded(manifest)
        paths["manifest"].write_bytes(raw)
        paths["signature"].write_bytes(private.sign(v.DOMAIN + raw))
        lock["manifest_digest"] = v.digest(raw)
        paths["lock"].write_bytes(encoded(lock))

    seal()
    return paths, manifest, lock, seal, private


def test_valid_release_no_extraction(release):
    paths, *_ = release
    assert v.verify(**paths, now=1500)["sequence"] == 7
    assert not (paths["archive"].parent / "fixtures").exists()


@pytest.mark.parametrize("name", ["archive", "manifest", "signature", "anchor"])
def test_tampering(release, name):
    paths, *_ = release
    content = bytearray(paths[name].read_bytes())
    content[0] ^= 1
    paths[name].write_bytes(content)
    with pytest.raises(ValueError):
        v.verify(**paths, now=1500)


@pytest.mark.parametrize("now", [999, 2000, 2001, True])
def test_time_window(release, now):
    paths, *_ = release
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=now)


@pytest.mark.parametrize(
    "key,value",
    [
        ("minimum_sequence", 8),
        ("authority", "personal/fork"),
        ("source_commit", "main"),
        ("consumer_commit", "latest"),
        ("owner_review", ""),
        ("anchor_provisioning", "independent"),
        ("versions", {"contract_schema": "2.0.0"}),
        ("assets", {}),
    ],
)
def test_consumer_constraints(release, key, value):
    paths, _, lock, seal, _ = release
    lock[key] = value
    seal()
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=1500)


def test_document_signature_is_not_release_authority(release):
    paths, _, _, _, private = release
    paths["signature"].write_bytes(private.sign(paths["manifest"].read_bytes()))
    with pytest.raises(v.VerificationError, match="signature"):
        v.verify(**paths, now=1500)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("C:/escape", tarfile.REGTYPE),
        ("a\\escape", tarfile.REGTYPE),
        ("CON.txt", tarfile.REGTYPE),
        ("a.", tarfile.REGTYPE),
        ("fixtures/basic.json", tarfile.SYMTYPE),
        ("fixtures/basic.json", tarfile.LNKTYPE),
        ("fixtures/basic.json", tarfile.FIFOTYPE),
        ("fixtures/basic.json", tarfile.DIRTYPE),
    ],
)
def test_even_signed_unsafe_archive_is_denied(release, name, kind):
    paths, manifest, lock, seal, _ = release
    data = packed([(name, kind)])
    paths["archive"].write_bytes(data)
    manifest["archive_digest"] = lock["archive_digest"] = v.digest(data)
    manifest["files"] = {name: v.digest(b"{}")}
    seal()
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=1500)


@pytest.mark.parametrize(
    "entries,files",
    [
        (["a", "a"], ["a"]),
        (["a", "b"], ["a"]),
        (["a"], ["a", "b"]),
        (["a", "A"], ["a", "A"]),
        (["a", "a/b"], ["a", "a/b"]),
    ],
)
def test_inventory_mismatch(release, entries, files):
    paths, manifest, lock, seal, _ = release
    data = packed([(name, tarfile.REGTYPE) for name in entries])
    paths["archive"].write_bytes(data)
    manifest["archive_digest"] = lock["archive_digest"] = v.digest(data)
    manifest["files"] = {name: v.digest(b"{}") for name in files}
    seal()
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=1500)


def test_wrong_file_hash(release):
    paths, manifest, _, seal, _ = release
    manifest["files"]["fixtures/basic.json"] = v.digest(b"other")
    seal()
    with pytest.raises(v.VerificationError, match="file digest"):
        v.verify(**paths, now=1500)


def test_duplicate_json():
    with pytest.raises(v.VerificationError):
        v.parse(b'{"sequence":7,"sequence":1}')


def test_resource_limit(release, monkeypatch):
    paths, *_ = release
    monkeypatch.setattr(v, "MAX_CONTENT", 1)
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=1500)


def test_compressed_header_bomb(release, monkeypatch):
    paths, manifest, lock, seal, _ = release
    monkeypatch.setattr(v, "MAX_CONTENT", 1)
    monkeypatch.setattr(v, "MAX_ENTRIES", 1)
    data = gzip.compress(b"x" * 20000)
    paths["archive"].write_bytes(data)
    manifest["archive_digest"] = lock["archive_digest"] = v.digest(data)
    seal()
    with pytest.raises(v.VerificationError, match="expanded tar"):
        v.verify(**paths, now=1500)


@pytest.mark.parametrize(
    "key,value",
    [
        ("sequence", True),
        ("issued_at", -1),
        ("format_version", True),
        ("key_id", "sha256:" + "0" * 64),
        ("authority", "personal/fork"),
        ("source_commit", "d" * 40),
        ("extra", "unsupported"),
    ],
)
def test_signed_manifest_constraints(release, key, value):
    paths, manifest, _, seal, _ = release
    manifest[key] = value
    seal()
    with pytest.raises(v.VerificationError):
        v.verify(**paths, now=1500)


def test_cli_success_and_failure(release):
    paths, manifest, _, seal, _ = release
    manifest["issued_at"] = 0
    manifest["expires_at"] = 4102444800
    seal()
    command = [sys.executable, str(SCRIPT)]
    for name, path in paths.items():
        command += ["--" + name, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"] is True
    paths["archive"].write_bytes(b"PRIVATE_CANARY")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
