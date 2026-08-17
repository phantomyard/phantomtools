import unittest

from phantomforge.compiler.blocks import extract_blocks, merge_content


class TestBlocks(unittest.TestCase):
    def test_extract_blocks_finds_named_sections(self):
        content = (
            "# Title\n"
            "<!-- FORJA:BEGIN security -->\nlevel 3\n<!-- FORJA:END security -->\n"
            "<!-- FORJA:BEGIN escalation -->\nescalates to X\n<!-- FORJA:END escalation -->\n"
        )
        blocks = extract_blocks(content)
        self.assertEqual(set(blocks.keys()), {"security", "escalation"})
        self.assertIn("level 3", blocks["security"])

    def test_merge_regenerates_inside_blocks_preserves_outside(self):
        existing = (
            "# Title\n"
            "<!-- FORJA:BEGIN security -->\nlevel 2 (old)\n<!-- FORJA:END security -->\n"
            "\n## Manual note\nThis was written by a person.\n"
        )
        new = (
            "# Title\n"
            "<!-- FORJA:BEGIN security -->\nlevel 3 (new)\n<!-- FORJA:END security -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("level 3 (new)", merged)
        self.assertNotIn("level 2 (old)", merged)
        self.assertIn("This was written by a person.", merged)

    def test_merge_adds_new_block_not_present_before(self):
        existing = (
            "# Title\n<!-- FORJA:BEGIN security -->\nA\n<!-- FORJA:END security -->\n"
        )
        new = (
            "# Title\n<!-- FORJA:BEGIN security -->\nA\n<!-- FORJA:END security -->\n"
            "<!-- FORJA:BEGIN escalation -->\nB\n<!-- FORJA:END escalation -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("FORJA:BEGIN escalation", merged)
        self.assertIn("B", merged)

    def test_merge_without_any_block_preserves_everything(self):
        existing = "Fully manual content, without markers.\n"
        new = "<!-- FORJA:BEGIN security -->\nnew\n<!-- FORJA:END security -->\n"
        merged = merge_content(existing, new)
        self.assertEqual(merged, existing)

    # F4: a manual annotation quoting a well-formed FORJA pair must not
    # be destroyed by the merge.
    def test_merge_preserves_manual_annotation_with_fake_pair(self):
        existing = (
            "## Manual note\n"
            "<!-- FORJA:BEGIN security -->\n"
            "fake body (manual annotation)\n"
            "<!-- FORJA:END security -->\n"
            "<!-- FORJA:BEGIN security -->\n"
            "level 2 (real, generated)\n"
            "<!-- FORJA:END security -->\n"
        )
        new = (
            "<!-- FORJA:BEGIN security -->\n"
            "level 3 (new)\n"
            "<!-- FORJA:END security -->\n"
        )
        with self.assertWarns(UserWarning):
            merged = merge_content(existing, new)
        self.assertEqual(merged, existing)
        self.assertIn("fake body (manual annotation)", merged)
        self.assertNotIn("level 3 (new)", merged)

    # F5: CRLF line endings must not freeze the file (no blocks matched).
    def test_merge_handles_crlf_existing_file(self):
        existing = (
            "# Title\r\n"
            "<!-- FORJA:BEGIN security -->\r\n"
            "level 2 (old)\r\n"
            "<!-- FORJA:END security -->\r\n"
            "\r\n## Manual note\r\n"
            "This was written by a person.\r\n"
        )
        new = (
            "# Title\n"
            "<!-- FORJA:BEGIN security -->\n"
            "level 3 (new)\n"
            "<!-- FORJA:END security -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("level 3 (new)", merged)
        self.assertNotIn("level 2 (old)", merged)
        self.assertIn("This was written by a person.", merged)

    def test_extract_blocks_handles_crlf(self):
        content = (
            "<!-- FORJA:BEGIN security -->\r\n"
            "level 3\r\n"
            "<!-- FORJA:END security -->\r\n"
        )
        blocks = extract_blocks(content)
        self.assertEqual(set(blocks.keys()), {"security"})
        self.assertIn("level 3", blocks["security"])

    # F6: a literal END marker inside a generated body must not truncate
    # the block (extract by the last END, not the first).
    def test_merge_keeps_full_generated_body_with_literal_end(self):
        existing = (
            "# Title\n<!-- FORJA:BEGIN tools -->\nold\n<!-- FORJA:END tools -->\n"
        )
        new = (
            "# Title\n"
            "<!-- FORJA:BEGIN tools -->\n"
            "- tool1\n"
            "docs say: <!-- FORJA:END tools --> is the marker\n"
            "- tool2 (after fake end)\n"
            "<!-- FORJA:END tools -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("- tool1", merged)
        self.assertIn("- tool2 (after fake end)", merged)
        self.assertNotIn("old", merged)

    def test_merge_removes_block_that_no_longer_exists_in_template(self):
        existing = (
            "# Title\n"
            "<!-- FORJA:BEGIN security -->\n"
            "A\n"
            "<!-- FORJA:END security -->\n"
            "\nKeep me.\n"
        )
        new = "# Title\n"
        merged = merge_content(existing, new)
        self.assertNotIn("FORJA:BEGIN security", merged)
        self.assertNotIn("A", merged)
        self.assertIn("Keep me.", merged)

    def test_merge_preserves_manual_annotation_with_fake_pair_single_line(self):
        # A single-line fake pair is a well-formed pair inside a manual
        # note on one line: still ambiguous -> preserve whole + warn.
        existing = (
            "<!-- FORJA:BEGIN security -->\n"
            "level 2 (real, generated)\n"
            "<!-- FORJA:END security -->\n"
            "Note: <!-- FORJA:BEGIN security -->fake<!-- FORJA:END security -->\n"
        )
        new = (
            "<!-- FORJA:BEGIN security -->\n"
            "level 3 (new)\n"
            "<!-- FORJA:END security -->\n"
        )
        with self.assertWarns(UserWarning):
            merged = merge_content(existing, new)
        self.assertEqual(merged, existing)


if __name__ == "__main__":
    unittest.main()
