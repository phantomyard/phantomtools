"""Tests for filing scaffold norms as drawer rows at deploy time (#34).

memory/norms.md is deprecated: on phantombot >= 1.1.282 the threat judge is
briefed from ranked rows in memory.sqlite (drawer_entries), never from the
markdown file. PhantomOrg's build emits norms.json (one plain-text line per
scaffold norm); deploy/norms.py files each line as a row via the phantombot
CLI. These tests inject a fake runner so no real binary is needed.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build
from phantomorg.compiler.build import NORMS_FILENAME
from phantomorg.deploy.norms import NormFilingResult, file_norms
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


def _build_au(tmp: Path, only: str | None = None) -> Path:
    spec, _ = validate_org(AU_ORG)
    out = tmp / "dist"
    build(spec, out, only=only)
    return out


class _FakeRunner:
    """Captures each invocation and returns a configurable result."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "filed norms x\n",
        stderr: str = "",
    ):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args, self.returncode, self.stdout, self.stderr
        )


class TestFileNorms(unittest.TestCase):
    def test_files_each_norm_line_as_a_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            runner = _FakeRunner()

            result = file_norms(out, phantombot_bin="phantombot", runner=runner)

            manifest = json.loads(
                (out / "dana" / NORMS_FILENAME).read_text(encoding="utf-8")
            )
            expected = manifest["entries"]
            self.assertGreaterEqual(len(expected), 5)

            # One `memory drawers --kind norms --file "<line>" --persona dana`
            # per norm line, in manifest order.
            self.assertEqual(len(runner.calls), len(expected))
            for line, call in zip(expected, runner.calls):
                self.assertEqual(
                    call,
                    [
                        "memory",
                        "drawers",
                        "--kind",
                        "norms",
                        "--file",
                        line,
                        "--persona",
                        "dana",
                    ],
                )
            self.assertEqual(result.filed, {"dana": len(expected)})
            self.assertEqual(result.errors, [])
            self.assertTrue(result.ok)

    def test_no_channels_fils_nothing(self):
        # An org without channels has an empty entries list -> no rows, no
        # binary invocations, and no error.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            spec, _ = validate_org(AU_ORG)
            out = tmp / "dist"
            build(spec, out, only="dana")
            # Simulate a channel-less org: empty manifest entries.
            manifest = out / "dana" / NORMS_FILENAME
            manifest.write_text(
                json.dumps(
                    {"kind": "norms", "origin": "phantomorg", "entries": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runner = _FakeRunner()

            result = file_norms(out, phantombot_bin="phantombot", runner=runner)

            self.assertEqual(runner.calls, [])
            self.assertEqual(result.filed, {})
            self.assertEqual(result.errors, [])
            self.assertTrue(result.ok)

    def test_missing_manifest_is_not_an_error(self):
        # No norms.json in the compiled tree -> nothing to file, no error.
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            (out / "dana" / NORMS_FILENAME).unlink()
            runner = _FakeRunner()

            result = file_norms(out, phantombot_bin="phantombot", runner=runner)

            self.assertEqual(runner.calls, [])
            self.assertEqual(result.filed, {})
            self.assertEqual(result.errors, [])

    def test_nonzero_exit_is_non_fatal_and_loud(self):
        # A too-old phantombot (no `memory drawers --file`) must produce a
        # recorded error — never a silent skip — while not raising.
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            runner = _FakeRunner(
                returncode=1, stdout="", stderr="unknown command: memory drawers\n"
            )

            result = file_norms(out, phantombot_bin="phantombot", runner=runner)

            self.assertEqual(result.filed, {})
            self.assertEqual(len(result.errors), 1)
            self.assertIn("dana", result.errors[0])
            self.assertIn("unknown command", result.errors[0])
            self.assertFalse(result.ok)

    def test_binary_missing_is_non_fatal(self):
        def missing(args: list[str]):
            raise FileNotFoundError("phantombot not found")

        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")

            result = file_norms(out, phantombot_bin="phantombot", runner=missing)

            self.assertEqual(result.filed, {})
            self.assertEqual(len(result.errors), 1)
            self.assertIn("dana", result.errors[0])
            self.assertIn("phantombot", result.errors[0])

    def test_default_runner_uses_binary_path(self):
        # Sanity: the default runner is a real subprocess of <bin>; we only
        # assert the arg shape by not injecting a runner (the binary will
        # fail to be found in a sandbox, which is the non-fatal path).
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            # Point at a binary that certainly does not exist.
            result = file_norms(
                out, phantombot_bin="definitely-not-a-real-binary-xyz"
            )
            self.assertEqual(result.filed, {})
            self.assertGreaterEqual(len(result.errors), 1)
            self.assertIn("definitely-not-a-real-binary-xyz", result.errors[0])


if __name__ == "__main__":
    unittest.main()
