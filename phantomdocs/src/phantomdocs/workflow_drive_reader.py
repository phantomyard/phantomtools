"""Read-only Drive metadata/media bridge for archive receipt consumption.

Uses an explicit private OAuth access-token file; no credential discovery,
refresh, upload, folder creation, rename, move or deletion is implemented.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .content import FileContent
from .workflows import WorkflowError, reference_is_safe


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an Authorization header to a redirected destination.
        return None


class DriveArchiveReader:
    def __init__(self, token_file, *, max_bytes, transport=None):
        if max_bytes <= 0:
            raise WorkflowError("positive reference byte limit required")
        path = Path(token_file)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise WorkflowError("Drive token must be in a private regular file")
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise WorkflowError(
                "Drive token file must be owned by caller with mode 0600"
            )
        token = path.read_text(encoding="utf-8").strip()
        if not token or len(token) > 16384 or any(ord(c) < 33 for c in token):
            raise WorkflowError("invalid Drive access-token file")
        self.token, self.max_bytes, self.cache = token, max_bytes, {}
        self.transport = transport or urllib.request.build_opener(_NoRedirect()).open

    def _open(self, ref, parameters):
        if not ref.startswith("gdrive://") or not reference_is_safe(ref):
            raise WorkflowError("Drive reader requires an exact safe file id")
        file_id = ref.split("://")[1]
        url = (
            "https://www.googleapis.com/drive/v3/files/"
            + file_id
            + "?"
            + urllib.parse.urlencode(parameters)
        )
        request = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self.token}, method="GET"
        )
        try:
            return self.transport(request, timeout=30)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise WorkflowError("cannot read archived Drive object") from exc

    def metadata(self, ref):
        if ref not in self.cache:
            with self._open(
                ref,
                {
                    "fields": "id,parents,size,trashed,md5Checksum,mimeType",
                    "supportsAllDrives": "true",
                },
            ) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise WorkflowError("oversized Drive metadata response")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise WorkflowError("invalid Drive metadata response")
            try:
                size = int(value["size"])
            except (KeyError, ValueError, TypeError) as exc:
                raise WorkflowError("archived object has no binary size") from exc
            if not 0 < size <= self.max_bytes:
                raise WorkflowError("archived Drive object exceeds the byte limit")
            self.cache[ref] = value
        return dict(self.cache[ref])

    def read_reference(self, ref, *, streaming=True):
        if streaming is not True:
            raise WorkflowError("archive reads require a bounded streaming snapshot")
        expected = int(self.metadata(ref)["size"])
        snapshot = FileContent()
        try:
            with self._open(
                ref, {"alt": "media", "supportsAllDrives": "true"}
            ) as response:
                total = 0
                while True:
                    chunk = response.read(min(1024 * 1024, expected - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected:
                        raise WorkflowError("Drive media exceeds its declared size")
                    snapshot.file.write(chunk)
            if total != expected:
                raise WorkflowError(
                    "Drive media size changed or download is incomplete"
                )
            snapshot.file.seek(0)
            return snapshot, {"backend": "gdrive", "ref": ref.split("://")[1]}
        except BaseException:
            snapshot.close()
            raise
