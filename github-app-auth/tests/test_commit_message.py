"""Pin down byte-exact commit-message extraction.

Regression cover for the API push route recreating commits under new SHAs:
the message was read with `--format=%B` + .strip(), which ate the message's
own trailing newline. One byte short => different commit SHA => upstream sees
a stranger's commit and marks the PR dirty.

The check here is deliberately end-to-end on real git objects: take a real
commit, swap ONLY the message back in via commit_message(), re-hash the object
and require the original SHA. If extraction loses (or adds) a byte, the hash
moves and the test fails.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
from ghapplib import commit_message, run_git

GIT = shutil.which("git")


def hash_commit_object(content: bytes) -> str:
    return hashlib.sha1(b"commit %d\0" % len(content) + content).hexdigest()


@unittest.skipIf(not GIT, "git not available")
class TestCommitMessage(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self._git("init", "-q", ".")
        self._git("config", "user.name", "Test Bot")
        self._git("config", "user.email", "bot@example.com")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args):
        return subprocess.run([GIT] + list(args), cwd=self.repo,
                              capture_output=True, check=True)

    def _commit(self, message, cleanup="default"):
        path = os.path.join(self.repo, "f.txt")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("x\n")
        self._git("add", "f.txt")
        msg_file = os.path.join(self.scratch, "msg")
        with open(msg_file, "w", encoding="utf-8", newline="") as fh:
            fh.write(message)
        self._git("commit", "-q", "--cleanup=" + cleanup, "-F", msg_file)
        return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def _raw(self, sha):
        return subprocess.run([GIT, "cat-file", "commit", sha], cwd=self.repo,
                              capture_output=True, check=True).stdout

    def _rebuild_with(self, sha, message: str) -> str:
        """Re-hash the commit object with `message` swapped in for the body."""
        header, sep, _ = self._raw(sha).partition(b"\n\n")
        self.assertEqual(sep, b"\n\n", "commit object has no header/body split")
        return hash_commit_object(header + sep + message.encode("utf-8"))

    # --- the property that matters -------------------------------------

    def test_extraction_preserves_commit_sha(self):
        for label, message in [
            ("single line", "docs: refresh README\n"),
            ("subject + body", "feat: thing\n\nWhy it matters.\nSecond line.\n"),
            ("trailing blank lines", "fix: oops\n\nbody\n"),
            ("utf-8", "chore: café naïve — em dash\n"),
            ("blank-line-heavy body", "a\n\nb\n\nc\n"),
        ]:
            with self.subTest(label):
                sha = self._commit(message)
                extracted = commit_message(GIT, sha, cwd=self.repo)
                # git's own object is the ground truth, not our input string.
                _, _, body = self._raw(sha).partition(b"\n\n")
                self.assertEqual(extracted.encode("utf-8"), body)
                self.assertEqual(self._rebuild_with(sha, extracted), sha)

    def test_trailing_newline_is_kept(self):
        sha = self._commit("docs: refresh README\n")
        self.assertTrue(commit_message(GIT, sha, cwd=self.repo).endswith("\n"))
        # and exactly one -- %B without -z would hand us two.
        self.assertFalse(commit_message(GIT, sha, cwd=self.repo).endswith("\n\n"))

    def test_no_record_separator_leaks_in(self):
        # `--format=%B` appends git's own newline separator; -z must replace it
        # with a NUL that we then drop, so extraction == the object's body.
        sha = self._commit("feat: thing\n\nbody line\n")
        naive = run_git(GIT, ["log", "-1", "--format=%B", sha],
                        text=True, cwd=self.repo).stdout
        self.assertEqual(naive, commit_message(GIT, sha, cwd=self.repo) + "\n")

    # --- the exact bug, kept red-able ---------------------------------

    def test_stripped_message_would_change_the_sha(self):
        """Guard the guard: prove the old .strip() really did move the SHA.

        Without this, a future 'harmless' .strip() could be reintroduced and
        the tests above would be the only thing standing between us and
        another round of recreated commits.
        """
        sha = self._commit("docs: refresh README\n")
        stripped = commit_message(GIT, sha, cwd=self.repo).strip()
        self.assertNotEqual(self._rebuild_with(sha, stripped), sha)

    def test_crlf_body_survives(self):
        # text=True would translate CRLF to LF and silently reshape the object.
        sha = self._commit("subject\r\n\r\nbody\r\n", cleanup="verbatim")
        _, _, body = self._raw(sha).partition(b"\n\n")
        if b"\r" not in body:
            self.skipTest("git normalised CRLF away; nothing to protect here")
        extracted = commit_message(GIT, sha, cwd=self.repo)
        self.assertEqual(extracted.encode("utf-8"), body)
        self.assertEqual(self._rebuild_with(sha, extracted), sha)


if __name__ == "__main__":
    unittest.main()
