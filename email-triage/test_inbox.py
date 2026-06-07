#!/usr/bin/env python3
"""Regression tests for email-triage (stdlib unittest + mock — no pip install).

Run:  python3 -m unittest test_inbox -v   (from the email-triage/ directory)

Covers the invariants that are easy to get subtly wrong:
  - mark-seen / mark-unseen flag POLARITY per backend (IMAP \\Seen is the
    opposite operation of Gmail's UNREAD label — a single shared boolean is a
    trap that once shipped a "reports success but stays unread" bug).
  - account -> backend resolution.
  - poller per-account dedup, independent failure isolation, legacy migration.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mail = _load("inbox_mail", "inbox-mail.py")
poll = _load("inbox_poll", "inbox-poll.py")


class TestResolveBackend(unittest.TestCase):
    def test_resolution(self):
        env = {"INBOX_EMAIL": "imap@x.com", "INBOX_GOG_ACCOUNTS": "g1@x.com, g2@x.com"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(mail.resolve_backend(None), ("imap@x.com", "imap"))
            self.assertEqual(mail.resolve_backend("g1@x.com"), ("g1@x.com", "gog"))
            self.assertEqual(mail.resolve_backend("g2@x.com"), ("g2@x.com", "gog"))
            self.assertEqual(mail.resolve_backend("other@x.com"), ("other@x.com", "imap"))


class TestMarkPolarity(unittest.TestCase):
    """The bug that bit: mark-seen must ADD \\Seen (IMAP) but REMOVE UNREAD (gog)."""

    def test_imap_mark_seen_adds_seen_flag(self):
        fake = mock.MagicMock()
        fake.uid.return_value = ("OK", [b"1 (UID 1 FLAGS (\\Seen))"])
        fake.__enter__.return_value = fake
        with mock.patch.dict(os.environ, {"INBOX_APP_PASSWORD": "pw"}, clear=False), \
             mock.patch.object(mail.imaplib, "IMAP4_SSL", return_value=fake):
            mail.imap_store("imap@x.com", ["7"], seen=True)
        op = fake.uid.call_args[0]
        self.assertEqual(op, ("store", "7", "+FLAGS", "(\\Seen)"))

    def test_imap_mark_unseen_removes_seen_flag(self):
        fake = mock.MagicMock()
        fake.uid.return_value = ("OK", [b""])
        fake.__enter__.return_value = fake
        with mock.patch.dict(os.environ, {"INBOX_APP_PASSWORD": "pw"}, clear=False), \
             mock.patch.object(mail.imaplib, "IMAP4_SSL", return_value=fake):
            mail.imap_store("imap@x.com", ["7"], seen=False)
        self.assertEqual(fake.uid.call_args[0], ("store", "7", "-FLAGS", "(\\Seen)"))

    def test_gog_mark_seen_removes_unread_label(self):
        with mock.patch.object(mail, "gog_run") as run:
            mail.gog_store("g@x.com", ["abc"], seen=True)
        args = run.call_args[0][1]
        self.assertIn("--remove", args)
        self.assertNotIn("--add", args)
        self.assertEqual(args[:3], ["gmail", "batch", "modify"])

    def test_gog_mark_unseen_adds_unread_label(self):
        with mock.patch.object(mail, "gog_run") as run:
            mail.gog_store("g@x.com", ["abc"], seen=False)
        args = run.call_args[0][1]
        self.assertIn("--add", args)
        self.assertNotIn("--remove", args)


class TestPollerLogic(unittest.TestCase):
    def _fresh(self, **env):
        tmp = tempfile.mkdtemp()
        poll.STATE_PATH = Path(tmp) / "state.json"
        poll.load_env_files = lambda: None
        for k in ("INBOX_EMAIL", "INBOX_APP_PASSWORD", "INBOX_GOG_ACCOUNTS"):
            os.environ.pop(k, None)
        os.environ.update(env)

    def test_dedup_and_multi_account_wake(self):
        self._fresh(INBOX_EMAIL="imap@x.com", INBOX_APP_PASSWORD="pw", INBOX_GOG_ACCOUNTS="g1@x.com")
        wakes = []
        imap_ids = {"v": ["1", "2"]}
        gog_ids = {"v": ["a"]}
        with mock.patch.object(poll, "wake_for_mail", lambda per: wakes.append(set(per))), \
             mock.patch.object(poll, "wake_for_failure", lambda e: wakes.append("fail")), \
             mock.patch.object(poll, "imap_unread_ids", lambda a: list(imap_ids["v"])), \
             mock.patch.object(poll, "gog_unread_ids", lambda a: list(gog_ids["v"])):
            poll.main()                       # first run: all new -> wake both
            self.assertEqual(wakes, [{"imap@x.com", "g1@x.com"}])
            wakes.clear()
            poll.main()                       # no change -> no wake
            self.assertEqual(wakes, [])
            gog_ids["v"] = ["a", "b"]
            poll.main()                       # new gog only -> wake gog only
            self.assertEqual(wakes, [{"g1@x.com"}])

    def test_one_backend_failure_does_not_blind_the_other(self):
        self._fresh(INBOX_EMAIL="imap@x.com", INBOX_APP_PASSWORD="pw", INBOX_GOG_ACCOUNTS="g1@x.com")
        wakes = []
        with mock.patch.object(poll, "wake_for_mail", lambda per: wakes.append(("mail", set(per)))), \
             mock.patch.object(poll, "wake_for_failure", lambda e: wakes.append(("fail", e))), \
             mock.patch.object(poll, "imap_unread_ids", mock.Mock(side_effect=RuntimeError("imap down"))), \
             mock.patch.object(poll, "gog_unread_ids", lambda a: []):
            rc = poll.main()
        self.assertEqual(rc, 0, "a working backend keeps exit 0")
        self.assertTrue(any(w[0] == "fail" for w in wakes), "still raises a throttled failure wake")

    def test_both_backends_down_exits_nonzero(self):
        self._fresh(INBOX_EMAIL="imap@x.com", INBOX_APP_PASSWORD="pw", INBOX_GOG_ACCOUNTS="g1@x.com")
        with mock.patch.object(poll, "wake_for_failure", lambda e: None), \
             mock.patch.object(poll, "imap_unread_ids", mock.Mock(side_effect=RuntimeError("imap down"))), \
             mock.patch.object(poll, "gog_unread_ids", mock.Mock(side_effect=RuntimeError("gog down"))):
            self.assertEqual(poll.main(), 1)

    def test_legacy_state_migration_avoids_spurious_wake(self):
        self._fresh(INBOX_EMAIL="imap@x.com", INBOX_APP_PASSWORD="pw")
        poll.STATE_PATH.write_text(json.dumps({"seen_unread_uids": ["1", "2"]}))
        wakes = []
        with mock.patch.object(poll, "wake_for_mail", lambda per: wakes.append("mail")), \
             mock.patch.object(poll, "imap_unread_ids", lambda a: ["1", "2"]):
            poll.main()
        self.assertEqual(wakes, [], "upgrading from a single-account install must not re-wake")


if __name__ == "__main__":
    unittest.main()
