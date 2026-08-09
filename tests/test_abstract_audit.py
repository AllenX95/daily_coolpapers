import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import abstract_audit, db


DIRTY_ABSTRACT = "Ada Lovelace Alan Turing A clean abstract sentence. cs.AI cs.LG"


def _paper(authors=None, subjects=None) -> dict:
    return {
        "id": 1,
        "arxiv_id": "2608.12345v2",
        "title": "Structured metadata",
        "abstract": DIRTY_ABSTRACT,
        "authors_list": authors or [],
        "subjects_list": subjects or [],
    }


class AbstractAuditTests(unittest.TestCase):
    def test_local_scan_reports_structured_prefix_and_suffix(self):
        findings = abstract_audit.scan_abstracts(
            [_paper(["Ada Lovelace", "Alan Turing"], ["cs.AI", "cs.LG"])]
        )

        self.assertEqual(len(findings), 1)
        self.assertIn("author_prefix", findings[0].signals)
        self.assertIn("subject_suffix", findings[0].signals)
        self.assertIsNone(findings[0].proposed_abstract)

    def test_subject_code_tail_finds_rows_with_empty_metadata_columns(self):
        findings = abstract_audit.scan_abstracts([_paper()])

        self.assertEqual(len(findings), 1)
        self.assertIn("subject_code_tail", findings[0].signals)

    def test_arxiv_verification_supplies_authoritative_dry_run_proposal(self):
        atom = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2608.12345v3</id>
            <summary>A clean abstract sentence.</summary>
          </entry>
        </feed>"""

        class FakeResponse:
            text = atom

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.closed = False

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

            def close(self):
                self.closed = True

        client = FakeClient()
        canonical = abstract_audit.fetch_arxiv_abstracts(["2608.12345v2"], client=client)
        findings = abstract_audit.scan_abstracts([_paper()], canonical)

        self.assertEqual(canonical, {"2608.12345": "A clean abstract sentence."})
        self.assertEqual(findings[0].proposed_abstract, "A clean abstract sentence.")
        self.assertTrue(findings[0].would_update)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(client.closed)

    def test_database_audit_never_changes_stored_abstract(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "audit.sqlite3"):
                db.init_db()
                paper_id = db.upsert_paper(
                    {
                        "arxiv_id": "2608.12345",
                        "title": "Structured metadata",
                        "authors": [],
                        "subjects": [],
                        "abstract": DIRTY_ABSTRACT,
                        "rank": 1,
                    },
                    "cs.AI",
                    "2026-08-09",
                )

                findings = abstract_audit.audit_database(limit=10)
                stored_after = db.get_paper(paper_id)["abstract"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(stored_after, DIRTY_ABSTRACT)

    def test_readonly_connection_rejects_writes_and_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                db.connect_readonly(missing)
            self.assertFalse(missing.exists())

            existing = Path(tmp) / "existing.sqlite3"
            with db.connect(existing) as conn:
                conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            with db.connect_readonly(existing) as conn:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO sample DEFAULT VALUES")

    def test_arxiv_verification_rejects_invalid_ids_before_http(self):
        class FakeClient:
            def get(self, *args, **kwargs):
                raise AssertionError("HTTP should not be called")

        with self.assertRaisesRegex(ValueError, "非法 arXiv ID"):
            abstract_audit.fetch_arxiv_abstracts(["https://evil.example/x"], client=FakeClient())


if __name__ == "__main__":
    unittest.main()
