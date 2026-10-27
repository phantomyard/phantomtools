import unittest
from copy import deepcopy

from fixtures import adapter

from phantomblog.review import compare


class ReviewTests(unittest.TestCase):
    def test_titles_are_named_by_article_and_language(self):
        before = {
            "articles": [
                {"slug": "fixture", "en": {"title": "Old"}, "es": {"title": "Anterior"}}
            ]
        }
        after = deepcopy(before)
        after["articles"][0]["es"]["title"] = "Revisión en español"
        self.assertEqual(
            compare(before, after),
            [
                {
                    "group": "Article: fixture",
                    "field": "Spanish / Title",
                    "kind": "changed",
                    "beforePresent": True,
                    "afterPresent": True,
                    "before": "Anterior",
                    "after": "Revisión en español",
                }
            ],
        )
        self.assertEqual(before["articles"][0]["es"]["title"], "Anterior")

    def test_every_type_of_change_and_source_order_is_visible(self):
        before = {
            "articles": [{"slug": "a"}, {"slug": "b"}],
            "site": {"x": None},
            "theme": {"sections": ["body", "lead"]},
        }
        after = {
            "articles": [{"slug": "b"}, {"slug": "a"}],
            "site": {"added": '<img onerror="bad">'},
            "theme": {"sections": ["lead", "body"]},
        }
        changes = compare(before, after)
        self.assertEqual(len(changes), 4)
        self.assertEqual({c["kind"] for c in changes}, {"added", "removed", "changed"})
        removed = next(c for c in changes if c["kind"] == "removed")
        self.assertTrue(removed["beforePresent"])
        self.assertIsNone(removed["before"])
        self.assertFalse(removed["afterPresent"])
        self.assertEqual(changes[0]["group"], "Articles")

    def test_added_removed_records_and_connection_changes(self):
        before = {"articles": [{"slug": "old"}], "connections": {"fixture": adapter()}}
        after = deepcopy(before)
        after["articles"] = [{"slug": "new"}]
        after["connections"]["fixture"]["server"] = "new-server"
        changes = compare(before, after)
        self.assertEqual(
            {c["group"] for c in changes},
            {"Article: old", "Article: new", "Connection: fixture"},
        )
        self.assertEqual(compare(after, after), [])
        self.assertEqual(len(compare({"value": 1}, {"value": True})), 1)
