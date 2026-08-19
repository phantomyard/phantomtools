"""Transactional rollback tests: `po rollback` restores the system to
exactly the state it was in before the deploy.

The contract (user requirement):
- archived personas are restored (backup consumed, not left behind);
- personas the deploy created are removed;
- if personas-archive/ did not exist before the deploy, it is deleted;
- if the target itself did not exist before and is now empty, it is
  deleted too — the system ends up exactly as it was before.
"""

import contextlib
import os
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from click.testing import CliRunner

from phantomorg.cli import main
from phantomorg.compiler import build
from phantomorg.deploy.session import (
    TRASH_PREFIX,
    RollbackError,
    _archive_stamp,
    _cleanup_stale_internals,
    _mark_session_state,
    begin_session,
    execute_rollback,
    load_sessions,
    plan_rollback,
    record_session,
)
from phantomorg.deploy.target import DeployResult, archives_dir, deploy
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


def _build_au(tmp: Path) -> Path:
    """Build the AU org into tmp/dist and return the compiled dir."""
    au_spec = load_org_yaml(AU_ORG)
    out = tmp / "dist"
    build(au_spec, out)
    return out


def _change_soul(out: Path, actor: str | None = None) -> None:
    """Modify an owned file INSIDE an ORG block so a redeploy actually
    overwrites it (additive deploy only writes files that changed).

    By default changes EVERY actor's SOUL.md (the org-level principle
    string is identical across actors), so a redeploy archives a
    per-file backup for each persona — what the rollback tests need to
    exercise multi-archive scenarios. Pass ``actor`` to change just one.
    """
    actors = [actor] if actor else [d.name for d in sorted(out.iterdir()) if d.is_dir()]
    for a in actors:
        soul = out / a / "SOUL.md"
        if not soul.exists():
            continue
        soul.write_text(
            soul.read_text(encoding="utf-8").replace(
                "Seguridad de la información antes que velocidad",
                "V2: Seguridad de la información antes que velocidad",
            ),
            encoding="utf-8",
        )


class TestSessionManifest(unittest.TestCase):
    def test_record_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            result = DeployResult(
                target=target,
                deployed=["dana", "maria"],
                created=["maria"],
                pruned=[],
                archived=[
                    (
                        "dana",
                        str(tmp / "personas-archive/dana-2026-08-09T00-00-00-000Z"),
                    )
                ],
                scopes_written=True,
                scopes_backup=str(tmp / "personas-archive/._pf_data_scopes.json-x"),
                scopes_created=False,
                humans_written=True,
                humans_backup=None,
                humans_created=True,
            )
            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            archive_root = archives_dir(target)
            sessions = load_sessions(archive_root)
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s["command"], "deploy")
            self.assertEqual(s["orgs"], ["verdant-aquaponics"])
            self.assertEqual(s["created"], ["maria"])
            self.assertEqual(s["archive_root_pre_existed"], False)
            self.assertEqual(s["target_pre_existed"], False)
            self.assertEqual(s["archived"][0]["name"], "dana")
            # Data-file backup info round-trips into the manifest.
            self.assertEqual(s["scopes_written"], True)
            self.assertEqual(
                s["scopes_backup"],
                str(tmp / "personas-archive/._pf_data_scopes.json-x"),
            )
            self.assertEqual(s["scopes_created"], False)
            self.assertEqual(s["humans_written"], True)
            self.assertEqual(s["humans_created"], True)

    def test_multiple_sessions_stack(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            for i in range(2):
                result = DeployResult(
                    target=target,
                    deployed=[f"p{i}"],
                    created=[f"p{i}"],
                    archived=[],
                )
                record_session(
                    target,
                    result,
                    command="deploy",
                    orgs=[],
                    archive_root_pre_existed=True,
                    target_pre_existed=True,
                )
            archive_root = archives_dir(target)
            sessions = load_sessions(archive_root)
            self.assertEqual(len(sessions), 2)
            latest = sessions[-1]
            plan = plan_rollback(archive_root, target)
            self.assertEqual(plan.session_id, latest["id"])


class TestRollbackExecute(unittest.TestCase):
    def test_rollback_restores_archived_and_deletes_archive_dir(self):
        """Deploy v1 -> deploy v2 (archives v1) -> rollback must give back
        v1 exactly, consume the backups and delete personas-archive/."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1: fresh creates
            self.assertTrue((target / "dana" / "SOUL.md").exists())

            # v2: overwrite — archives v1 (only the changed owned file)
            _change_soul(out)
            result = deploy(out, target)
            self.assertIn("dana", result.archived[0][0] if result.archived else "")
            archive_root = archives_dir(target)
            self.assertTrue(archive_root.is_dir())

            # record the v2 session (as cli.py would)
            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,  # created by v2
                target_pre_existed=True,
            )

            # mark the current SOUL so we can prove the restore
            (target / "dana" / "SOUL.md").write_text("# v2 marker\n", encoding="utf-8")
            v1_archive = next(archive_root.glob("dana-*"))

            plan = plan_rollback(archive_root, target)
            rb = execute_rollback(plan)

            self.assertIn("dana", rb.restored)
            # backup consumed: the archive dir is gone
            self.assertFalse(v1_archive.exists())
            # archive root deleted (did not pre-exist)
            self.assertTrue(rb.archive_root_deleted)
            self.assertFalse(archive_root.exists())
            # manifest gone with it
            self.assertEqual(load_sessions(archive_root), [])
            # target still exists (pre-existed), with the restored persona
            self.assertTrue((target / "dana" / "SOUL.md").exists())

    def test_rollback_removes_created_personas(self):
        """Fresh deploy (all personas created) -> rollback removes them."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            result = deploy(out, target)
            self.assertEqual(len(result.created), len(result.deployed))

            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            archive_root = archives_dir(target)
            plan = plan_rollback(archive_root, target)
            rb = execute_rollback(plan)

            self.assertEqual(sorted(rb.removed_created), sorted(result.created))
            self.assertFalse(target.exists())  # target did not pre-exist -> deleted

    def test_rollback_restores_pre_existing_data_files(self):
        """Data-dir derived files (scopes.json / HUMANS.md) that existed
        before a deploy are snapshotted into personas-archive/ and restored
        byte-for-byte on rollback (option (a): rollback IS authoritative
        over the derived data dir)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            scopes = target.parent / "scopes.json"
            humans = target.parent / "HUMANS.md"

            # v1: fresh deploy -- creates scopes.json + HUMANS.md in the
            # data dir. They did not pre-exist.
            r1 = deploy(out, target)
            self.assertTrue(scopes.exists())
            self.assertTrue(humans.exists())
            self.assertTrue(r1.scopes_created)
            self.assertTrue(r1.humans_created)
            self.assertIsNone(r1.scopes_backup)
            self.assertIsNone(r1.humans_backup)

            # Customize the data-dir files so a later restore is meaningful:
            # the pre-deploy state is distinct from anything the build
            # regenerates.
            scopes.write_text('{"custom": "pre-deploy scopes"}', encoding="utf-8")
            humans.write_text("# Pre-deploy HUMANS\n", encoding="utf-8")

            # v2: overwrite -- archives the custom pre-existing files.
            r2 = deploy(out, target)
            record_session(
                target,
                r2,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            self.assertIsNotNone(r2.scopes_backup)
            self.assertIsNotNone(r2.humans_backup)
            self.assertFalse(r2.scopes_created)
            self.assertFalse(r2.humans_created)

            archive_root = archives_dir(target)
            plan = plan_rollback(archive_root, target)
            self.assertTrue(plan.restore_data)
            rb = execute_rollback(plan)

            # The exact pre-deploy content is back.
            self.assertEqual(
                scopes.read_text(encoding="utf-8"), '{"custom": "pre-deploy scopes"}'
            )
            self.assertEqual(
                humans.read_text(encoding="utf-8"), "# Pre-deploy HUMANS\n"
            )
            self.assertEqual(
                rb.restored_data, [str(scopes.resolve()), str(humans.resolve())]
            )
            # The consumed backups are gone.
            self.assertEqual(list(archive_root.glob("._pf_data_*")), [])

    def test_rollback_removes_data_files_created_by_fresh_deploy(self):
        """A deploy that CREATED scopes.json / HUMANS.md (they did not
        pre-exist) has its data dir returned to the exact pre-deploy state
        on rollback: the files are removed (option (a), remove-if-not-
        pre-existing)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            scopes = target.parent / "scopes.json"
            humans = target.parent / "HUMANS.md"

            r1 = deploy(out, target)  # fresh: creates the data files
            self.assertTrue(r1.scopes_created)
            self.assertTrue(r1.humans_created)
            record_session(
                target,
                r1,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )

            archive_root = archives_dir(target)
            plan = plan_rollback(archive_root, target)
            self.assertTrue(plan.remove_data)
            rb = execute_rollback(plan)

            # The files did not pre-exist: the exact pre-deploy state is
            # absent, so rollback removes them.
            self.assertFalse(scopes.exists())
            self.assertFalse(humans.exists())
            self.assertEqual(
                sorted(rb.removed_data),
                sorted([str(scopes.resolve()), str(humans.resolve())]),
            )

    def test_rollback_data_files_backcompat_old_session(self):
        """A session recorded BEFORE data-file backup support (no
        scopes_backup / scopes_created / humans_* keys) must still roll
        back cleanly: the data files are left untouched and the user is
        warned via data_skipped (their pre-deploy state is unknowable)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            scopes = target.parent / "scopes.json"

            deploy(out, target)  # v1: creates the data files
            _change_soul(out)
            result = deploy(out, target)  # v2: archives v1 personas
            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)

            # Simulate an old-format session: strip ALL data-file keys the
            # manifest would have gained with backup support.
            manifest = archive_root / ".phantomorg-manifest.json"
            import json

            data = json.loads(manifest.read_text(encoding="utf-8"))
            for s in data["sessions"]:
                for k in (
                    "scopes_written",
                    "scopes_backup",
                    "scopes_created",
                    "humans_written",
                    "humans_backup",
                    "humans_created",
                ):
                    s.pop(k, None)
            manifest.write_text(json.dumps(data), encoding="utf-8")

            plan = plan_rollback(archive_root, target)
            # No restore/remove, and the pre-deploy state is unknowable.
            self.assertEqual(plan.restore_data, [])
            self.assertEqual(plan.remove_data, [])
            self.assertTrue(plan.data_skipped)
            rb = execute_rollback(plan)
            self.assertTrue(rb.data_skipped)
            # The data file is left UNTOUCHED (v1-generated bytes remain).
            self.assertTrue(scopes.exists())
            # The persona rollback itself still completes.
            self.assertTrue(rb.restored or rb.removed_created)

    def test_rollback_restores_pruned_personas(self):
        """Deploy with an actor, then deploy --prune after removing it from
        the build -> rollback brings the pruned actor back."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)
            self.assertTrue((target / "elias").exists())

            # simulate Elias removed from spec: drop from compiled output
            shutil.rmtree(out / "elias")
            result = deploy(out, target, prune=True)
            self.assertIn("elias", result.pruned)
            self.assertFalse((target / "elias").exists())

            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            plan = plan_rollback(archive_root, target)
            rb = execute_rollback(plan)

            self.assertIn("elias", rb.restored)
            self.assertTrue((target / "elias" / "SOUL.md").exists())

    def test_rollback_keeps_preexisting_archive_dir(self):
        """If personas-archive/ existed before (e.g. phantombot archives),
        rollback must NOT delete it — only consume our own backups."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            archive_root = archives_dir(target)
            foreign = archive_root / "phantom-2026-01-01T00-00-00-000Z"
            foreign.mkdir(parents=True)
            (foreign / "SOUL.md").write_text("# foreign\n", encoding="utf-8")

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=["verdant-aquaponics"],
                archive_root_pre_existed=True,  # phantombot was here first
                target_pre_existed=True,
            )

            plan = plan_rollback(archive_root, target)
            rb = execute_rollback(plan)

            self.assertFalse(rb.archive_root_deleted)
            self.assertTrue(archive_root.is_dir())
            # foreign archive untouched
            self.assertTrue(
                (archive_root / "phantom-2026-01-01T00-00-00-000Z").is_dir()
            )

    def test_plan_rollback_raises_when_archive_missing(self):
        """If an archived persona dir is missing, refuse (incomplete rollback)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            # user manually deleted one backup
            archive_root = archives_dir(target)
            first_archive = next(archive_root.glob("dana-*"))
            shutil.rmtree(first_archive)

            from phantomorg.deploy.session import RollbackError

            with self.assertRaises(RollbackError):
                plan_rollback(archive_root, target)

    def test_rollback_discards_post_deploy_edits(self):
        """The post-deploy version is discarded; the pre-deploy one wins."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            v1_soul = (target / "dana" / "SOUL.md").read_text(encoding="utf-8")
            _change_soul(out)
            result = deploy(out, target)  # v2
            # user edits the deployed persona after the v2 deploy
            (target / "dana" / "SOUL.md").write_text(
                "# edited after deploy\n", encoding="utf-8"
            )

            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            rb = execute_rollback(plan_rollback(archive_root, target))

            self.assertIn("dana", rb.discarded)
            self.assertEqual(
                (target / "dana" / "SOUL.md").read_text(encoding="utf-8"), v1_soul
            )
            # the discarded post-deploy edit was NOT deleted outright: it went
            # to the trash dir, which is removed only after full success.
            self.assertFalse(list(archive_root.glob("._pf_trash_*")))

    def test_rollback_failure_keeps_discarded_data_in_trash(self):
        """If the rollback fails mid-way, discarded content survives in the
        trash dir and the manifest entry is kept (retry / manual recovery
        stay possible)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            # post-deploy edit that must survive a failed rollback
            edited = target / "dana" / "SOUL.md"
            edited.write_text("# precious post-deploy edit\n", encoding="utf-8")

            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            plan = plan_rollback(archive_root, target)

            # Simulate a mid-rollback filesystem failure: the FIRST restore
            # copy fails (after the post-deploy version was already moved to
            # the trash). The discarded edit must survive in the trash and
            # the manifest entry must be kept.
            import phantomorg.deploy.session as session_mod

            original_copy2 = shutil.copy2

            def flaky_copy2(src, dst, **kw):
                if "personas-archive" in str(src) and not getattr(
                    flaky_copy2, "failed", False
                ):
                    flaky_copy2.failed = True
                    raise OSError("simulated IO error")
                return original_copy2(src, dst, **kw)

            with (
                unittest.mock.patch.object(
                    session_mod.shutil, "copy2", side_effect=flaky_copy2
                ),
                self.assertRaises(RollbackError),
            ):
                execute_rollback(plan)

            # Nothing was lost: the discarded post-deploy edit is preserved
            # in the trash dir, the manifest still lists the session (retry
            # possible), and the archived dana is still in the archive root.
            trash_dirs = list(archive_root.glob("._pf_trash_*"))
            self.assertEqual(len(trash_dirs), 1)
            trash_soul = trash_dirs[0] / "dana" / "SOUL.md"
            self.assertEqual(
                trash_soul.read_text(encoding="utf-8"),
                "# precious post-deploy edit\n",
            )
            self.assertEqual(len(load_sessions(archive_root)), 1)
            self.assertTrue(
                any(d.name.startswith("dana-") for d in archive_root.iterdir())
            )

    def test_rollback_twice_undoes_two_sessions(self):
        """Stack semantics: one rollback per session, in order."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            archive_root = archives_dir(target)

            # session 1: fresh deploy
            r1 = deploy(out, target)
            record_session(
                target,
                r1,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            # session 2: overwrite
            r2 = deploy(out, target)
            record_session(
                target,
                r2,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            self.assertEqual(len(load_sessions(archive_root)), 2)

            # first rollback undoes session 2. The archive root must SURVIVE
            # because session 1's manifest entry still lives there (it must
            # remain rollback-able).
            rb1 = execute_rollback(plan_rollback(archive_root, target))
            self.assertFalse(rb1.archive_root_deleted)
            self.assertEqual(len(load_sessions(archive_root)), 1)
            # but the personas from session 1 (fresh creates) are still there
            self.assertTrue((target / "dana").exists())

            # second rollback undoes session 1: created personas removed,
            # manifest empty -> archive root deleted, target deleted
            rb2 = execute_rollback(plan_rollback(archive_root, target))
            self.assertIn("dana", rb2.removed_created)
            self.assertTrue(rb2.archive_root_deleted)
            self.assertFalse(target.exists())
            self.assertEqual(load_sessions(archive_root), [])

    def test_rollback_cleanup_only_retry_after_trash_failure(self):
        """R1: if the trash removal fails after the restore succeeded, the
        session stays recorded and a retry becomes a cleanup-only plan that
        finishes the job (nothing is lost, nothing is stuck)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)

            # First rollback: simulate the trash removal failing. The
            # restore and discard succeed (trash exists), then rmtree
            # fails on the trash dir.
            import phantomorg.deploy.session as session_mod

            original_rmtree = shutil.rmtree

            def failing_rmtree(path, *a, **kw):
                if str(path).endswith("._pf_trash_"):
                    pass
                if "_pf_trash_" in str(path):
                    raise OSError("simulated trash removal failure")
                return original_rmtree(path, *a, **kw)

            with (
                unittest.mock.patch.object(
                    session_mod.shutil, "rmtree", side_effect=failing_rmtree
                ),
                self.assertRaises(RollbackError),
            ):
                execute_rollback(plan_rollback(archive_root, target))

            # The session is STILL recorded (retry possible) and the
            # restored personas are in place.
            self.assertEqual(len(load_sessions(archive_root)), 1)
            self.assertTrue((target / "dana" / "SOUL.md").exists())

            # Retry: archives were all consumed -> cleanup-only plan.
            plan2 = plan_rollback(archive_root, target)
            self.assertTrue(plan2.cleanup_only)
            self.assertEqual(plan2.restore, [])
            execute_rollback(plan2)
            self.assertFalse(archive_root.exists())

    def test_record_session_lock_serializes_concurrent_writes(self):
        """R2: concurrent deploys must not lose each other's session record.
        Without the lock, racing load-modify-save cycles overwrite one
        another; with it, every session survives. The race is exercised on
        record_session itself (the manifest load-modify-save cycle), not
        on deploy() — concurrent deploys to the same target race on the
        filesystem independently of the manifest."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            archive_root = archives_dir(target)
            target.mkdir(parents=True, exist_ok=True)

            import threading

            results: list[dict] = []
            errors: list[Exception] = []

            def worker(i: int):
                try:
                    result = DeployResult(target=target, deployed=[f"actor-{i}"])
                    results.append(
                        record_session(
                            target,
                            result,
                            command="deploy",
                            orgs=[f"org-{i}"],
                            archive_root_pre_existed=True,
                            target_pre_existed=True,
                        )
                    )
                except Exception as e:  # noqa: BLE001 - worker boundary
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            self.assertEqual(errors, [])
            sessions = load_sessions(archive_root)
            # every thread's session must be recorded (no lost updates)
            self.assertEqual(len(sessions), 8)

    def test_plan_rollback_refuses_symlink_archive(self):
        """R5: an archived persona that is a symlink is refused, not
        restored through the link."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)
            _change_soul(out)
            result = deploy(out, target)
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)

            # replace the archived dana with a symlink
            archived = next(archive_root.glob("dana-*"))
            shutil.rmtree(archived)
            archived.symlink_to(tmp / "outside")
            (tmp / "outside").mkdir(exist_ok=True)

            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("symlink", str(ctx.exception))

    def test_plan_rollback_cleanup_only_when_all_archives_gone(self):
        """A session whose archives were all consumed (previous partial
        rollback) plans a cleanup-only rollback instead of failing."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)
            _change_soul(out)
            result = deploy(out, target)
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            for d in list(archive_root.glob("*-*")):
                if d.name.startswith("."):
                    continue
                shutil.rmtree(d)

            plan = plan_rollback(archive_root, target)
            self.assertTrue(plan.cleanup_only)
            self.assertEqual(plan.restore, [])

    def test_plan_rollback_refuses_mixed_missing_archives(self):
        """Some archives present + some missing = interrupted restore:
        refuse rather than mix states."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)
            _change_soul(out)
            result = deploy(out, target)
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # remove ONE of the archived personas
            first = next(d for d in archive_root.glob("dana-*"))
            shutil.rmtree(first)

            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("missing", str(ctx.exception))

    def test_rollback_keeps_other_sessions_when_removing_root(self):
        """Regression: rolling back the LAST session deletes the archive
        root, but only when no OTHER session entries survive. A manifest
        with remaining sessions must keep the root alive."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"
            archive_root = archives_dir(target)

            r1 = deploy(out, target)
            record_session(
                target,
                r1,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            r2 = deploy(out, target)
            record_session(
                target,
                r2,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )

            rb1 = execute_rollback(plan_rollback(archive_root, target))
            self.assertFalse(rb1.archive_root_deleted)
            self.assertEqual(len(load_sessions(archive_root)), 1)

            rb2 = execute_rollback(plan_rollback(archive_root, target))
            self.assertTrue(rb2.archive_root_deleted)
            self.assertFalse(archive_root.exists())
            self.assertFalse(target.exists())

    def test_plan_rollback_continues_after_interrupted_restore(self):
        """F2: an interrupted rollback that already consumed some
        archives (trash dir left behind, persona back in the target) is
        retryable — the next plan finishes the job instead of refusing.

        Before v0.4.11 this raised RollbackError: the missing archives
        were indistinguishable from manual deletion. The trash dir is
        the evidence that a rollback ran: execute_rollback ALWAYS
        discards the replaced version to a ._pf_trash_* dir before every
        restore of an existing persona.
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # Simulate a rollback that restored 'dana' (archive consumed,
            # pre-deploy version back in the target) and then died before
            # cleanup: a trash dir was left behind by the discard that
            # precedes every restore of an existing persona.
            alma_archive = next(d for d in archive_root.glob("dana-*"))
            shutil.rmtree(target / "dana")
            shutil.move(str(alma_archive), str(target / "dana"))
            trash = archive_root / "._pf_trash_leftover"
            trash.mkdir()

            plan = plan_rollback(archive_root, target)
            # The remaining archives are restored; dana is already back.
            self.assertNotIn("dana", [n for n, _ in plan.restore])
            self.assertTrue(plan.restore)

    def test_plan_rollback_refuses_missing_persona_not_in_target(self):
        """F2: even WITH trash evidence, a missing archive whose persona
        is NOT in the target means the pre-deploy version is genuinely
        lost — refuse with a manual-recovery message."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # Trash evidence exists, the archive is gone AND the persona
            # is not in the target: the pre-deploy version is lost.
            trash = archive_root / "._pf_trash_leftover"
            trash.mkdir()
            shutil.rmtree(target / "dana")
            shutil.rmtree(next(d for d in archive_root.glob("dana-*")))

            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("manual recovery", str(ctx.exception))

    def test_plan_rollback_refuses_without_trash_evidence(self):
        """F2: mixed present/missing archives WITHOUT trash evidence are
        external deletions — refuse (this is the historical behavior,
        kept intact by the trash-evidence gate)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # Remove one archive, leave the rest, NO trash dir.
            shutil.rmtree(next(d for d in archive_root.glob("dana-*")))

            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("removed outside PhantomOrg", str(ctx.exception))

    def test_plan_rollback_preserves_trash_evidence(self):
        """Audit v0.5.8 #1 (HIGH): planning must never destroy the trash
        evidence of a previous interrupted rollback.

        Scenario (from the ChatGPT re-verification): a rollback that
        crashed before the rollback_in_progress transition existed
        (pre-v0.5.8) left its journal entry ``committed``, consumed the
        archives (they are missing) and left a trash dir as the only
        proof. After >24h, running ``po rollback`` must still produce the
        "finish the cleanup" plan — NOT a permanent refusal — and the
        trash must survive planning (the CLI plans twice).
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # An interrupted rollback consumed EVERY archive (moved each
            # persona back into the target) and left its trash behind.
            # Only archive DIRECTORIES are removed here: the deploy-time
            # data-file backups (._pf_data_* plain files, added with
            # data-file rollback support) are not personas and are left
            # in place.
            for d in list(archive_root.glob("*-*-*-*-*-*")):
                if d.name.startswith(TRASH_PREFIX) or not d.is_dir():
                    continue
                shutil.rmtree(d)
            trash = archive_root / f"{TRASH_PREFIX}{_archive_stamp()}"
            trash.mkdir(parents=True)
            (trash / "dana").mkdir()
            (trash / "dana" / "SOUL.md").write_text("v2", encoding="utf-8")
            # Age it past the GC cutoff so a stale-cleanup WOULD remove it.
            old = datetime.now(timezone.utc) - timedelta(days=2)
            os.utime(trash, (old.timestamp(), old.timestamp()))
            # The restored persona is back in the target (restore moved it).
            self.assertTrue((target / "dana").is_dir())

            plan = plan_rollback(archive_root, target)
            # The interrupted rollback already restored everything: the
            # plan only finishes the cleanup (never a refusal).
            self.assertTrue(plan.cleanup_only)
            self.assertEqual(plan.restore, [])
            self.assertTrue(
                trash.exists(),
                "planning must not destroy the trash evidence",
            )

    def test_execute_rollback_transitions_before_cleanup(self):
        """Audit v0.5.8 #1 (HIGH): the journal transition to
        rollback_in_progress happens BEFORE any cleanup/mutation, so a
        crash mid-rollback leaves a protected session — never a
        committed one whose trash the GC could later collect."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            out = _build_au(tmp)
            target = tmp / "personas"

            deploy(out, target)  # v1
            _change_soul(out)
            result = deploy(out, target)  # v2 archives v1
            record_session(
                target,
                result,
                command="deploy",
                orgs=[],
                archive_root_pre_existed=False,
                target_pre_existed=True,
            )
            archive_root = archives_dir(target)
            # Stale trash from a previous interrupted rollback (the
            # evidence the old order would have deleted before marking).
            trash = archive_root / f"{TRASH_PREFIX}{_archive_stamp()}"
            trash.mkdir(parents=True)
            old = datetime.now(timezone.utc) - timedelta(days=2)
            os.utime(trash, (old.timestamp(), old.timestamp()))
            plan = plan_rollback(archive_root, target)

            # Fail on the very first filesystem mutation: the state must
            # already be rollback_in_progress at that moment.
            import phantomorg.deploy.session as session_mod

            def fail_all_moves(src, dst):
                raise OSError("simulated IO error")

            with (
                unittest.mock.patch.object(
                    session_mod.shutil, "move", side_effect=fail_all_moves
                ),
                self.assertRaises(RollbackError),
            ):
                execute_rollback(plan)

            # The session was transitioned BEFORE the failed mutation:
            # the retry sees rollback_in_progress (protected from GC),
            # never a plain committed entry.
            sessions = load_sessions(archive_root)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(str(sessions[0].get("state")), "rollback_in_progress")
            self.assertTrue(
                trash.exists(),
                "trash must survive the failed rollback (state written first)",
            )

    def test_plan_rollback_suffixed_session_id_matches_same_stamp_archive(self):
        """F6: a session id with a numeric suffix (-N, from a
        same-millisecond collision) must still match an archive stamped at
        the session's own base millisecond. Previously the suffix sorted
        AFTER the base stamp lexicographically, so the archive was
        misclassified as "archived before this session started" and
        skipped."""
        from phantomorg.deploy.session import begin_session

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            target.mkdir()
            session = begin_session(
                target,
                command="deploy",
                orgs=["org1"],
                planned_archived=["dana"],
                planned_created=[],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            # Give the session id the collision suffix (-1): the archive
            # below is stamped at exactly the session's base stamp.
            base_id = session["id"]
            session["id"] = f"{base_id}-1"
            from phantomorg.deploy.session import _save_sessions

            archive_root = archives_dir(target)
            _save_sessions(archive_root, [session])
            # Archive stamped at the session's base millisecond (no suffix).
            archive_dir = archive_root / f"dana-{base_id}"
            archive_dir.mkdir(parents=True)
            (archive_dir / "SOUL.md").write_text("OLD", encoding="utf-8")
            (target / "dana").mkdir()
            (target / "dana" / "SOUL.md").write_text("NEW", encoding="utf-8")

            plan = plan_rollback(archive_root, target)
            restored_names = [n for n, _ in plan.restore]
            # Without the F6 fix this archive was skipped (stamp <
            # "...-1") and the plan had nothing to restore.
            self.assertEqual(restored_names, ["dana"])

            result = execute_rollback(plan)
            self.assertEqual(result.restored, ["dana"])
            self.assertEqual((target / "dana" / "SOUL.md").read_text(), "OLD")

    def test_discard_session_removes_manifest_under_lock(self):
        """F10: the manifest unlink in discard_session happens while the
        manifest lock is held (a concurrent record_session between save
        and unlink must not be destroyed)."""
        from unittest import mock

        from phantomorg.deploy import session as session_mod
        from phantomorg.deploy.session import (
            MANIFEST_NAME,
            _acquire_lock_file,
            _release_lock_file,
            discard_session,
        )

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            target.mkdir()
            session = {
                "id": "s1",
                "state": "committed",
                "command": "deploy",
                "target": str(target.resolve()),
                "archive_root_pre_existed": False,
                "target_pre_existed": False,
                "archived": [],
                "created": [],
            }
            from phantomorg.deploy.session import _save_sessions

            archive_root = archives_dir(target)
            _save_sessions(archive_root, [session])

            events: list[str] = []

            def spy_acquire(lock_path):
                events.append("lock-acquire")
                return real_acquire(lock_path)

            def spy_release(f):
                events.append("lock-release")
                return real_release(f)

            real_acquire = _acquire_lock_file
            real_release = _release_lock_file
            manifest_path = archive_root / MANIFEST_NAME
            real_unlink = manifest_path.unlink

            def spy_unlink(self, *a, **kw):
                # Only record the manifest unlink, not lockfile/other
                # cleanups.
                if self == manifest_path:
                    events.append("manifest-unlink")
                return real_unlink(*a, **kw)

            with (
                mock.patch.object(
                    session_mod, "_acquire_lock_file", side_effect=spy_acquire
                ),
                mock.patch.object(
                    session_mod, "_release_lock_file", side_effect=spy_release
                ),
                mock.patch("pathlib.Path.unlink", new=spy_unlink),
            ):
                discard_session(target, "s1", archive_root_pre_existed=False)

            self.assertIn("manifest-unlink", events)
            # The unlink must happen while the lock is held (between
            # acquire and release) — not after release.
            self.assertLess(
                events.index("lock-acquire"), events.index("manifest-unlink")
            )
            self.assertLess(
                events.index("manifest-unlink"), events.index("lock-release")
            )

    def test_execute_rollback_acquires_transaction_lock(self):
        """F12: execute_rollback acquires the transaction lock itself, so
        a direct library caller is serialized against concurrent
        deploys/rollbacks (the CLI also holds it — reentrant)."""
        from unittest import mock

        from phantomorg.deploy import session as session_mod

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            target.mkdir()
            session = {
                "id": "s1",
                "state": "committed",
                "command": "deploy",
                "target": str(target.resolve()),
                "archive_root_pre_existed": False,
                "target_pre_existed": False,
                "archived": [],
                "created": [],
            }
            archive_root = archives_dir(target)
            session_mod._save_sessions(archive_root, [session])
            archive_dir = archive_root / "dana-2026-08-09T00-00-00-000Z"
            archive_dir.mkdir(parents=True)
            (archive_dir / "SOUL.md").write_text("OLD", encoding="utf-8")
            (target / "dana").mkdir()
            (target / "dana" / "SOUL.md").write_text("NEW", encoding="utf-8")
            session["archived"] = [{"name": "dana", "dir": str(archive_dir)}]
            session_mod._save_sessions(archive_root, [session])

            plan = session_mod.plan_rollback(archive_root, target)
            real_lock = session_mod._transaction_lock
            calls: list[str] = []

            @contextlib.contextmanager
            def spy_lock(tgt):
                calls.append("lock-enter")
                with real_lock(tgt):
                    yield
                calls.append("lock-exit")

            with mock.patch.object(
                session_mod, "_transaction_lock", side_effect=spy_lock
            ):
                result = execute_rollback(plan)
            self.assertEqual(result.restored, ["dana"])
            self.assertEqual(calls, ["lock-enter", "lock-exit"])


class TestManifestCorruption(unittest.TestCase):
    """F3: a corrupt session manifest must never be silently overwritten
    (that destroys the rollback history). It is quarantined and every
    operation that would touch it is refused."""

    def _deploy_and_record(self, tmp: Path) -> tuple[Path, Path]:
        """Deploy twice and record a committed session; return
        (archive_root, target)."""
        out = _build_au(tmp)
        target = tmp / "personas"
        deploy(out, target)  # v1
        _change_soul(out)
        result = deploy(out, target)  # v2 archives v1
        record_session(
            target,
            result,
            command="deploy",
            orgs=[],
            archive_root_pre_existed=False,
            target_pre_existed=True,
        )
        return archives_dir(target), target

    def _corrupt_manifest(self, archive_root: Path) -> Path:
        mp = archive_root / ".phantomorg-manifest.json"
        mp.write_text("{ not valid json !!", encoding="utf-8")
        return mp

    def test_load_sessions_raises_on_corrupt(self):
        from phantomorg.deploy.session import ManifestError

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root, _ = self._deploy_and_record(tmp)
            self._corrupt_manifest(archive_root)

            with self.assertRaises(ManifestError):
                load_sessions(archive_root)

    def test_begin_session_quarantines_and_refuses(self):
        from phantomorg.deploy.session import begin_session
        from phantomorg.deploy.target import DeployError

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root, target = self._deploy_and_record(tmp)
            self._corrupt_manifest(archive_root)

            with self.assertRaises(DeployError) as ctx:
                begin_session(
                    target,
                    command="deploy",
                    orgs=[],
                    planned_archived=[],
                    planned_created=[],
                    planned_pruned=[],
                    archive_root_pre_existed=False,
                    target_pre_existed=True,
                )
            self.assertIn("corrupt", str(ctx.exception))
            # The corrupt file was preserved, not overwritten.
            quarantined = list(archive_root.glob(".phantomorg-manifest.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1, str(ctx.exception))
            self.assertFalse((archive_root / ".phantomorg-manifest.json").exists())

    def test_plan_rollback_refuses_on_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root, target = self._deploy_and_record(tmp)
            self._corrupt_manifest(archive_root)

            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("unreadable or corrupt", str(ctx.exception))

    def test_empty_after_internals_keeps_root_on_corrupt(self):
        from phantomorg.deploy.session import _empty_after_internals

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root, _ = self._deploy_and_record(tmp)
            self._corrupt_manifest(archive_root)

            # Cannot know whether the corrupt manifest records sessions:
            # the root must survive.
            self.assertFalse(_empty_after_internals(archive_root))


class _TmpOrgsTestCase(unittest.TestCase):
    """Copies the real organizations/ into a per-test tmp dir."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.orgs_dir = self.tmp / "organizations"
        shutil.copytree(Path(__file__).parent.parent / "organizations", self.orgs_dir)
        self.runner = CliRunner()

    def tearDown(self):
        self._tmpdir.cleanup()


class TestRollbackCLI(_TmpOrgsTestCase):
    def _build_and_deploy(self, target: Path, out: Path, deploy_args=None):
        result = self.runner.invoke(
            main,
            ["build-all", "--base", str(self.orgs_dir), "--out", str(out)],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.runner.invoke(
            main,
            [
                "deploy-all",
                "--base",
                str(self.orgs_dir),
                "--dist-base",
                str(out),
                "--target",
                str(target),
                "--yes",
            ]
            + (deploy_args or []),
        )
        self.assertEqual(result.exit_code, 0, result.output)

    def test_cli_rollback_list_and_rollback(self):
        target = self.tmp / "personas"
        out = self.tmp / "dist"
        self._build_and_deploy(target, out)

        # --list shows the session
        result = self.runner.invoke(
            main, ["rollback", "--list", "--target", str(target)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("deploy-all", result.output)
        self.assertIn("verdant-aquaponics", result.output)

        # second deploy (after changing the org) -> archives the first
        au_org = self.orgs_dir / "verdant-aquaponics/org.yaml"
        doc = yaml.safe_load(au_org.read_text(encoding="utf-8"))
        doc["organization"]["name"] = "Verdant Aquaponics Co-op v2"
        au_org.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        self._build_and_deploy(target, out)

        # rollback with --yes
        result = self.runner.invoke(
            main, ["rollback", "--target", str(target), "--yes"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Rolled back", result.output)
        # target still has the first-deploy personas (created in session 1)
        self.assertTrue((target / "dana").exists())

        # one more rollback undoes session 1 entirely
        result = self.runner.invoke(
            main, ["rollback", "--target", str(target), "--yes"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((target / "dana").exists())

    def test_cli_rollback_nothing_to_do(self):
        target = self.tmp / "personas"
        result = self.runner.invoke(main, ["rollback", "--target", str(target)])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Nothing to roll back", result.output)

    def test_cli_rollback_restores_exactly(self):
        """End-to-end: deploy v1, deploy v2, rollback -> v1 back, no leftovers."""
        target = self.tmp / "personas"
        out = self.tmp / "dist"
        self._build_and_deploy(target, out)
        # v1 content marker
        v1 = (target / "dana" / "SOUL.md").read_text(encoding="utf-8")

        # Change the org so the second deploy actually overwrites (additive
        # deploy is a no-op when nothing changed).
        au_org = self.orgs_dir / "verdant-aquaponics/org.yaml"
        doc = yaml.safe_load(au_org.read_text(encoding="utf-8"))
        doc["organization"]["name"] = "Verdant Aquaponics Co-op v2"
        au_org.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

        self._build_and_deploy(target, out)  # v2 archives v1
        archive_root = archives_dir(target)
        self.assertTrue(archive_root.is_dir())

        result = self.runner.invoke(
            main, ["rollback", "--target", str(target), "--yes"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("restored :", result.output)
        # v1 content is back
        self.assertEqual((target / "dana" / "SOUL.md").read_text(encoding="utf-8"), v1)
        # session 1's manifest entry still lives in the archive root (it must
        # remain rollback-able), so the root survives this rollback.
        self.assertTrue(archive_root.is_dir())

        # rollback of session 1 removes everything (fresh deploy): created
        # personas removed, manifest empty -> archive root and target deleted.
        result = self.runner.invoke(
            main, ["rollback", "--target", str(target), "--yes"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(target.exists())
        self.assertFalse(archive_root.exists())


class TestDurableJournal(unittest.TestCase):
    """H1: the deploy writes a durable in_progress journal entry BEFORE
    mutating the target, so a crash mid-deploy never leaves an untraceable
    mutation; rollback reconciles an interrupted attempt."""

    def _target(self, tmp):
        return Path(tmp) / "personas"

    def test_begin_writes_in_progress_entry(self):
        from phantomorg.deploy.session import begin_session

        with tempfile.TemporaryDirectory() as t:
            target = self._target(t)
            session = begin_session(
                target,
                command="deploy",
                orgs=["org1"],
                planned_archived=["dana"],
                planned_created=["maria"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            self.assertEqual(session["state"], "in_progress")
            archive_root = archives_dir(target)
            sessions = load_sessions(archive_root)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["id"], session["id"])
            self.assertEqual(sessions[0]["state"], "in_progress")
            self.assertEqual(sessions[0]["planned_archived"], ["dana"])

    def test_commit_transitions_to_committed(self):
        from phantomorg.deploy.session import begin_session, commit_session

        with tempfile.TemporaryDirectory() as t:
            target = self._target(t)
            session = begin_session(
                target,
                command="deploy",
                orgs=["org1"],
                planned_archived=["dana"],
                planned_created=["maria"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            result = DeployResult(
                target=target,
                deployed=["dana", "maria"],
                created=["maria"],
                pruned=[],
                archived=[("dana", str(archives_dir(target) / "dana-stamp"))],
            )
            commit_session(target, session["id"], result)
            sessions = load_sessions(archives_dir(target))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["state"], "committed")
            self.assertEqual(sessions[0]["created"], ["maria"])
            self.assertEqual(sessions[0]["archived"][0]["name"], "dana")

    def test_rollback_reconciles_interrupted_deploy_with_archive(self):
        """An in_progress session whose attempt already archived a persona:
        rollback restores it (and consumes the archive)."""
        from phantomorg.deploy.session import begin_session

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = self._target(tmp)
            target.mkdir()
            # Simulate the interrupted attempt: old persona archived to a
            # dir whose stamp >= the journal id (the id IS a stamp).
            session = begin_session(
                target,
                command="deploy",
                orgs=["org1"],
                planned_archived=["dana"],
                planned_created=["maria"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            archive_root = archives_dir(target)
            archive_dir = archive_root / f"dana-{session['id']}"
            archive_dir.mkdir(parents=True)
            (archive_dir / "SOUL.md").write_text("OLD", encoding="utf-8")
            # The attempt also replaced the target version (post-deploy).
            (target / "dana").mkdir()
            (target / "dana" / "SOUL.md").write_text("NEW", encoding="utf-8")
            # And created maria before dying.
            (target / "maria").mkdir()
            (target / "maria" / "SOUL.md").write_text("NEW", encoding="utf-8")

            plan = plan_rollback(archive_root, target)
            self.assertEqual(plan.session_id, session["id"])
            self.assertFalse(plan.cleanup_only)
            restored_names = [n for n, _ in plan.restore]
            self.assertEqual(restored_names, ["dana"])
            self.assertIn("maria", plan.remove_created)

            result = execute_rollback(plan)
            self.assertEqual(result.restored, ["dana"])
            self.assertIn("maria", result.removed_created)
            # The old version is back in the target, the post-deploy version
            # went to the trash (inside the archive root) and was then
            # deleted after success.
            self.assertEqual((target / "dana" / "SOUL.md").read_text(), "OLD")
            self.assertFalse((target / "maria").exists())
            # Nothing else survives: fresh archive root and fresh target
            # (except the manifest that still holds session 1 committed-able
            # state — actually the in_progress entry is dropped last).
            self.assertEqual(load_sessions(archive_root), [])

    def test_rollback_reconciles_interrupted_deploy_cleanup_only(self):
        """An in_progress session whose attempt only created personas
        (nothing archived): rollback is a cleanup-only plan."""
        from phantomorg.deploy.session import begin_session

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = self._target(tmp)
            target.mkdir()
            begin_session(
                target,
                command="deploy",
                orgs=["org1"],
                planned_archived=[],
                planned_created=["maria"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            (target / "maria").mkdir()
            (target / "maria" / "SOUL.md").write_text("NEW", encoding="utf-8")

            plan = plan_rollback(archives_dir(target), target)
            # There is work to do (remove the created persona) so this is
            # NOT a cleanup-only plan — cleanup_only means "nothing left to
            # do at all".
            self.assertFalse(plan.cleanup_only)
            self.assertEqual(plan.remove_created, ["maria"])
            result = execute_rollback(plan)
            self.assertIn("maria", result.removed_created)
            self.assertFalse((target / "maria").exists())

    def test_deploy_leaves_committed_session(self):
        """End-to-end via CLI: a successful deploy records a committed
        session (not in_progress)."""
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            compiled = _build_au(tmp)
            # deploy needs --target pointing at a directory whose parent is
            # writable; use the compiled dir directly.
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(compiled),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            sessions = load_sessions(archives_dir(target))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["state"], "committed")
            self.assertTrue(sessions[0]["deployed"])


class TestTransactionLock(unittest.TestCase):
    """H2: whole deploy/rollback transactions are serialized by a lock at
    the runtime dir level (not just manifest load/save)."""

    def test_transaction_lock_serializes_threads(self):
        import threading

        from phantomorg.deploy.session import _transaction_lock

        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "personas"
            target.parent.mkdir(parents=True, exist_ok=True)
            active = 0
            max_active = 0
            lock = threading.Lock()
            barrier = threading.Barrier(8)
            errors: list[Exception] = []

            def worker():
                nonlocal active, max_active
                try:
                    barrier.wait()
                    with _transaction_lock(target):
                        with lock:
                            active += 1
                            max_active = max(max_active, active)
                        import time

                        time.sleep(0.01)
                        with lock:
                            active -= 1
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            self.assertEqual(max_active, 1)

    def test_transaction_lock_file_created(self):
        from phantomorg.deploy.session import TRANSACTION_LOCK_NAME, _transaction_lock

        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "personas"
            with _transaction_lock(target):
                pass
            self.assertTrue((target.parent / TRANSACTION_LOCK_NAME).is_file())

    def test_deploy_and_rollback_use_transaction_lock(self):
        """CLI deploy writes the transaction lock file next to the target
        (the lock is really taken by the command)."""
        from phantomorg.deploy.session import TRANSACTION_LOCK_NAME

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            compiled = _build_au(tmp)
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(compiled),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((target.parent / TRANSACTION_LOCK_NAME).is_file())


class TestManifestConfinement(unittest.TestCase):
    """H3: manifest-supplied names and paths are validated before use, so
    a corrupt or tampered manifest cannot make rollback touch arbitrary
    filesystem content."""

    def _seed_manifest(self, tmp, session):
        from phantomorg.deploy.session import _save_sessions

        target = Path(tmp) / "personas"
        archive_root = archives_dir(target)
        _save_sessions(archive_root, [session])
        return target, archive_root

    def _session(self, tmp, **overrides):
        target = Path(tmp) / "personas"
        base = {
            "id": "s1",
            "state": "committed",
            "command": "deploy",
            "target": str(target.resolve()),
            "archive_root_pre_existed": False,
            "target_pre_existed": False,
            "archived": [],
            "created": [],
        }
        base.update(overrides)
        return base

    def test_unsafe_name_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            session = self._session(
                tmp,
                archived=[{"name": "../../evil", "dir": "/tmp/x"}],
            )
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("unsafe persona name", str(ctx.exception))

    def test_name_with_separator_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            session = self._session(
                tmp,
                archived=[{"name": "a/b", "dir": "/tmp/x"}],
            )
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("unsafe persona name", str(ctx.exception))

    def test_non_string_name_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            session = self._session(
                tmp,
                archived=[{"name": 42, "dir": "/tmp/x"}],
            )
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("unsafe persona name", str(ctx.exception))

    def test_archive_dir_outside_root_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            outside = Path(tempfile.mkdtemp()) / "other"
            session = self._session(
                tmp,
                archived=[{"name": "dana", "dir": str(outside)}],
            )
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("escapes the archive root", str(ctx.exception))

    def test_relative_archive_dir_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            session = self._session(
                tmp,
                archived=[{"name": "dana", "dir": "personas-archive/dana-1"}],
            )
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("not absolute", str(ctx.exception))

    def test_created_name_unsafe_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            session = self._session(tmp, created=["../etc"])
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("unsafe persona name", str(ctx.exception))

    def test_mismatched_target_rejected(self):
        """A session whose recorded target differs from the invoked one is
        never planned: the caller filters by target, so rollback refuses
        instead of touching anything."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            other = Path(tempfile.mkdtemp()) / "other-personas"
            session = self._session(tmp, target=str(other.resolve()))
            target, archive_root = self._seed_manifest(tmp, session)
            with self.assertRaises(RollbackError) as ctx:
                plan_rollback(archive_root, target)
            self.assertIn("nothing to roll back", str(ctx.exception))


class TestSuffixExhaustion(unittest.TestCase):
    """H3: when all 1000 numeric suffixes for a name are taken, the code
    raises instead of letting shutil.move nest the source INSIDE the
    existing directory (silently corrupting the archive/trash layout)."""

    def test_archive_suffix_exhaustion_raises_not_nests(self):
        """archive_persona: with 999 same-name archive dirs present plus
        the base, the 1000th archive must raise DeployError instead of
        moving the persona inside ``<base>-999/<name>/``."""
        from phantomorg.deploy.target import (
            DeployError,
            _archive_stamp,
            archive_persona,
            archives_dir,
        )

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            personas_dir = target / "personas"
            personas_dir.mkdir(parents=True)
            src = personas_dir / "dana"
            src.mkdir()
            (src / "SOUL.md").write_text("v1", encoding="utf-8")

            archive_root = archives_dir(personas_dir)
            archive_root.mkdir(parents=True)
            stamp = _archive_stamp()
            base = f"dana-{stamp}"
            for n in range(1000):
                name = base if n == 0 else f"{base}-{n}"
                (archive_root / name).mkdir()

            import phantomorg.deploy.target as target_mod

            with (
                unittest.mock.patch.object(
                    target_mod, "_archive_stamp", return_value=stamp
                ),
                self.assertRaises(DeployError) as ctx,
            ):
                archive_persona(personas_dir, "dana")
            self.assertIn(
                "unable to allocate a unique archive destination", str(ctx.exception)
            )
            # the source was NOT moved (nothing nested inside base-999)
            self.assertTrue(src.is_dir())
            self.assertTrue((src / "SOUL.md").exists())
            # and no new archive dir appeared
            self.assertEqual(len(list(archive_root.iterdir())), 1000)

    def test_discard_suffix_exhaustion_raises_not_nests(self):
        """rollback _discard: with all trash suffixes taken, moving a
        discarded persona must raise RollbackError rather than nest it
        inside an existing trash entry."""
        from phantomorg.deploy.session import (
            TRASH_PREFIX,
            _archive_stamp,
            execute_rollback,
            plan_rollback,
        )
        from phantomorg.deploy.target import archives_dir

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            target.mkdir(parents=True)
            archive_root = archives_dir(target)
            archive_root.mkdir(parents=True)

            # an archived persona to restore
            archived = archive_root / "dana-old"
            archived.mkdir()
            (archived / "SOUL.md").write_text("old", encoding="utf-8")

            # a post-deploy version in the target that must be discarded
            (target / "dana").mkdir()
            (target / "dana" / "SOUL.md").write_text("new", encoding="utf-8")

            session = {
                "id": "sess-1",
                "command": "deploy",
                "target": str(target),
                "state": "committed",
                "created": [],
                "archived": [{"name": "dana", "dir": str(archived)}],
            }
            from phantomorg.deploy.session import _save_sessions

            _save_sessions(archive_root, [session])

            # fill the trash dir with 1000 same-name entries: "dana" plus
            # 999 suffixes. "dana" itself must also exist so the loop has
            # to try all 999 suffixes before giving up.
            stamp = _archive_stamp()
            trash_dir = archive_root / f"{TRASH_PREFIX}{stamp}"
            trash_dir.mkdir(parents=True)
            for n in range(1000):
                name = "dana" if n == 0 else f"dana-{n}"
                (trash_dir / name).mkdir()

            import phantomorg.deploy.session as session_mod

            with unittest.mock.patch.object(
                session_mod, "_archive_stamp", return_value=stamp
            ):
                plan = plan_rollback(archive_root, target)
                with self.assertRaises(RollbackError) as ctx:
                    execute_rollback(plan)
            self.assertIn(
                "unable to allocate a unique trash destination", str(ctx.exception)
            )
            # the discard failed atomically: nothing was moved or deleted
            self.assertTrue((target / "dana").exists())
            self.assertTrue(archived.exists())
            self.assertEqual(len(list(trash_dir.iterdir())), 1000)

    def test_session_id_collision_gets_unique_suffix(self):
        """Two sessions recorded in the same millisecond must never share
        an id — a shared id would make rollback drop BOTH sessions."""
        from phantomorg.deploy.session import record_session

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            target = tmp / "personas"
            target.mkdir(parents=True)
            archive_root = archives_dir(target)

            result1 = DeployResult(target=target, deployed=["dana"], created=["dana"])
            result2 = DeployResult(target=target, deployed=["lucia"], created=["lucia"])

            s1 = record_session(
                target,
                result1,
                command="deploy",
                orgs=["org1"],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            s2 = record_session(
                target,
                result2,
                command="deploy",
                orgs=["org2"],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            self.assertNotEqual(s1["id"], s2["id"])
            sessions = load_sessions(archive_root)
            ids = [s["id"] for s in sessions]
            self.assertEqual(len(ids), len(set(ids)), "session ids must be unique")


class TestStaleInternalsCleanup(unittest.TestCase):
    """H1 (adversarial review v0.5.5): rollback trash is only garbage-
    collected when NO session is still in_progress. A stale trash dir
    from an interrupted rollback is the sole recovery evidence — it must
    never be deleted while the journal entry is still open."""

    def _make_old_trash(self, archive_root: Path) -> Path:
        """Create a trash dir with an mtime older than the staleness
        cutoff, so the age check alone would remove it."""
        trash = archive_root / f"{TRASH_PREFIX}{_archive_stamp()}"
        trash.mkdir(parents=True)
        (trash / "dana").mkdir()
        (trash / "dana" / "SOUL.md").write_text("old", encoding="utf-8")
        old = datetime.now(timezone.utc) - timedelta(days=2)
        os.utime(trash, (old.timestamp(), old.timestamp()))
        return trash

    def _archive_root(self, tmp: Path) -> Path:
        target = tmp / "personas"
        target.mkdir(parents=True)
        return archives_dir(target)

    def test_trash_kept_while_session_in_progress(self):
        """A stale trash dir must survive when the manifest still has an
        in_progress session (interrupted rollback)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            begin_session(
                tmp / "personas",
                command="deploy",
                orgs=["org"],
                planned_archived=[],
                planned_created=["dana"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            # The recorded session stays in_progress (never committed).
            self.assertTrue(
                any(
                    str(s.get("state")) == "in_progress"
                    for s in load_sessions(archive_root)
                )
            )
            trash = self._make_old_trash(archive_root)
            _cleanup_stale_internals(archive_root)
            self.assertTrue(trash.exists(), "trash must survive an open session")

    def test_trash_collected_without_in_progress_session(self):
        """With no in_progress session (no manifest, or only committed
        sessions), a stale trash dir is garbage-collected as before."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            trash = self._make_old_trash(archive_root)
            _cleanup_stale_internals(archive_root)
            self.assertFalse(trash.exists(), "stale trash should be collected")

    def test_tmp_files_always_collected(self):
        """Temp manifest files are never evidence: they are collected
        even when a session is in_progress."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            begin_session(
                tmp / "personas",
                command="deploy",
                orgs=["org"],
                planned_archived=[],
                planned_created=["dana"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            stale = archive_root / ".phantomorg-manifest.json.12345.tmp"
            stale.write_text("x", encoding="utf-8")
            old = datetime.now(timezone.utc) - timedelta(days=2)
            os.utime(stale, (old.timestamp(), old.timestamp()))
            _cleanup_stale_internals(archive_root)
            self.assertFalse(stale.exists())

    def test_trash_kept_while_session_rollback_in_progress(self):
        """Audit v0.5.7 #1: a session marked ``rollback_in_progress``
        must also protect its trash from GC. If a rollback crashed after
        discarding versions into the trash but before dropping the
        session, the trash is the only evidence that the archives were
        consumed — deleting it would turn the retry into a permanent
        refusal."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            session = begin_session(
                tmp / "personas",
                command="deploy",
                orgs=["org"],
                planned_archived=[],
                planned_created=["dana"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            _mark_session_state(archive_root, session["id"], "rollback_in_progress")
            self.assertTrue(
                any(
                    str(s.get("state")) == "rollback_in_progress"
                    for s in load_sessions(archive_root)
                )
            )
            trash = self._make_old_trash(archive_root)
            _cleanup_stale_internals(archive_root)
            self.assertTrue(
                trash.exists(),
                "trash must survive a rollback_in_progress session",
            )

    def test_trash_kept_with_committed_session(self):
        """Audit v0.5.8 #1 (ChatGPT re-verification): a ``committed``
        journal entry does NOT make stale trash collectible.

        A completed rollback deletes its own trash as its final step, so
        committed + trash is exactly the state a rollback that crashed
        BEFORE the rollback_in_progress transition (pre-v0.5.8, or in
        the v0.5.7 cleanup-before-mark window) leaves behind — and that
        trash is the sole recovery evidence for the retry. Garbage-
        collecting it would turn the retry into a permanent refusal.
        ANY recorded session keeps every trash dir.
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            session = begin_session(
                tmp / "personas",
                command="deploy",
                orgs=["org"],
                planned_archived=[],
                planned_created=["dana"],
                planned_pruned=[],
                archive_root_pre_existed=False,
                target_pre_existed=False,
            )
            _mark_session_state(archive_root, session["id"], "committed")
            trash = self._make_old_trash(archive_root)
            _cleanup_stale_internals(archive_root)
            self.assertTrue(
                trash.exists(),
                "trash must survive any recorded session, committed included",
            )

    def test_trash_collected_only_without_any_session(self):
        """Audit v0.5.8 #1: stale trash is ONLY collected when the
        manifest is absent/empty — with no recorded session there is no
        rollback that could retry, so the trash cannot be recovery
        evidence (this is the historical GC behavior, now the only case
        that collects)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            trash = self._make_old_trash(archive_root)
            self.assertEqual(load_sessions(archive_root), [])
            _cleanup_stale_internals(archive_root)
            self.assertFalse(trash.exists(), "stale trash should be collected")

    def test_trash_kept_with_corrupt_manifest(self):
        """Audit v0.5.8 #1: a corrupt manifest also keeps trash. The
        docstring always claimed this, but the code defaulted trash_guard
        to False when load_sessions raised, so the trash WAS collected —
        now ``sessions is None`` protects it (we cannot know whether a
        session exists)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            archive_root = self._archive_root(tmp)
            trash = self._make_old_trash(archive_root)
            manifest = archive_root / ".phantomorg-manifest.json"
            manifest.write_text("{ not json", encoding="utf-8")
            _cleanup_stale_internals(archive_root)
            self.assertTrue(trash.exists(), "trash must survive a corrupt manifest")


if __name__ == "__main__":
    unittest.main()
