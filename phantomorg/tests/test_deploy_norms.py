"""Tests for filing scaffold norms as drawer rows at deploy time (#34).

memory/norms.md is deprecated: on phantombot >= 1.1.282 the threat judge is
briefed from ranked rows in memory.sqlite (drawer_entries), never from the
markdown file. PhantomOrg's build emits norms.json (one plain-text line per
scaffold norm); deploy/norms.py files each line as a row via the phantombot
CLI. These tests inject a fake runner so no real binary is needed.

Also covers the two deploy-time guards (#40 review):

- **Misfile**: before filing, ``memory today --persona <id>`` must resolve the
  actor (and, when the target is known, print a path under it); otherwise an
  error is recorded and filing is skipped — rows must never land in a
  database unrelated to the deploy target.
- **Stale rows**: a persisted ledger (``.phantomorg-norms.json``) records the
  lines filed last deploy; lines that are gone now are reported as
  ``superseded`` (their rows still decay in the judge for 365 days).
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build
from phantomorg.compiler.build import NORMS_FILENAME
from phantomorg.deploy.norms import (
    NORMS_STATE_FILENAME,
    _hash_line,
    file_norms,
    load_norms_state,
    next_norms_state,
    save_norms_state,
)
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


def _build_au(tmp: Path, only: str | None = None) -> Path:
    spec, _ = validate_org(AU_ORG)
    out = tmp / "dist"
    build(spec, out, only=only)
    return out


class _FakeRunner:
    """Captures each invocation; routes the persona probe (``memory today``)
    and the filing command (``memory drawers``) to separate results."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "filed norms x\n",
        stderr: str = "",
        probe_rc: int = 0,
        probe_stdout: str = "",
    ):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.probe_rc = probe_rc
        self.probe_stdout = probe_stdout

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if len(args) >= 2 and args[1] == "today":
            return subprocess.CompletedProcess(args, self.probe_rc, self.probe_stdout, "")
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

            # One persona probe first, then one `memory drawers --file`
            # per norm line, in manifest order.
            self.assertEqual(runner.calls[0], ["memory", "today", "--persona", "dana"])
            file_calls = runner.calls[1:]
            self.assertEqual(len(file_calls), len(expected))
            for line, call in zip(expected, file_calls):
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
        # binary invocations (no probe either), and no error.
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
        # recorded error — never a silent skip — while not raising. The
        # persona probe (`memory today`) still resolves, so the failure is
        # from the filing command itself.
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
            result = file_norms(out, phantombot_bin="definitely-not-a-real-binary-xyz")
            self.assertEqual(result.filed, {})
            self.assertGreaterEqual(len(result.errors), 1)
            self.assertIn("definitely-not-a-real-binary-xyz", result.errors[0])


class TestPersonaMisfileGuard(unittest.TestCase):
    """#40 Major 2: file_norms must refuse to file when the binary does not
    resolve the actor as a persona (its own config picks personasDir /
    memoryDbPath, uncorrelated with the deploy --target)."""

    def test_probe_nonzero_skips_filing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            runner = _FakeRunner(probe_rc=2)

            result = file_norms(out, phantombot_bin="phantombot", runner=runner)

            self.assertEqual(result.filed, {})
            self.assertEqual(len(result.errors), 1)
            self.assertIn("persona not resolvable", result.errors[0])
            # Only the probe ran — no `memory drawers --file` calls.
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0], ["memory", "today", "--persona", "dana"])

    def test_probe_path_outside_target_skips_filing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = _build_au(tmp, only="dana")
            target = tmp / "personas"
            # Probe resolves the persona in a DIFFERENT personas dir.
            other_dir = tmp / "elsewhere" / "dana" / "memory" / "2026-08-25.md"
            runner = _FakeRunner(probe_stdout=str(other_dir) + "\n")

            result = file_norms(
                out, phantombot_bin="phantombot", runner=runner, target=target
            )

            self.assertEqual(result.filed, {})
            self.assertEqual(len(result.errors), 1)
            self.assertIn("not the deploy target", result.errors[0])
            self.assertEqual(len(runner.calls), 1)

    def test_probe_path_under_target_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = _build_au(tmp, only="dana")
            target = tmp / "personas"
            probe_path = target / "dana" / "memory" / "2026-08-25.md"
            runner = _FakeRunner(probe_stdout=str(probe_path) + "\n")

            result = file_norms(
                out, phantombot_bin="phantombot", runner=runner, target=target
            )

            self.assertEqual(result.errors, [])
            self.assertIn("dana", result.filed)
            self.assertGreater(result.filed["dana"], 0)


class TestStaleRowLedger(unittest.TestCase):
    """#40 Major 1: changed/dropped norms leave stale rows briefing the judge
    (content-addressed rows decay over 365 days, never removed). The persisted
    ledger lets the NEXT deploy surface rows that are gone now."""

    def _current_entries(self, out: Path) -> list[str]:
        return json.loads(
            (out / "dana" / NORMS_FILENAME).read_text(encoding="utf-8")
        )["entries"]

    def test_superseded_rows_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            current = self._current_entries(out)
            stale_text = "canal eliminado: ya no se usa"
            # Ledger: one line still present + one that no longer exists.
            previous = {
                "dana": {
                    _hash_line(current[0]): current[0],
                    _hash_line(stale_text): stale_text,
                }
            }
            runner = _FakeRunner()

            result = file_norms(
                out, phantombot_bin="phantombot", runner=runner, previous=previous
            )

            self.assertEqual(result.superseded, {"dana": [stale_text]})
            # The still-current line is re-filed (reaffirmed), not superseded.
            self.assertNotIn(current[0], result.superseded["dana"])

    def test_no_supersession_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            current = self._current_entries(out)
            previous = {"dana": {_hash_line(ln): ln for ln in current}}
            runner = _FakeRunner()

            result = file_norms(
                out, phantombot_bin="phantombot", runner=runner, previous=previous
            )

            self.assertEqual(result.superseded, {})

    def test_supersession_scoped_to_current_build(self):
        # An actor in the ledger but NOT in this build (e.g. an org skipped
        # by deploy-all) must NOT be reported as superseded — we cannot judge
        # whether its norms changed without seeing its manifest.
        with tempfile.TemporaryDirectory() as tmp:
            out = _build_au(Path(tmp), only="dana")
            previous = {"other-actor": {_hash_line("x"): "x"}}
            runner = _FakeRunner()

            result = file_norms(
                out, phantombot_bin="phantombot", runner=runner, previous=previous
            )

            self.assertEqual(result.superseded, {})

    def test_ledger_roundtrip_and_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = _build_au(tmp, only="dana")
            state_path = tmp / "personas-archive" / NORMS_STATE_FILENAME
            current = self._current_entries(out)
            runner = _FakeRunner()

            # First deploy: file everything, persist the ledger.
            result = file_norms(out, phantombot_bin="phantombot", runner=runner)
            save_norms_state(
                state_path,
                next_norms_state({}, result, {"dana"}),
            )
            prev = load_norms_state(state_path)
            self.assertIn("dana", prev)
            self.assertEqual(set(prev["dana"].values()), set(current))

            # Second deploy: the manifest drops all but one line.
            kept = current[0]
            (out / "dana" / NORMS_FILENAME).write_text(
                json.dumps(
                    {"kind": "norms", "origin": "phantomorg", "entries": [kept]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result2 = file_norms(
                out, phantombot_bin="phantombot", runner=runner, previous=prev
            )
            dropped = sorted(set(current) - {kept})
            self.assertEqual(result2.superseded, {"dana": dropped})

            # The dropped lines leave the ledger; the kept one stays.
            nxt = next_norms_state(prev, result2, {"dana"})
            self.assertEqual(set(nxt["dana"].values()), {kept})

    def test_next_state_keeps_skipped_actors(self):
        # On a clean deploy, an actor in the ledger that is NOT in the build
        # (skipped org) keeps its entry — its DB rows are unchanged.
        prev = {"dana": {_hash_line("a"): "a"}, "skipped": {_hash_line("b"): "b"}}
        from phantomorg.deploy.norms import NormFilingResult

        result = NormFilingResult(filed_lines={"dana": ["a"]})
        nxt = next_norms_state(prev, result, {"dana"})
        self.assertEqual(set(nxt), {"dana", "skipped"})
        self.assertEqual(set(nxt["skipped"].values()), {"b"})

    def test_next_state_keeps_entries_on_errors(self):
        # On a deploy WITH errors, a previously-recorded actor that did not
        # file keeps its entry (the DB is unchanged, so the ledger must still
        # match reality) — the next deploy can surface the supersession.
        from phantomorg.deploy.norms import NormFilingResult

        prev = {"dana": {_hash_line("a"): "a"}}
        result = NormFilingResult(errors=["dana: boom"], filed_lines={})
        nxt = next_norms_state(prev, result, {"dana"})
        self.assertEqual(set(nxt["dana"].values()), {"a"})


if __name__ == "__main__":
    unittest.main()
