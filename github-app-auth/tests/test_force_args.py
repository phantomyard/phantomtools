"""Unit tests for the force-push classification and the guard's target parsing.

The default-branch guard now runs before the HTTPS push as well, which means
it has to recognise a history rewrite from the argument list alone. These
tests pin that down, plus the refspec parsing that decides *which* remote
branch the push actually updates.
"""

import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
from ghapplib import (is_force_arg, push_refspec_dst, push_targets_every_branch,
                      force_push_to_default_blocked)

CASES_FILE = os.path.join(os.path.dirname(__file__), "force_cases.txt")


def load_cases():
    cases = []
    with open(CASES_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            expect, _, argv = line.partition("\t")
            cases.append((expect.strip() == "force", argv.split()))
    return cases


class TestForceArgs(unittest.TestCase):

    def test_shared_truth_table(self):
        cases = load_cases()
        self.assertGreater(len(cases), 15, "case file looks truncated")
        for expected, argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(any(is_force_arg(a) for a in argv), expected)

    def test_force_if_includes_is_not_a_force(self):
        # It qualifies a force, it isn't one — and it starts with --force,
        # so a prefix match would wrongly flag every safety-conscious push.
        self.assertFalse(is_force_arg("--force-if-includes"))

    def test_mirror_forces_without_an_explicit_flag(self):
        self.assertTrue(is_force_arg("--mirror"))

    def test_empty_and_bare_plus(self):
        self.assertFalse(is_force_arg(""))
        self.assertFalse(is_force_arg("+"))


class TestRefspecDst(unittest.TestCase):

    def test_plain_branch(self):
        self.assertEqual(push_refspec_dst("main"), "main")

    def test_src_colon_dst_uses_the_destination(self):
        # The whole point: the local ref says HEAD, the remote ref says main.
        self.assertEqual(push_refspec_dst("HEAD:main"), "main")
        self.assertEqual(push_refspec_dst("feature:main"), "main")

    def test_force_marker_is_stripped(self):
        self.assertEqual(push_refspec_dst("+main"), "main")
        self.assertEqual(push_refspec_dst("+feature:main"), "main")

    def test_fully_qualified_ref(self):
        self.assertEqual(push_refspec_dst("refs/heads/main"), "main")
        self.assertEqual(push_refspec_dst("+HEAD:refs/heads/main"), "main")

    def test_delete_form(self):
        self.assertEqual(push_refspec_dst(":main"), "main")

    def test_empty(self):
        self.assertEqual(push_refspec_dst(""), "")


class TestGuardTargeting(unittest.TestCase):
    """The refspec forms that used to walk straight past the guard."""

    def blocked(self, refspec, args=(), default="main"):
        target = (default if push_targets_every_branch(list(args))
                  else push_refspec_dst(refspec))
        return force_push_to_default_blocked(target, default, True, False)

    def test_head_colon_main_is_blocked(self):
        self.assertTrue(self.blocked("HEAD:main"))

    def test_plus_refspec_to_default_is_blocked(self):
        self.assertTrue(self.blocked("+refs/heads/main:refs/heads/main"))

    def test_feature_branch_still_allowed(self):
        self.assertFalse(self.blocked("HEAD:feat/x"))
        self.assertFalse(self.blocked("feat/x"))

    def test_all_and_mirror_include_the_default_branch(self):
        self.assertTrue(self.blocked("", args=["--all"]))
        self.assertTrue(self.blocked("", args=["--mirror"]))

    def test_non_default_repo_name(self):
        self.assertTrue(self.blocked("trunk", default="trunk"))
        self.assertFalse(self.blocked("main", default="trunk"))


if __name__ == "__main__":
    unittest.main()
