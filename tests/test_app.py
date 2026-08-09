import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


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
        self.assertIn("抓取最新 Metadata".encode("utf-8"), response.data)
        self.assertIn("补抓到最新".encode("utf-8"), response.data)
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

    def test_management_pages_load(self):
        client = self.app.test_client()
        for path in ["/favorites", "/categories", "/prompts", "/llm-profiles", "/settings", "/logs"]:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_favorites_page_loads(self):
        client = self.app.test_client()
        response = client.get("/favorites")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fulltext-favorites".encode(), response.data)
        self.assertIn("score_desc".encode(), response.data)

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

    def test_llm_page_has_prompt_model_bindings(self):
        client = self.app.test_client()
        response = client.get("/llm-profiles")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt 模型绑定".encode("utf-8"), response.data)
        self.assertIn("参数说明".encode("utf-8"), response.data)
        self.assertIn("Context Window Tokens".encode("utf-8"), response.data)

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
        response = client.get("/api/jobs/progress")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("jobs", data)
        if data["jobs"]:
            self.assertIn("progress_details", data["jobs"][0])
            self.assertIn("type_label", data["jobs"][0])
            self.assertIn("status_label", data["jobs"][0])
            self.assertIn("progress_label", data["jobs"][0])

    def test_split_crawl_and_eval_routes_enqueue(self):
        client = self.app.test_client()
        csrf_data = self.csrf_data(client)
        with patch("daily_coolpapers.app.job_runner.enqueue", return_value=999) as enqueue:
            for path, text in [
                ("/api/crawl/run", "metadata 抓取任务"),
                ("/api/crawl/catch-up", "metadata 补抓到最新任务"),
                ("/api/abstract-evaluations/run", "摘要评估任务"),
            ]:
                response = client.post(path, data=csrf_data, follow_redirects=True)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn(text.encode("utf-8"), response.data)
            self.assertEqual(enqueue.call_count, 3)

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
