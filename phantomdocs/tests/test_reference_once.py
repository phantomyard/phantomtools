"""Reference deduplication stays inside the real service/manifest lock."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner
from test_documents import _repo, _svc

from phantomdocs.cli import main
from phantomdocs.documents import DocumentError, DocumentService

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Existing store filesystem operations require POSIX"
)


def test_same_drive_ref_is_not_read_or_hashed_twice(tmp_path, monkeypatch):
    root, _, service = _svc(tmp_path)
    calls = []

    def read(ref, **kwargs):
        calls.append(ref)
        return b"meeting text", {"backend": "gdrive", "ref": ref.split("://")[1]}

    monkeypatch.setattr("phantomdocs.storage.read_reference", read)
    args = {
        "ref": "gdrive://existing-drive-id",
        "slug": "meeting",
        "category": "category-1",
        "folder": None,
        "owners": ["cfo"],
    }
    service.index_reference_once(**args)

    def no_hash(*args):
        pytest.fail("existing reference must not be hashed")

    monkeypatch.setattr("phantomdocs.documents.content_hash", no_hash)
    assert service.index_reference_once(**args)["unchanged"]
    assert service.index_reference_once(**{**args, "slug": "other-name"})["unchanged"]
    assert calls == ["gdrive://existing-drive-id"]
    assert len([n for n in _repo(root).data["nodes"] if n["kind"] == "doc"]) == 1
    assert not (tmp_path / "blobs").exists()


def test_reference_policy_conflict_and_slug_collision_are_denied_before_read(
    tmp_path, monkeypatch
):
    _, _, service = _svc(tmp_path)
    monkeypatch.setattr(
        "phantomdocs.storage.read_reference",
        lambda ref, **kw: (b"text", {"backend": "gdrive", "ref": ref.split("://")[1]}),
    )
    args = {
        "ref": "gdrive://file-one",
        "slug": "meeting",
        "category": "category-1",
        "folder": None,
        "owners": ["cfo"],
    }
    service.index_reference_once(**args)
    monkeypatch.setattr(
        "phantomdocs.storage.read_reference",
        lambda *a, **kw: pytest.fail("must refuse before reading"),
    )
    with pytest.raises(DocumentError, match="conflicting"):
        service.index_reference_once(**{**args, "category": "category-2"})
    with pytest.raises(DocumentError, match="another document"):
        service.index_reference_once(**{**args, "ref": "gdrive://file-two"})


def test_concurrent_reference_ingest_has_one_read_and_one_node(tmp_path, monkeypatch):
    root, org, _ = _svc(tmp_path)
    calls = []

    def read(ref, **kw):
        calls.append(ref)
        return b"meeting", {"backend": "gdrive", "ref": "file-one"}

    monkeypatch.setattr("phantomdocs.storage.read_reference", read)

    def index(_):
        return DocumentService(root, org, "roberto").index_reference_once(
            ref="gdrive://file-one",
            slug="meeting",
            category="category-1",
            folder=None,
            owners=["cfo"],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(index, range(4)))
    assert len(calls) == 1
    assert sum(r.get("unchanged", False) for r in results) == 3
    assert len(_repo(root).data["nodes"]) == 1


def test_cli_reference_skip_uses_real_service_without_copying(tmp_path, monkeypatch):
    root, org, _ = _svc(tmp_path)
    calls = []

    def read(ref, **kw):
        calls.append(ref)
        return b"record", {"backend": "gdrive", "ref": "file-one"}

    monkeypatch.setattr("phantomdocs.storage.read_reference", read)
    args = [
        "add",
        "--ref",
        "gdrive://file-one",
        "--skip-existing-ref",
        "--slug",
        "meeting",
        "--category",
        "1",
        "--owners",
        "cfo",
        "--actor",
        "roberto",
        "--org-yaml",
        org,
        "--root",
        root,
    ]
    runner = CliRunner()
    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(main, args)
    assert second.exit_code == 0, second.output
    assert "unchanged" in second.output and len(calls) == 1
