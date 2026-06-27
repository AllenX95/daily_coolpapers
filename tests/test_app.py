import os
import unittest
from unittest.mock import patch


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DAILY_COOLPAPERS_DISABLE_WORKER"] = "1"
        os.environ["DAILY_COOLPAPERS_DISABLE_SHUTDOWN"] = "1"
        from daily_coolpapers.app import create_app

        cls.app = create_app()
        cls.app.config.update(TESTING=True)

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

    def test_llm_page_has_prompt_model_bindings(self):
        client = self.app.test_client()
        response = client.get("/llm-profiles")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt 模型绑定".encode("utf-8"), response.data)
        self.assertIn("参数说明".encode("utf-8"), response.data)
        self.assertIn("Context Window Tokens".encode("utf-8"), response.data)

    def test_prompt_model_binding_save(self):
        client = self.app.test_client()
        response = client.post("/api/prompt-model-bindings", follow_redirects=True)
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
        with patch("daily_coolpapers.app.job_runner.enqueue", return_value=999) as enqueue:
            for path, text in [
                ("/api/crawl/run", "metadata 抓取任务"),
                ("/api/crawl/catch-up", "metadata 补抓到最新任务"),
                ("/api/abstract-evaluations/run", "摘要评估任务"),
            ]:
                response = client.post(path, follow_redirects=True)
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
        response = client.post("/api/shutdown")
        self.assertEqual(response.status_code, 200)
        self.assertIn("服务正在退出".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
