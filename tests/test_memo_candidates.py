import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db, memo_candidates, memo_db


class MemoCandidateTests(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "main.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.directories_patch = patch.object(db, "ensure_directories")
        self.directories_patch.start()
        self.addCleanup(self.directories_patch.stop)
        db.init_db()
        self.ids = self._fixture()

    def _fixture(self):
        specs = [
            (1, "Alpha% Data", 80),
            (2, "βeta_%", "90"),
            (3, "Gamma", True),
            (4, "Delta", 3.5),
            (5, "No fulltext", 0),
            (6, "Nonfavorite", 70),
            (7, "İstanbul", 80),
        ]
        ids = {}
        for number, title, score in specs:
            ids[number] = db.upsert_papers(
                [{
                    "arxiv_id": f"2609.{number:05}",
                    "title": title,
                    "authors": ["Synthetic Author"],
                    "abstract": "Memo candidate fixture.",
                    "subjects": ["cs.AI"],
                    "published_at": "2026-09-01",
                    "rank": number,
                }],
                "cs.AI",
                "2026-09-01",
            )[0]
            if number != 5:
                db.create_evaluation(
                    ids[number],
                    "fulltext_review",
                    None,
                    None,
                    None,
                    "fixture-model",
                    "success",
                    {"score": score},
                    "{}",
                    None,
                )
        # A newer failed evaluation must not hide a historical successful row.
        for number in (2, 4):
            db.create_evaluation(
                ids[number], "fulltext_review", None, None, None,
                "fixture-model", "failed", None, None, "failure",
            )

        now = db.now_iso()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for number in (1, 2, 3, 4, 5, 7):
                conn.execute(
                    "INSERT INTO paper_dispositions(paper_id,decision,created_at,updated_at) "
                    "VALUES (?,?,?,?)",
                    (ids[number], "favorite", f"2026-09-{number:02} 10:00:00", now),
                )
            conn.execute(
                "INSERT INTO paper_dispositions(paper_id,decision,created_at,updated_at) "
                "VALUES (?,?,?,?)",
                (ids[6], "skipped", "2026-09-06 10:00:00", now),
            )

        direction_id = db.create_attention_direction("Memo direction", "Memo scope")
        theme_id = db.create_investment_theme("Memo theme")
        second_theme_id = db.create_investment_theme("Memo second theme")
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for number, model_decision, manual_decision in (
                (1, "matched", None),
                (2, "possible", None),
                (3, "unmatched", None),
                (4, "matched", "rejected"),
                (5, "matched", "confirmed"),
                (6, "matched", None),
                (7, "possible", "confirmed"),
            ):
                conn.execute(
                    """
                    INSERT INTO paper_direction_results(
                        paper_id,direction_id,model_decision,manual_decision,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (ids[number], direction_id, model_decision, manual_decision, now, now),
                )
            for number, selected in ((1, (theme_id, second_theme_id)), (2, (theme_id,)), (5, (theme_id,)), (6, (theme_id,))):
                for selected_id in selected:
                    conn.execute(
                        "INSERT INTO paper_investment_themes(paper_id,theme_id,created_at) VALUES (?,?,?)",
                        (ids[number], selected_id, now),
                    )
            conn.execute(
                """
                INSERT INTO research_authors(
                    id,name,normalized_name,author_category,notes,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (1, "Élodie", "élodie", "industry", "", "archived", now, now),
            )
            conn.execute(
                """
                INSERT INTO research_organizations(
                    id,name,normalized_name,organization_type,region,notes,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (1, "Org_% Lab", "org_% lab", "company", "", "", "archived", now, now),
            )
            conn.execute(
                """
                INSERT INTO paper_team_tracking(
                    paper_id,lead_author_id,organization_id,status,notes,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (ids[1], 1, 1, "archived", "historic team", now, now),
            )
        return {"papers": ids, "direction": direction_id, "theme": theme_id}

    def _read_conn(self):
        conn = db.connect()
        conn.execute("BEGIN")
        self.addCleanup(conn.close)
        return conn

    def test_page_matches_legacy_oracle_and_global_source_contract(self):
        source = {"mode": "attention_direction", "id": self.ids["direction"]}
        filters = {"sort": "score_desc"}
        conn = self._read_conn()

        legacy_candidates, legacy_counts = memo_db.candidate_data(conn, source, filters)
        pages = []
        first = memo_candidates.page_data(conn, source, filters, page=1, page_size=2)
        pages.append(first)
        for page in range(2, first["pages"] + 1):
            pages.append(memo_candidates.page_data(conn, source, filters, page=page, page_size=2))
        actual_candidates = [item for page in pages for item in page["candidates"]]
        self.assertEqual(actual_candidates, legacy_candidates)
        self.assertEqual(first["total"], len(legacy_candidates))
        self.assertEqual(first["counts"], legacy_counts)
        self.assertEqual(
            memo_candidates.selection_ids(conn, source, filters),
            [item["id"] for item in legacy_candidates],
        )

        self.assertEqual(first["pages"], 3)
        self.assertTrue(first["has_next"])
        self.assertFalse(first["has_previous"])
        self.assertEqual(pages[-1]["page"], 3)
        self.assertFalse(pages[-1]["has_next"])

        expected_source_only = [self.ids["papers"][1], self.ids["papers"][7]]
        self.assertEqual(memo_candidates.selection_ids(conn, source, filters, source_only=True), expected_source_only)
        self.assertEqual(
            memo_candidates.preselected_orders(
                conn,
                source,
                filters,
                [self.ids["papers"][1], self.ids["papers"][7], self.ids["papers"][2]],
            ),
            {self.ids["papers"][1]: 1, self.ids["papers"][7]: 2},
        )
        self.assertEqual(
            memo_candidates.selection_ids(
                conn,
                source,
                {"sort": "favorite_desc", "query": "does-not-match", "min_score": 100},
                source_only=True,
            ),
            [self.ids["papers"][7], self.ids["papers"][1]],
        )

        all_ids = list(self.ids["papers"].values())
        self.assertEqual(
            memo_candidates.eligible_ids(conn, all_ids),
            {self.ids["papers"][number] for number in (1, 2, 3, 4, 7)},
        )

        theme_source = {"mode": "investment_theme", "id": self.ids["theme"]}
        theme_page = memo_candidates.page_data(conn, theme_source, {"sort": "title"}, page_size=100)
        self.assertEqual(theme_page["counts"]["source_total"], 4)
        self.assertEqual(theme_page["counts"]["source_favorites"], 3)
        self.assertEqual(theme_page["counts"]["preselected"], 2)
        self.assertEqual(theme_page["counts"]["not_favorite"], 1)
        self.assertEqual(theme_page["counts"]["missing_fulltext"], 1)

        manual = memo_candidates.page_data(conn, {"mode": "manual", "id": None}, filters, page_size=2)
        self.assertEqual(manual["counts"], {key: 0 for key in memo_candidates.COUNT_KEYS})
        self.assertEqual(memo_candidates.selection_ids(conn, {"mode": "manual"}, source_only=True), [])
        self.assertEqual(
            memo_candidates.preselected_orders(conn, {"mode": "manual"}, filters, [1, 2]),
            {},
        )

    def test_sql_filters_are_casefolded_literal_and_do_not_snapshot(self):
        conn = self._read_conn()
        source = {"mode": "attention_direction", "id": self.ids["direction"]}
        with patch.object(memo_db, "paper_snapshots", side_effect=AssertionError("snapshot path used")):
            unicode_page = memo_candidates.page_data(
                conn, source, {"query": "İSTANBUL", "sort": "title"}, page_size=100
            )
            literal_page = memo_candidates.page_data(
                conn, source, {"query": "_%", "sort": "title"}, page_size=100
            )
            author_page = memo_candidates.page_data(
                conn, source, {"author": "éLODIE", "sort": "title"}, page_size=100
            )
            organization_page = memo_candidates.page_data(
                conn, source, {"organization": "_%", "sort": "title"}, page_size=100
            )
            direction_page = memo_candidates.page_data(
                conn,
                source,
                {"direction_id": self.ids["direction"], "sort": "title"},
                page_size=100,
            )
        self.assertEqual([item["id"] for item in unicode_page["candidates"]], [self.ids["papers"][7]])
        self.assertEqual([item["id"] for item in literal_page["candidates"]], [self.ids["papers"][2]])
        self.assertEqual([item["id"] for item in author_page["candidates"]], [self.ids["papers"][1]])
        self.assertEqual([item["id"] for item in organization_page["candidates"]], [self.ids["papers"][1]])
        self.assertEqual(
            {item["id"] for item in direction_page["candidates"]},
            {self.ids["papers"][1], self.ids["papers"][7]},
        )


if __name__ == "__main__":
    unittest.main()
