"""Root-only filesystem tests in disposable directories; no host deployment."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from support import load_control


@unittest.skipUnless(
    sys.platform == "linux" and getattr(os, "geteuid", lambda: -1)() == 0,
    "Requires root for actual ownership checks",
)
class TestTrustedConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="psc-test-", dir="/root")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "example.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": "example",
                    "account": "nobody",
                    "uid": 65534,
                    "unit": "example.service",
                }
            ),
            encoding="utf-8",
        )
        self.config.chmod(0o644)

    def reject(self, path=None):
        with self.assertRaises((self.control.Rejected, OSError)):
            self.control.load_binding(str(path or self.config))

    def test_accepts_root_owned_readable_metadata_and_rejects_uid_drift(self):
        self.assertEqual(self.control.load_binding(str(self.config)).account, "nobody")
        data = json.loads(self.config.read_text())
        data["uid"] = 12345
        self.config.write_text(json.dumps(data))
        self.reject()

    def test_rejects_writable_or_nonroot_owned_file_and_parent(self):
        self.config.chmod(0o666)
        self.reject()
        self.config.chmod(0o644)
        os.chown(self.config, 65534, 65534)
        self.reject()
        os.chown(self.config, 0, 0)
        self.directory.chmod(0o777)
        self.reject()

    def test_rejects_symlink_file_and_symlink_parent(self):
        link = self.directory / "link.json"
        link.symlink_to(self.config)
        self.reject(link)
        parent_link = self.directory / "alias"
        real_parent = self.directory / "real"
        real_parent.mkdir()
        (real_parent / "example.json").write_bytes(self.config.read_bytes())
        parent_link.symlink_to(real_parent, target_is_directory=True)
        self.reject(parent_link / "example.json")

    def test_rejects_fifo_oversized_duplicate_and_unknown_fields(self):
        fifo = self.directory / "fifo.json"
        os.mkfifo(fifo, 0o600)
        self.reject(fifo)
        self.config.write_bytes(b"x" * (self.control.MAX_CONFIG + 1))
        self.reject()
        self.config.write_text('{"version":1,"version":1}')
        self.reject()
        self.config.write_text('{"command":"arbitrary"}')
        self.reject()


if __name__ == "__main__":
    unittest.main()
