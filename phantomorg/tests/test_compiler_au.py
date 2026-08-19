import datetime
import os
import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"
EXPECTED_ACTORS = {"marco", "lucia", "diego", "dana", "elias"}
# build() also reports the org-level derived artifact under __scopes__ and
# per-actor structured warnings under __warnings__ (empty when none).
EXPECTED_BUILD_KEYS = EXPECTED_ACTORS | {"__scopes__", "__humans__", "__warnings__"}


class TestCompilerAU(unittest.TestCase):
    def test_au_is_valid(self):
        _, result = validate_org(AU_ORG)
        self.assertTrue(result.ok, result.errors)

    def test_build_generates_expected_files(self):
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir)
            self.assertEqual(set(written.keys()), EXPECTED_BUILD_KEYS)

            for actor_id in EXPECTED_ACTORS:
                actor_dir = out_dir / actor_id
                self.assertTrue((actor_dir / "IDENTITY.md").exists())
                self.assertTrue((actor_dir / "SOUL.md").exists())
                self.assertTrue((actor_dir / "tools.md").exists())
                self.assertTrue((actor_dir / "MEMORY.md").exists())
                # Phantombot-shaped scaffold: memory drawers are FILES, the
                # kb category dirs + seed files exist.
                self.assertTrue((actor_dir / "memory" / "archive").is_dir())
                self.assertTrue((actor_dir / "memory" / "people.md").exists())
                self.assertTrue((actor_dir / "memory" / "decisions.md").exists())
                self.assertTrue((actor_dir / "memory" / "lessons.md").exists())
                self.assertTrue((actor_dir / "memory" / "commitments.md").exists())
                self.assertTrue((actor_dir / "memory" / "norms.md").exists())
                self.assertTrue((actor_dir / "kb" / "runbooks").is_dir())
                self.assertTrue((actor_dir / "kb" / "Home.md").exists())
                self.assertTrue(
                    (actor_dir / "kb" / "templates" / "concept.md").exists()
                )

    def test_build_generates_humans_registry(self):
        """The org-wide HUMANS.md registry is written when the org
        declares a ``humans:`` block; absent otherwise."""
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir)
            humans_path = out_dir / "HUMANS.md"
            self.assertTrue(humans_path.exists())
            self.assertIn("__humans__", written)
            content = humans_path.read_text(encoding="utf-8")
            self.assertIn("Human Registry — Verdant Aquaponics Co-op", content)
            for human_id in ("mar", "julia", "leo", "mirta"):
                self.assertIn(f"`{human_id}`", content)
            # telegram ids rendered
            self.assertIn("1000000001", content)
            self.assertIn("1000000002", content)

    def test_build_without_humans_writes_no_registry(self):
        """An org without a humans block builds no HUMANS.md."""

        import yaml

        from phantomorg.spec.model import OrgSpec
        from phantomorg.spec.shape_validator import validate_shape

        with open(AU_ORG, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        doc.pop("humans", None)
        validate_shape(doc)
        spec = OrgSpec.from_dict(doc)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir)
            self.assertNotIn("__humans__", written)
            self.assertFalse((out_dir / "HUMANS.md").exists())

    def test_build_refuses_symlinked_actor_dir(self):
        """H2 (adversarial review v0.5.5): an actor output directory that
        is a symlink must be refused, even when the link points INSIDE
        the output tree (resolve() alone only catches links escaping it)."""
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            # Plant out/dana -> out/lucia: resolve() keeps it "inside" the
            # tree, but alice's files would land in lucia's directory.
            (out_dir / "lucia").mkdir()
            (out_dir / "dana").symlink_to(out_dir / "lucia", target_is_directory=True)
            with self.assertRaises(ValueError):
                build(spec, out_dir)

    def test_build_refuses_symlink_escaping_tree(self):
        """H2: the existing resolve()-based containment check still
        refuses a link that escapes the output tree."""
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (out_dir / "dana").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                build(spec, out_dir)

    def test_scaffold_seeds_are_never_overwritten(self):
        """
        Seed files (drawers, kb/Home.md, templates) must survive a rebuild
        with user edits: they are written only if missing, like phantombot's
        own persona scaffold.
        """
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")
            drawer = out_dir / "dana" / "memory" / "people.md"
            self.assertTrue(drawer.exists())
            drawer.write_text("# People\n\n## (custom entries)\n", encoding="utf-8")

            # Second build must not touch the edited drawer.
            written = build(spec, out_dir, only="dana")
            self.assertNotIn(
                "dana/memory/people.md",
                [str(p.relative_to(out_dir)) for p in written["dana"]],
            )
            self.assertEqual(
                drawer.read_text(encoding="utf-8"),
                "# People\n\n## (custom entries)\n",
            )

    def test_build_is_idempotent(self):
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = build(spec, out_dir)
            file_keys = [k for k in first if k not in ("__scopes__", "__warnings__")]
            self.assertTrue(
                all(len(files) > 0 for k, files in first.items() if k in file_keys)
            )

            second = build(spec, out_dir)
            self.assertTrue(
                all(len(files) == 0 for k, files in second.items() if k in file_keys)
            )

    def test_category_0_exception_only_on_paco(self):
        spec, _ = validate_org(AU_ORG)
        marco = spec.actor_by_id("marco")
        lucia = spec.actor_by_id("lucia")
        self.assertIn("category-0", marco.actor_exceptions)
        self.assertNotIn("category-0", lucia.actor_exceptions)

    def test_training_lead_role_has_category_3(self):
        spec, _ = validate_org(AU_ORG)
        role = spec.role_by_id("training_lead")
        self.assertIn("category-3", role.security_exceptions)

    def test_manual_note_outside_blocks_is_preserved_but_blocks_regenerate(self):
        """
        The real fix: a manual note OUTSIDE the ORG blocks survives a
        regeneration, and at the same time the sections derived from
        org.yaml (e.g. security) DO update when the spec changes. Before,
        marking any part of the file as manual froze the whole file.
        """
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")

            soul_path = out_dir / "dana" / "SOUL.md"
            original = soul_path.read_text(encoding="utf-8")
            self.assertIn("<!-- ORG:BEGIN security -->", original)

            # We add a manual note OUTSIDE any block.
            manual_note = (
                "\n## Personal note\nDana prefers a direct tone with Operations.\n"
            )
            edited = original + manual_note
            soul_path.write_text(edited, encoding="utf-8")

            # We change the spec: we give project_lead a new security
            # exception (equivalent to what actually happened with
            # Elias/Category 3 in the original audit).
            role = spec.role_by_id("project_lead")
            role.security_exceptions.append("category-2")

            build(spec, out_dir, only="dana")
            rebuilt = soul_path.read_text(encoding="utf-8")

            # The manual note is still there...
            self.assertIn("Dana prefers a direct tone with Operations.", rebuilt)
            # ...and the security block did update with the spec change.
            self.assertIn("category-2", rebuilt)

    def test_file_without_any_org_block_is_left_untouched(self):
        """Deliberate opt-out: if no ORG marker remains, nothing is touched.

        The build now warns (F5) that spec changes are not applied to a
        marker-less file that differs from the fresh render.
        """
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")

            soul_path = out_dir / "dana" / "SOUL.md"
            fully_manual = "# Completely rewritten by hand, without markers.\n"
            soul_path.write_text(fully_manual, encoding="utf-8")

            with self.assertWarns(UserWarning):
                written = build(spec, out_dir, only="dana")
            self.assertNotIn(soul_path, written["dana"])
            self.assertEqual(soul_path.read_text(encoding="utf-8"), fully_manual)

    def test_memory_md_is_never_regenerated_after_creation(self):
        """
        MEMORY.md accumulates facts during real operation (the runtime
        writes there). A later build must not touch it, with or without
        ORG blocks, even if the spec changes.
        """
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")

            memory_path = out_dir / "dana" / "MEMORY.md"
            runtime_written_content = "# Memory\n\n- Fact accumulated in production: client X prefers email.\n"
            memory_path.write_text(runtime_written_content, encoding="utf-8")

            written = build(spec, out_dir, only="dana")
            self.assertNotIn(memory_path, written["dana"])
            self.assertEqual(
                memory_path.read_text(encoding="utf-8"), runtime_written_content
            )

    def test_build_cleans_stale_mkstemp_leftovers(self):
        """C (crash-point audit v0.5.6): SIGKILL between mkstemp and
        os.replace leaves `.name.XXXXXX` garbage in the output tree.
        Build must sweep stale ones (older than 1h) without touching
        fresh temps or real files."""
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")

            # Plant a stale mkstemp leftover (old mtime) next to a fresh
            # one (recent mtime) and a lookalike real file.
            stale = out_dir / "dana" / ".SOUL.md.abc123"
            stale.write_text("partial", encoding="utf-8")
            old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                hours=2
            )
            os.utime(stale, (old.timestamp(), old.timestamp()))

            fresh = out_dir / "dana" / ".SOUL.md.zzz999"
            fresh.write_text("live writer", encoding="utf-8")

            lookalike = out_dir / "dana" / ".SOUL.md.backup"
            lookalike.write_text("real file", encoding="utf-8")

            build(spec, out_dir, only="dana")

            self.assertFalse(stale.exists(), "stale tmp must be swept")
            self.assertTrue(fresh.exists(), "fresh tmp must survive")
            self.assertTrue(lookalike.exists(), "lookalike real file must survive")


class TestNpubBuildWarnings(unittest.TestCase):
    """build() reports structured no-npub warnings per actor (step 3).

    The real AU org now declares npubs for every actor, so these tests
    strip them (or some of them) from a temp copy to exercise the
    warning paths.
    """

    def _strip_npubs(self, keep: set[str] | None = None):
        import yaml

        keep = keep or set()
        raw = yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
        for actor in raw["actors"]:
            if actor["id"] not in keep:
                actor.pop("npub", None)
        with tempfile.TemporaryDirectory() as tmp:
            org_path = Path(tmp) / "org.yaml"
            org_path.write_text(
                yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8"
            )
            spec, result = validate_org(org_path)
            self.assertTrue(result.ok, result.errors)
            return spec

    def test_au_without_npubs_warns_for_every_actor(self):
        spec = self._strip_npubs()
        with tempfile.TemporaryDirectory() as tmp:
            written = build(spec, Path(tmp))
        warnings_out = written.get("__warnings__", [])
        self.assertEqual(len(warnings_out), 5)
        self.assertEqual(
            {w["actor"] for w in warnings_out},
            {"marco", "lucia", "diego", "dana", "elias"},
        )
        for w in warnings_out:
            self.assertEqual(w["code"], "no-npub")
            self.assertIn("phantomchat", w["message"])

    def test_actor_with_npub_does_not_warn(self):
        spec = self._strip_npubs(keep={"marco"})
        with tempfile.TemporaryDirectory() as tmp:
            written = build(spec, Path(tmp))
        warnings_out = written.get("__warnings__", [])
        self.assertEqual(len(warnings_out), 4)
        warned = {w["actor"] for w in warnings_out}
        self.assertNotIn("marco", warned)
        self.assertEqual(warned, {"lucia", "diego", "dana", "elias"})

    def test_only_build_warns_for_that_actor(self):
        spec = self._strip_npubs()
        with tempfile.TemporaryDirectory() as tmp:
            written = build(spec, Path(tmp), only="dana")
        warnings_out = written.get("__warnings__", [])
        self.assertEqual(len(warnings_out), 1)
        self.assertEqual(warnings_out[0]["actor"], "dana")
        self.assertEqual(warnings_out[0]["code"], "no-npub")


if __name__ == "__main__":
    unittest.main()
