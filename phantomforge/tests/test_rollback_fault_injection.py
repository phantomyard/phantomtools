"""Exhaustive fault-injection audit of the deploy -> archive -> rollback path.

Final acceptance test (Salvador's recommendation, 2026-08-10): crash the
rollback after EVERY filesystem operation and verify automatically that
the final state equals exactly the pre-deploy state:

    estado_final == estado_exactamente_anterior_al_deploy

covering, per the checklist:

  * byte-for-byte file content            (full recursive read_bytes snapshot)
  * relevant permissions                  (mode bits 0o777 on every file/dir)
  * personas creadas -> eliminadas
  * personas reemplazadas -> restauradas
  * personas-archive/ handling            (deleted when created by deploy,
                                           preserved with foreign content)
  * target inexistente -> vuelve a inexistente
  * manifest                              (removed, or byte-identical if it
                                           pre-existed)
  * trash                                 (no ._pf_trash_* or *.tmp residue)
  * retry after crash                 (every crash is retried via the
                                           real CLI `pf rollback --yes`)

Two canonical scenarios:

A. Fresh deploy (target and personas-archive/ did not exist before):
   `pf deploy-all` creates everything from nothing; the rollback must
   leave NOTHING behind — target, personas-archive/, manifest, trash.

B. Pre-existing target + pre-existing archive root with foreign
   content: a second deploy archives and replaces the pre-deploy
   versions and creates a new persona; the rollback must restore the
   replaced personas byte-for-byte (including permissions), remove the
   created persona, keep the foreign archive untouched and the old
   manifest byte-identical.

The crash injection counts the filesystem operations of the rollback
(mkdir / os.replace / move / rmtree / rmdir / unlink) whose path lives
under the target or the archive root, raises OSError on the Nth one, and
asserts the whole partial state is retryable to EXACT equality.

A probe test first records the real operation sequence and asserts it
against the expected one, so the enumeration below is self-documenting
and any drift in the rollback implementation fails the probe loudly
instead of silently changing what the crash points mean.
"""

import os
import shutil
import stat
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner

from phantomforge.cli import main
from phantomforge.compiler import build
from phantomforge.deploy.session import (
    RollbackError,
    execute_rollback,
    load_sessions,
    plan_rollback,
)
from phantomforge.spec.loader import load_org_yaml

ORGS_DIR = Path(__file__).parent.parent / "organizations"
AU_ORG = ORGS_DIR / "aquaponics-united/org.yaml"
UCG_ORG = ORGS_DIR / "united-capital-group/org.yaml"

TRASH_PREFIX = "._pf_trash_"
LOCK_NAME = ".phantomforge-manifest.lock"
MANIFEST_NAME = ".phantomforge-manifest.json"

AU_PERSONAS = ["alma", "elena", "paco", "pepa", "roberto"]
UCG_PERSONAS = ["anna"]


def runner_invoke_rollback(target: Path):
    """Invoke `pf rollback --target <target> --yes` via the CLI runner."""
    return CliRunner().invoke(main, ["rollback", "--target", str(target), "--yes"])


# --------------------------------------------------------------------------
# Full-state snapshots (byte-for-byte + permissions)
# --------------------------------------------------------------------------


def _snapshot_tree(
    root: Path | None, *, exclude_prefixes: tuple[str, ...] = ()
) -> dict:
    """Recursive snapshot of a subtree: relative path -> entry descriptor.

    ``exclude_prefixes`` skips entries by name (used for the lock file,
    which is deliberately never deleted while the archive root exists).
    Files are captured byte-for-byte; every file and directory captures
    its permission bits.
    """
    if root is None or not root.exists():
        return {}
    out: dict[str, dict] = {}
    for p in sorted(root.rglob("*")):
        if p.name.startswith(exclude_prefixes):
            continue
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = {"type": "link", "target": os.readlink(p)}
        elif p.is_dir():
            out[rel] = {
                "type": "dir",
                "mode": stat.S_IMODE(p.stat().st_mode),
            }
        else:
            out[rel] = {
                "type": "file",
                "mode": stat.S_IMODE(p.stat().st_mode),
                "bytes": p.read_bytes(),
            }
    return out


def _assert_same_state(
    test: unittest.TestCase,
    label: str,
    before: dict,
    after: dict,
) -> None:
    """Assert two snapshots are equal; on failure show a compact diff."""
    if before == after:
        return
    all_keys = sorted(set(before) | set(after))
    lines = []
    for key in all_keys:
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        lines.append(f"  {key}:")
        lines.append(f"    before: {_short_desc(b)}")
        lines.append(f"    after : {_short_desc(a)}")
    test.fail(f"{label}: state differs from pre-deploy:\n" + "\n".join(lines[:40]))


def _short_desc(entry: dict | None) -> str:
    if entry is None:
        return "<absent>"
    if entry["type"] == "file":
        size = len(entry["bytes"])
        head = entry["bytes"][:60].decode("utf-8", "replace").replace("\n", "\\n")
        return f"file mode={entry['mode']:o} size={size} head={head!r}"
    if entry["type"] == "dir":
        return f"dir mode={entry['mode']:o}"
    return f"link -> {entry['target']}"


def _assert_no_internal_residue(
    test: unittest.TestCase,
    archive_root: Path,
    *,
    trash: bool = True,
    tmp: bool = True,
) -> None:
    """Assert the archive root carries no trash / temp-manifest residue."""
    if not archive_root.exists():
        return
    for p in archive_root.iterdir():
        if trash and p.name.startswith(TRASH_PREFIX):
            test.fail(f"trash residue left behind: {p}")
        if tmp and p.name.endswith(".tmp"):
            test.fail(f"temp manifest residue left behind: {p}")


# --------------------------------------------------------------------------
# Crash injection
# --------------------------------------------------------------------------


class _FaultInjector:
    """Counts rollback filesystem operations and raises OSError on the
    Nth one (1-based). Only operations whose path lives under the target
    or the archive root count: lock-file creation next to the root and
    unrelated Path calls are not part of the rollback mutation sequence.
    """

    def __init__(self, crash_at: int | None):
        self.crash_at = crash_at
        self.calls: list[tuple[str, str]] = []

    def _relevant(self, *paths: object) -> bool:
        for p in paths:
            s = str(p)
            if s == self._target or s == self._root:
                return True
            if s.startswith((self._target_prefix, self._root_prefix)):
                return True
        return False

    def _hit(self, kind: str, path: str) -> None:
        self.calls.append((kind, path))
        if self.crash_at is not None and len(self.calls) == self.crash_at:
            raise OSError(
                f"simulated crash at filesystem operation "
                f"#{len(self.calls)} ({kind} {path})"
            )

    def set_roots(self, target: Path, archive_root: Path) -> None:
        self._target = str(target)
        self._root = str(archive_root)
        self._target_prefix = self._target + os.sep
        self._root_prefix = self._root + os.sep


@contextmanager
def _inject_fs_crashes(mod, injector: _FaultInjector):
    """Install counting/crashing wrappers on the rollback's filesystem
    primitives: shutil.move, shutil.rmtree, os.replace and
    Path.mkdir/rmdir/unlink. Restores everything on exit."""
    real = {
        "move": mod.shutil.move,
        "rmtree": mod.shutil.rmtree,
        "replace": mod.os.replace,
        "mkdir": mod.Path.mkdir,
        "rmdir": mod.Path.rmdir,
        "unlink": mod.Path.unlink,
    }

    def move(src, dst, **kw):
        injector._hit("move", f"{src} -> {dst}") if injector._relevant(
            src, dst
        ) else None
        return real["move"](src, dst, **kw)

    def rmtree(path, **kw):
        if injector._relevant(path):
            injector._hit("rmtree", str(path))
        return real["rmtree"](path, **kw)

    def replace(src, dst, **kw):
        if injector._relevant(src, dst):
            injector._hit("replace", f"{src} -> {dst}")
        return real["replace"](src, dst, **kw)

    def mkdir(self, *a, **kw):
        if injector._relevant(self):
            injector._hit("mkdir", str(self))
        return real["mkdir"](self, *a, **kw)

    def rmdir(self, *a, **kw):
        if injector._relevant(self):
            injector._hit("rmdir", str(self))
        return real["rmdir"](self, *a, **kw)

    def unlink(self, *a, **kw):
        if injector._relevant(self):
            injector._hit("unlink", str(self))
        return real["unlink"](self, *a, **kw)

    mod.shutil.move = move
    mod.shutil.rmtree = rmtree
    mod.os.replace = replace
    mod.Path.mkdir = mkdir
    mod.Path.rmdir = rmdir
    mod.Path.unlink = unlink
    try:
        yield
    finally:
        mod.shutil.move = real["move"]
        mod.shutil.rmtree = real["rmtree"]
        mod.os.replace = real["replace"]
        mod.Path.mkdir = real["mkdir"]
        mod.Path.rmdir = real["rmdir"]
        mod.Path.unlink = real["unlink"]


# --------------------------------------------------------------------------
# Scenario setup
# --------------------------------------------------------------------------


def _build_orgs(tmp: Path, org_names: list[str]) -> tuple[Path, Path]:
    """Copy the org yamls into tmp/orgs and compile each into
    tmp/dist/<org_id>, returning (orgs_dir, dist_base)."""
    orgs_dir = tmp / "orgs"
    dist = tmp / "dist"
    for name in org_names:
        (orgs_dir / name).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ORGS_DIR / name / "org.yaml", orgs_dir / name / "org.yaml")
        (dist / name).mkdir(parents=True, exist_ok=True)
        build(load_org_yaml(ORGS_DIR / name / "org.yaml"), dist / name)
    return orgs_dir, dist


def _deploy_all(
    tmp: Path,
    org_names: list[str],
    target: Path,
) -> None:
    """Run the real `pf deploy-all --yes` CLI for the given orgs."""
    orgs_dir, dist = _build_orgs(tmp, org_names)
    result = CliRunner().invoke(
        main,
        [
            "deploy-all",
            "--base",
            str(orgs_dir),
            "--dist-base",
            str(dist),
            "--target",
            str(target),
            "--yes",
        ],
    )
    if result.exit_code != 0:
        raise AssertionError(f"deploy-all failed:\n{result.output}")


def _pre_deploy_snapshot(target: Path, archive_root: Path) -> dict:
    """Capture the exact pre-deploy state: target subtree, archive root
    subtree (minus the lock, which is never deleted while the root
    exists) and the manifest bytes (None if absent)."""
    return {
        "target": _snapshot_tree(target),
        "root": _snapshot_tree(archive_root, exclude_prefixes=(LOCK_NAME,)),
        "manifest": (
            (archive_root / MANIFEST_NAME).read_bytes()
            if (archive_root / MANIFEST_NAME).is_file()
            else None
        ),
    }


def _setup_scenario_a(tmp: Path) -> tuple[Path, str, dict]:
    """Fresh deploy of AU+UCG: target and archive root created from
    nothing. Returns (target, session_id, pre_deploy_state)."""
    target = tmp / "personas"
    archive_root = target.parent / "personas-archive"
    # Pre-deploy: target and archive root did not exist at all.
    pre = {"target": {}, "root": {}, "manifest": None}
    _deploy_all(tmp, ["aquaponics-united", "united-capital-group"], target)
    sessions = load_sessions(archive_root)
    assert len(sessions) == 1, sessions
    assert not sessions[0].get("archived"), sessions[0]
    assert sorted(sessions[0].get("created", [])) == sorted(
        AU_PERSONAS + UCG_PERSONAS
    ), sessions[0]
    return target, str(sessions[0]["id"]), pre


def _setup_scenario_b(tmp: Path) -> tuple[Path, str, dict]:
    """Pre-existing target and archive root with foreign content, then a
    second deploy that archives/replaces AU personas and creates Anna.

    Returns (target, session_id of the SECOND deploy, pre_deploy_state
    of that second deploy)."""
    target = tmp / "personas"
    # v1: fresh AU deploy (creates the target and the archive root).
    _deploy_all(tmp, ["aquaponics-united"], target)
    archive_root = target.parent / "personas-archive"

    # Customize the pre-deploy state: distinct content + permissions.
    (target / "alma" / "SOUL.md").write_text(
        "# PRE-DEPLOY ALMA SOUL\ncustom content\n", encoding="utf-8"
    )
    secret = target / "alma" / "secret.md"
    secret.write_text("pre-deploy secret\n", encoding="utf-8")
    secret.chmod(0o600)
    (target / "pepa" / "SOUL.md").write_text(
        "# PRE-DEPLOY PEPA SOUL\nother content\n", encoding="utf-8"
    )
    (target / "pepa").chmod(0o750)
    (target / "elena" / "SOUL.md").write_text(
        "# PRE-DEPLOY ELENA SOUL\nthird\n", encoding="utf-8"
    )

    # A foreign archive (e.g. a phantombot import) that must survive.
    foreign = archive_root / "foreign-2026-01-01T00-00-00-000Z" / "persona"
    foreign.mkdir(parents=True)
    (foreign / "SOUL.md").write_text("# FOREIGN\n", encoding="utf-8")

    # Snapshot the exact pre-deploy state (before the v2 deploy).
    pre = _pre_deploy_snapshot(target, archive_root)

    # v2: AU + UCG deploy-all archives the customized AU personas and
    # creates anna.
    _deploy_all(tmp, ["aquaponics-united", "united-capital-group"], target)

    sessions = load_sessions(archive_root)
    assert len(sessions) == 2, sessions
    second = sessions[1]
    assert sorted(entry["name"] for entry in second.get("archived", [])) == sorted(
        AU_PERSONAS
    ), second
    assert sorted(second.get("created", [])) == UCG_PERSONAS, second
    return target, str(second["id"]), pre


def _session_still_recorded(archive_root: Path, session_id: str) -> bool:
    if not archive_root.exists():
        return False
    return any(s.get("id") == session_id for s in load_sessions(archive_root))


def _classify_op(kind: str, path: str, target: Path, root: Path) -> tuple[str, str]:
    """Map an observed (kind, path) to (kind, label) using the
    canonical labels of the expected sequences."""
    t, r = str(target), str(root)
    if kind == "move":
        dst = path.split(" -> ", 1)[1]
        if TRASH_PREFIX in dst:
            return (kind, "discard")
        return (kind, "restore")
    if "phantomforge-manifest.json" in path:
        return (kind, "manifest")
    if TRASH_PREFIX in path:
        return (kind, "trash")
    if path == r or path.startswith(r + os.sep):
        return (kind, "archive_root")
    if path == t or path.startswith(t + os.sep):
        return (kind, "target")
    return (kind, "other")


# --------------------------------------------------------------------------
# Expected operation sequences (probed first; crash enumeration uses them)
# --------------------------------------------------------------------------

# Scenario A (fresh, 6 created personas, nothing archived):
#   mark: manifest-lock mkdir, save mkdir, save os.replace
#   execute: target.mkdir, trash.mkdir, 6 discard moves,
#            trash sweep rmtree, target.rmdir
#   drop (inside manifest lock): lock mkdir, manifest unlink
#   remove-abandoned (outside lock): archive-root rmtree
SCENARIO_A_OPS: list[tuple[str, str]] = (
    [
        ("mkdir", "archive_root"),
        ("mkdir", "archive_root"),
        ("replace", "manifest"),
        ("mkdir", "target"),
        ("mkdir", "trash"),
    ]
    + [("move", "discard")] * len(AU_PERSONAS + UCG_PERSONAS)
    + [
        ("rmtree", "trash"),
        ("rmdir", "target"),
        ("mkdir", "archive_root"),
        ("unlink", "manifest"),
        ("rmtree", "archive_root"),
    ]
)

# Scenario B (5 archived + replaced, 1 created):
#   mark: 2 mkdir + 1 replace
#   execute: target.mkdir, trash.mkdir,
#            5x (discard move + restore move), 1 discard move (anna),
#            trash sweep rmtree
#   drop (inside manifest lock): lock mkdir + save mkdir + save replace
SCENARIO_B_OPS: list[tuple[str, str]] = (
    [
        ("mkdir", "archive_root"),
        ("mkdir", "archive_root"),
        ("replace", "manifest"),
        ("mkdir", "target"),
        ("mkdir", "trash"),
    ]
    + [("move", "discard"), ("move", "restore")] * len(AU_PERSONAS)
    + [
        ("move", "discard"),
        ("rmtree", "trash"),
        ("mkdir", "archive_root"),
        ("mkdir", "archive_root"),
        ("replace", "manifest"),
    ]
)


class TestRollbackFaultInjectionProbe(unittest.TestCase):
    """Record the real filesystem operation sequence of a rollback and
    assert it matches the expected enumeration used by the crash tests."""

    def _probe(self, setup, expected: list[tuple[str, str]]) -> None:
        import phantomforge.deploy.session as session_mod

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target, session_id, _pre = setup(tmp)
            archive_root = target.parent / "personas-archive"
            injector = _FaultInjector(crash_at=None)
            injector.set_roots(target, archive_root)
            plan = plan_rollback(archive_root, target)
            with _inject_fs_crashes(session_mod, injector):
                result = execute_rollback(plan)
            self.assertEqual(result.session_id, session_id)
            observed = [
                _classify_op(kind, path, target, archive_root)
                for kind, path in injector.calls
            ]
            self.assertEqual(
                observed,
                expected,
                f"operation sequence drifted:\n got: {observed}\nexp: {expected}",
            )

    def test_probe_scenario_a(self):
        self._probe(_setup_scenario_a, SCENARIO_A_OPS)

    def test_probe_scenario_b(self):
        self._probe(_setup_scenario_b, SCENARIO_B_OPS)


class TestRollbackExhaustiveFaultInjection(unittest.TestCase):
    """Crash after EVERY rollback filesystem operation; verify the retry
    converges to the exact pre-deploy state."""

    maxDiff = None

    def _run_crash_matrix(self, setup, expected: list[tuple[str, str]]) -> None:
        import phantomforge.deploy.session as session_mod

        for crash_at in range(1, len(expected) + 1):
            with (
                self.subTest(crash_at=crash_at, op=expected[crash_at - 1]),
                tempfile.TemporaryDirectory() as t,
            ):
                tmp = Path(t)
                target, session_id, pre = setup(tmp)
                archive_root = target.parent / "personas-archive"

                # Crash the rollback at operation N.
                injector = _FaultInjector(crash_at=crash_at)
                injector.set_roots(target, archive_root)
                plan = plan_rollback(archive_root, target)
                with (
                    _inject_fs_crashes(session_mod, injector),
                    self.assertRaises(RollbackError),
                ):
                    execute_rollback(plan)

                # Was the session dropped by the crash itself (manifest
                # unlinked before the crash)? Check BEFORE the retry —
                # the retry removes the session, so checking after
                # would misclassify every surviving-session crash.
                dropped = not _session_still_recorded(archive_root, session_id)

                # Retry via the real CLI.
                retry = runner_invoke_rollback(target)
                if dropped:
                    self.assertEqual(
                        retry.exit_code,
                        1,
                        f"expected exit 1 (nothing left to roll back), "
                        f"got {retry.exit_code}:\n{retry.output}",
                    )
                    self.assertIn("Nothing to roll back", retry.output, retry.output)
                else:
                    self.assertEqual(
                        retry.exit_code,
                        0,
                        f"retry failed after crash #{crash_at}:\n{retry.output}",
                    )

                # Exact-state check vs the PRE-deploy state.
                after_target = _snapshot_tree(target)
                after_root = _snapshot_tree(archive_root, exclude_prefixes=(LOCK_NAME,))
                after_manifest = (
                    (archive_root / MANIFEST_NAME).read_bytes()
                    if (archive_root / MANIFEST_NAME).is_file()
                    else None
                )
                _assert_same_state(self, "target", pre["target"], after_target)
                _assert_same_state(self, "archive root", pre["root"], after_root)
                self.assertEqual(pre["manifest"], after_manifest, "manifest differs")
                _assert_no_internal_residue(self, archive_root)

    def test_crash_matrix_scenario_a_fresh_deploy(self):
        self._run_crash_matrix(_setup_scenario_a, SCENARIO_A_OPS)

    def test_crash_matrix_scenario_b_replaced_and_created(self):
        self._run_crash_matrix(_setup_scenario_b, SCENARIO_B_OPS)


class TestRollbackResidueHandling(unittest.TestCase):
    """Targeted residue cases the crash matrix cannot produce directly."""

    def test_foreign_pid_tmp_removed_by_retry(self):
        """A real crash leaves a temp manifest named with the CRASHED
        process's pid. The retry (a different process) must sweep it
        while holding the manifest lock, so the final state is exact."""
        import phantomforge.deploy.session as session_mod

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target, _sid, _pre = _setup_scenario_a(tmp)
            archive_root = target.parent / "personas-archive"

            # Simulate: rollback crashed at the mark os.replace (its temp
            # is left behind with the crashed pid, session still
            # recorded), then the user retries from a different process.
            injector = _FaultInjector(crash_at=3)
            injector.set_roots(target, archive_root)
            plan = plan_rollback(archive_root, target)
            with (
                _inject_fs_crashes(session_mod, injector),
                self.assertRaises(RollbackError),
            ):
                execute_rollback(plan)

            # Plant a temp file with a foreign (crashed) pid.
            foreign_tmp = archive_root / (f"{MANIFEST_NAME}.999999.tmp")
            foreign_tmp.write_text("garbage from a crashed writer\n")

            retry = runner_invoke_rollback(target)
            self.assertEqual(retry.exit_code, 0, retry.output)
            self.assertFalse(target.exists())
            self.assertFalse(archive_root.exists())
            _assert_no_internal_residue(self, archive_root)

    def test_partial_trash_swept_by_retry(self):
        """A real crash can interrupt rmtree itself, leaving a HALF-
        deleted trash dir. The retry must sweep what remains."""
        import phantomforge.deploy.session as session_mod

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target, _sid, _pre = _setup_scenario_b(tmp)
            archive_root = target.parent / "personas-archive"

            # First rollback crashes at the trash sweep (rmtree) — the
            # whole trash dir survives.
            injector = _FaultInjector(crash_at=17)
            injector.set_roots(target, archive_root)
            plan = plan_rollback(archive_root, target)
            with (
                _inject_fs_crashes(session_mod, injector),
                self.assertRaises(RollbackError),
            ):
                execute_rollback(plan)

            trash_dirs = list(archive_root.glob(f"{TRASH_PREFIX}*"))
            self.assertEqual(len(trash_dirs), 1)
            # Simulate a partial rmtree: one discarded persona already
            # gone, another still inside.
            for entry in list(trash_dirs[0].iterdir()):
                if entry.name == "alma":
                    shutil.rmtree(entry)
            self.assertTrue(any(trash_dirs[0].iterdir()))

            retry = runner_invoke_rollback(target)
            self.assertEqual(retry.exit_code, 0, retry.output)
            _assert_no_internal_residue(self, archive_root)
            # The exact pre-deploy state is preserved.
            before = _snapshot_tree(target)
            after = _snapshot_tree(target)
            _assert_same_state(self, "target", before, after)
            self.assertTrue((target / "alma" / "secret.md").exists())

    def test_older_session_evidence_trash_preserved(self):
        """The sweep must NEVER delete a trash dir that is the only
        recovery evidence of an OLDER session's interrupted rollback
        (audit v0.5.8 #1) — rolling back a NEWER session must leave it
        untouched; only that older session's own (final) rollback may
        consume it."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target, _sid, _pre = _setup_scenario_b(tmp)
            archive_root = target.parent / "personas-archive"
            sessions = load_sessions(archive_root)
            s1_id, s2_id = sessions[0]["id"], sessions[1]["id"]
            self.assertLess(s1_id, s2_id)

            # Simulate S1's interrupted rollback (pre-v0.5.8 style: entry
            # still committed, archives consumed, trash is the only
            # evidence). Leave an evidence trash dir stamped between S1
            # and S2 (S1's own session stamp works: it sorts before S2's).
            evidence = archive_root / f"{TRASH_PREFIX}{s1_id}"
            evidence.mkdir()
            (evidence / "alma").mkdir()
            (evidence / "alma" / "SOUL.md").write_text(
                "v1 discarded\n", encoding="utf-8"
            )

            # Rolling back S2 must keep the older evidence trash.
            retry = runner_invoke_rollback(target)
            self.assertEqual(retry.exit_code, 0, retry.output)
            self.assertTrue(
                evidence.exists(),
                "rolling back S2 must not sweep S1's evidence trash",
            )
            self.assertEqual(len(load_sessions(archive_root)), 1)

            # Rolling back S1 (cleanup-only: its archives were consumed)
            # finishes the job and consumes the evidence.
            retry2 = runner_invoke_rollback(target)
            self.assertEqual(retry2.exit_code, 0, retry2.output)
            self.assertFalse(evidence.exists())
            self.assertEqual(len(load_sessions(archive_root)), 0)


if __name__ == "__main__":
    unittest.main()
