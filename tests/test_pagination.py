import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db


def _paper(arxiv_id: str, title: str, rank: int, stars: int = 0) -> dict:
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": [],
        "abstract": "",
        "subjects": [],
        "published_at": "2026-08-01",
        "rank": rank,
        "reading_stars": stars,
    }


class PaperPaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "papers.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()

    def test_page_deduplicates_before_limit_and_preserves_memberships(self):
        shared_id = db.upsert_paper(_paper("2608.00001", "Shared", 1, 9), "cs.AI", "2026-08-01")
        db.upsert_paper(_paper("2608.00001", "Shared", 4, 5), "cs.LG", "2026-08-01")
        db.upsert_paper(_paper("2608.00002", "Second", 2), "cs.AI", "2026-08-01")
        db.upsert_paper(_paper("2608.00003", "Third", 3), "cs.AI", "2026-08-01")

        first = db.list_paper_page(crawl_date="2026-08-01", page=1, page_size=1)
        second = db.list_paper_page(crawl_date="2026-08-01", page=2, page_size=1)

        self.assertEqual(first["total"], 3)
        self.assertEqual(first["items"][0]["id"], shared_id)
        self.assertEqual(
            {item["category"] for item in first["items"][0]["category_memberships"]},
            {"cs.AI", "cs.LG"},
        )
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertTrue(first["has_next"])
        self.assertTrue(second["has_previous"])

    def test_category_and_attention_are_filtered_before_pagination(self):
        ai_id = db.upsert_paper(_paper("2608.00011", "AI", 1), "cs.AI", "2026-08-01")
        db.upsert_paper(_paper("2608.00011", "AI", 5), "cs.LG", "2026-08-01")
        other_id = db.upsert_paper(_paper("2608.00012", "Other", 2), "cs.LG", "2026-08-01")
        db.create_evaluation(
            ai_id, "abstract_review", None, None, None, "model", "success",
            {"score": 90, "attention": "read"}, "{}", None,
        )
        db.create_evaluation(
            other_id, "abstract_review", None, None, None, "model", "success",
            {"score": 10, "attention": "ignore"}, "{}", None,
        )

        page = db.list_paper_page(
            crawl_date="2026-08-01",
            category="cs.AI",
            attention="read",
            page_size=1,
        )

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], ai_id)
        self.assertEqual(
            {item["category"] for item in page["items"][0]["category_memberships"]},
            {"cs.AI", "cs.LG"},
        )

    def test_page_size_is_bounded_and_empty_late_page_keeps_total(self):
        db.upsert_paper(_paper("2608.00021", "Only", 1), "cs.AI", "2026-08-01")

        page = db.list_paper_page(crawl_date="2026-08-01", page=99, page_size=999)

        self.assertEqual(page["page_size"], 100)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"], [])


if __name__ == "__main__":
    unittest.main()
