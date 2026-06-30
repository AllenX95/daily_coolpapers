import csv
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
from daily_coolpapers.jobs import JobProgressWriter, JobRunner
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

    def test_paper_digest_query_normalizes_request_filters(self):
        range_query = db.PaperDigestQuery.from_raw(
            date_value="2099/01/03",
            date_from="20990102",
            date_to="2099.01.01",
            category=" cs.AI ",
            attention="read",
            sort="bad-sort",
            latest_crawl_date="2026-06-05",
        )

        self.assertEqual(range_query.selected_date, "")
        self.assertEqual(range_query.date_from, "2099-01-01")
        self.assertEqual(range_query.date_to, "2099-01-02")
        self.assertEqual(range_query.category, "cs.AI")
        self.assertEqual(range_query.attention, "read")
        self.assertEqual(range_query.sort, "rank")
        self.assertTrue(range_query.use_date_range)
        self.assertEqual(range_query.url_args(sort="stars_desc")["sort"], "stars_desc")

        single_date_query = db.PaperDigestQuery.from_raw(
            date_value="20990103",
            latest_crawl_date="2026-06-05",
        )
        self.assertEqual(single_date_query.selected_date, "2099-01-03")
        self.assertFalse(single_date_query.use_date_range)

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
        self.assertIn("idx_jobs_status_recent", indexes)

    def test_list_active_job_progress_returns_only_polling_fields(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.sqlite3"):
                db.init_db()
                pending_id = db.create_job("crawl", {"large": "payload"})
                running_id = db.create_job("abstract_eval", {"paper_id": 1})
                done_id = db.create_job("cleanup", {})
                db.update_job(running_id, "running")
                db.update_job(done_id, "running")
                db.update_job(done_id, "success")
                db.update_job_progress(pending_id, 0, 3, "waiting", {"phase": "crawl"})

                rows = db.list_active_job_progress()

        rows_by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(rows_by_id), {pending_id, running_id})
        self.assertEqual({row["status"] for row in rows}, {"pending", "running"})
        self.assertNotIn("payload", rows[0])
        self.assertEqual(rows_by_id[pending_id]["progress_details"]["phase"], "crawl")

    def test_job_progress_writer_throttles_intermediate_updates(self):
        ticks = iter([0.0, 0.1, 0.2])
        with patch("daily_coolpapers.jobs.db.update_job_progress") as update_progress:
            writer = JobProgressWriter(42, min_interval_seconds=1.0, clock=lambda: next(ticks))
            writer.update(0, 10, "start", {"phase": "crawl"})
            writer.update(1, 10, "one", {"phase": "crawl"})
            writer.update(10, 10, "done", {"phase": "crawl"})

        self.assertEqual(update_progress.call_count, 2)
        self.assertEqual(
            update_progress.call_args_list[0].args,
            (42, 0, 10, "start", {"phase": "crawl"}),
        )
        self.assertEqual(
            update_progress.call_args_list[1].args,
            (42, 10, 10, "done", {"phase": "crawl"}),
        )

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
                filtered = db.list_paper_rows(
                    db.PaperDigestQuery.from_raw(date_value="2026-06-05", attention="read")
                )
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

    def test_paper_evaluation_result_model_prepares_template_fields(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.sqlite3"):
                db.init_db()
                paper_id = db.upsert_papers(
                    [
                        {
                            "arxiv_id": "2606.00003",
                            "title": "Result Paper",
                            "authors": ["Ada"],
                            "abstract": "A result paper.",
                            "subjects": ["cs.AI"],
                            "published_at": "2026-06-05",
                            "pdf_url": "https://arxiv.org/pdf/2606.00003",
                            "abs_url": "https://arxiv.org/abs/2606.00003",
                            "papers_cool_url": "https://papers.cool/arxiv/2606.00003",
                            "rank": 1,
                            "reading_stars": 9,
                            "pdf_clicks": 4,
                            "kimi_clicks": 5,
                        }
                    ],
                    "cs.AI",
                    "2026-06-05",
                )[0]
                prompt_id = db.save_prompt(
                    {
                        "name": "Fulltext Prompt",
                        "type": "fulltext_review",
                        "template": "Body: {{ markdown }}",
                        "llm_profile_id": None,
                        "is_default": True,
                        "enabled": True,
                    }
                )
                db.create_evaluation(
                    paper_id,
                    "fulltext_review",
                    prompt_id,
                    1,
                    None,
                    "test-model",
                    "success",
                    {
                        "score": 88,
                        "attention": "must_read",
                        "one_sentence_summary": "One line",
                        "detailed_summary_zh": "Detailed",
                        "vc_perspective": {
                            "impact": "Strong impact",
                            "startup_opportunities": ["tooling"],
                        },
                    },
                    "{}",
                    None,
                )
                db.create_evaluation(
                    paper_id,
                    "fulltext_review",
                    prompt_id,
                    1,
                    None,
                    "test-model",
                    "failed",
                    None,
                    "not-json",
                    "newer failure",
                )

                result_model = services.paper_evaluation_result_model(paper_id)

        fulltext = result_model["latest_successful_fulltext"]
        self.assertEqual(fulltext["score_text"], "88")
        self.assertEqual(fulltext["attention"], "must_read")
        self.assertEqual(fulltext["prompt_label"], "Fulltext Prompt")
        self.assertEqual(fulltext["sections"][0], {"title": "详细总结", "body": "Detailed"})
        self.assertTrue(fulltext["vc"]["has_content"])
        self.assertEqual(fulltext["vc"]["startup_opportunities"], ["tooling"])
        self.assertEqual(result_model["latest_fulltext_failure"]["error_message"], "newer failure")
        self.assertEqual(result_model["history"][0]["status"], "failed")

    def test_paper_evaluation_export_uses_result_model(self):
        paper = {
            "id": 7,
            "title": "Export Paper",
            "arxiv_id": "2606.00007",
            "published_at": "2026-06-30",
            "pdf_url": "https://arxiv.org/pdf/2606.00007",
            "abstract": "A paper.",
        }
        result_model = {
            "history": [
                {
                    "type_label": "全文评估",
                    "status_label": "成功",
                    "created_at": "2026-06-30 12:00:00",
                    "prompt_label": "Fulltext Prompt",
                    "profile_label": "Profile / model-x",
                    "has_result": True,
                    "result_json": {"score": 88, "attention": "must_read"},
                    "error_message": "",
                    "raw_output": "",
                },
                {
                    "type_label": "摘要评估",
                    "status_label": "失败",
                    "created_at": "2026-06-30 12:05:00",
                    "prompt_label": "Abstract Prompt",
                    "profile_label": "Profile / model-x",
                    "has_result": False,
                    "result_json": {},
                    "error_message": "bad json",
                    "raw_output": "not-json",
                },
            ]
        }

        body = services.build_paper_evaluation_export(paper, result_model)

        self.assertIn("# Export Paper", body)
        self.assertIn("## 全文评估 - 成功 - 2026-06-30 12:00:00", body)
        self.assertIn("- Prompt: Fulltext Prompt", body)
        self.assertIn('"score": 88', body)
        self.assertIn("## 摘要评估 - 失败 - 2026-06-30 12:05:00", body)
        self.assertIn("bad json", body)
        self.assertIn("```text\nnot-json\n```", body)

    def test_paper_digest_csv_uses_evaluation_view(self):
        body = services.build_paper_digest_csv(
            [
                {
                    "crawl_date": "2026-06-05",
                    "category": "cs.AI",
                    "rank": 1,
                    "reading_stars": 9,
                    "title": "Fast Paper",
                    "arxiv_id": "2606.00001",
                    "pdf_url": "https://arxiv.org/pdf/2606.00001",
                    "latest_abstract_eval": {
                        "status": "success",
                        "result": {"score": 99, "attention": "read"},
                    },
                },
                {
                    "crawl_date": "2026-06-05",
                    "category": "cs.LG",
                    "rank": 2,
                    "reading_stars": 1,
                    "title": "Failed Paper",
                    "arxiv_id": "2606.00002",
                    "pdf_url": "https://arxiv.org/pdf/2606.00002",
                    "latest_abstract_eval": {
                        "status": "failed",
                        "result": {"score": 1, "attention": "ignore"},
                    },
                },
            ]
        )

        rows = list(csv.reader(body.splitlines()))
        self.assertEqual(
            rows[0],
            ["date", "category", "rank", "stars", "score", "attention", "title", "arxiv_id", "pdf_url"],
        )
        self.assertEqual(rows[1][4:6], ["99", "read"])
        self.assertEqual(rows[2][4:6], ["", ""])

    def test_evaluation_runner_uses_preloaded_config_without_prompt_profile_reads(self):
        config = services.EvaluationConfig(
            evaluation_type="abstract_review",
            prompt={
                "id": 11,
                "version": 3,
                "type": "abstract_review",
                "enabled": 1,
                "template": "Title: {{ title }}",
            },
            profile={
                "id": 22,
                "enabled": 1,
                "provider": "openai_compatible",
                "model": "test-model",
                "context_window_tokens": 100000,
                "max_output_tokens": 100,
            },
        )
        paper = {
            "id": 7,
            "title": "Configured Paper",
            "abstract": "Short abstract",
            "published_at": "2026-06-05",
            "subjects_list": ["cs.AI"],
        }

        with (
            patch.object(services.db, "get_paper", return_value=paper),
            patch.object(services.db, "get_paper_categories", return_value=[]),
            patch.object(services.db, "get_prompt") as get_prompt,
            patch.object(services.db, "get_default_prompt") as get_default_prompt,
            patch.object(services.db, "get_llm_profile") as get_profile,
            patch.object(services.db, "get_default_llm_profile") as get_default_profile,
            patch.object(services, "call_llm", return_value=LLMResponse("{}", {"score": 77})),
            patch.object(services.db, "create_evaluation", return_value=123) as create_evaluation,
        ):
            result = services.EvaluationRunner(config=config).evaluate(
                services.EvaluationRequest(7, "abstract_review")
            )

        self.assertEqual(result["evaluation_id"], 123)
        get_prompt.assert_not_called()
        get_default_prompt.assert_not_called()
        get_profile.assert_not_called()
        get_default_profile.assert_not_called()
        self.assertEqual(create_evaluation.call_args.args[2:6], (11, 3, 22, "test-model"))

    def test_evaluation_config_validates_prompt_type_and_profile_enabled(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"

            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()
                disabled_profile_id = db.save_llm_profile(
                    {
                        "name": "Disabled Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "disabled-model",
                        "enabled": False,
                        "is_default_abstract": False,
                        "is_default_fulltext": False,
                    }
                )
                default_profile_id = db.save_llm_profile(
                    {
                        "name": "Default Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "default-model",
                        "enabled": True,
                        "is_default_abstract": True,
                        "is_default_fulltext": False,
                    }
                )
                abstract_prompt_id = db.save_prompt(
                    {
                        "name": "Abstract Prompt",
                        "type": "abstract_review",
                        "template": "Title: {{ title }}",
                        "llm_profile_id": disabled_profile_id,
                        "is_default": True,
                        "enabled": True,
                    }
                )
                fulltext_prompt_id = db.save_prompt(
                    {
                        "name": "Fulltext Prompt",
                        "type": "fulltext_review",
                        "template": "Body: {{ markdown }}",
                        "llm_profile_id": default_profile_id,
                        "is_default": True,
                        "enabled": True,
                    }
                )

                config = services.resolve_evaluation_config(
                    "abstract_review",
                    prompt_id=abstract_prompt_id,
                )
                options = services.evaluation_prompt_options("abstract_review")
                with self.assertRaisesRegex(ValueError, "not abstract_review"):
                    services.resolve_evaluation_config(
                        "abstract_review",
                        prompt_id=fulltext_prompt_id,
                    )

        self.assertEqual(config.prompt_id, abstract_prompt_id)
        self.assertEqual(config.profile_id, default_profile_id)
        self.assertEqual(config.model, "default-model")
        self.assertEqual(options[0]["value"], str(abstract_prompt_id))
        self.assertIn("default-model", options[0]["label"])
        self.assertNotIn("disabled-model", options[0]["label"])

    def test_evaluation_prompt_options_show_bound_profile_for_default_prompt(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"

            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()
                db.save_llm_profile(
                    {
                        "name": "Default Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "default-model",
                        "enabled": True,
                        "is_default_abstract": True,
                        "is_default_fulltext": False,
                    }
                )
                bound_profile_id = db.save_llm_profile(
                    {
                        "name": "Bound Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "bound-model",
                        "enabled": True,
                        "is_default_abstract": False,
                        "is_default_fulltext": False,
                    }
                )
                prompt_id = db.save_prompt(
                    {
                        "name": "Bound Prompt",
                        "type": "abstract_review",
                        "template": "Title: {{ title }}",
                        "llm_profile_id": bound_profile_id,
                        "is_default": True,
                        "enabled": True,
                    }
                )

                options = services.evaluation_prompt_options("abstract_review")

        self.assertEqual(options[0]["value"], str(prompt_id))
        self.assertIn("bound-model", options[0]["label"])
        self.assertNotIn("default-model", options[0]["label"])

    def test_fulltext_evaluation_renders_large_prompt_once(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"
            md_path = Path(tmp) / "paper.md"
            md_path.write_text("full text " * 100, encoding="utf-8")

            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()
                profile_id = db.save_llm_profile(
                    {
                        "name": "Fulltext Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "test-model",
                        "enabled": True,
                        "is_default_abstract": False,
                        "is_default_fulltext": True,
                        "max_output_tokens": 100,
                        "context_window_tokens": 100000,
                    }
                )
                db.save_prompt(
                    {
                        "name": "Fulltext Prompt",
                        "type": "fulltext_review",
                        "template": "Title: {{ title }}\nBody: {{ markdown }}",
                        "llm_profile_id": profile_id,
                        "is_default": True,
                        "enabled": True,
                    }
                )
                paper_id = db.upsert_papers(
                    [
                        {
                            "arxiv_id": "2606.00003",
                            "title": "Fulltext Paper",
                            "authors": ["Ada"],
                            "abstract": "Short abstract",
                            "subjects": ["cs.AI"],
                            "published_at": "2026-06-05",
                            "pdf_url": "https://arxiv.org/pdf/2606.00003",
                            "abs_url": "https://arxiv.org/abs/2606.00003",
                            "papers_cool_url": "https://papers.cool/arxiv/2606.00003",
                            "rank": 1,
                            "reading_stars": 9,
                            "pdf_clicks": 4,
                            "kimi_clicks": 5,
                        }
                    ],
                    "cs.AI",
                    "2026-06-05",
                )[0]

                with (
                    patch.object(services, "ensure_markdown", return_value=(md_path, False)),
                    patch.object(services, "call_llm", return_value=LLMResponse("{}", {"score": 77})),
                    patch.object(services, "render_prompt", wraps=render_prompt) as render_mock,
                ):
                    result = services.evaluate_paper(paper_id, "fulltext_review")

                evaluations = db.list_evaluations(paper_id)

        self.assertEqual(result["evaluation_id"], evaluations[0]["id"])
        self.assertEqual(evaluations[0]["status"], "success")
        self.assertEqual(evaluations[0]["result"]["score"], 77)
        self.assertEqual(render_mock.call_count, 1)

    def test_evaluation_records_invalid_json_through_public_interface(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"

            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()
                profile_id = db.save_llm_profile(
                    {
                        "name": "Abstract Profile",
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "test-model",
                        "enabled": True,
                        "is_default_abstract": True,
                        "is_default_fulltext": False,
                    }
                )
                db.save_prompt(
                    {
                        "name": "Abstract Prompt",
                        "type": "abstract_review",
                        "template": "Title: {{ title }}\nAbstract: {{ abstract }}",
                        "llm_profile_id": profile_id,
                        "is_default": True,
                        "enabled": True,
                    }
                )
                paper_id = db.upsert_papers(
                    [
                        {
                            "arxiv_id": "2606.00004",
                            "title": "JSON Paper",
                            "authors": ["Grace"],
                            "abstract": "A paper with invalid model output.",
                            "subjects": ["cs.AI"],
                            "published_at": "2026-06-05",
                            "pdf_url": "https://arxiv.org/pdf/2606.00004",
                            "abs_url": "https://arxiv.org/abs/2606.00004",
                            "papers_cool_url": "https://papers.cool/arxiv/2606.00004",
                            "rank": 2,
                            "reading_stars": 3,
                            "pdf_clicks": 1,
                            "kimi_clicks": 2,
                        }
                    ],
                    "cs.AI",
                    "2026-06-05",
                )[0]

                with patch.object(services, "call_llm", return_value=LLMResponse("not-json", None)):
                    with self.assertRaises(services.LLMError):
                        services.evaluate_paper(paper_id, "abstract_review")

                evaluations = db.list_evaluations(paper_id)

        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["status"], "failed")
        self.assertEqual(evaluations[0]["raw_output"], "not-json")
        self.assertIn("合法 JSON", evaluations[0]["error_message"])

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

    def test_llm_profiles_migrated_to_separate_db(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"
            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                # Create legacy schema with llm_profiles in main DB
                with db.connect() as conn:
                    conn.executescript(
                        """
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
                        );
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO llm_profiles(
                            name, provider, base_url, model, encrypted_api_key_ref,
                            custom_headers, temperature, max_output_tokens,
                            context_window_tokens, timeout_seconds, enabled,
                            is_default_abstract, is_default_fulltext, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "Test Profile", "openai_compatible", "https://api.openai.com",
                            "gpt-4o", "fernet:encrypted-key", "{}", 0.2, 2000, 128000, 120,
                            1, 1, 0, "2026-01-01 00:00:00", "2026-01-01 00:00:00",
                        ),
                    )

                db.migrate_llm_profiles_from_main_db()

                with db.connect_llm_profiles() as conn:
                    rows = conn.execute("SELECT * FROM llm_profiles").fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["name"], "Test Profile")
                    self.assertEqual(rows[0]["encrypted_api_key_ref"], "fernet:encrypted-key")

                with db.connect() as conn:
                    legacy = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_profiles_legacy'"
                    ).fetchone()
                    self.assertIsNotNone(legacy)
                    old = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_profiles'"
                    ).fetchone()
                    self.assertIsNone(old)

    def test_list_prompts_hydrates_from_llm_profiles_db(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"
            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()

                with db.connect_llm_profiles() as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO llm_profiles(name, provider, base_url, model, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("My Profile", "openai_compatible", "https://api.example.com", "model-x", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
                    )
                    profile_id = int(cur.lastrowid)

                with db.connect() as conn:
                    conn.execute("DELETE FROM prompts")
                    conn.execute(
                        """
                        INSERT INTO prompts(name, type, template, llm_profile_id, version, is_default, enabled, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("Prompt", "abstract_review", "Template", profile_id, 1, 1, 1, "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
                    )

                prompts = db.list_prompts()
                self.assertEqual(len(prompts), 1)
                self.assertEqual(prompts[0]["llm_profile_name"], "My Profile")
                self.assertEqual(prompts[0]["llm_model"], "model-x")

    def test_list_evaluations_hydrates_from_llm_profiles_db(self):
        with tempfile.TemporaryDirectory(dir=self.tmp_root) as tmp:
            main_db = Path(tmp) / "main.sqlite3"
            llm_db = Path(tmp) / "llm.sqlite3"
            with (
                patch.object(db, "DB_PATH", main_db),
                patch.object(db, "LLM_PROFILES_DB_PATH", llm_db),
            ):
                db.init_db()
                db.init_llm_profiles_db()

                with db.connect_llm_profiles() as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO llm_profiles(name, provider, base_url, model, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("Eval Profile", "openai_compatible", "https://api.example.com", "model-y", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
                    )
                    profile_id = int(cur.lastrowid)

                with db.connect() as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO papers(arxiv_id, title, authors, abstract, subjects, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("2606.00001", "Paper", "[]", "Abstract", "[]", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
                    )
                    paper_id = int(cur.lastrowid)
                    conn.execute(
                        """
                        INSERT INTO evaluations(
                            paper_id, evaluation_type, prompt_id, prompt_version,
                            llm_profile_id, model, status, result_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (paper_id, "abstract_review", None, 1, profile_id, "model-y", "success", '{"score": 90}', "2026-01-01 00:00:00"),
                    )

                evaluations = db.list_evaluations(paper_id)
                self.assertEqual(len(evaluations), 1)
                self.assertEqual(evaluations[0]["llm_profile_name"], "Eval Profile")
                self.assertEqual(evaluations[0]["result"]["score"], 90)


if __name__ == "__main__":
    unittest.main()
