import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
from ghapplib import (determine_push_strategy, force_push_to_default_blocked,
                      select_unmerged_commits)

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


class TestSelectUnmergedCommits(unittest.TestCase):
    # The 377ed77 bug: pushing a rebased branch recreated already-merged
    # commits as duplicates because `--not --remotes` dedupes by SHA only.

    def test_no_dupes_keeps_everything(self):
        # Genuinely new branch, nothing merged yet — every commit survives so
        # the caller keeps preserve_parents/orphan-safety behaviour.
        commits = ["a", "b", "c"]
        trees = {"a": "ta", "b": "tb", "c": "tc"}
        self.assertEqual(
            select_unmerged_commits(commits, trees, merged_trees=set()),
            commits,
        )

    def test_squash_merge_drops_merged_prefix(self):
        # c15,c16,c17 were squash-merged into one commit whose tree == tree(c17);
        # intermediate commits match neither tree nor patch-id. Everything up to
        # and including the deepest tree match is dropped; new work survives.
        commits = ["c15", "c16", "c17", "newA", "newB"]
        trees = {"c15": "t15", "c16": "t16", "c17": "tSquash",
                 "newA": "tA", "newB": "tB"}
        merged_trees = {"tSquash"}
        self.assertEqual(
            select_unmerged_commits(commits, trees, merged_trees),
            ["newA", "newB"],
        )

    def test_squash_merge_of_whole_branch_drops_all(self):
        commits = ["c15", "c16", "c17"]
        trees = {"c15": "t15", "c16": "t16", "c17": "tSquash"}
        self.assertEqual(
            select_unmerged_commits(commits, trees, {"tSquash"}),
            [],
        )

    def test_rebase_merge_drops_by_patch_id(self):
        # Rebase/ordinary merge keeps per-commit patch-ids upstream; git cherry
        # flags them ('-') and we drop them wherever they sit.
        commits = ["c1", "c2", "newA"]
        trees = {"c1": "t1", "c2": "t2", "newA": "tA"}
        survivors = select_unmerged_commits(
            commits, trees, merged_trees=set(),
            patch_dup_shas={"c1", "c2"},
        )
        self.assertEqual(survivors, ["newA"])

    def test_non_linear_skips_squash_cut(self):
        # A merge commit in the range disables the tree-based squash cut (the
        # "deepest match ⇒ prefix" guarantee needs linear history); only the
        # explicit patch-id dupes are dropped, never a tree coincidence.
        commits = ["c15", "c16", "c17", "newA"]
        trees = {"c15": "t15", "c16": "t16", "c17": "tSquash", "newA": "tA"}
        survivors = select_unmerged_commits(
            commits, trees, merged_trees={"tSquash"}, linear=False
        )
        self.assertEqual(survivors, commits)

    def test_empty_input(self):
        self.assertEqual(select_unmerged_commits([], {}, {"x"}), [])


if __name__ == '__main__':
    unittest.main()
