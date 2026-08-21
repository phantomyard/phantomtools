import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from phantomorg.cli import main
from phantomorg.compiler import build
from phantomorg.deploy.target import DeployCollisionError, DeployError, deploy
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"
UCG_ORG = Path(__file__).parent.parent / "organizations/harbor-capital/org.yaml"


def _build_au_module(tmp: Path) -> Path:
    """Build the AU org into tmp/dist and return the compiled dir."""
    au_spec = load_org_yaml(AU_ORG)
    out = tmp / "dist"
    build(au_spec, out)
    return out


_COLLIDING_ORG_YAML = """
version: 1
organization: {id: otra-empresa, name: "Other Company", sector: pyme, languages: [es]}
departments:
  - {id: direccion, name: "Management", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    access_level: level-3
actors:
  - id: dana   # actor id deliberately equal to Verdant Aquaponics Co-op's
    role: ceo
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
"""


class TestDeployMultiOrg(unittest.TestCase):
    def test_deploy_two_different_orgs_no_collision(self):
        au_spec = load_org_yaml(AU_ORG)
        ucg_spec = load_org_yaml(UCG_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, ucg_out, target = tmp / "au_out", tmp / "ucg_out", tmp / "target"

            build(au_spec, au_out)
            build(ucg_spec, ucg_out)

            result1 = deploy(au_out, target)
            self.assertEqual(
                set(result1.deployed), {"marco", "lucia", "diego", "dana", "elias"}
            )

            result2 = deploy(
                ucg_out, target
            )  # no collision: "nadia" does not exist yet
            self.assertEqual(result2.deployed, ["nadia"])

    def test_deploy_collision_between_organizations_is_blocked(self):
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            colliding_org_path = tmp / "otra-empresa.yaml"
            colliding_org_path.write_text(_COLLIDING_ORG_YAML, encoding="utf-8")
            colliding_spec = load_org_yaml(colliding_org_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(colliding_spec, other_out)

            deploy(au_out, target)  # deploys Verdant Aquaponics Co-op's "dana"

            with self.assertRaises(DeployCollisionError):
                deploy(other_out, target)  # "dana" from otra-empresa collides

    def test_deploy_collision_with_force_overwrites(self):
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            colliding_org_path = tmp / "otra-empresa.yaml"
            colliding_org_path.write_text(_COLLIDING_ORG_YAML, encoding="utf-8")
            colliding_spec = load_org_yaml(colliding_org_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(colliding_spec, other_out)

            deploy(au_out, target)
            result = deploy(other_out, target, force=True)
            self.assertIn("dana", result.deployed)

            # We confirm that the deployed actor now belongs to "otra-empresa"
            meta = (target / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8")
            self.assertIn("otra-empresa", meta)

    def test_collision_in_preflight_mutates_nothing(self):
        """H2: collisions are detected BEFORE any mutation. A rejected
        deploy must leave the target and the archive exactly as they
        were — no partial deploys, no consumed backups, no staging
        leftovers, no phantom in_progress session entries."""
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            colliding_org_path = tmp / "otra-empresa.yaml"
            colliding_org_path.write_text(_COLLIDING_ORG_YAML, encoding="utf-8")
            colliding_spec = load_org_yaml(colliding_org_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(colliding_spec, other_out)

            deploy(au_out, target)  # deploys Verdant Aquaponics Co-op's "dana"
            before = {
                p.name: p
                for p in sorted(target.iterdir())
                if p.name != "personas-archive"
            }

            with self.assertRaises(DeployCollisionError):
                deploy(other_out, target)  # "dana" from otra-empresa collides

            # target unchanged: no new actor, no overwrite, no staging dir
            after = {
                p.name: p
                for p in sorted(target.iterdir())
                if p.name != "personas-archive"
            }
            self.assertEqual(set(after), set(before))
            self.assertEqual(
                [p for p in target.iterdir() if p.name.startswith(".pf-staging-")],
                [],
            )
            # archive unchanged: no backup was consumed
            self.assertEqual(
                len(list((target.parent / "personas-archive").glob("dana-*"))), 0
            )

    def test_deploy_all_skips_colliding_org_and_deploys_rest(self):
        """H2: in deploy-all, an org whose actors collide is skipped in
        preflight (it mutates nothing) while the other orgs still deploy
        and are recorded in the session."""
        au_spec = load_org_yaml(AU_ORG)
        ucg_spec = load_org_yaml(UCG_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            colliding_org_path = tmp / "otra-empresa.yaml"
            colliding_org_path.write_text(_COLLIDING_ORG_YAML, encoding="utf-8")
            colliding_spec = load_org_yaml(colliding_org_path)

            au_out, ucg_out, other_out, target = (
                tmp / "au_out",
                tmp / "ucg_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(ucg_spec, ucg_out)
            build(colliding_spec, other_out)

            deploy(au_out, target)  # "dana" occupied

            from click.testing import CliRunner

            from phantomorg.cli import main

            runner = CliRunner()
            # deploy-all iterates base/<org_id>/org.yaml and expects
            # --dist-base/<org_id> for each
            base_dir = tmp / "base"
            ucg_org_dir = base_dir / "harbor-capital"
            other_org_dir = base_dir / "otra-empresa"
            ucg_org_dir.mkdir(parents=True)
            other_org_dir.mkdir(parents=True)
            (ucg_org_dir / "org.yaml").write_text(
                (
                    Path(__file__).parent.parent
                    / "organizations/harbor-capital/org.yaml"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (other_org_dir / "org.yaml").write_text(
                _COLLIDING_ORG_YAML, encoding="utf-8"
            )
            dist_base = tmp / "dist-base"
            (dist_base / "harbor-capital").mkdir(parents=True)
            (dist_base / "otra-empresa").mkdir(parents=True)
            shutil.copytree(ucg_out, dist_base / "harbor-capital", dirs_exist_ok=True)
            shutil.copytree(other_out, dist_base / "otra-empresa", dirs_exist_ok=True)

            result = runner.invoke(
                main,
                [
                    "deploy-all",
                    "--base",
                    str(base_dir),
                    "--dist-base",
                    str(dist_base),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            self.assertEqual(
                result.exit_code, 1, result.output
            )  # collision -> non-zero
            # ucg's "nadia" deployed, colliding org's "dana" NOT overwritten
            self.assertTrue((target / "nadia").exists())
            self.assertEqual(
                (target / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
                (au_out / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
            )
            # session recorded for the successful org only
            from phantomorg.deploy.session import load_sessions

            sessions = load_sessions(target.parent / "personas-archive")
            self.assertTrue(sessions, "a session must be recorded")
            self.assertTrue(
                all(s.get("state") == "committed" for s in sessions),
                "no phantom in_progress sessions may remain",
            )

    def test_deploy_all_mid_mutation_failure_is_recoverable(self):
        """A deploy-all where one org fails MID-mutation (after some of
        its actors were already archived/created) leaves an in_progress
        session that `po rollback` reconciles completely — restoring the
        failed org's partial archives and removing its partial creates,
        returning the target to its exact pre-deploy state."""
        au_spec = load_org_yaml(AU_ORG)
        ucg_spec = load_org_yaml(UCG_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, ucg_out, target = tmp / "au_out", tmp / "ucg_out", tmp / "target"
            build(au_spec, au_out)
            build(ucg_spec, ucg_out)

            # Pre-deploy au so the target has content; ucg's "nadia" will
            # be created by the deploy-all (it does not exist yet).
            deploy(au_out, target)
            pre_state = sorted(p.name for p in target.iterdir())

            base_dir = tmp / "base"
            au_org_dir = base_dir / "verdant-aquaponics"
            ucg_org_dir = base_dir / "harbor-capital"
            au_org_dir.mkdir(parents=True)
            ucg_org_dir.mkdir(parents=True)
            (au_org_dir / "org.yaml").write_text(
                AU_ORG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (ucg_org_dir / "org.yaml").write_text(
                UCG_ORG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            dist_base = tmp / "dist-base"
            (dist_base / "verdant-aquaponics").mkdir(parents=True)
            (dist_base / "harbor-capital").mkdir(parents=True)
            shutil.copytree(
                au_out, dist_base / "verdant-aquaponics", dirs_exist_ok=True
            )
            shutil.copytree(ucg_out, dist_base / "harbor-capital", dirs_exist_ok=True)

            from click.testing import CliRunner

            from phantomorg.cli import main

            # Simulate a disk error on ucg's ONLY actor: writing nadia's
            # first owned file fails — the org aborts mid-mutation.
            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                if Path(dst).parent.name == "nadia" and Path(dst).name == "IDENTITY.md":
                    raise OSError(f"simulated disk error on {dst}")
                return real_replace(src, dst, **kwargs)

            with unittest.mock.patch(
                "phantomorg.deploy.target.os.replace", side_effect=flaky_replace
            ):
                runner = CliRunner()
                result = runner.invoke(
                    main,
                    [
                        "deploy-all",
                        "--base",
                        str(base_dir),
                        "--dist-base",
                        str(dist_base),
                        "--target",
                        str(target),
                        "--yes",
                    ],
                )
            # F3 (cli-tests): a mid-mutation filesystem failure must exit
            # non-zero even when some orgs deployed — CI treats exit 0 as
            # "everything deployed".
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("in_progress", result.output)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("in_progress", result.output)
            # au deployed; ucg's nadia has no owned files written (only
            # its scaffold dirs may exist from the failed attempt)
            anna_dir = target / "nadia"
            self.assertFalse(
                (anna_dir / "SOUL.md").exists(),
                "nadia's owned files must not have been written",
            )
            self.assertFalse((anna_dir / "IDENTITY.md").exists())

            # Rollback reconciles the interrupted session.
            runner2 = CliRunner()
            rb = runner2.invoke(main, ["rollback", "--target", str(target), "--yes"])
            self.assertEqual(rb.exit_code, 0, rb.output)
            self.assertEqual({p.name for p in target.iterdir()}, set(pre_state))
            self.assertIn("restored", rb.output)

    def test_deploy_all_interrupted_org_archives_in_session_created_persona(self):
        """Deep edge case (finding #4 residual): in a deploy-all, org A
        CREATES a persona that org B (--force, shared actor id) then
        archives before failing mid-mutation. That archive is an
        in-session artifact — rollback must DISCARD it (not restore it,
        which would resurrect an in-session persona, and not leave it
        orphaned in the archive root).

        Regression for the pre-fix behavior where the archive stayed
        behind forever and kept personas-archive/ alive."""
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            colliding_org_path = tmp / "otra-empresa.yaml"
            # Org B shares actor id "dana" with org A AND has a second
            # actor "zoe" (sorted after "dana") so the deploy-all fails
            # mid-mutation AFTER B archived A's freshly-created "dana".
            _ALMA_ZOE_ORG_YAML = """
version: 1
organization: {id: otra-empresa, name: "Other Company", sector: pyme, languages: [es]}
departments:
  - {id: direccion, name: "Management", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    access_level: level-3
actors:
  - id: dana
    role: ceo
    tools: [email]
  - id: zoe
    role: ceo
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
"""
            colliding_org_path.write_text(_ALMA_ZOE_ORG_YAML, encoding="utf-8")
            colliding_spec = load_org_yaml(colliding_org_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(colliding_spec, other_out)
            # Org B compiles to actors [dana, zoe] (sorted)
            self.assertEqual(
                sorted(d.name for d in other_out.iterdir() if d.is_dir()),
                ["dana", "zoe"],
            )

            # EMPTY pre-deploy target: org A creates "dana" fresh, org B
            # (sharing the actor id, --force) archives A's fresh "dana"
            # and swaps its own, then fails on the NEXT actor ("zoe").
            base_dir = tmp / "base"
            au_org_dir = base_dir / "verdant-aquaponics"
            other_org_dir = base_dir / "otra-empresa"
            au_org_dir.mkdir(parents=True)
            other_org_dir.mkdir(parents=True)
            (au_org_dir / "org.yaml").write_text(
                AU_ORG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (other_org_dir / "org.yaml").write_text(
                _ALMA_ZOE_ORG_YAML, encoding="utf-8"
            )
            dist_base = tmp / "dist-base"
            (dist_base / "verdant-aquaponics").mkdir(parents=True)
            (dist_base / "otra-empresa").mkdir(parents=True)
            shutil.copytree(
                au_out, dist_base / "verdant-aquaponics", dirs_exist_ok=True
            )
            shutil.copytree(other_out, dist_base / "otra-empresa", dirs_exist_ok=True)

            from click.testing import CliRunner

            from phantomorg.cli import main

            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                # Fail on "zoe" (sorted last: dana, zoe): org B archived
                # A's fresh "dana" before hitting the error.
                if Path(dst).parent.name == "zoe" and Path(dst).name == "IDENTITY.md":
                    raise OSError(f"simulated disk error on {dst}")
                return real_replace(src, dst, **kwargs)

            with unittest.mock.patch(
                "phantomorg.deploy.target.os.replace", side_effect=flaky_replace
            ):
                runner = CliRunner()
                result = runner.invoke(
                    main,
                    [
                        "deploy-all",
                        "--base",
                        str(base_dir),
                        "--dist-base",
                        str(dist_base),
                        "--target",
                        str(target),
                        "--yes",
                        "--force",
                    ],
                )
            # F3 (cli-tests): mid-mutation failure exits non-zero.
            self.assertEqual(result.exit_code, 1, result.output)
            archive_root = target.parent / "personas-archive"
            # org B archived A's freshly-created "dana" before failing
            alma_archives = [
                p for p in archive_root.iterdir() if p.name.startswith("dana-")
            ]
            self.assertEqual(len(alma_archives), 1, result.output)

            runner2 = CliRunner()
            rb = runner2.invoke(main, ["rollback", "--target", str(target), "--yes"])
            self.assertEqual(rb.exit_code, 0, rb.output)
            # The in-session archive was DISCARDED, not restored, and the
            # archive root (which did not pre-exist) is fully removed.
            self.assertIn("discarded", rb.output)
            self.assertIn("dana-", rb.output)
            self.assertFalse(archive_root.exists(), rb.output)
            # Target is back to the pre-deploy state (empty).
            if target.exists():
                self.assertEqual(list(target.iterdir()), [])


class TestReviewCollisionBug(unittest.TestCase):
    """The exact scenario ChatGPT called the 'most concrete reproducible
    bug' (recommended-priority review, 2026-08-09):

        actor A -> successfully archived/deployed
        actor B -> collision
        deploy exits with error
        record_session() never runs

    i.e. a collision discovered MID-deploy, after earlier actors were
    already mutated, leaving a modified runtime with no rollback
    transaction. This is not reproducible in the current code: preflight
    (v0.4.4 H2) checks every actor's collision/symlink state BEFORE any
    mutation, so a rejected deploy never touches the target and never
    leaves a session behind. These tests pin that behaviour so a
    regression (e.g. moving collision detection back into the deploy
    loop) fails loudly."""

    def _build_orgs(self, tmp: Path):
        """Builds AU (actors: dana, elias, marco, lucia, diego) and a
        colliding org (actor id 'dana' = cross-org collision with AU).
        Returns (au_out, other_out, target)."""
        au_spec = load_org_yaml(AU_ORG)
        other_path = tmp / "otra-empresa.yaml"
        other_path.write_text(_COLLIDING_ORG_YAML, encoding="utf-8")
        other_spec = load_org_yaml(other_path)

        au_out, other_out, target = (
            tmp / "au_out",
            tmp / "other_out",
            tmp / "target",
        )
        build(au_spec, au_out)
        build(other_spec, other_out)
        return au_out, other_out, target

    def test_collision_mid_deploy_leaves_no_partial_state_and_no_session(self):
        """Deploy AU on top of a target that already has 'dana' from
        otra-empresa. 'dana' sorts FIRST, so the old code would have
        archived+deployed actors before reaching the collision; the
        current code rejects the whole deploy in preflight."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, other_out, target = self._build_orgs(tmp)

            # Pre-seed the target with otra-empresa's 'dana' (a
            # hand-written/meta-less persona would also collide, but the
            # cross-org case is the one in the review).
            target.mkdir(parents=True)
            shutil.copytree(other_out / "dana", target / "dana")

            archive_root = target.parent / "personas-archive"
            from click.testing import CliRunner

            runner = CliRunner()
            # 'dana' would be archived+redeployed first, then the
            # cross-org collision would surface — IF detection happened
            # mid-loop. Preflight rejects before any of that.
            result = runner.invoke(
                main,
                ["deploy", "--from", str(au_out), "--target", str(target), "--yes"],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("collision", result.output.lower())
            self.assertIn("no changes were made", result.output.lower())

            # NOTHING was mutated: no archive dir, no session manifest,
            # no new actors, no lock leftovers.
            self.assertFalse(archive_root.exists(), result.output)
            self.assertEqual(
                sorted(p.name for p in target.iterdir()), ["dana"], result.output
            )
            manifest = target.parent / ".phantomorg-manifest.json"
            self.assertFalse(manifest.exists(), result.output)

    def test_collision_discards_session_does_not_block_next_deploy(self):
        """After a rejected deploy, the next deploy of a NON-colliding
        org must work: the discarded session must not linger as
        in_progress."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, other_out, target = self._build_orgs(tmp)
            target.mkdir(parents=True)
            shutil.copytree(other_out / "dana", target / "dana")

            from click.testing import CliRunner

            runner = CliRunner()
            r1 = runner.invoke(
                main,
                ["deploy", "--from", str(au_out), "--target", str(target), "--yes"],
            )
            self.assertNotEqual(r1.exit_code, 0, r1.output)

            # Now deploy UCG (no collision with 'dana') — must succeed.
            ucg_spec = load_org_yaml(UCG_ORG)
            ucg_out = tmp / "ucg_out"
            build(ucg_spec, ucg_out)
            r2 = runner.invoke(
                main,
                ["deploy", "--from", str(ucg_out), "--target", str(target), "--yes"],
            )
            self.assertEqual(r2.exit_code, 0, r2.output)
            self.assertTrue((target / "nadia").exists(), r2.output)
            # The other org's 'dana' is untouched.
            self.assertTrue((target / "dana").exists(), r2.output)

    def test_force_overwrites_collision_and_records_rollbackable_session(self):
        """With --force the collision is deliberate: the deploy proceeds
        (archiving the existing persona first) and records a COMMITTED
        session, so `po rollback` can restore the overwritten persona.
        This is the other half of the review's concern: even the force
        path leaves a rollback transaction."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, other_out, target = self._build_orgs(tmp)
            target.mkdir(parents=True)
            shutil.copytree(other_out / "dana", target / "dana")
            archive_root = target.parent / "personas-archive"

            from click.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(au_out),
                    "--target",
                    str(target),
                    "--force",
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(archive_root.is_dir(), result.output)
            # AU's 'dana' replaced otra-empresa's, and it was archived.
            alma_archives = [
                p for p in archive_root.iterdir() if p.name.startswith("dana-")
            ]
            self.assertEqual(len(alma_archives), 1, result.output)
            self.assertTrue((target / "dana" / ".phantomorg.yaml").exists())

            # Rollback restores the archived otra-empresa 'dana'.
            rb = runner.invoke(main, ["rollback", "--target", str(target), "--yes"])
            self.assertEqual(rb.exit_code, 0, rb.output)
            self.assertIn("dana", rb.output)
            # The restored persona is otra-empresa's (it has its own
            # metadata), NOT verdant-aquaponics's.
            restored_meta = target / "dana" / ".phantomorg.yaml"
            self.assertTrue(restored_meta.exists(), rb.output)
            import yaml as _yaml

            with open(restored_meta, encoding="utf-8") as f:
                meta = _yaml.safe_load(f)
            self.assertEqual(meta.get("organization_id"), "otra-empresa", rb.output)

    def test_deploy_plan_state_is_computed_under_transaction_lock(self):
        """Regression for a race: the journal plan (planned_archived/
        planned_created/planned_pruned, archive_root_pre_existed,
        target_pre_existed) must be computed AFTER acquiring the
        transaction lock. A pre-lock snapshot can go stale under a
        concurrent deploy (a persona planned_created may already exist
        by the time we hold the lock), and an interrupted rollback would
        then misclassify it as "created by this deploy" and discard its
        archive — losing the pre-deploy version.

        Structural check: every target-state inspection must happen
        between lock-enter and lock-exit."""
        import contextlib

        from click.testing import CliRunner

        import phantomorg.cli as cli_mod

        events: list[str] = []

        @contextlib.contextmanager
        def fake_lock(target):
            events.append("lock-enter")
            yield
            events.append("lock-exit")

        real_archives_dir = cli_mod.archives_dir
        real_is_dir = Path.is_dir

        def tracking_archives_dir(target: Path) -> Path:
            events.append(f"inspect:archives_dir:{target.name}")
            return real_archives_dir(target)

        def tracking_is_dir(self):
            # only record inspections of the target tree
            if "target" in str(self):
                events.append(f"inspect:is_dir:{self.name}")
            return real_is_dir(self)

        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, target = tmp / "out", tmp / "target"
            build(au_spec, out)
            target.mkdir()
            (target / "dana").mkdir()
            (target / "dana" / ".phantomorg.yaml").write_text(
                "organization_id: otra-empresa\nactor_id: dana\nrole_id: ceo\n",
                encoding="utf-8",
            )

            runner = CliRunner()
            with (
                unittest.mock.patch.object(cli_mod, "_transaction_lock", fake_lock),
                unittest.mock.patch.object(
                    cli_mod, "archives_dir", tracking_archives_dir
                ),
                unittest.mock.patch.object(Path, "is_dir", tracking_is_dir),
            ):
                result = runner.invoke(
                    main,
                    [
                        "deploy",
                        "--from",
                        str(out),
                        "--target",
                        str(target),
                        "--yes",
                        "--force",
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("lock-enter", events)
        self.assertIn("lock-exit", events)
        enter = events.index("lock-enter")
        exit_i = events.index("lock-exit")
        # The journal plan is the ONLY pre-mutation consumer of
        # archives_dir() (deploy_target never calls it), so the FIRST
        # archives_dir inspection must sit between lock-enter and
        # lock-exit. With the old code the plan was computed before the
        # lock and this first call happened before lock-enter.
        first_archives = next(
            i for i, ev in enumerate(events) if ev.startswith("inspect:archives_dir")
        )
        self.assertTrue(
            enter < first_archives < exit_i,
            f"first archives_dir inspection (the journal plan) happened "
            f"OUTSIDE the lock (events={events})",
        )
        # (A couple of archives_dir reads after lock-exit exist on
        # purpose: they only decide whether to print the post-deploy
        # "backup archive created" info banner — never the journal.)

    def test_rollback_leaves_foreign_archives_untouched(self):
        """Interrupted-session reconcile must NOT touch archive dirs it
        did not create. A foreign dir in personas-archive/ (valid
        <name>-<stamp> format, stamp >= session id, name in NO planned
        list — e.g. left by phantombot import-persona or a manual
        restore) was previously "restored" into the target (or, if its
        name collided with a planned_created name, discarded into the
        trash and deleted): the rollback consumed an archive it never
        created. It must now be left EXACTLY as found and reported."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, target = tmp / "out", tmp / "target"
            build(au_spec, out)
            target.mkdir()
            # pre-existing dana from a THIRD org
            (target / "dana").mkdir()
            (target / "dana" / ".phantomorg.yaml").write_text(
                "organization_id: tercera-empresa\nactor_id: dana\nrole_id: ceo\n",
                encoding="utf-8",
            )

            from click.testing import CliRunner

            from phantomorg.deploy.session import _archive_stamp

            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                # Fail on the first owned-file write inside 'dana' AFTER
                # its (differing) .phantomorg.yaml was archived per-file.
                if (
                    Path(dst).parent.name == "dana"
                    and Path(dst).name == ".phantomorg.yaml"
                ):
                    raise OSError(f"simulated disk error on {dst}")
                return real_replace(src, dst, **kwargs)

            with unittest.mock.patch(
                "phantomorg.deploy.target.os.replace", side_effect=flaky_replace
            ):
                runner = CliRunner()
                result = runner.invoke(
                    main,
                    [
                        "deploy",
                        "--from",
                        str(out),
                        "--target",
                        str(target),
                        "--yes",
                        "--force",
                    ],
                )
            self.assertEqual(result.exit_code, 1, result.output)
            archive_root = target.parent / "personas-archive"
            self.assertTrue(archive_root.is_dir())

            # Plant a FOREIGN archive: valid format, fresh stamp (>= the
            # session id), name in NO planned list.
            external = archive_root / f"extern-{_archive_stamp()}"
            external.mkdir()
            (external / "MEMORY.md").write_text(
                "foreign archive, not ours\n", encoding="utf-8"
            )

            rb = CliRunner().invoke(
                main, ["rollback", "--target", str(target), "--yes"]
            )
            self.assertEqual(rb.exit_code, 0, rb.output)
            # reported as foreign, left as-is
            self.assertIn("left untouched: extern", rb.output)
            self.assertTrue(external.is_dir(), rb.output)
            # the real archive was restored, the target is exact
            self.assertEqual(
                sorted(p.name for p in target.iterdir()), ["dana"], rb.output
            )
            restored_meta = target / "dana" / ".phantomorg.yaml"
            with open(restored_meta, encoding="utf-8") as f:
                import yaml as _yaml

                meta = _yaml.safe_load(f)
            self.assertEqual(meta.get("organization_id"), "tercera-empresa", rb.output)
            # archive root survives because it holds foreign content
            self.assertTrue(archive_root.is_dir(), rb.output)

    def test_double_archive_same_name_restores_oldest_not_in_session(self):
        """ChatGPT's dedup concern, made concrete: in a deploy-all where
        TWO orgs share an actor id (--force) and that name pre-existed in
        the target, the name is archived TWICE in one session:

          dana-S1 = org A archives the PRE-SESSION version
          dana-S2 = org B later archives org A's freshly deployed version

        The in_progress reconcile must restore ONLY dana-S1 (the
        pre-session version) and discard dana-S2 (in-session artifact).
        Restoring both would clobber: the second restore trashes the
        freshly restored pre-session version and leaves the in-session
        version in the target."""
        au_spec = load_org_yaml(AU_ORG)
        _ALMA_ZOE_ORG_YAML = """
version: 1
organization: {id: otra-empresa, name: "Other Company", sector: pyme, languages: [es]}
departments:
  - {id: direccion, name: "Management", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    access_level: level-3
actors:
  - id: dana
    role: ceo
    tools: [email]
  - id: zoe
    role: ceo
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            other_path = tmp / "otra-empresa.yaml"
            other_path.write_text(_ALMA_ZOE_ORG_YAML, encoding="utf-8")
            other_spec = load_org_yaml(other_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(other_spec, other_out)

            # PRE-EXISTING target with 'dana' from a THIRD org (the
            # pre-session version that must survive the rollback).
            target.mkdir(parents=True)
            (target / "dana").mkdir()
            (target / "dana" / ".phantomorg.yaml").write_text(
                "organization_id: tercera-empresa\nactor_id: dana\nrole_id: ceo\n",
                encoding="utf-8",
            )

            base_dir = tmp / "base"
            au_org_dir = base_dir / "verdant-aquaponics"
            other_org_dir = base_dir / "otra-empresa"
            au_org_dir.mkdir(parents=True)
            other_org_dir.mkdir(parents=True)
            (au_org_dir / "org.yaml").write_text(
                AU_ORG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (other_org_dir / "org.yaml").write_text(
                _ALMA_ZOE_ORG_YAML, encoding="utf-8"
            )
            dist_base = tmp / "dist-base"
            (dist_base / "verdant-aquaponics").mkdir(parents=True)
            (dist_base / "otra-empresa").mkdir(parents=True)
            shutil.copytree(
                au_out, dist_base / "verdant-aquaponics", dirs_exist_ok=True
            )
            shutil.copytree(other_out, dist_base / "otra-empresa", dirs_exist_ok=True)

            from click.testing import CliRunner

            from phantomorg.cli import main

            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                # Fail on "zoe" (sorted last in otra-empresa: dana, zoe)
                # AFTER both orgs archived 'dana'.
                if Path(dst).parent.name == "zoe" and Path(dst).name == "IDENTITY.md":
                    raise OSError(f"simulated disk error on {dst}")
                return real_replace(src, dst, **kwargs)

            with unittest.mock.patch(
                "phantomorg.deploy.target.os.replace", side_effect=flaky_replace
            ):
                runner = CliRunner()
                result = runner.invoke(
                    main,
                    [
                        "deploy-all",
                        "--base",
                        str(base_dir),
                        "--dist-base",
                        str(dist_base),
                        "--target",
                        str(target),
                        "--yes",
                        "--force",
                    ],
                )
            # F3 (cli-tests): mid-mutation failure exits non-zero.
            self.assertEqual(result.exit_code, 1, result.output)
            archive_root = target.parent / "personas-archive"
            alma_archives = sorted(
                p for p in archive_root.iterdir() if p.name.startswith("dana-")
            )
            # org A (AU) archived the pre-session dana, org B archived
            # AU's fresh dana: two archives, two different stamps.
            self.assertEqual(len(alma_archives), 2, result.output)

            runner2 = CliRunner()
            rb = runner2.invoke(main, ["rollback", "--target", str(target), "--yes"])
            self.assertEqual(rb.exit_code, 0, rb.output)
            # The in-session archive (dana-S2) was discarded, the
            # pre-session one restored.
            self.assertIn("discarded", rb.output)
            self.assertIn("dana-", rb.output)
            # The restored 'dana' is the PRE-SESSION version (third
            # org), not AU's in-session version.
            restored_meta = target / "dana" / ".phantomorg.yaml"
            self.assertTrue(restored_meta.exists(), rb.output)
            import yaml as _yaml

            with open(restored_meta, encoding="utf-8") as f:
                meta = _yaml.safe_load(f)
            self.assertEqual(meta.get("organization_id"), "tercera-empresa", rb.output)
            # Archive root was created by the deploy (did not pre-exist)
            # and is fully removed after the rollback.
            self.assertFalse(archive_root.exists(), rb.output)

    def test_double_archive_same_name_committed_restores_oldest(self):
        """The COMMITTED-path mirror of the in_progress dedupe test.

        A deploy-all --force where TWO orgs share an actor id ('dana')
        and that name pre-existed in the target archives it TWICE in one
        committed session:

          dana-S1 = org A archives the PRE-SESSION version
          dana-S2 = org B later archives org A's freshly deployed version

        The committed branch of plan_rollback must restore ONLY dana-S1
        and discard dana-S2 (an in-session artifact). Restoring both in
        recorded order would clobber: the second restore trashes the
        freshly restored pre-session version and the trash is deleted,
        permanently losing it.

        The pre-v0.4.11 code had NO dedupe in the committed branch (the
        in_progress branch was fixed first), so this test failed on the
        old code: rollback restored dana-S2 on top of dana-S1.
        """
        au_spec = load_org_yaml(AU_ORG)
        _ALMA_ZOE_ORG_YAML = """
version: 1
organization: {id: otra-empresa, name: "Other Company", sector: pyme, languages: [es]}
departments:
  - {id: direccion, name: "Management", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    access_level: level-3
actors:
  - id: dana
    role: ceo
    tools: [email]
  - id: zoe
    role: ceo
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            other_path = tmp / "otra-empresa.yaml"
            other_path.write_text(_ALMA_ZOE_ORG_YAML, encoding="utf-8")
            other_spec = load_org_yaml(other_path)

            au_out, other_out, target = (
                tmp / "au_out",
                tmp / "other_out",
                tmp / "target",
            )
            build(au_spec, au_out)
            build(other_spec, other_out)

            # PRE-EXISTING target with 'dana' from a THIRD org (the
            # pre-session version that must survive the rollback).
            target.mkdir(parents=True)
            (target / "dana").mkdir()
            (target / "dana" / ".phantomorg.yaml").write_text(
                "organization_id: tercera-empresa\nactor_id: dana\nrole_id: ceo\n",
                encoding="utf-8",
            )

            base_dir = tmp / "base"
            au_org_dir = base_dir / "verdant-aquaponics"
            other_org_dir = base_dir / "otra-empresa"
            au_org_dir.mkdir(parents=True)
            other_org_dir.mkdir(parents=True)
            (au_org_dir / "org.yaml").write_text(
                AU_ORG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (other_org_dir / "org.yaml").write_text(
                _ALMA_ZOE_ORG_YAML, encoding="utf-8"
            )
            dist_base = tmp / "dist-base"
            (dist_base / "verdant-aquaponics").mkdir(parents=True)
            (dist_base / "otra-empresa").mkdir(parents=True)
            shutil.copytree(
                au_out, dist_base / "verdant-aquaponics", dirs_exist_ok=True
            )
            shutil.copytree(other_out, dist_base / "otra-empresa", dirs_exist_ok=True)

            from click.testing import CliRunner

            from phantomorg.cli import main

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "deploy-all",
                    "--base",
                    str(base_dir),
                    "--dist-base",
                    str(dist_base),
                    "--target",
                    str(target),
                    "--yes",
                    "--force",
                ],
            )
            # Both orgs succeed: the session is COMMITTED, and its
            # recorded 'archived' list contains BOTH dana archives.
            self.assertEqual(result.exit_code, 0, result.output)
            archive_root = target.parent / "personas-archive"
            alma_archives = sorted(
                p for p in archive_root.iterdir() if p.name.startswith("dana-")
            )
            self.assertEqual(len(alma_archives), 2, result.output)

            # Confirm the committed session records BOTH archives.
            import json

            manifest = json.loads(
                (archive_root / ".phantomorg-manifest.json").read_text(encoding="utf-8")
            )
            committed = [
                s for s in manifest["sessions"] if s.get("state") == "committed"
            ]
            self.assertEqual(len(committed), 1, result.output)
            archived_names = [e.get("name") for e in committed[0].get("archived", [])]
            self.assertEqual(archived_names.count("dana"), 2, result.output)

            runner2 = CliRunner()
            rb = runner2.invoke(main, ["rollback", "--target", str(target), "--yes"])
            self.assertEqual(rb.exit_code, 0, rb.output)
            # The in-session archive (dana-S2) was discarded, the
            # pre-session one restored.
            self.assertIn("discarded", rb.output)
            # The restored 'dana' is the PRE-SESSION version (third
            # org), not AU's in-session version.
            restored_meta = target / "dana" / ".phantomorg.yaml"
            self.assertTrue(restored_meta.exists(), rb.output)
            import yaml as _yaml

            with open(restored_meta, encoding="utf-8") as f:
                meta = _yaml.safe_load(f)
            self.assertEqual(meta.get("organization_id"), "tercera-empresa", rb.output)
            # Archive root was created by the deploy (did not pre-exist)
            # and is fully removed after the rollback.
            self.assertFalse(archive_root.exists(), rb.output)


class TestDeployPrune(unittest.TestCase):
    """
    Closes the orphaned-folder gap: if an actor is removed from the spec
    (remove-actor), its folder stayed forever in the deploy target.
    --prune cleans it up, but only for actors of the SAME organization —
    it never touches actors of another one, even if they are not in the
    current build.
    """

    def test_prune_removes_actor_no_longer_in_spec(self):
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"

            build(au_spec, au_out)
            deploy(au_out, target)
            self.assertTrue((target / "elias").exists())

            # We simulate Elias being removed from the spec: we recompile only
            # with the other 4 actors (build --only does not cover "all but
            # one", so we simulate it by deleting her folder from the build output).
            shutil.rmtree(au_out / "elias")

            result = deploy(au_out, target, prune=True)
            self.assertIn("elias", result.pruned)
            # Prune reverts OWNED content but never removes the persona dir
            # (it is runtime state): the metadata is gone and the ORG blocks
            # are stripped, but the dir and its seeds remain.
            self.assertTrue((target / "elias").exists())
            self.assertFalse((target / "elias" / ".phantomorg.yaml").exists())
            self.assertNotIn(
                "ORG:BEGIN",
                (target / "elias" / "SOUL.md").read_text(encoding="utf-8"),
            )
            # the rest stays deployed, untouched
            self.assertTrue((target / "dana").exists())

    def test_prune_never_touches_other_organizations_actors(self):
        au_spec = load_org_yaml(AU_ORG)
        ucg_spec = load_org_yaml(UCG_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, ucg_out, target = tmp / "au_out", tmp / "ucg_out", tmp / "target"

            build(au_spec, au_out)
            build(ucg_spec, ucg_out)
            deploy(au_out, target)
            deploy(ucg_out, target)  # "nadia" is now also in target

            # We recompile AU without "elias", but ucg (nadia) is not touched:
            shutil.rmtree(au_out / "elias")
            result = deploy(au_out, target, prune=True)

            self.assertIn("elias", result.pruned)
            self.assertTrue((target / "nadia").exists())  # never touched

    def test_prune_is_noop_without_flag(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)
            shutil.rmtree(au_out / "elias")

            result = deploy(au_out, target, prune=False)
            self.assertEqual(result.pruned, [])
            self.assertTrue((target / "elias").exists())  # still there


class TestDeployProtectsUnmanagedPersonas(unittest.TestCase):
    """
    Reproduces the real scenario reported: migrating an infrastructure of
    5 agents with hand-written SOULs (never generated by PhantomOrg,
    therefore without .phantomorg.yaml). Before, `po deploy` silently
    overwrote them because `existing_org is None` meant the collision
    condition was never met. Now a target without metadata is treated as
    a collision — it requires explicit --force.
    """

    def test_deploy_blocks_overwriting_handwritten_soul_without_force(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            # We simulate an already-deployed hand-written SOUL, WITHOUT .phantomorg.yaml
            handwritten_dir = target / "dana"
            handwritten_dir.mkdir(parents=True)
            original_content = "# Hand-written SOUL\nDana's unique principles, never generated by PhantomOrg.\n"
            (handwritten_dir / "SOUL.md").write_text(original_content, encoding="utf-8")

            with self.assertRaises(DeployCollisionError):
                deploy(au_out, target, force=False)

            # The hand-written content must remain intact after the blocked attempt
            self.assertEqual(
                (handwritten_dir / "SOUL.md").read_text(encoding="utf-8"),
                original_content,
            )

    def test_deploy_with_force_does_overwrite_handwritten_soul(self):
        # --force remains an explicit escape hatch, on purpose.
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            handwritten_dir = target / "dana"
            handwritten_dir.mkdir(parents=True)
            (handwritten_dir / "SOUL.md").write_text(
                "# Handwritten\n", encoding="utf-8"
            )

            result = deploy(au_out, target, force=True)
            self.assertIn("dana", result.deployed)
            # now it does carry metadata, because it came from the PhantomOrg build
            self.assertTrue((handwritten_dir / ".phantomorg.yaml").exists())

    def test_error_message_explains_it_may_be_handwritten(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            (target / "dana").mkdir(parents=True)
            (target / "dana" / "SOUL.md").write_text("x", encoding="utf-8")

            with self.assertRaises(DeployCollisionError) as ctx:
                deploy(au_out, target, force=False)
            self.assertIn("has NO PhantomOrg metadata", str(ctx.exception))
            self.assertIn("hand-written persona", str(ctx.exception))

    def test_deploying_to_fresh_empty_target_is_unaffected(self):
        # The normal case (empty target) must not be affected by this fix.
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            result = deploy(au_out, target, force=False)
            self.assertEqual(
                set(result.deployed), {"marco", "lucia", "diego", "dana", "elias"}
            )


class TestDeployArchive(unittest.TestCase):
    """Additive deploy: only files PhantomOrg owns are written, in place.
    When an owned file actually changes, its previous version is backed up
    PER-FILE into personas-archive/ (never the whole directory). Prune
    archives the owned files, not the accumulated mind."""

    def test_overwrite_archives_existing_persona_first(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            deploy(au_out, target)  # first deploy: fresh create, nothing to archive
            # Change an owned file (SOUL.md), then redeploy. The change must
            # be INSIDE an ORG block (outside content is preserved from live).
            soul = au_out / "dana" / "SOUL.md"
            soul.write_text(
                soul.read_text(encoding="utf-8").replace(
                    "Seguridad de la información antes que velocidad",
                    "V2: Seguridad de la información antes que velocidad",
                ),
                encoding="utf-8",
            )
            result = deploy(au_out, target)  # second deploy: overwrites SOUL.md

            self.assertIn("dana", result.deployed)
            archive_root = target.parent / "personas-archive"
            self.assertTrue(archive_root.is_dir())
            archived = [d for d in archive_root.iterdir() if d.name.startswith("dana-")]
            self.assertEqual(len(archived), 1)
            # the per-file archive keeps the PREVIOUS SOUL.md (with metadata)
            self.assertTrue((archived[0] / "SOUL.md").exists())
            self.assertFalse((archived[0] / "IDENTITY.md").exists())
            # the live persona is the fresh one
            self.assertTrue((target / "dana" / "SOUL.md").exists())
            self.assertIn("V2: Seguridad", (target / "dana" / "SOUL.md").read_text())
            self.assertEqual(result.archived[0][0], "dana")

    def test_additive_deploy_preserves_runtime_state(self):
        """The core phantomyard requirement: a redeploy must NEVER touch
        runtime-owned files (identity.json, vault.sqlite, accumulated
        MEMORY.md, daily memory, kb notes). Only owned files change."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)

            # Simulate runtime state accumulated in the live persona.
            dana = target / "dana"
            (dana / "identity.json").write_text(
                '{"nsec": "nsec1runtime"}', encoding="utf-8"
            )
            (dana / "vault.sqlite").write_text("encrypted-secrets", encoding="utf-8")
            (dana / "memory" / "2026-08-19.md").write_text(
                "# daily log\n", encoding="utf-8"
            )
            (dana / "kb" / "concepts" / "note.md").write_text(
                "a curated kb note\n", encoding="utf-8"
            )
            mem = dana / "MEMORY.md"
            mem.write_text(mem.read_text() + "\n# Accumulated fact\n", encoding="utf-8")

            # Change an owned file and redeploy (inside an ORG block).
            soul = au_out / "dana" / "SOUL.md"
            soul.write_text(
                soul.read_text().replace(
                    "Seguridad de la información antes que velocidad",
                    "V2: Seguridad de la información antes que velocidad",
                ),
                encoding="utf-8",
            )
            deploy(au_out, target)

            # Runtime state survives untouched.
            self.assertEqual(
                (dana / "identity.json").read_text(encoding="utf-8"),
                '{"nsec": "nsec1runtime"}',
            )
            self.assertEqual(
                (dana / "vault.sqlite").read_text(encoding="utf-8"), "encrypted-secrets"
            )
            self.assertTrue((dana / "memory" / "2026-08-19.md").exists())
            self.assertTrue((dana / "kb" / "concepts" / "note.md").exists())
            self.assertIn("Accumulated fact", (dana / "MEMORY.md").read_text())
            # owned file updated in place
            self.assertIn("V2: Seguridad", (dana / "SOUL.md").read_text())

    def test_archive_name_matches_phantombot_convention(self):
        # phantombot parses "<name>-<YYYY-MM-DDTHH-MM-SS-mmmZ>" (with
        # optional "-N" suffix); PhantomOrg must write exactly that so
        # `phantombot import-persona` can list/restore the archive.
        import re

        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)
            soul = au_out / "dana" / "SOUL.md"
            soul.write_text(
                soul.read_text().replace(
                    "Seguridad de la información antes que velocidad",
                    "V2: Seguridad de la información antes que velocidad",
                ),
                encoding="utf-8",
            )
            deploy(au_out, target)

            archive_root = target.parent / "personas-archive"
            names = [d.name for d in archive_root.iterdir()]
            pattern = re.compile(
                r"^.+-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z(?:-\d+)?$"
            )
            self.assertTrue(any(pattern.match(n) for n in names), names)

    def test_created_archive_dir_is_reported(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)
            soul = au_out / "dana" / "SOUL.md"
            soul.write_text(
                soul.read_text().replace(
                    "Seguridad de la información antes que velocidad",
                    "V2: Seguridad de la información antes que velocidad",
                ),
                encoding="utf-8",
            )
            result = deploy(au_out, target)
            self.assertIn(
                target.parent / "personas-archive", result.created_archive_dirs
            )

    def test_prune_archives_instead_of_deleting(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)
            self.assertTrue((target / "elias").exists())

            # simulate Elias being removed from the spec: her folder
            # disappears from the compiled output
            shutil.rmtree(au_out / "elias")
            result = deploy(au_out, target, prune=True)

            self.assertIn("elias", result.pruned)
            # The persona dir stays (runtime state); the owned SOUL.md was
            # archived (per-file) and its ORG blocks stripped in place.
            self.assertTrue((target / "elias").exists())
            archive_root = target.parent / "personas-archive"
            archived_elena = [
                d for d in archive_root.iterdir() if d.name.startswith("elias-")
            ]
            self.assertEqual(len(archived_elena), 1)  # archived, not deleted
            self.assertTrue((archived_elena[0] / "SOUL.md").exists())
            # The live SOUL.md lost its owned blocks but kept its manual parts.
            self.assertNotIn(
                "ORG:BEGIN",
                (target / "elias" / "SOUL.md").read_text(encoding="utf-8"),
            )

    def test_prune_preserves_runtime_mind(self):
        """Prune reverts the owned markers/plain files but leaves the
        accumulated mind (identity, vault, memory, kb) byte-identical."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)
            elias = target / "elias"
            (elias / "identity.json").write_text(
                '{"nsec": "nsec1elena"}', encoding="utf-8"
            )
            (elias / "vault.sqlite").write_text("secrets", encoding="utf-8")
            (elias / "memory" / "2026-08-19.md").write_text("daily\n", encoding="utf-8")

            shutil.rmtree(au_out / "elias")
            result = deploy(au_out, target, prune=True)

            self.assertIn("elias", result.pruned)
            # owned markers gone, runtime mind preserved
            self.assertNotIn(
                "ORG:BEGIN",
                (elias / "SOUL.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (elias / "identity.json").read_text(), '{"nsec": "nsec1elena"}'
            )
            self.assertEqual((elias / "vault.sqlite").read_text(), "secrets")
            self.assertTrue((elias / "memory" / "2026-08-19.md").exists())

    def test_prune_preserves_runtime_state_byte_for_byte(self):
        """Prune reverts ONLY the PhantomOrg-owned regions: identity, vault,
        memory, KB notes, channel state, and every byte outside the ORG
        markers survive byte-for-byte (the phantomyard prune regression)."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            deploy(au_out, target)

            elias = target / "elias"
            # Runtime state PhantomOrg must never touch.
            (elias / "identity.json").write_text(
                '{"nsec": "nsec1secret"}', encoding="utf-8"
            )
            (elias / "vault.sqlite").write_bytes(b"\x00vault\x01bytes")
            (elias / "state.json").write_text(
                '{"default_persona": "elias"}', encoding="utf-8"
            )
            daily = elias / "memory" / "2026-08-19.md"
            daily.write_text("# daily\ncaptured fact\n", encoding="utf-8")
            kb_note = elias / "kb" / "concepts" / "field-note.md"
            kb_note.parent.mkdir(parents=True, exist_ok=True)
            kb_note.write_text(
                "---\ntype: concept\ntitle: field\n---\n\n# field\n", encoding="utf-8"
            )
            # Manual content OUTSIDE the ORG markers in a merge file.
            soul = elias / "SOUL.md"
            soul.write_text(
                soul.read_text(encoding="utf-8")
                + "\n# Manual note (outside any ORG block)\nkeep me\n",
                encoding="utf-8",
            )

            before = {
                "identity": (elias / "identity.json").read_bytes(),
                "vault": (elias / "vault.sqlite").read_bytes(),
                "state": (elias / "state.json").read_bytes(),
                "daily": daily.read_bytes(),
                "kb_note": kb_note.read_bytes(),
                "soul_manual": b"# Manual note (outside any ORG block)\nkeep me\n",
            }

            shutil.rmtree(au_out / "elias")
            result = deploy(au_out, target, prune=True)
            self.assertIn("elias", result.pruned)

            # Byte-for-byte: every runtime file survives exactly.
            self.assertEqual((elias / "identity.json").read_bytes(), before["identity"])
            self.assertEqual((elias / "vault.sqlite").read_bytes(), before["vault"])
            self.assertEqual((elias / "state.json").read_bytes(), before["state"])
            self.assertEqual(daily.read_bytes(), before["daily"])
            self.assertEqual(kb_note.read_bytes(), before["kb_note"])
            # The manual content outside markers survives in SOUL.md.
            self.assertIn(before["soul_manual"], (elias / "SOUL.md").read_bytes())
            # But the owned ORG blocks are gone.
            self.assertNotIn(
                "ORG:BEGIN",
                (elias / "SOUL.md").read_text(encoding="utf-8"),
            )
            # And the owned metadata is gone.
            self.assertFalse((elias / ".phantomorg.yaml").exists())

    def test_force_adopts_handwritten_preserving_soul(self):
        """--force on a hand-written persona proceeds additively: the
        hand-written SOUL.md (no ORG blocks) is preserved whole (opt-out),
        while the other owned files (.phantomorg.yaml, MEMORY.md, seeds)
        are added. Nothing is destroyed."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            handwritten = target / "dana"
            handwritten.mkdir(parents=True)
            (handwritten / "SOUL.md").write_text(
                "# Handwritten\nprecious content\n", encoding="utf-8"
            )

            result = deploy(au_out, target, force=True)
            self.assertIn("dana", result.deployed)
            # the hand-written SOUL (no ORG markers) is preserved in place
            self.assertEqual(
                (target / "dana" / "SOUL.md").read_text(encoding="utf-8"),
                "# Handwritten\nprecious content\n",
            )
            # the org-owned metadata was added (the persona is now managed)
            self.assertTrue((target / "dana" / ".phantomorg.yaml").exists())
            # nothing was archived (nothing was overwritten)
            archive_root = target.parent / "personas-archive"
            self.assertFalse(
                any(d.name.startswith("dana-") for d in archive_root.iterdir())
                if archive_root.is_dir()
                else False
            )


class TestDeployStagingAtomicity(unittest.TestCase):
    """Additive deploy writes each owned file atomically (tmp + os.replace
    on the FILE). A write failure must leave the target and any per-file
    backup intact."""

    def test_write_failure_leaves_target_and_backup_intact(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            deploy(au_out, target)  # v1 (fresh)
            v1_soul = (target / "dana" / "SOUL.md").read_text(encoding="utf-8")

            # Change an owned file so the redeploy would write it.
            soul = au_out / "dana" / "SOUL.md"
            soul.write_text(
                soul.read_text().replace(
                    "Seguridad de la información antes que velocidad",
                    "V2: Seguridad de la información antes que velocidad",
                ),
                encoding="utf-8",
            )

            # Simulate a mid-write failure: the atomic os.replace of the
            # FILE fails. The target file keeps its old content and its
            # per-file backup was already written (recoverable).

            real_replace = os.replace

            def flaky_replace(src, dst, **kw):
                if Path(dst).name == "SOUL.md" and Path(dst).parent.name == "dana":
                    raise OSError("simulated write failure")
                return real_replace(src, dst, **kw)

            with (
                unittest.mock.patch(
                    "phantomorg.deploy.target.os.replace", side_effect=flaky_replace
                ),
                self.assertRaises(OSError),
            ):
                deploy(au_out, target)

            # the previous version is untouched
            self.assertEqual(
                (target / "dana" / "SOUL.md").read_text(encoding="utf-8"), v1_soul
            )

    def test_deploy_leaves_no_staging_dir_after_success(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            deploy(au_out, target)
            deploy(au_out, target)  # overwrite
            self.assertEqual(
                [p for p in target.iterdir() if p.name.startswith(".pf-staging-")],
                [],
            )

    def test_staging_dir_names_are_unique_per_deploy(self):
        """H1: staging dir names carry a UUID, so two deploys started in
        the same millisecond can never share a staging dir (a timestamp-
        only name could collide, making one deploy clobber the other's
        staging copy)."""
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            seen: set[str] = set()
            import phantomorg.deploy.target as target_mod

            original_staging = target_mod._staging_dir

            def capture_staging_dir(target: Path) -> Path:
                d = original_staging(target)
                seen.add(d.name)
                return d

            with unittest.mock.patch.object(
                target_mod, "_staging_dir", side_effect=capture_staging_dir
            ):
                deploy(au_out, target)
                deploy(au_out, target)

            self.assertEqual(len(seen), 2, "each deploy must use its own staging dir")

    def test_stale_staging_dirs_are_cleaned_but_fresh_ones_kept(self):
        """H1: leftover staging dirs from interrupted deploys are only
        removed when demonstrably stale (> 1h). A fresh dir must be left
        alone: a live deploy in another process may be using it."""
        au_spec = load_org_yaml(AU_ORG)
        import phantomorg.deploy.target as target_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            target.mkdir(parents=True, exist_ok=True)

            stale = target / ".pf-staging-deadbeef"
            fresh = target / ".pf-staging-01234567"
            stale.mkdir()
            fresh.mkdir()
            # force the stale dir into the past
            old = datetime.now(timezone.utc) - timedelta(hours=2)
            os.utime(stale, (old.timestamp(), old.timestamp()))

            with unittest.mock.patch.object(
                target_mod.shutil, "rmtree", wraps=target_mod.shutil.rmtree
            ):
                deploy(au_out, target)

            self.assertFalse(stale.exists(), "stale staging dir must be removed")
            self.assertTrue(fresh.exists(), "fresh staging dir must be kept")

    def test_other_dotdirs_are_never_touched_by_stale_cleanup(self):
        """H1: the stale-staging sweep only ever removes OUR dotfile-
        prefixed staging dirs, never any other directory in the target."""
        import phantomorg.deploy.target as target_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "target"
            target.mkdir()
            other = target / ".pf-something-else"  # same prefix, not a staging dir
            other.mkdir()
            old = datetime.now(timezone.utc) - timedelta(hours=2)
            os.utime(other, (old.timestamp(), old.timestamp()))

            target_mod._cleanup_stale_staging(target)
            self.assertTrue(other.exists())


class TestDeploySymlinkSafety(unittest.TestCase):
    """R5: PhantomOrg never moves or copies through symlinks. A symlink
    in the compiled output, or a target entry that is a symlink, is
    refused with a clear error instead of being followed."""

    def test_deploy_refuses_symlink_inside_compiled_actor(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)

            # plant a symlink inside a compiled actor
            (au_out / "dana" / "evil-link").symlink_to("/etc")

            with self.assertRaises(DeployCollisionError) as ctx:
                deploy(au_out, target)
            self.assertIn("symlink", str(ctx.exception))
            # nothing was deployed
            self.assertEqual(list(target.iterdir()) if target.exists() else [], [])
            self.assertFalse((target.parent / "personas-archive").exists())

    def test_deploy_refuses_symlink_target_entry(self):
        au_spec = load_org_yaml(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out, target = tmp / "au_out", tmp / "target"
            build(au_spec, au_out)
            target.mkdir(parents=True)

            # target/dana is a symlink pointing outside the tree
            outside = tmp / "outside"
            outside.mkdir()
            (target / "dana").symlink_to(outside)

            with self.assertRaises(DeployCollisionError) as ctx:
                deploy(au_out, target, force=True)
            self.assertIn("symlink", str(ctx.exception))
            # the symlink itself is untouched
            self.assertTrue((target / "dana").is_symlink())
            # nothing was archived
            self.assertFalse((target.parent / "personas-archive").exists())

    def test_archive_persona_refuses_symlink(self):
        """archive_persona must refuse a symlink target entry instead of
        moving the link (and later restoring through it)."""
        from phantomorg.deploy.target import archive_persona

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "personas"
            target.mkdir()
            outside = tmp / "outside"
            outside.mkdir()
            (target / "dana").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(DeployCollisionError) as ctx:
                archive_persona(target, "dana")
            self.assertIn("symlink", str(ctx.exception))
            # the symlink is untouched and nothing was archived
            self.assertTrue((target / "dana").is_symlink())
            self.assertFalse((target.parent / "personas-archive").exists())

    def test_archive_persona_refuses_non_directory_file(self):
        """F7: a plain FILE at the target must be refused, not archived
        as a file that rollback can never restore (pre-deploy file
        permanently lost under the compiled dir)."""
        from phantomorg.deploy.target import DeployError, archive_persona

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "personas"
            target.mkdir()
            (target / "dana").write_text(
                "just a file, not a persona dir", encoding="utf-8"
            )

            with self.assertRaises(DeployError) as ctx:
                archive_persona(target, "dana")
            self.assertIn("not a directory", str(ctx.exception))
            # the file is untouched and nothing was archived
            self.assertTrue((target / "dana").is_file())
            self.assertFalse((target.parent / "personas-archive").exists())

    def test_deploy_refuses_symlink_archive_root(self):
        """Audit v0.5.7 #2: a pre-planted symlink at the archive root
        (``personas-archive``) must be refused BEFORE any backup is
        moved — ``mkdir(exist_ok=True)`` happily follows a symlink to
        an existing directory, which would redirect every archive
        outside the expected tree."""
        from phantomorg.deploy.target import archive_persona

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "personas"
            target.mkdir()
            (target / "dana").mkdir()
            (target / "dana" / "SOUL.md").write_text("PRE-dana", encoding="utf-8")

            outside = tmp / "attacker-backups"
            outside.mkdir()
            (tmp / "personas-archive").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(DeployError) as ctx:
                archive_persona(target, "dana")
            self.assertIn("archive root", str(ctx.exception))
            self.assertIn("symlink", str(ctx.exception))
            # nothing moved: the real dana dir is untouched and the
            # external location received no backup
            self.assertTrue((target / "dana" / "SOUL.md").exists())
            self.assertEqual(list(outside.iterdir()), [])


class TestDeployMetaHardening(unittest.TestCase):
    """Adversarial review deploy-2.md F4: malformed `.phantomorg.yaml`
    anywhere (target or build) must never crash a deploy with a raw
    traceback or leave a phantom in_progress journal behind."""

    def _build_au(self, tmp: Path):
        au_spec = load_org_yaml(AU_ORG)
        au_out = tmp / "au_out"
        build(au_spec, au_out)
        return au_out

    def test_malformed_meta_in_unrelated_target_persona_does_not_block_deploy(self):
        """The prune scan reads the meta of EVERY target dir. One
        unrelated persona with a broken meta file must be skipped, not
        crash the whole deploy (and must not leave an in_progress
        journal)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out = self._build_au(tmp)
            target = tmp / "target"
            target.mkdir()

            # An unrelated hand-written persona with a broken meta file
            # (invalid YAML). Without the fix this crashes with a raw
            # yaml.YAMLError traceback during the prune scan.
            unrelated = target / "unrelated"
            unrelated.mkdir()
            (unrelated / ".phantomorg.yaml").write_text(
                "{invalid: [yaml", encoding="utf-8"
            )
            (unrelated / "SOUL.md").write_text("hand-written", encoding="utf-8")

            from click.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(au_out),
                    "--target",
                    str(target),
                    "--prune",
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            # the unrelated persona is untouched (no metadata, never
            # pruned, never archived)
            self.assertTrue((target / "unrelated").is_dir())
            self.assertTrue((target / "marco").is_dir())
            # no phantom in_progress journal
            from phantomorg.deploy.session import load_sessions

            sessions = load_sessions(target.parent / "personas-archive")
            self.assertTrue(sessions)
            self.assertTrue(all(s.get("state") == "committed" for s in sessions))

    def test_non_dict_meta_in_target_does_not_crash(self):
        """A meta file that is valid YAML but NOT a mapping (e.g. a
        list) previously caused AttributeError on .get — must be treated
        as 'no metadata'."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out = self._build_au(tmp)
            target = tmp / "target"
            target.mkdir()

            colliding = target / "dana"
            colliding.mkdir()
            (colliding / ".phantomorg.yaml").write_text(
                "- just\n- a\n- list\n", encoding="utf-8"
            )

            from click.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["deploy", "--from", str(au_out), "--target", str(target), "--yes"],
            )
            # 'dana' collides (existing target without readable
            # metadata): refused cleanly, no traceback.
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertIn("no phantomorg metadata", result.output.lower())
            self.assertIn("no changes were made", result.output.lower())
            # no phantom journal left behind
            self.assertFalse((target.parent / ".phantomorg-manifest.json").exists())

    def test_compiled_actor_without_meta_collides_like_unmanaged(self):
        """deploy.md F4: a compiled actor WITHOUT metadata overwriting an
        existing target entry must be refused unless --force — symmetric
        to the existing-without-metadata case."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out = self._build_au(tmp)
            target = tmp / "target"
            target.mkdir()
            # Pre-existing managed persona from AU (has metadata)
            shutil.copytree(au_out / "dana", target / "dana")

            # Hand-assembled build dir: actor WITHOUT .phantomorg.yaml
            fake_build = tmp / "fake-build"
            fake_build.mkdir()
            actor_dir = fake_build / "dana"
            actor_dir.mkdir()
            (actor_dir / "SOUL.md").write_text("tampered", encoding="utf-8")

            from click.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["deploy", "--from", str(fake_build), "--target", str(target), "--yes"],
            )
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("no phantomorg metadata", result.output.lower())
            self.assertNotIn("Traceback", result.output)
            # the existing persona was NOT overwritten
            self.assertEqual(
                (target / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
                (au_out / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
            )

            # With --force it deploys anyway.
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(fake_build),
                    "--target",
                    str(target),
                    "--force",
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            # Additive --force: the fake build's SOUL.md has no ORG blocks,
            # so the managed persona's SOUL.md is preserved (opt-out); the
            # fake build's metadata is NOT written over the real one.
            self.assertTrue((target / "dana" / "SOUL.md").exists())
            self.assertNotEqual(
                (target / "dana" / "SOUL.md").read_text(encoding="utf-8"),
                "tampered",
            )
            # the managed persona's metadata is preserved
            self.assertEqual(
                (target / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
                (au_out / "dana" / ".phantomorg.yaml").read_text(encoding="utf-8"),
            )


class TestDeployMissingDirs(unittest.TestCase):
    """Adversarial review deploy-2.md F9: missing compiled/base dirs
    must produce a friendly message and exit 1, not a raw traceback."""

    def test_deploy_missing_compiled_dir(self):
        from click.testing import CliRunner

        with tempfile.TemporaryDirectory() as tmp:
            runner = CliRunner()
            target = Path(tmp) / "target"
            result = runner.invoke(
                main,
                [
                    "deploy",
                    "--from",
                    str(Path(tmp) / "no-such-build"),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            # click's own exists=True validation: friendly usage error,
            # never a raw traceback.
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertIn("does not exist", result.output.lower())

    def test_deploy_all_missing_base_and_dist(self):
        from click.testing import CliRunner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            runner = CliRunner()
            target = tmp / "target"
            # missing --base: click does NOT validate it, our own check
            # produces the friendly error (exit 1). dist-base must exist
            # so click's own validation does not fire first.
            dist_base = tmp / "dist"
            dist_base.mkdir()
            result = runner.invoke(
                main,
                [
                    "deploy-all",
                    "--base",
                    str(tmp / "no-base"),
                    "--dist-base",
                    str(dist_base),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertIn("not found", result.output.lower())
            # missing --dist-base: click's own exists=True validation
            # (friendly usage error, exit 2).
            base = tmp / "base"
            base.mkdir()
            result = runner.invoke(
                main,
                [
                    "deploy-all",
                    "--base",
                    str(base),
                    "--dist-base",
                    str(tmp / "no-dist"),
                    "--target",
                    str(target),
                    "--yes",
                ],
            )
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertIn("does not exist", result.output.lower())


class TestDeployCommitSessionFailure(unittest.TestCase):
    """Adversarial review deploy-2.md F5: if the manifest write fails
    AFTER a successful deploy, the CLI must say so explicitly and exit
    1 — not dump a raw traceback claiming the deploy failed."""

    def test_commit_session_oserror_reports_deploy_succeeded(self):
        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            au_out = tmp / "au_out"
            build(au_spec, au_out)
            target = tmp / "target"

            from click.testing import CliRunner

            runner = CliRunner()
            with unittest.mock.patch(
                "phantomorg.cli.commit_session",
                side_effect=OSError("disk full"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "deploy",
                        "--from",
                        str(au_out),
                        "--target",
                        str(target),
                        "--yes",
                    ],
                )
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertIn("SUCCEEDED", result.output)
            self.assertIn("could not be committed", result.output.lower())


class TestDeployAllPlannedPrunedByMetadata(unittest.TestCase):
    """cli-tests F12: deploy-all's journal planned_pruned must match the
    metadata org id (what _preflight actually prunes), not the
    organizations/ folder name. If an org.yaml's organization.id differs
    from its folder name, the old folder-name matching recorded a
    planned_pruned list that diverged from the real prune — and an
    interrupted rollback would then misclassify the pruned actor."""

    def test_planned_pruned_uses_metadata_org_id(self):
        from click.testing import CliRunner

        au_spec = load_org_yaml(AU_ORG)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Org folder named "mapped" but with organization.id
            # "otra-empresa" (deliberate mismatch).
            base_dir = tmp / "base"
            # AU org must live under base/au/org.yaml for deploy-all to see it.
            au_org_dir = base_dir / "au"
            au_org_dir.mkdir(parents=True)
            shutil.copyfile(AU_ORG, au_org_dir / "org.yaml")
            mapped_dir = base_dir / "mapped"
            mapped_dir.mkdir(parents=True)
            mapped_dir.joinpath("org.yaml").write_text(
                """version: 1
organization: {id: otra-empresa, name: "Other Company", sector: pyme, languages: [es]}
departments:
  - {id: direccion, name: "Management", parent: null, access_policy: level-3}
roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    access_level: level-3
actors:
  - id: zoe
    role: ceo
    tools: [email]
policies:
  access_levels:
    level-3: {label: "Executive", categories: [1,2,3]}
  security_categories:
    category-1: {label: "Public"}
escalation_matrix: []
communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
""",
                encoding="utf-8",
            )

            # Deploy AU fully so elias exists in the target with
            # metadata organization_id = verdant-aquaponics.
            au_out = tmp / "au_out"
            build(au_spec, au_out)
            target = tmp / "target"
            deploy(au_out, target)

            # Rebuild AU with elias REMOVED from the compiled output (a
            # stale actor of the same org — the prune candidate).
            shutil.rmtree(au_out / "elias")
            dist_base = tmp / "dist"
            dist_au = dist_base / "au"
            shutil.copytree(au_out, dist_au)
            dist_mapped = dist_base / "mapped"
            mapped_spec = load_org_yaml(mapped_dir / "org.yaml")
            build(mapped_spec, dist_mapped)

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "deploy-all",
                    "--base",
                    str(base_dir),
                    "--dist-base",
                    str(dist_base),
                    "--target",
                    str(target),
                    "--prune",
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)

            # elias WAS actually pruned (metadata org id matches): its owned
            # markers are stripped and .phantomorg.yaml removed, but the
            # persona dir stays (runtime state is never owned/removed).
            self.assertTrue((target / "elias").exists())
            self.assertFalse((target / "elias" / ".phantomorg.yaml").exists())

            # And the durable journal recorded it under planned_pruned,
            # using the metadata org id — the old folder-name matching
            # (org ids {"au", "mapped"}) would have omitted elias.
            manifest = target.parent / "personas-archive" / ".phantomorg-manifest.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            session = doc["sessions"][-1]
            self.assertIn("elias", session.get("planned_pruned", []))


class TestDeployTargetSymlink(unittest.TestCase):
    """C1 (adversarial review v0.5.5): the deploy target root must not be
    a symlink — writing through one redirects the whole deployment to an
    arbitrary directory."""

    def test_deploy_refuses_symlink_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real"
            real.mkdir()
            link = base / "personas-link"
            link.symlink_to(real, target_is_directory=True)

            out = _build_au_module(base)
            with self.assertRaises(DeployError):
                deploy(out, target_dir=link)
            # Nothing was written through the link.
            self.assertEqual(list(real.iterdir()), [])

    def test_deploy_accepts_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "personas"
            out = _build_au_module(base)
            result = deploy(out, target_dir=target)
            self.assertTrue((target / "dana").is_dir())
            self.assertIn("dana", result.deployed)


if __name__ == "__main__":
    unittest.main()
