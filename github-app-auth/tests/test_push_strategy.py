import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
from ghapplib import determine_push_strategy

class TestPushStrategy(unittest.TestCase):

    def test_force_push(self):
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", True, True, True, force=True
        )
        self.assertEqual(commits, ["L"])
        self.assertEqual(parent, "")
        self.assertTrue(force)
        self.assertFalse(preserve)

    def test_fast_forward(self):
        commits, parent, force, preserve = determine_push_strategy(
            "L", "R", True, True, True, force=False
        )
        self.assertIsNone(commits)
        self.assertEqual(parent, "R")
        self.assertFalse(force)
        self.assertTrue(preserve)

    def test_recreated_sha_on_remote(self):
        # Remote exists but is not known locally (recreated by App)
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
        self.assertEqual(commits, ["L"])
        self.assertEqual(parent, "")
        self.assertTrue(force)
        self.assertFalse(preserve)

    def test_new_branch(self):
        commits, parent, force, preserve = determine_push_strategy(
            "L", "", False, False, False, force=False
        )
        self.assertEqual(commits, ["L"])
        self.assertEqual(parent, "")
        self.assertFalse(force)
        self.assertFalse(preserve)

if __name__ == '__main__':
    unittest.main()
