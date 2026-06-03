import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
from ghapplib import determine_push_strategy, force_push_to_default_blocked

class TestPushStrategy(unittest.TestCase):

    def test_force_push(self):
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", True, True, True, force=True
        )
        self.assertIsNone(commits)
        self.assertEqual(parent, "")
        self.assertTrue(force)
        self.assertTrue(preserve)

    def test_fast_forward(self):
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", True, True, True, force=False
        )
        self.assertIsNone(commits)
        self.assertEqual(parent, "R")
        self.assertFalse(force)
        self.assertTrue(preserve)

    def test_recreated_sha_on_remote(self):
        # Remote exists but is not known locally (recreated by an earlier
        # App-push). There is no sound local rev-list here, so the strategy must
        # NOT return None — that would make the caller run
        # `rev-list remote_sha..local_sha` against a SHA it doesn't have (crash)
        # or re-push the whole history as duplicates. Push the local tip onto
        # the recreated remote tip instead.
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", False, False, True, force=False
        )
        self.assertEqual(commits, ["L"])
        self.assertEqual(parent, "R")
        self.assertFalse(force)
        self.assertFalse(preserve)

    def test_divergent_branch(self):
        # Remote exists and known locally, but not ancestor
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", True, False, True, force=False
        )
        self.assertIsNone(commits)
        self.assertEqual(parent, "")
        self.assertTrue(force)
        self.assertTrue(preserve)

    def test_new_branch(self):
        # New branch: caller must compute the full rev-list of commits not yet
        # on the remote AND preserve original parents — otherwise we'd push
        # only the tip as an orphan, disconnected from main.
        commits, parent, force, preserve = determine_push_strategy(
            "L", "", False, False, False, force=False
        )
        self.assertIsNone(commits, "caller must do the rev-list itself")
        self.assertEqual(parent, "")
        self.assertFalse(force)
        self.assertTrue(preserve, "must preserve parents to avoid orphan commits")

    def test_new_branch_never_orphans(self):
        # Regression guard for the bug Copilot caught: for any new-branch
        # invocation (force=False), the strategy must signal preserve_parents
        # so the original parent chain is mapped onto the remote branchpoint.
        _, _, _, preserve = determine_push_strategy(
            "L", "", False, False, False, force=False
        )
        self.assertTrue(preserve)


class TestDefaultBranchForceGuard(unittest.TestCase):
    DEFAULT = "develop"  # user does not use 'main' — guard must be name-agnostic

    def test_explicit_force_to_default_is_blocked(self):
        self.assertTrue(force_push_to_default_blocked(
            "develop", self.DEFAULT, force=True, needs_force=False))

    def test_divergent_needs_force_to_default_is_blocked(self):
        # rebase+force scenario: determine_push_strategy returns needs_force
        self.assertTrue(force_push_to_default_blocked(
            "develop", self.DEFAULT, force=False, needs_force=True))

    def test_force_to_feature_branch_is_allowed(self):
        self.assertFalse(force_push_to_default_blocked(
            "feat/x", self.DEFAULT, force=True, needs_force=True))

    def test_fast_forward_to_default_is_allowed(self):
        # no force, no needs_force → ordinary push, never blocked
        self.assertFalse(force_push_to_default_blocked(
            "develop", self.DEFAULT, force=False, needs_force=False))

    def test_override_bypasses_block(self):
        self.assertFalse(force_push_to_default_blocked(
            "develop", self.DEFAULT, force=True, needs_force=True, override=True))


if __name__ == '__main__':
    unittest.main()
