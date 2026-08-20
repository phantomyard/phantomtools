"""
Regression tests for the adversarial-review MEDIUM/LOW findings in the
compiler area (v0.4.13):

- F2/F3: atomic writes + refusal to follow file-level symlinks
  (arbitrary-file overwrite via a planted symlink in the output tree)
- F7: silent language fallback now warns
- F8: `po build --only <unknown-id>` is a clean CLI error, no KeyError
- F9: `build-all` warns when the org directory name != organization.id
"""

import tempfile
import unittest
import warnings
from pathlib import Path

from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.compiler import build
from phantomorg.compiler.build import (
    write_if_changed,
    write_if_missing,
    write_plain_if_changed,
)
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


def _au_spec():
    spec, result = validate_org(AU_ORG)
    assert result.ok, result.errors
    return spec


class TestAtomicWrites(unittest.TestCase):
    """F2: no truncated file can survive a mid-write crash."""

    def _simulate_crash(self, writer, path, content):
        """Monkeypatch os.replace to raise like a crash between write and
        replace: the temp file may exist, the target must not be touched."""
        import sys

        build_mod = sys.modules["phantomorg.compiler.build"]
        orig_replace = build_mod.os.replace

        def boom(src, dst):
            raise KeyboardInterrupt("simulated crash")

        build_mod.os.replace = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                writer(path, content)
        finally:
            build_mod.os.replace = orig_replace
        # the target keeps its previous content (never truncated)
        return path.read_text(encoding="utf-8")

    def test_write_if_changed_crash_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "SOUL.md"
            original = (
                "<!-- ORG:BEGIN tools -->\nold\n<!-- ORG:END tools -->\nmanual note\n"
            )
            p.write_text(original, encoding="utf-8")
            new_content = (
                "<!-- ORG:BEGIN tools -->\nnew\n<!-- ORG:END tools -->\nmanual note\n"
            )
            surviving = self._simulate_crash(write_if_changed, p, new_content)
            self.assertEqual(surviving, original)

    def test_plain_write_crash_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".phantomorg.yaml"
            p.write_text("organization_id: x", encoding="utf-8")
            surviving = self._simulate_crash(
                write_plain_if_changed, p, "organization_id: y\n"
            )
            self.assertEqual(surviving, "organization_id: x")

    def test_write_is_atomic_no_leftover_temp(self):
        """After a successful write there are no stray .<name>.* temp files."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "SOUL.md"
            write_if_changed(p, "content")
            leftovers = [
                f for f in Path(tmp).iterdir() if f.name.startswith(".SOUL.md.")
            ]
            self.assertEqual(leftovers, [])


class TestSymlinkRefusal(unittest.TestCase):
    """F3: file-level symlinks in the output tree are never followed."""

    def test_write_if_changed_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.md"
            victim.write_text("PRECIOUS", encoding="utf-8")
            link = Path(tmp) / "SOUL.md"
            link.symlink_to(victim)
            with self.assertRaises(ValueError):
                write_if_changed(link, "ORG content")
            self.assertEqual(victim.read_text(encoding="utf-8"), "PRECIOUS")

    def test_write_plain_if_changed_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "important.cfg"
            victim.write_text("keep", encoding="utf-8")
            link = Path(tmp) / ".phantomorg.yaml"
            link.symlink_to(victim)
            with self.assertRaises(ValueError):
                write_plain_if_changed(link, "organization_id: x\n")
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_write_if_missing_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.md"
            victim.write_text("PRECIOUS", encoding="utf-8")
            link = Path(tmp) / "MEMORY.md"
            link.symlink_to(victim)
            with self.assertRaises(ValueError):
                write_if_missing(link, "content")
            self.assertEqual(victim.read_text(encoding="utf-8"), "PRECIOUS")

    def test_symlink_is_replaced_not_written_through(self):
        """os.replace replaces the symlink itself; target file untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.md"
            victim.write_text("PRECIOUS", encoding="utf-8")
            link = Path(tmp) / "SOUL.md"
            link.symlink_to(victim)
            # first call refuses (read path). Delete the link, then verify
            # a *fresh* symlink planted between read and write is replaced.
            link.unlink()
            link.symlink_to(victim)
            # directly exercise _atomic_write on the symlink path
            from phantomorg.compiler.build import _atomic_write

            _atomic_write(link, "new content")
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_text(encoding="utf-8"), "new content")
            self.assertEqual(victim.read_text(encoding="utf-8"), "PRECIOUS")


class TestLanguageFallbackWarning(unittest.TestCase):
    """F7: a spec language with no translation must warn, not stay silent."""

    def test_unknown_language_warns(self):
        spec = _au_spec()
        # force the org language to something untranslated
        spec.organization.default_language = "fr"
        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                build(spec, Path(tmp))
            self.assertTrue(
                any("no translation" in str(w.message) for w in caught),
                [str(w.message) for w in caught],
            )


class TestOnlyUnknownActor(unittest.TestCase):
    """F8: pf build --only <unknown> is a clean error."""

    def test_only_unknown_actor_clean_error(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["build", "--org", str(AU_ORG), "--out", "unused", "--only", "ghost"],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no actor with id 'ghost'", result.output)
        self.assertNotIn("Traceback", result.output)


class TestBuildAllDirIdMismatch(unittest.TestCase):
    """F9: build-all warns when directory name != organization.id."""

    def _copy_au_org(self, base: Path, dir_name: str) -> Path:
        """Copy the AU org (id verdant-aquaponics) under ``dir_name``."""
        org_yaml = base / dir_name / "org.yaml"
        org_yaml.parent.mkdir(parents=True)
        org_yaml.write_bytes(AU_ORG.read_bytes())
        return org_yaml

    def test_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "orgs"
            out = Path(tmp) / "out"
            self._copy_au_org(base, "renamed-dir")
            runner = CliRunner()
            result = runner.invoke(
                main, ["build-all", "--base", str(base), "--out", str(out)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(
                "directory name 'renamed-dir' != organization.id", result.output
            )
            # still builds into the dir-name key (layout behavior unchanged)
            self.assertTrue((out / "renamed-dir" / "marco" / "SOUL.md").exists())

    def test_match_no_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "orgs"
            out = Path(tmp) / "out"
            self._copy_au_org(base, "verdant-aquaponics")
            runner = CliRunner()
            result = runner.invoke(
                main, ["build-all", "--base", str(base), "--out", str(out)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("!= organization.id", result.output)


if __name__ == "__main__":
    unittest.main()
