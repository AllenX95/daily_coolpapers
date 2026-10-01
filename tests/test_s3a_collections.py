import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import app as app_module, db, services


class S3aCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "main.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.directories_patch = patch.object(db, "ensure_directories")
        self.directories_patch.start()
        self.addCleanup(self.directories_patch.stop)
        self.app_patches = [
            patch.object(app_module, "has_pdf", return_value=False),
            patch.object(app_module, "has_markdown", return_value=False),
        ]
        for item in self.app_patches:
            item.start()
            self.addCleanup(item.stop)
        db.init_db()
        self.app = app_module.create_app(secret_key="s3a-test")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _paper(
        self,
        number: int,
        title: str,
        score,
        *,
        category: str = "cs.AI",
        result: dict | None = None,
    ) -> int:
        paper_id = db.upsert_papers(
            [{
                "arxiv_id": f"2609.{number:05}",
                "title": title,
                "authors": ["Synthetic Author"],
                "abstract": "A local S3a fixture.",
                "subjects": ["cs.AI"],
                "published_at": "2026-09-01",
                "rank": number,
            }],
            category,
            "2026-09-01",
        )[0]
        result = result if result is not None else {
            "score": score,
            "attention": "read",
            "one_sentence_summary": "S3a summary",
            "detailed_summary_zh": "S3a detail",
            "vc_perspective": {"impact": "S3a impact"},
        }
        db.create_evaluation(
            paper_id,
            "fulltext_review",
            None,
            None,
            None,
            "fixture-model",
            "success",
            result,
            "{}",
            None,
        )
        return paper_id

    def test_paged_ids_match_legacy_full_list_for_each_sort(self):
        ids = [
            self._paper(1, "Zeta", 90),
            self._paper(2, "alpha", 80),
            self._paper(3, "Beta", True),
            self._paper(4, "delta", 70),
            self._paper(5, "Gamma", 90),
            self._paper(6, "String score", "91"),
            self._paper(7, "Real score", 3.9999999999999996),
            self._paper(8, "Null score", None),
            self._paper(9, "Missing score", 0, result={"attention": False}),
        ]
        # A later failure must not hide the previous successful fulltext row.
        db.create_evaluation(ids[0], "fulltext_review", None, None, None, "fixture-model", "failed", None, None, "failure")
        db.set_paper_decision(ids[0], "favorite")
        db.set_paper_decision(ids[1], "favorite")
        db.set_paper_decision(ids[2], "favorite")
        db.set_paper_decision(ids[3], "favorite")
        theme_id = db.create_investment_theme("S3a theme")
        second_theme_id = db.create_investment_theme("S3a second theme")
        db.set_paper_investment_themes(ids[0], [theme_id])
        db.set_paper_investment_themes(ids[1], [theme_id])
        db.set_paper_investment_themes(ids[0], [theme_id, second_theme_id])
        theme_without_success = db.upsert_papers(
            [{
                "arxiv_id": "2609.00010",
                "title": "Theme without successful fulltext",
                "authors": ["Synthetic Author"],
                "abstract": "A theme-only fixture.",
                "subjects": ["cs.AI"],
                "published_at": "2026-09-01",
                "rank": 10,
            }],
            "cs.AI",
            "2026-09-01",
        )[0]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO paper_investment_themes(paper_id, theme_id, created_at) VALUES (?, ?, ?)",
                (theme_without_success, theme_id, db.now_iso()),
            )
        db.upsert_papers(
            [{
                "arxiv_id": "2609.00001",
                "title": "Zeta",
                "authors": ["Synthetic Author"],
                "abstract": "A local S3a fixture.",
                "subjects": ["cs.AI"],
                "published_at": "2026-09-01",
                "rank": 1,
            }],
            "cs.LG",
            "2026-09-01",
        )

        for sort in ("evaluated_desc", "score_desc", "rank", "title"):
            expected = [
                row["id"]
                for row in db.list_fulltext_reviewed_papers(
                    sort=sort,
                    decision="all",
                )
            ]
            actual = []
            first = db.list_fulltext_reviewed_papers_page(
                sort=sort,
                decision="all",
                page=1,
                page_size=2,
            )
            self.assertEqual(first["total"], len(expected))
            self.assertEqual(first["page_size"], 2)
            for page in range(1, first["pages"] + 1):
                actual.extend(
                    row["id"]
                    for row in db.list_fulltext_reviewed_papers_page(
                        sort=sort,
                        decision="all",
                        page=page,
                        page_size=2,
                    )["items"]
                )
            self.assertEqual(actual, expected, sort)

        page = db.list_fulltext_reviewed_papers_page(
            sort="score_desc", decision="favorite", page=1, page_size=2
        )
        self.assertEqual(page["items"][0]["fulltext_result"]["score"], 90)
        self.assertEqual(page["items"][0]["fulltext_model"], "fixture-model")
        self.assertIsInstance(page["items"][0]["fulltext_result"]["vc_perspective"], dict)
        self.assertEqual(page["items"][0]["fulltext_result"]["attention"], "read")
        all_page = db.list_fulltext_reviewed_papers_page(sort="title", page_size=100)
        true_row = next(item for item in all_page["items"] if item["id"] == ids[2])
        false_row = next(item for item in all_page["items"] if item["id"] == ids[8])
        self.assertIs(true_row["fulltext_result"]["score"], True)
        self.assertIs(false_row["fulltext_result"]["attention"], False)

        with db.connect() as conn:
            projection_types = {
                row["paper_id"]: (row["score_type"], row["score_scalar"], row["typeof_scalar"])
                for row in conn.execute(
                    """
                    SELECT paper_id, score_type, score_scalar,
                           typeof(score_scalar) AS typeof_scalar
                    FROM fulltext_evaluation_projection
                    WHERE paper_id IN (?,?,?,?,?)
                    """,
                    [ids[5], ids[6], ids[7], ids[8], ids[2]],
                )
            }
        self.assertEqual(projection_types[ids[5]], ("text", "91", "text"))
        self.assertEqual(projection_types[ids[6]][0], "real")
        self.assertEqual(projection_types[ids[6]][2], "real")
        self.assertAlmostEqual(projection_types[ids[6]][1], 3.9999999999999996)
        self.assertEqual(projection_types[ids[7]], ("null", None, "null"))
        self.assertEqual(projection_types[ids[8]], ("missing", None, "null"))
        self.assertEqual(projection_types[ids[2]][0], "true")

        theme_expected = [
            row["id"]
            for row in db.list_fulltext_reviewed_papers(theme_id=theme_id, sort="added_desc")
        ]
        theme_actual = [
            row["id"]
            for page_number in range(1, db.list_fulltext_reviewed_papers_page(
                theme_id=theme_id, sort="added_desc", page_size=1
            )["pages"] + 1)
            for row in db.list_fulltext_reviewed_papers_page(
                theme_id=theme_id, sort="added_desc", page=page_number, page_size=1
            )["items"]
        ]
        self.assertEqual(theme_actual, theme_expected)
        self.assertIn(theme_without_success, theme_actual)
        theme_page = db.list_fulltext_reviewed_papers_page(
            theme_id=theme_id, sort="added_desc", page=1, page_size=100
        )
        theme_row = next(item for item in theme_page["items"] if item["id"] == ids[0])
        self.assertEqual({item["id"] for item in theme_row["investment_themes"]}, {theme_id, second_theme_id})
        self.assertEqual(len(theme_row["categories"]), 2)
        no_success_row = next(item for item in theme_page["items"] if item["id"] == theme_without_success)
        self.assertIsNone(no_success_row["fulltext_evaluation_id"])
        self.assertEqual(no_success_row["fulltext_result"], {})

    def test_projection_triggers_and_one_time_backfill(self):
        paper_id = self._paper(10, "Projection", 88)
        with db.connect() as conn:
            projection = conn.execute(
                "SELECT evaluation_id, paper_id, score_type, score_scalar "
                "FROM fulltext_evaluation_projection"
            ).fetchone()
            evaluation_id = projection["evaluation_id"]
            self.assertEqual((projection["paper_id"], projection["score_type"], projection["score_scalar"]),
                             (paper_id, "integer", 88))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                                          (db.FULLTEXT_EVALUATION_PROJECTION_MIGRATION,)).fetchone()[0], 1)
        db.init_db()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fulltext_evaluation_projection").fetchone()[0], 1)
            conn.execute("UPDATE evaluations SET status='failed' WHERE id=?", (evaluation_id,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fulltext_evaluation_projection").fetchone()[0], 0)
            conn.execute("UPDATE evaluations SET status='success', result_json=? WHERE id=?",
                         ('{"score":true}', evaluation_id))
            self.assertEqual(conn.execute("SELECT score_type,score_scalar FROM fulltext_evaluation_projection").fetchone()[:],
                             ("true", 1))
            replacement_id = evaluation_id + 1000
            conn.execute(
                """
                UPDATE evaluations
                SET id=?, evaluation_type=?, paper_id=?, status=?, result_json=?, created_at=?
                WHERE id=?
                """,
                (
                    replacement_id,
                    "fulltext_review",
                    paper_id,
                    "success",
                    '{"score":3.25}',
                    "2026-09-02 00:00:00",
                    evaluation_id,
                ),
            )
            projection = conn.execute(
                "SELECT evaluation_id,paper_id,score_type,score_scalar FROM fulltext_evaluation_projection"
            ).fetchone()
            self.assertEqual(tuple(projection), (replacement_id, paper_id, "real", 3.25))
            conn.execute("DELETE FROM evaluations WHERE id=?", (evaluation_id,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fulltext_evaluation_projection").fetchone()[0], 1)
            conn.execute("DELETE FROM evaluations WHERE id=?", (replacement_id,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fulltext_evaluation_projection").fetchone()[0], 0)

    def test_projection_setup_rolls_back_on_marker_failure(self):
        paper_id = self._paper(11, "Projection rollback", 77)
        conn = db.connect()
        self.addCleanup(conn.close)
        for trigger in (
            "trg_fulltext_projection_insert",
            "trg_fulltext_projection_update",
            "trg_fulltext_projection_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP INDEX IF EXISTS idx_fulltext_projection_latest")
        conn.execute("DROP TABLE IF EXISTS fulltext_evaluation_projection")
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.commit()

        class RejectMarkerConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def execute(self, sql, parameters=()):
                if str(sql).lstrip().upper().startswith("INSERT INTO SCHEMA_MIGRATIONS"):
                    raise RuntimeError("injected projection marker failure")
                return self.wrapped.execute(sql, parameters)

        with self.assertRaisesRegex(RuntimeError, "injected projection marker failure"):
            db._ensure_fulltext_evaluation_projection(RejectMarkerConnection(conn))

        for object_type, object_name in (
            ("table", "fulltext_evaluation_projection"),
            ("table", "schema_migrations"),
            ("trigger", "trg_fulltext_projection_insert"),
            ("trigger", "trg_fulltext_projection_update"),
            ("trigger", "trg_fulltext_projection_delete"),
        ):
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                    (object_type, object_name),
                ).fetchone()
            )

        db._ensure_fulltext_evaluation_projection(conn)
        first_row = conn.execute(
            "SELECT evaluation_id,paper_id,score_type,score_scalar "
            "FROM fulltext_evaluation_projection"
        ).fetchone()
        self.assertEqual((first_row["paper_id"], first_row["score_type"], first_row["score_scalar"]),
                         (paper_id, "integer", 77))
        first_changes = conn.total_changes
        db._ensure_fulltext_evaluation_projection(conn)
        second_row = conn.execute(
            "SELECT evaluation_id,paper_id,score_type,score_scalar "
            "FROM fulltext_evaluation_projection"
        ).fetchone()
        self.assertEqual(tuple(second_row), tuple(first_row))
        self.assertEqual(conn.total_changes, first_changes)

    def test_routes_use_page_bounds_and_keep_legacy_service_full_list(self):
        for number in range(1, 5):
            paper_id = self._paper(number, f"Paper {number}", number * 10)
            db.set_paper_decision(paper_id, "favorite")
        self.assertEqual(len(services.favorite_papers_page_model()["papers"]), 4)

        response = self.client.get("/favorites?page=2&page_size=2")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("共 4 条", html)
        self.assertIn("上一页", html)
        self.assertIn("Paper 1", html)
        self.assertNotIn("Paper 4", html)

        response = self.client.get("/favorites?page=99&page_size=999")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("共 4 条", html)
        self.assertIn("返回末页", html)


if __name__ == "__main__":
    unittest.main()
