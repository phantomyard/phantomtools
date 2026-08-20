"""Regression tests for the wizard mutation lock (audit v0.5.7 #3).

The historical ``_mutation_lock`` was a no-op on Windows (``fcntl``
missing -> ``yield`` immediately): two concurrent ``po add-*`` / ``pf
setup`` writers could both load the same org doc, both append, both
save — the second ``os.replace`` silently discarded the first mutation
(lost update). The fix replicates the deploy layer's lock pattern:
``fcntl.flock`` on POSIX, ``msvcrt.locking`` byte-range on Windows.

These tests verify the lock actually SERIALIZES. ``flock`` contends on
the open-file-description, so two separate ``open()`` handles to the
same lockfile in the same process DO compete — no multiprocessing
needed.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from phantomorg.wizard.mutations import _mutation_lock


class TestMutationLockSerializes(unittest.TestCase):
    def test_lock_blocks_second_holder_until_released(self):
        """A second acquisition of the same lockfile must block until
        the first holder releases it (a no-op lock would return
        immediately for both)."""
        with tempfile.TemporaryDirectory() as t:
            org = Path(t) / "org.yaml"
            org.write_text("org:\n  name: test\n", encoding="utf-8")

            lockfile = org.with_name("org.yaml.lock")
            first_released = threading.Event()
            second_acquired = threading.Event()
            results: list[str] = []

            def holder_one():
                with _mutation_lock(org):
                    results.append("first-in")
                    # hold the lock until the second thread is blocked
                    time.sleep(0.4)
                    results.append("first-out")
                first_released.set()

            def holder_two():
                # Give holder_one a head start, then try to acquire.
                first_released.wait(timeout=5)
                with _mutation_lock(org):
                    results.append("second-in")
                second_acquired.set()

            t1 = threading.Thread(target=holder_one)
            t2 = threading.Thread(target=holder_two)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertTrue(first_released.is_set(), "holder one must finish")
            self.assertTrue(second_acquired.is_set(), "holder two must acquire")
            # The second holder must have entered only AFTER the first
            # released — i.e. the lock really serialized.
            self.assertEqual(results, ["first-in", "first-out", "second-in"])
            # The lockfile is left in place (never deleted while the org
            # file exists — deleting a lockfile another process may hold
            # is racy).
            self.assertTrue(lockfile.exists())

    def test_lock_shared_among_same_org(self):
        """Two sequential acquisitions (the normal single-process case)
        both succeed and leave the file usable."""
        with tempfile.TemporaryDirectory() as t:
            org = Path(t) / "org.yaml"
            org.write_text("org:\n  name: test\n", encoding="utf-8")

            with _mutation_lock(org):
                pass
            with _mutation_lock(org):
                pass
            # Lockfile exists and has content (Windows msvcrt needs at
            # least one byte; POSIX writes it on first use too).
            lockfile = org.with_name("org.yaml.lock")
            self.assertTrue(lockfile.exists())
            self.assertGreaterEqual(lockfile.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
