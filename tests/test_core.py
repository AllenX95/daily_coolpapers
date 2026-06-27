import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers.cache_manager import cleanup_directory
from daily_coolpapers.config import BASE_DIR
from daily_coolpapers.crawler import (
    available_arxiv_dates_after,
    build_category_url,
    extract_page_date,
    fetch_category,
    latest_available_arxiv_date,
    parse_papers,
)
from daily_coolpapers.jobs import JobRunner
from daily_coolpapers.llm import LLMResponse, parse_json_response
from daily_coolpapers.prompt_engine import estimate_tokens, render_prompt
from daily_coolpapers.security import SecretStore
from daily_coolpapers.services import build_catch_up_date_plan
from daily_coolpapers import db, services


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_root = BASE_DIR / "tmp" / "tests"
        cls.tmp_root.mkdir(parents=True, exist_ok=True)

    def test_render_prompt(self):
        prompt = "标题：{{ title }}\nSubjects: {{ subjects }}\n缺失：{{ missing }}"
        rendered = render_prompt(prompt, {"title": "Paper", "subjects": ["cs.AI", "cs.LG"]})
        self.assertIn("标题：Paper", rendered)
        self.assertIn("Subjects: cs.AI, cs.LG", rendered)
        self.assertIn("缺失：", rendered)

    def test_estimate_tokens(self):
        self.assertGreater(estimate_tokens("hello world" * 20), 0)
        self.assertGreater(estimate_tokens("中文内容" * 20), 0)

    def test_parse_json_response(self):
        parsed = parse_json_response('```json\n{"score": 88, "attention": "read"}\n```')
        self.assertEqual(parsed["score"], 88)
        parsed = parse_json_response('前缀 {"ok": true} 后缀')
        self.assertTrue(parsed["ok"])

    def test_parse_papers(self):
        html = """
        <html><body>
          <h2><a href="/arxiv/2501.12345">#1</a>
          <a href="/arxiv/2501.12345">A Strong Agent Paper</a>
          <a href="https://arxiv.org/pdf/2501.12345">PDF10</a>
          <a href="/kimi/2501.12345">Kimi17</a></h2>
          <p>Authors: Ada Lovelace, Alan Turing</p>
          <p>This paper studies autonomous agents for research workflows.</p>
          <p>Subjects: cs.AI, cs.LG</p>
          <p>Publish: 2026-05-17</p>
        </body></html>
        """
        papers = parse_papers(html, "cs.AI", 30, "https://papers.cool/arxiv/cs.AI?sort=1")
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.arxiv_id, "2501.12345")
        self.assertEqual(paper.rank, 1)
        self.assertEqual(paper.reading_stars, 27)
        self.assertIn("autonomous agents", paper.abstract)
        self.assertEqual(paper.abs_url, "https://arxiv.org/abs/2501.12345")

    def test_fetch_category_uses_injected_client_without_closing_it(self):
        html = """
        <html><body>
          <h2><a href="/arxiv/2501.12345">#1</a>
          <a href="/arxiv/2501.12345">A Strong Agent Paper</a>
          <a href="https://arxiv.org/pdf/2501.12345">PDF10</a></h2>
          <p>Authors: Ada Lovelace</p>
          <p>This paper studies autonomous agents.</p>
          <p>Subjects: cs.AI</p>
          <p>Publish: 2026-05-17</p>
        </body></html>
        """

        class FakeResponse:
            text = html

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self):
                self.closed = False
                self.get_calls = 0

            def get(self, *args, **kwargs):
                self.get_calls += 1
                return FakeResponse()

            def close(self):
                self.closed = True

        client = FakeClient()
        papers = fetch_category("cs.AI", top_n=30, client=client)

        self.assertEqual(len(papers), 1)
        self.assertEqual(client.get_calls, 1)
        self.assertFalse(client.closed)

    def test_build_category_url_with_date(self):
        url = build_category_url("cs.AI", "sort=1", top_n=30, crawl_date="2026-06-05")
        self.assertIn("date=2026-06-05", url)
        self.assertIn("sort=1", url)

    def test_extract_page_date(self):
        html = '<a onclick="openArxivCalendar()" class="date">2026-06-05</a>'
        self.assertEqual(extract_page_date(html), "2026-06-05")

    def test_latest_available_arxiv_date_skips_weekends(self):
        current = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(latest_available_arxiv_date(current), "2026-06-05")

    def test_available_arxiv_dates_after_skips_weekends(self):
        dates = available_arxiv_dates_after("2026-06-04", "2026-06-09")
        self.assertEqual(dates, ["2026-06-05", "2026-06-08", "2026-06-09"])

    def test_catch_up_plan_ignores_dates_after_target(self):
        plan = build_catch_up_date_plan("2026-06-06", "2026-06-05", "2026-06-02")
        self.assertTrue(plan["ignored_later_db_date"])
        self.assertEqual(plan["latest_reference_date"], "2026-06-02")
        self.assertEqual(plan["missing_dates"], ["2026-06-03", "2026-06-04", "2026-06-05"])

    def test_deduped_rows_keep_all_category_memberships(self):
        rows = [
            {
                "id": 1,
                "title": "Shared Paper",
                "category": "cs.AI",
                "crawl_date": "2026-06-03",
                "rank": 1,
                "reading_stars": 5,
                "pdf_clicks": 3,
                "kimi_clicks": 2,
            },
            {
                "id": 1,
                "title": "Shared Paper",
                "category": "cs.LG",
                "crawl_date": "2026-06-03",
                "rank": 2,
                "reading_stars": 9,
                "pdf_clicks": 5,
                "kimi_clicks": 4,
            },
            {
                "id": 2,
                "title": "Other Paper",
                "category": "cs.CV",
                "crawl_date": "2026-06-03",
                "rank": 3,
                "reading_stars": 1,
                "pdf_clicks": 1,
                "kimi_clicks": 0,
            },
        ]
        deduped = db._dedupe_paper_category_rows(rows, category="cs.AI", sort="stars_desc")

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["id"], 1)
        self.assertEqual(deduped[0]["reading_stars"], 9)
        self.assertEqual(
            {item["category"] for item in deduped[0]["category_memberships"]},
            {"cs.AI", "cs.LG"},
        )

    def test_init_db_creates_performance_indexes(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.sqlite3"):
                db.init_db()
                with db.connect() as conn:
                    indexes = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        ).fetchall()
                    }

        self.assertIn("idx_paper_categories_crawl_rank", indexes)
        self.assertIn("idx_paper_categories_paper_date", indexes)
        self.assertIn("idx_evaluations_latest", indexes)
        self.assertIn("idx_evaluations_latest_success", indexes)
        self.assertIn("idx_jobs_recent", indexes)

    def test_list_paper_rows_hydrates_latest_evaluations_in_batch(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.sqlite3"):
                db.init_db()
                paper_ids = db.upsert_papers(
                    [
                        {
                            "arxiv_id": "2606.00001",
                            "title": "Fast Paper",
                            "authors": ["Ada"],
                            "abstract": "A fast paper.",
                            "subjects": ["cs.AI"],
                            "published_at": "2026-06-05",
                            "pdf_url": "https://arxiv.org/pdf/2606.00001",
                            "abs_url": "https://arxiv.org/abs/2606.00001",
                            "papers_cool_url": "https://papers.cool/arxiv/2606.00001",
                            "rank": 1,
                            "reading_stars": 9,
                            "pdf_clicks": 6,
                            "kimi_clicks": 3,
                        },
                        {
                            "arxiv_id": "2606.00002",
                            "title": "Quiet Paper",
                            "authors": ["Grace"],
                            "abstract": "A quiet paper.",
                            "subjects": ["cs.LG"],
                            "published_at": "2026-06-05",
                            "pdf_url": "https://arxiv.org/pdf/2606.00002",
                            "abs_url": "https://arxiv.org/abs/2606.00002",
                            "papers_cool_url": "https://papers.cool/arxiv/2606.00002",
                            "rank": 2,
                            "reading_stars": 1,
                            "pdf_clicks": 1,
                            "kimi_clicks": 0,
                        },
                    ],
                    "cs.AI",
                    "2026-06-05",
                )
                db.create_evaluation(
                    paper_ids[0],
                    "abstract_review",
                    None,
                    None,
                    None,
                    "test-model",
                    "failed",
                    {"score": 1, "attention": "ignore"},
                    None,
                    "old failure",
                )
                db.create_evaluation(
                    paper_ids[0],
                    "abstract_review",
                    None,
                    None,
                    None,
                    "test-model",
                    "success",
                    {"score": 99, "attention": "read"},
                    "{}",
                    None,
                )
                db.create_evaluation(
                    paper_ids[0],
                    "fulltext_review",
                    None,
                    None,
                    None,
                    "test-model",
                    "success",
                    {"score": 88, "attention": "must_read"},
                    "{}",
                    None,
                )
                db.create_evaluation(
                    paper_ids[0],
                    "fulltext_review",
                    None,
                    None,
                    None,
                    "test-model",
                    "failed",
                    None,
                    None,
                    "newer failure",
                )

                rows = db.list_paper_rows(crawl_date="2026-06-05")
                by_arxiv = {row["arxiv_id"]: row for row in rows}
                filtered = db.list_paper_rows(crawl_date="2026-06-05", attention="read")
                favorites = db.list_fulltext_reviewed_papers()

        self.assertEqual(len(paper_ids), 2)
        self.assertEqual(by_arxiv["2606.00001"]["latest_abstract_eval"]["result"]["score"], 99)
        self.assertEqual(by_arxiv["2606.00001"]["latest_fulltext_eval"]["status"], "failed")
        self.assertEqual(
            by_arxiv["2606.00001"]["latest_successful_fulltext_eval"]["result"]["score"],
            88,
        )
        self.assertIsNone(by_arxiv["2606.00002"]["latest_abstract_eval"])
        self.assertEqual([row["arxiv_id"] for row in filtered], ["2606.00001"])
        self.assertEqual(len(favorites), 1)
        self.assertEqual(favorites[0]["fulltext_result"]["score"], 88)
        self.assertEqual(favorites[0]["latest_category"]["category"], "cs.AI")

    def test_fulltext_evaluation_renders_large_prompt_once(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            md_path = Path(tmp) / "paper.md"
            md_path.write_text("full text " * 100, encoding="utf-8")
            paper = {
                "id": 1,
                "arxiv_id": "2606.00003",
                "title": "Fulltext Paper",
                "abstract": "Short abstract",
                "published_at": "2026-06-05",
                "subjects_list": ["cs.AI"],
            }
            prompt = {
                "id": 1,
                "version": 1,
                "template": "Title: {{ title }}\nBody: {{ markdown }}",
                "llm_profile_id": None,
            }
            profile = {
                "id": 1,
                "provider": "openai_compatible",
                "model": "test-model",
                "context_window_tokens": 100000,
                "max_output_tokens": 100,
            }

            with (
                patch.object(services.db, "get_paper", return_value=paper),
                patch.object(services.db, "get_default_prompt", return_value=prompt),
                patch.object(services.db, "get_llm_profile", return_value=None),
                patch.object(services.db, "get_default_llm_profile", return_value=profile),
                patch.object(services.db, "create_evaluation", return_value=123),
                patch.object(services, "paper_variables", return_value={
                    "title": paper["title"],
                    "category": "",
                    "rank": "",
                    "stars": "",
                    "published_at": paper["published_at"],
                    "subjects": paper["subjects_list"],
                    "abstract": paper["abstract"],
                    "markdown": "",
                }),
                patch.object(services, "ensure_markdown", return_value=(md_path, False)),
                patch.object(services, "call_llm", return_value=LLMResponse("{}", {"score": 77})),
                patch.object(services, "render_prompt", wraps=render_prompt) as render_mock,
            ):
                result = services.evaluate_paper(1, "fulltext_review")

        self.assertEqual(result["evaluation_id"], 123)
        self.assertEqual(render_mock.call_count, 1)

    def test_parse_title_ignores_bracketed_action_links(self):
        html = """
        <html><body>
          <h2><a href="/arxiv/2605.15195">#1</a>
          <a href="/arxiv/2605.15195">VGGT-$Ω$: Visual Geometry Grounded Transformer</a>
          <a href="https://arxiv.org/pdf/2605.15195">[PDF21]</a>
          <a href="/copy/2605.15195">[Copy]</a>
          <a href="/kimi/2605.15195">[Kimi14]</a>
          <a href="/rel/2605.15195">[REL]</a></h2>
          <p>Authors: Example Author</p>
          <p>We introduce a visual geometry model.</p>
          <p>Subjects: cs.CV</p>
          <p>Publish: 2026-05-17</p>
        </body></html>
        """
        papers = parse_papers(html, "cs.CV", 30, "https://papers.cool/arxiv/cs.CV?sort=1")
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "VGGT-$Ω$: Visual Geometry Grounded Transformer")
        self.assertEqual(papers[0].reading_stars, 35)

    def test_cleanup_directory(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            path = Path(tmp) / "old.pdf"
            path.write_text("x", encoding="utf-8")
            old = (datetime.now() - timedelta(days=10)).timestamp()
            os.utime(path, (old, old))
            deleted = cleanup_directory(Path(tmp), "*.pdf", 5)
            self.assertEqual(deleted, 1)
            self.assertFalse(path.exists())

    def test_secret_store_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            store = SecretStore(Path(tmp) / "key")
            encrypted = store.encrypt("sk-test-secret")
            self.assertNotIn("sk-test-secret", encrypted)
            self.assertEqual(store.decrypt(encrypted), "sk-test-secret")

    def test_job_reconcile_throttles_repeated_polling(self):
        runner = JobRunner()
        runner._started = True

        with (
            patch("daily_coolpapers.jobs.time.monotonic", side_effect=[100.0, 110.0, 131.0]),
            patch("daily_coolpapers.jobs.db.mark_pending_jobs_interrupted_except", return_value=0) as mark_pending,
        ):
            runner.reconcile_orphaned_pending_jobs(min_interval_seconds=30)
            runner.reconcile_orphaned_pending_jobs(min_interval_seconds=30)
            runner.reconcile_orphaned_pending_jobs(min_interval_seconds=30)

        self.assertEqual(mark_pending.call_count, 2)


if __name__ == "__main__":
    unittest.main()
