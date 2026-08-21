import unittest

from phantomorg.compiler.blocks import extract_blocks, merge_content


class TestBlocks(unittest.TestCase):
    def test_extract_blocks_finds_named_sections(self):
        content = (
            "# Title\n"
            "<!-- ORG:BEGIN security -->\nlevel 3\n<!-- ORG:END security -->\n"
            "<!-- ORG:BEGIN escalation -->\nescalates to X\n<!-- ORG:END escalation -->\n"
        )
        blocks = extract_blocks(content)
        self.assertEqual(set(blocks.keys()), {"security", "escalation"})
        self.assertIn("level 3", blocks["security"])

    def test_merge_regenerates_inside_blocks_preserves_outside(self):
        existing = (
            "# Title\n"
            "<!-- ORG:BEGIN security -->\nlevel 2 (old)\n<!-- ORG:END security -->\n"
            "\n## Manual note\nThis was written by a person.\n"
        )
        new = (
            "# Title\n"
            "<!-- ORG:BEGIN security -->\nlevel 3 (new)\n<!-- ORG:END security -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("level 3 (new)", merged)
        self.assertNotIn("level 2 (old)", merged)
        self.assertIn("This was written by a person.", merged)

    def test_merge_adds_new_block_not_present_before(self):
        existing = (
            "# Title\n<!-- ORG:BEGIN security -->\nA\n<!-- ORG:END security -->\n"
        )
        new = (
            "# Title\n<!-- ORG:BEGIN security -->\nA\n<!-- ORG:END security -->\n"
            "<!-- ORG:BEGIN escalation -->\nB\n<!-- ORG:END escalation -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("ORG:BEGIN escalation", merged)
        self.assertIn("B", merged)

    def test_merge_without_any_block_preserves_everything(self):
        existing = "Fully manual content, without markers.\n"
        new = "<!-- ORG:BEGIN security -->\nnew\n<!-- ORG:END security -->\n"
        merged = merge_content(existing, new)
        self.assertEqual(merged, existing)

    # F4: a manual annotation quoting a well-formed ORG pair must not
    # be destroyed by the merge.
    def test_merge_preserves_manual_annotation_with_fake_pair(self):
        existing = (
            "## Manual note\n"
            "<!-- ORG:BEGIN security -->\n"
            "fake body (manual annotation)\n"
            "<!-- ORG:END security -->\n"
            "<!-- ORG:BEGIN security -->\n"
            "level 2 (real, generated)\n"
            "<!-- ORG:END security -->\n"
        )
        new = "<!-- ORG:BEGIN security -->\nlevel 3 (new)\n<!-- ORG:END security -->\n"
        with self.assertWarns(UserWarning):
            merged = merge_content(existing, new)
        self.assertEqual(merged, existing)
        self.assertIn("fake body (manual annotation)", merged)
        self.assertNotIn("level 3 (new)", merged)

    # F5: CRLF line endings must not freeze the file (no blocks matched).
    def test_merge_handles_crlf_existing_file(self):
        existing = (
            "# Title\r\n"
            "<!-- ORG:BEGIN security -->\r\n"
            "level 2 (old)\r\n"
            "<!-- ORG:END security -->\r\n"
            "\r\n## Manual note\r\n"
            "This was written by a person.\r\n"
        )
        new = (
            "# Title\n"
            "<!-- ORG:BEGIN security -->\n"
            "level 3 (new)\n"
            "<!-- ORG:END security -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("level 3 (new)", merged)
        self.assertNotIn("level 2 (old)", merged)
        self.assertIn("This was written by a person.", merged)

    def test_extract_blocks_handles_crlf(self):
        content = (
            "<!-- ORG:BEGIN security -->\r\nlevel 3\r\n<!-- ORG:END security -->\r\n"
        )
        blocks = extract_blocks(content)
        self.assertEqual(set(blocks.keys()), {"security"})
        self.assertIn("level 3", blocks["security"])

    # F6: a literal END marker inside a generated body must not truncate
    # the block (extract by the last END, not the first).
    def test_merge_keeps_full_generated_body_with_literal_end(self):
        existing = "# Title\n<!-- ORG:BEGIN tools -->\nold\n<!-- ORG:END tools -->\n"
        new = (
            "# Title\n"
            "<!-- ORG:BEGIN tools -->\n"
            "- tool1\n"
            "docs say: <!-- ORG:END tools --> is the marker\n"
            "- tool2 (after fake end)\n"
            "<!-- ORG:END tools -->\n"
        )
        merged = merge_content(existing, new)
        self.assertIn("- tool1", merged)
        self.assertIn("- tool2 (after fake end)", merged)
        self.assertNotIn("old", merged)

    def test_merge_removes_block_that_no_longer_exists_in_template(self):
        existing = (
            "# Title\n"
            "<!-- ORG:BEGIN security -->\n"
            "A\n"
            "<!-- ORG:END security -->\n"
            "\nKeep me.\n"
        )
        new = "# Title\n"
        merged = merge_content(existing, new)
        self.assertNotIn("ORG:BEGIN security", merged)
        self.assertNotIn("A", merged)
        self.assertIn("Keep me.", merged)

    def test_merge_preserves_manual_annotation_with_fake_pair_single_line(self):
        # A single-line fake pair is a well-formed pair inside a manual
        # note on one line: still ambiguous -> preserve whole + warn.
        existing = (
            "<!-- ORG:BEGIN security -->\n"
            "level 2 (real, generated)\n"
            "<!-- ORG:END security -->\n"
            "Note: <!-- ORG:BEGIN security -->fake<!-- ORG:END security -->\n"
        )
        new = "<!-- ORG:BEGIN security -->\nlevel 3 (new)\n<!-- ORG:END security -->\n"
        with self.assertWarns(UserWarning):
            merged = merge_content(existing, new)
        self.assertEqual(merged, existing)


if __name__ == "__main__":
    unittest.main()
