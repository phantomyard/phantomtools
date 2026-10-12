"""Read-only HTTP bridge with fake transport; no Drive calls or real tokens."""

import io
import json
import os
import urllib.error

import pytest

from phantomdocs.workflow_drive_reader import DriveArchiveReader, _NoRedirect
from phantomdocs.workflows import WorkflowError


def reader(tmp_path, metadata, data=b"document"):
    token = tmp_path / "test-token"
    token.write_text("synthetic-test-token")
    token.chmod(0o600)
    requests = []

    def transport(request, timeout):
        requests.append(request)
        return io.BytesIO(
            data if "alt=media" in request.full_url else json.dumps(metadata).encode()
        )

    return DriveArchiveReader(token, max_bytes=1024, transport=transport), requests


def test_metadata_and_bounded_media_reads_are_get_only(tmp_path):
    value, requests = reader(
        tmp_path,
        {"id": "file-id", "size": "8", "parents": ["archive"], "trashed": False},
    )
    assert value.metadata("gdrive://file-id")["parents"] == ["archive"]
    content, location = value.read_reference("gdrive://file-id")
    with content:
        assert content.file.read() == b"document"
    assert location == {"backend": "gdrive", "ref": "file-id"}
    assert len(requests) == 2 and all(r.get_method() == "GET" for r in requests)
    assert all(
        r.full_url.startswith("https://www.googleapis.com/drive/v3/files/file-id?")
        for r in requests
    )
    assert "q=" not in requests[0].full_url


@pytest.mark.parametrize(
    "size,data", [("0", b""), ("2048", b"x"), ("9", b"document"), ("2", b"document")]
)
def test_invalid_sizes_or_incomplete_media_fail_closed(tmp_path, size, data):
    value, _ = reader(tmp_path, {"id": "file-id", "size": size}, data)
    with pytest.raises(WorkflowError):
        value.read_reference("gdrive://file-id")


def test_redirects_are_not_followed_with_credentials(tmp_path):
    assert (
        _NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://other.example"
        )
        == None
    )
    value, _ = reader(tmp_path, {"id": "file-id", "size": "8"})

    def rejected(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)

    value.transport = rejected
    with pytest.raises(WorkflowError) as error:
        value.metadata("gdrive://file-id")
    assert "synthetic-test-token" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode check")
def test_public_token_file_is_refused(tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token")
    token.chmod(0o644)
    with pytest.raises(WorkflowError, match="0600"):
        DriveArchiveReader(token, max_bytes=1024)
