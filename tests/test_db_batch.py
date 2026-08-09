import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db


def _paper(index: int, title: str | None = None) -> dict:
    return {
        "arxiv_id": f"2608.{index:05d}",
        "title": title or f"Paper {index}",
        "authors": ["Author"],
        "abstract": "Abstract",
        "subjects": ["cs.AI"],
        "rank": index,
    }


class DatabaseBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "batch.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()

    def test_upsert_papers_queries_ids_once_per_chunk(self):
        statements: list[str] = []
        connection = db.connect()
        connection.set_trace_callback(statements.append)
        with patch.object(db, "connect", return_value=connection):
            ids = db.upsert_papers([_paper(index) for index in range(1, 101)], "cs.AI", "2026-08-01")

        id_queries = [sql for sql in statements if "SELECT id, arxiv_id FROM papers" in sql]
        self.assertEqual(len(ids), 100)
        self.assertEqual(len(id_queries), 1)

    def test_duplicate_arxiv_keeps_return_order_and_last_value(self):
        ids = db.upsert_papers(
            [_paper(1, "First"), _paper(1, "Last")],
            "cs.AI",
            "2026-08-01",
        )

        self.assertEqual(ids[0], ids[1])
        self.assertEqual(db.get_paper(ids[0])["title"], "Last")
        with db.connect() as conn:
            membership_count = conn.execute("SELECT COUNT(*) AS n FROM paper_categories").fetchone()["n"]
        self.assertEqual(membership_count, 1)

    def test_serialization_failure_does_not_partially_write(self):
        bad = _paper(2)
        bad["authors"] = {object()}

        with self.assertRaises(TypeError):
            db.upsert_papers([_paper(1), bad], "cs.AI", "2026-08-01")

        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"], 0)

    def test_connection_has_busy_timeout_and_initialized_journal(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertIn(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), {"wal", "delete"})


if __name__ == "__main__":
    unittest.main()
