import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _empty_evaluation_results():
    return {
        "history": [],
        "latest_abstract": None,
        "latest_fulltext": None,
        "latest_successful_fulltext": None,
        "latest_fulltext_failure": None,
    }


def _renderable_evaluation_results():
    fulltext = {
        "id": 77,
        "evaluation_type": "fulltext_review",
        "type_label": "全文评估",
        "status": "success",
        "status_label": "成功",
        "is_success": True,
        "is_failed": False,
        "created_at": "2026-06-30 12:00:00",
        "prompt_label": "Fulltext Prompt",
        "profile_label": "Profile / model-x",
        "score_text": "92",
        "attention": "must_read",
        "one_sentence_summary": "Fulltext one line",
        "sections": [{"title": "详细总结", "body": "Detailed result"}],
        "vc": {
            "has_content": True,
            "impact": "Strong impact",
            "market_relevance": "",
            "commercialization_path": "",
            "startup_opportunities": [],
            "investment_risks": [],
        },
        "has_result": True,
        "result_json": {"score": 92},
        "error_message": "",
        "raw_output": "",
    }
    return {
        "history": [fulltext],
        "latest_abstract": None,
        "latest_fulltext": fulltext,
        "latest_successful_fulltext": fulltext,
        "latest_fulltext_failure": None,
    }


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from daily_coolpapers import app as app_module
        from daily_coolpapers import cache_manager, config, db, llm, logging_setup, security
        from daily_coolpapers.security import SecretStore

        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.runtime_root = Path(cls._tmp.name)
        instance_dir = cls.runtime_root / "instance"
        data_dir = cls.runtime_root / "data"
        cache_dir = cls.runtime_root / "cache"
        pdf_cache_dir = cache_dir / "pdf"
        markdown_cache_dir = cache_dir / "markdown"
        log_dir = cls.runtime_root / "logs"
        current_log = log_dir / "current.log"
        db_path = data_dir / "daily_coolpapers.sqlite3"
        llm_profiles_db_path = instance_dir / "llm_profiles.sqlite3"

        cls._patches = ExitStack()
        cls.addClassCleanup(cls._patches.close)
        cls._patches.enter_context(
            patch.dict(
                os.environ,
                {
                    "DAILY_COOLPAPERS_DISABLE_WORKER": "1",
                    "DAILY_COOLPAPERS_DISABLE_SHUTDOWN": "1",
                },
            )
        )
        for module, name, value in [
            (config, "INSTANCE_DIR", instance_dir),
            (config, "DATA_DIR", data_dir),
            (config, "CACHE_DIR", cache_dir),
            (config, "PDF_CACHE_DIR", pdf_cache_dir),
            (config, "MARKDOWN_CACHE_DIR", markdown_cache_dir),
            (config, "LOG_DIR", log_dir),
            (config, "CURRENT_LOG", current_log),
            (config, "DB_PATH", db_path),
            (config, "LLM_PROFILES_DB_PATH", llm_profiles_db_path),
            (db, "DB_PATH", db_path),
            (db, "LLM_PROFILES_DB_PATH", llm_profiles_db_path),
            (cache_manager, "PDF_CACHE_DIR", pdf_cache_dir),
            (cache_manager, "MARKDOWN_CACHE_DIR", markdown_cache_dir),
            (logging_setup, "CURRENT_LOG", current_log),
            (app_module, "INSTANCE_DIR", instance_dir),
            (app_module, "CURRENT_LOG", current_log),
        ]:
            cls._patches.enter_context(patch.object(module, name, value))
        secret_store = SecretStore(instance_dir / "fernet.key")
        cls._patches.enter_context(patch.object(app_module, "secret_store", secret_store))
        cls._patches.enter_context(patch.object(llm, "secret_store", secret_store))
        cls._patches.enter_context(patch.object(security, "secret_store", secret_store))
        cls._patches.enter_context(patch.object(app_module, "setup_logging", return_value=None))

        cls.runtime = app_module.start_runtime(start_worker=False)
        cls.addClassCleanup(cls.runtime.stop)
        cls.app = app_module.create_app()
        cls.app.config.update(TESTING=True)

    def test_runtime_state_is_isolated(self):
        from daily_coolpapers import app as app_module
        from daily_coolpapers import cache_manager, db

        self.assertTrue(db.DB_PATH.is_relative_to(self.runtime_root))
        self.assertTrue(db.LLM_PROFILES_DB_PATH.is_relative_to(self.runtime_root))
        self.assertTrue(cache_manager.PDF_CACHE_DIR.is_relative_to(self.runtime_root))
        self.assertTrue(cache_manager.MARKDOWN_CACHE_DIR.is_relative_to(self.runtime_root))
        self.assertTrue(app_module.secret_store.key_path.is_relative_to(self.runtime_root))
        self.assertTrue(db.DB_PATH.exists())
        self.assertTrue(db.LLM_PROFILES_DB_PATH.exists())

    def csrf_data(self, client, path="/"):
        client.get(path)
        with client.session_transaction() as flask_session:
            return {"csrf_token": flask_session["_csrf_token"]}

    def test_index_loads(self):
        client = self.app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily Cool Papers".encode(), response.data)
        self.assertIn("job-progress-panel".encode(), response.data)
        self.assertIn("rank_desc".encode(), response.data)
        self.assertIn("stars_desc".encode(), response.data)
        self.assertIn('name="date_from"'.encode(), response.data)
        self.assertIn('name="date_to"'.encode(), response.data)
        self.assertIn("抓取并摘要评估".encode("utf-8"), response.data)
        self.assertIn("补抓并摘要评估".encode("utf-8"), response.data)
        self.assertIn("评估缺失摘要".encode("utf-8"), response.data)
        self.assertIn("全文评估".encode("utf-8"), response.data)

    def test_index_supports_date_range_filters(self):
        client = self.app.test_client()
        response = client.get("/?date_from=2099-01-01&date_to=2099-01-02&sort=stars_desc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("当前范围".encode("utf-8"), response.data)
        self.assertIn("2099-01-01".encode(), response.data)
        self.assertIn("2099-01-02".encode(), response.data)

        export_response = client.get(
            "/export.csv?date_from=2099-01-01&date_to=2099-01-02&sort=rank_desc"
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("date,category,rank,stars".encode(), export_response.data)

    def test_index_accepts_compact_date_inputs(self):
        client = self.app.test_client()
        response = client.get("/?date_from=20990102&date_to=20990101&sort=stars_desc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("2099-01-01".encode(), response.data)
        self.assertIn("2099-01-02".encode(), response.data)

        single_response = client.get("/?date=20990101")
        self.assertEqual(single_response.status_code, 200)
        self.assertIn("2099-01-01".encode(), single_response.data)

        export_response = client.get("/export.csv?date_from=20990101&date_to=20990102&sort=rank_desc")
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("date,category,rank,stars".encode(), export_response.data)

    def test_digest_routes_use_normalized_query_object(self):
        from daily_coolpapers import db

        client = self.app.test_client()
        empty_page = {
            "items": [],
            "page": 1,
            "page_size": 50,
            "total": 0,
            "pages": 0,
            "has_previous": False,
            "has_next": False,
        }
        with patch("daily_coolpapers.app.db.list_paper_page", return_value=empty_page) as list_page:
            response = client.get(
                "/?date_from=20990102&date_to=2099.01.01&category=%20cs.AI%20&attention=read&sort=bad-sort"
            )

        self.assertEqual(response.status_code, 200)
        page_args = list_page.call_args.kwargs
        self.assertEqual(page_args["date_from"], "2099-01-01")
        self.assertEqual(page_args["date_to"], "2099-01-02")
        self.assertEqual(page_args["category"], "cs.AI")
        self.assertEqual(page_args["attention"], "read")
        self.assertEqual(page_args["sort"], "rank")

        with patch("daily_coolpapers.app.db.list_paper_rows", return_value=[]) as list_rows:
            response = client.get("/export.csv?date=20990103&sort=stars_desc")

        self.assertEqual(response.status_code, 200)
        query = list_rows.call_args.args[0]
        self.assertIsInstance(query, db.PaperDigestQuery)
        self.assertEqual(query.selected_date, "2099-01-03")
        self.assertEqual(query.sort, "stars_desc")

    def test_management_pages_load(self):
        client = self.app.test_client()
        for path in ["/favorites", "/reviewed-papers", "/categories", "/prompts", "/llm-profiles", "/settings", "/logs"]:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_favorites_page_loads(self):
        client = self.app.test_client()
        response = client.get("/favorites")
        self.assertEqual(response.status_code, 200)
        self.assertIn("personal-favorites".encode(), response.data)
        self.assertIn("score_desc".encode(), response.data)

    def test_favorites_route_uses_page_model(self):
        client = self.app.test_client()
        page_model = {
            "papers": [],
            "sort": "evaluated_desc",
            "sort_options": [
                {"value": "evaluated_desc", "label": "评估时间", "selected": True},
                {"value": "score_desc", "label": "全文评分", "selected": False},
            ],
        }
        with (
            patch("daily_coolpapers.app.favorite_papers_page_model", return_value=page_model) as model,
            patch("daily_coolpapers.app.db.list_fulltext_reviewed_papers") as list_reviewed,
            patch("daily_coolpapers.app.render_template", return_value="ok") as render,
        ):
            response = client.get("/favorites?sort=bad")

        self.assertEqual(response.status_code, 200)
        model.assert_called_once_with("bad")
        list_reviewed.assert_not_called()
        self.assertEqual(render.call_args.args[0], "favorites.html")
        self.assertEqual(render.call_args.kwargs, page_model)

    def test_favorites_page_renders_card_model(self):
        client = self.app.test_client()
        page_model = {
            "sort": "score_desc",
            "sort_options": [
                {"value": "evaluated_desc", "label": "评估时间", "selected": False},
                {"value": "score_desc", "label": "全文评分", "selected": True},
            ],
            "papers": [
                {
                    "id": 7,
                    "title": "Favorite Paper",
                    "arxiv_id": "2606.00007",
                    "abs_url": "https://arxiv.org/abs/2606.00007",
                    "category_label": "cs.AI / Rank 1 / Stars 9",
                    "evaluation_label": "2026-06-30 12:00:00 / model-x",
                    "score_text": "91",
                    "attention": "must_read",
                    "one_sentence_summary": "One line",
                    "summary_excerpt": "Detailed summary",
                    "vc_summary": "Strong impact",
                    "market_relevance": "High",
                    "recommended_action": "Read deeply",
                    "novelty_assessment": "Novel",
                    "tags": ["agent", "infra"],
                }
            ],
        }
        with patch("daily_coolpapers.app.favorite_papers_page_model", return_value=page_model):
            response = client.get("/favorites?sort=score_desc")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Favorite Paper".encode(), response.data)
        self.assertIn("cs.AI / Rank 1 / Stars 9".encode(), response.data)
        self.assertIn("Detailed summary".encode(), response.data)
        self.assertIn("Strong impact".encode(), response.data)
        self.assertIn("agent".encode(), response.data)

    def test_settings_has_abstract_concurrency(self):
        client = self.app.test_client()
        response = client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("abstract_concurrency".encode(), response.data)
        self.assertIn("crawler_trust_env_proxy".encode(), response.data)
        self.assertIn("crawler_proxy_url".encode(), response.data)
        self.assertIn("llm_trust_env_proxy".encode(), response.data)
        self.assertIn("pdf_download_timeout_seconds".encode(), response.data)
        self.assertIn("pdf_download_retries".encode(), response.data)

    def test_settings_reject_invalid_range_without_write(self):
        client = self.app.test_client()
        data = self.csrf_data(client, "/settings")
        data["abstract_concurrency"] = "21"
        with patch("daily_coolpapers.app.db.save_settings") as save_settings:
            response = client.post("/api/settings", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("abstract_concurrency".encode(), response.data)
        save_settings.assert_not_called()

    def test_llm_profile_rejects_non_object_headers(self):
        client = self.app.test_client()
        data = {
            **self.csrf_data(client, "/llm-profiles"),
            "name": "Test",
            "provider": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "model-x",
            "custom_headers": "[]",
        }
        with patch("daily_coolpapers.app.db.save_llm_profile") as save_profile:
            response = client.post("/api/llm-profiles", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["errors"]["custom_headers"], "必须是 JSON object")
        save_profile.assert_not_called()

    def test_settings_and_logs_use_job_status_payloads(self):
        client = self.app.test_client()
        job = {
            "id": 321,
            "type": "crawl",
            "status": "failed",
            "progress_current": 1,
            "progress_total": 3,
            "progress_percent": 33,
            "progress_message": "ignored for failed jobs",
            "progress_details": {},
            "error_message": "boom",
            "started_at": "2026-06-29 10:00:00",
            "finished_at": "2026-06-29 10:01:00",
            "created_at": "2026-06-29 09:59:00",
        }

        with patch("daily_coolpapers.app.db.list_job_summaries", return_value=[job]) as list_jobs:
            response = client.get("/settings")
        self.assertEqual(response.status_code, 200)
        list_jobs.assert_called_once_with(30)
        self.assertIn("Metadata".encode(), response.data)
        self.assertIn("boom".encode(), response.data)

        with patch("daily_coolpapers.app.db.list_job_summaries", return_value=[job]) as list_jobs:
            response = client.get("/logs")
        self.assertEqual(response.status_code, 200)
        list_jobs.assert_called_once_with(80)
        self.assertIn("Metadata".encode(), response.data)
        self.assertIn("boom".encode(), response.data)

    def test_llm_page_has_prompt_model_bindings(self):
        client = self.app.test_client()
        response = client.get("/llm-profiles")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt 模型绑定".encode("utf-8"), response.data)
        self.assertIn("参数说明".encode("utf-8"), response.data)
        self.assertIn("Context Window Tokens".encode("utf-8"), response.data)

    def test_llm_page_stays_available_when_a_saved_key_cannot_be_decrypted(self):
        from daily_coolpapers import db

        profile_id = db.save_llm_profile(
            {
                "name": "Unreadable Key",
                "provider": "openai_compatible",
                "base_url": "https://example.test/v1",
                "model": "model-x",
                "encrypted_api_key_ref": "dpapi:not-valid-base64",
                "custom_headers": "{}",
                "temperature": 0.2,
                "max_output_tokens": 2000,
                "context_window_tokens": 128000,
                "timeout_seconds": 120,
                "enabled": True,
                "is_default_abstract": False,
                "is_default_fulltext": False,
            }
        )

        def remove_profile():
            with db.connect_llm_profiles() as conn:
                conn.execute("DELETE FROM llm_profiles WHERE id = ?", (profile_id,))

        self.addCleanup(remove_profile)

        response = self.app.test_client().get("/llm-profiles")

        self.assertEqual(response.status_code, 200)
        self.assertIn("无法解密，请重新输入".encode("utf-8"), response.data)

    def test_paper_detail_uses_evaluation_action_view_model(self):
        client = self.app.test_client()
        paper = {
            "id": 7,
            "title": "Action Paper",
            "authors_list": ["Ada"],
            "arxiv_id": "2606.00007",
            "published_at": "2026-06-30",
            "abstract": "A paper.",
        }

        def prompt_options(evaluation_type):
            return [{"value": "11", "label": f"{evaluation_type} prompt", "disabled": False}]

        with (
            patch("daily_coolpapers.app.db.get_paper", return_value=paper),
            patch("daily_coolpapers.app.db.get_paper_categories", return_value=[]),
            patch("daily_coolpapers.app.paper_evaluation_result_model", return_value=_empty_evaluation_results()) as results,
            patch("daily_coolpapers.app.has_pdf", return_value=False),
            patch("daily_coolpapers.app.has_markdown", return_value=False),
            patch("daily_coolpapers.app.evaluation_prompt_options", side_effect=prompt_options) as options,
            patch("daily_coolpapers.app.render_template", return_value="ok") as render,
        ):
            response = client.get("/papers/7")

        self.assertEqual(response.status_code, 200)
        options.assert_any_call("abstract_review")
        options.assert_any_call("fulltext_review")
        results.assert_called_once_with(7)
        context = render.call_args.kwargs
        self.assertEqual(render.call_args.args[0], "paper_detail.html")
        self.assertIn("evaluation_actions", context)
        self.assertIn("evaluation_results", context)
        self.assertNotIn("abstract_prompts", context)
        self.assertNotIn("fulltext_prompts", context)
        self.assertNotIn("evaluations", context)
        self.assertNotIn("latest_abstract_eval", context)
        self.assertNotIn("latest_fulltext_eval", context)
        self.assertNotIn("latest_successful_fulltext_eval", context)
        self.assertEqual(
            [action["key"] for action in context["evaluation_actions"]],
            ["abstract_review", "fulltext_review"],
        )

    def test_paper_detail_renders_evaluation_actions(self):
        from flask import template_rendered

        client = self.app.test_client()
        paper = {
            "id": 7,
            "title": "Action Paper",
            "authors_list": ["Ada"],
            "arxiv_id": "2606.00007",
            "published_at": "2026-06-30",
            "abstract": "A paper.",
        }
        recorded = []

        def record(_sender, template, context, **_extra):
            recorded.append((template.name, context))

        template_rendered.connect(record, self.app)
        try:
            with (
                patch("daily_coolpapers.app.db.get_paper", return_value=paper),
                patch("daily_coolpapers.app.db.get_paper_categories", return_value=[]),
                patch(
                    "daily_coolpapers.app.paper_evaluation_result_model",
                    return_value=_renderable_evaluation_results(),
                ),
                patch("daily_coolpapers.app.has_pdf", return_value=False),
                patch("daily_coolpapers.app.has_markdown", return_value=False),
                patch(
                    "daily_coolpapers.app.evaluation_prompt_options",
                    side_effect=lambda evaluation_type: [
                        {"value": "11", "label": f"{evaluation_type} prompt", "disabled": False}
                    ],
                ),
            ):
                response = client.get("/papers/7")
        finally:
            template_rendered.disconnect(record, self.app)

        self.assertEqual(response.status_code, 200)
        self.assertIn("abstract_review prompt".encode(), response.data)
        self.assertIn("fulltext_review prompt".encode(), response.data)
        self.assertIn("Fulltext one line".encode(), response.data)
        self.assertIn("Detailed result".encode(), response.data)
        self.assertIn("全文评估".encode("utf-8"), response.data)
        self.assertEqual(recorded[0][0], "paper_detail.html")
        self.assertIn("evaluation_actions", recorded[0][1])
        self.assertIn("evaluation_results", recorded[0][1])

    def test_paper_markdown_export_uses_evaluation_export_builder(self):
        client = self.app.test_client()
        paper = {
            "id": 7,
            "title": "Export Paper",
            "arxiv_id": "2606.00007",
            "published_at": "2026-06-30",
            "abstract": "A paper.",
        }
        with (
            patch("daily_coolpapers.app.db.get_paper", return_value=paper),
            patch("daily_coolpapers.app.build_paper_evaluation_export", return_value="# Export") as build_export,
        ):
            response = client.get("/papers/7/export.md")

        self.assertEqual(response.status_code, 200)
        build_export.assert_called_once_with(paper)
        self.assertEqual(response.data, b"# Export")

    def test_paper_evaluation_routes_validate_prompt_before_enqueue(self):
        from daily_coolpapers import services

        client = self.app.test_client()
        csrf_data = self.csrf_data(client)
        config = services.EvaluationConfig(
            evaluation_type="abstract_review",
            prompt={"id": 44, "version": 2, "template": "Title: {{ title }}", "enabled": 1},
            profile={"id": 55, "model": "test-model", "enabled": 1},
        )
        with (
            patch("daily_coolpapers.app.resolve_evaluation_config", return_value=config) as resolve,
            patch("daily_coolpapers.app.job_runner.enqueue", return_value=999) as enqueue,
        ):
            response = client.post(
                "/api/papers/7/evaluate-abstract",
                data={**csrf_data, "prompt_id": "44", "from_detail": "1"},
            )

        self.assertEqual(response.status_code, 302)
        resolve.assert_called_once_with("abstract_review", 44)
        enqueue.assert_called_once_with("abstract_eval", {"paper_id": 7, "prompt_id": 44})

        with (
            patch("daily_coolpapers.app.resolve_evaluation_config", side_effect=ValueError("wrong prompt")),
            patch("daily_coolpapers.app.job_runner.enqueue") as enqueue,
        ):
            response = client.post(
                "/api/papers/7/evaluate-fulltext",
                data={
                    **csrf_data,
                    "prompt_id": "44",
                    "force_markdown": "1",
                    "from_detail": "1",
                },
            )

        self.assertEqual(response.status_code, 302)
        enqueue.assert_not_called()
    def test_prompt_model_binding_save(self):
        client = self.app.test_client()
        response = client.post(
            "/api/prompt-model-bindings",
            data=self.csrf_data(client, "/llm-profiles"),
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt 模型绑定已保存".encode("utf-8"), response.data)

    def test_jobs_progress_endpoint(self):
        client = self.app.test_client()
        job = {
            "id": 123,
            "type": "crawl",
            "status": "running",
            "progress_current": 1,
            "progress_total": 4,
            "progress_percent": 25,
            "progress_message": "running",
            "progress_details": {
                "phase": "crawl",
                "summary": {"success": 1, "total": 2, "failed": 0, "saved": 1, "running": 1, "pending": 0},
                "categories": [{"category": "cs.AI", "status": "running", "attempt": 1, "max_attempts": 3}],
            },
            "error_message": None,
            "started_at": "2026-06-29 10:00:00",
            "finished_at": None,
            "created_at": "2026-06-29 09:59:00",
        }
        with (
            patch("daily_coolpapers.app.job_runner.reconcile_orphaned_pending_jobs") as reconcile,
            patch("daily_coolpapers.app.db.list_active_job_progress", return_value=[job]) as list_progress,
        ):
            response = client.get("/api/jobs/progress")

        self.assertEqual(response.status_code, 200)
        reconcile.assert_not_called()
        list_progress.assert_called_once_with(12)
        data = response.get_json()
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(data["jobs"][0]["id"], 123)
        self.assertIn("progress_details", data["jobs"][0])
        self.assertIn("type_label", data["jobs"][0])
        self.assertIn("status_label", data["jobs"][0])
        self.assertIn("progress_label", data["jobs"][0])
        self.assertEqual(data["jobs"][0]["message"], "running")
        self.assertEqual(data["jobs"][0]["detail"]["item_rows"][0]["title"], "cs.AI")

    def test_split_crawl_and_eval_routes_enqueue(self):
        client = self.app.test_client()
        csrf_data = self.csrf_data(client)
        with (
            patch("daily_coolpapers.app.job_runner.enqueue", return_value=999) as enqueue,
            patch("daily_coolpapers.app.job_runner.enqueue_pipeline", return_value=(998, True)) as pipeline,
        ):
            for path, text in [
                ("/api/crawl/run", "抓取并摘要评估任务"),
                ("/api/crawl/catch-up", "补抓并摘要评估任务"),
                ("/api/abstract-evaluations/run", "摘要评估任务"),
            ]:
                response = client.post(path, data=csrf_data)
                self.assertEqual(response.status_code, 302, path)
                with client.session_transaction() as session:
                    self.assertTrue(any(text in message for _, message in session.get('_flashes', [])))
                if path.startswith('/api/crawl/'):
                    self.assertTrue(response.location.endswith('/jobs/998'))
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(pipeline.call_count, 2)

    def test_health_endpoint(self):
        client = self.app.test_client()
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "daily-coolpapers")

    def test_shutdown_page_loads_without_exiting_in_tests(self):
        client = self.app.test_client()
        response = client.post("/api/shutdown", data=self.csrf_data(client))
        self.assertEqual(response.status_code, 200)
        self.assertIn("服务正在退出".encode("utf-8"), response.data)

    def test_post_without_csrf_token_is_rejected_before_enqueue(self):
        client = self.app.test_client()
        with patch("daily_coolpapers.app.job_runner.enqueue") as enqueue:
            response = client.post("/api/crawl/run")
        self.assertEqual(response.status_code, 403)
        enqueue.assert_not_called()

    def test_cross_origin_post_is_rejected(self):
        client = self.app.test_client()
        with patch("daily_coolpapers.app.job_runner.enqueue") as enqueue:
            response = client.post(
                "/api/crawl/run",
                data=self.csrf_data(client),
                headers={"Origin": "https://evil.example"},
            )
        self.assertEqual(response.status_code, 403)
        enqueue.assert_not_called()

    def test_back_redirect_rejects_external_referrer(self):
        from daily_coolpapers.app import _back_to_detail_or_index

        with self.app.test_request_context(
            "/api/papers/1/evaluate-abstract",
            method="POST",
            headers={"Referer": "https://evil.example/landing"},
        ):
            self.assertEqual(_back_to_detail_or_index(1), "/")

    def test_session_cookie_uses_lax_samesite(self):
        client = self.app.test_client()
        response = client.get("/")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)


if __name__ == "__main__":
    unittest.main()
