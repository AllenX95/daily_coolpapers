import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db


LEGACY_SCHEMA = """
CREATE TABLE llm_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    encrypted_api_key_ref TEXT,
    custom_headers TEXT NOT NULL DEFAULT '{}',
    temperature REAL NOT NULL DEFAULT 0.2,
    max_output_tokens INTEGER NOT NULL DEFAULT 2000,
    context_window_tokens INTEGER NOT NULL DEFAULT 128000,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default_abstract INTEGER NOT NULL DEFAULT 0,
    is_default_fulltext INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _insert_profile(conn, name: str, model: str) -> None:
    conn.execute(
        """
        INSERT INTO llm_profiles(
            id, name, provider, base_url, model, encrypted_api_key_ref,
            custom_headers, temperature, max_output_tokens,
            context_window_tokens, timeout_seconds, enabled,
            is_default_abstract, is_default_fulltext, created_at, updated_at
        ) VALUES (1, ?, 'openai_compatible', 'https://example.test', ?, NULL,
                  '{}', 0.2, 2000, 128000, 120, 1, 1, 0, ?, ?)
        """,
        (name, model, "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
    )


class ProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, "DB_PATH", root / "main.sqlite3"),
            patch.object(db, "LLM_PROFILES_DB_PATH", root / "profiles.sqlite3"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        with db.connect() as conn:
            conn.execute(LEGACY_SCHEMA)
            _insert_profile(conn, "Source", "source-model")

    def test_partial_target_is_upserted_and_repeated_run_is_noop(self):
        db.init_llm_profiles_db()
        with db.connect_llm_profiles() as conn:
            _insert_profile(conn, "Stale Target", "stale-model")

        db.migrate_llm_profiles_from_main_db()
        db.migrate_llm_profiles_from_main_db()

        with db.connect_llm_profiles() as conn:
            rows = conn.execute("SELECT * FROM llm_profiles").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Source")
        self.assertEqual(rows[0]["model"], "source-model")
        with db.connect() as conn:
            self.assertFalse(db._table_exists(conn, "llm_profiles"))
            self.assertTrue(db._table_exists(conn, "llm_profiles_legacy"))

    def test_failure_after_target_commit_can_be_retried(self):
        with patch.object(db, "_archive_legacy_llm_profiles", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                db.migrate_llm_profiles_from_main_db()

        with db.connect() as conn:
            self.assertTrue(db._table_exists(conn, "llm_profiles"))
        with db.connect_llm_profiles() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM llm_profiles").fetchone()["n"], 1)

        db.migrate_llm_profiles_from_main_db()
        with db.connect() as conn:
            self.assertFalse(db._table_exists(conn, "llm_profiles"))
            self.assertTrue(db._table_exists(conn, "llm_profiles_legacy"))

    def test_source_and_legacy_table_conflict_fails_closed(self):
        with db.connect() as conn:
            conn.execute("CREATE TABLE llm_profiles_legacy (id INTEGER PRIMARY KEY)")

        with self.assertRaisesRegex(RuntimeError, "迁移冲突"):
            db.migrate_llm_profiles_from_main_db()

        with db.connect() as conn:
            self.assertTrue(db._table_exists(conn, "llm_profiles"))


if __name__ == "__main__":
    unittest.main()
