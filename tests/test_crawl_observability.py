import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx

from daily_coolpapers import cache_manager, db, services
from daily_coolpapers.crawler import (
    CategoryFetchResult,
    CrawlFetchError,
    extract_declared_total,
    fetch_category,
    fetch_category_report,
    parse_papers_with_diagnostics,
)


def _paper(arxiv_id: str, title: str = "Useful Paper") -> dict:
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": ["Ada"],
        "abstract": "A useful abstract.",
        "subjects": ["cs.AI"],
        "published_at": "2026-09-03 01:00:00 UTC",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "papers_cool_url": f"https://papers.cool/arxiv/{arxiv_id}",
        "rank": 1,
        "reading_stars": 3,
        "pdf_clicks": 2,
        "kimi_clicks": 1,
    }


def _paper_block(arxiv_id: str, rank: int, *, abstract: str = "A useful abstract.") -> str:
    abstract_html = f"<p>{abstract}</p>" if abstract else ""
    return f"""
      <h2><a href="/arxiv/{arxiv_id}">#{rank}</a>
      <a href="/arxiv/{arxiv_id}">Paper {rank}</a></h2>
      <p>Authors: Ada</p>
      {abstract_html}
      <p>Subjects: cs.AI</p>
      <p>Publish: 2026-09-03 01:00:00 UTC</p>
    """


def _html(*blocks: str, page_date: str | None = "2026-09-03", total: int | None = None) -> str:
    date_html = f'<a class="date">{page_date}</a>' if page_date else ""
    total_html = f"<div>Total: {total:,}</div>" if total is not None else ""
    return f"<html><body>{date_html}{total_html}{''.join(blocks)}</body></html>"


class FakeResponse:
    def __init__(self, html: str, *, status_code: int = 200, url: str | None = None):
        self.text = html
        self.content = html.encode("utf-8")
        self.status_code = status_code
        self.url = httpx.URL(url or "https://papers.cool/arxiv/cs.AI?show=30&sort=1")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", str(self.url))
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=request, response=httpx.Response(self.status_code)
            )


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def get(self, *_args, **_kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


class CrawlParsingTests(unittest.TestCase):
    def test_parser_normalizes_arxiv_version_suffix(self):
        versioned = _html(_paper_block("2609.00001v3", 1), total=1)

        parsed = parse_papers_with_diagnostics(
            versioned, "cs.AI", 30, "https://papers.cool/arxiv/cs.AI"
        )

        self.assertEqual(parsed.papers[0].arxiv_id, "2609.00001")

    def test_extracts_declared_total_and_complete_metrics(self):
        html = _html(
            _paper_block("2609.00001", 1),
            _paper_block("2609.00002", 2),
            total=2,
        )
        parsed = parse_papers_with_diagnostics(
            html, "cs.AI", 30, "https://papers.cool/arxiv/cs.AI"
        )
        result = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(html)),
        )

        self.assertEqual(extract_declared_total(html), 2)
        self.assertEqual(parsed.metrics["parsed_count"], 2)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.metrics["expected_count"], 2)
        self.assertEqual(result.metrics["valid_arxiv_count"], 2)
        self.assertEqual(result.metrics["response_bytes"], len(html.encode("utf-8")))
        self.assertEqual(len(result.metrics["content_sha256"]), 64)
        self.assertEqual(
            [event["event"] for event in result.attempt_events],
            ["attempt", "http_succeeded"],
        )

    def test_legal_empty_unknown_total_and_anomalous_empty_are_distinct(self):
        legal = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(_html(total=0))),
        )
        unknown_with_data = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(_html(_paper_block("2609.00003", 1)))),
        )
        nonzero_empty = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(_html(total=3))),
        )
        unknown_empty = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(_html())),
        )

        self.assertEqual(legal.status, "empty_success")
        self.assertEqual(unknown_with_data.status, "warning")
        self.assertIn("declared_total_unknown", unknown_with_data.error_codes)
        self.assertEqual(nonzero_empty.status, "failed")
        self.assertIn("parse_zero_with_nonzero_total", nonzero_empty.error_codes)
        self.assertEqual(unknown_empty.status, "failed")
        self.assertEqual(
            set(unknown_empty.error_codes),
            {"declared_total_unknown", "parse_zero_without_total"},
        )
        self.assertEqual(unknown_empty.metrics["primary_error_code"], "parse_zero_without_total")
        with self.assertRaisesRegex(CrawlFetchError, "完整性检查失败"):
            fetch_category(
                "cs.AI",
                crawl_date="2026-09-03",
                client=FakeClient(FakeResponse(_html(total=1))),
            )

    def test_date_and_field_integrity_generate_stable_codes(self):
        mismatched = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(
                FakeResponse(
                    _html(_paper_block("2609.00004", 1), page_date="2026-09-02", total=1)
                )
            ),
        )
        missing_abstract = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(
                FakeResponse(_html(_paper_block("2609.00005", 1, abstract=""), total=1))
            ),
        )
        tolerated = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            missing_field_warning_rate=1.0,
            client=FakeClient(
                FakeResponse(_html(_paper_block("2609.00006", 1, abstract=""), total=1))
            ),
        )

        self.assertEqual(mismatched.status, "failed")
        self.assertIn("page_date_mismatch", mismatched.error_codes)
        self.assertEqual(missing_abstract.status, "warning")
        self.assertEqual(missing_abstract.metrics["missing_abstract"], 1)
        self.assertIn("missing_critical_fields", missing_abstract.error_codes)
        self.assertEqual(tolerated.status, "success")

    def test_incomplete_and_missing_identifier_metrics_are_explicit(self):
        incomplete = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(_html(_paper_block("2609.00008", 1), total=2))),
        )
        missing_id_html = _html(
            """
            <h2><a href="/not-arxiv">#1</a><a href="/not-arxiv">Unknown Paper</a></h2>
            <p>Authors: Ada</p><p>Abstract text.</p><p>Publish: 2026-09-03</p>
            """,
            total=1,
        )
        missing_id = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(FakeResponse(missing_id_html)),
        )
        unknown_date = fetch_category_report(
            "cs.AI",
            crawl_date="2026-09-03",
            client=FakeClient(
                FakeResponse(_html(_paper_block("2609.00009", 1), page_date=None, total=1))
            ),
        )

        self.assertEqual(incomplete.status, "warning")
        self.assertIn("parse_incomplete", incomplete.error_codes)
        self.assertEqual(missing_id.metrics["missing_arxiv_id"], 1)
        self.assertEqual(missing_id.metrics["parsed_count"], 1)
        self.assertEqual(missing_id.metrics["valid_arxiv_count"], 0)
        self.assertIn("missing_arxiv_id", missing_id.error_codes)
        self.assertEqual(missing_id.status, "failed")
        self.assertEqual(unknown_date.status, "warning")
        self.assertIn("page_date_unknown", unknown_date.error_codes)

    def test_retry_metrics_and_unexpected_redirect_are_safe(self):
        client = FakeClient(
            httpx.ReadTimeout("timeout"),
            FakeResponse(_html(_paper_block("2609.00007", 1), total=1)),
        )
        result = fetch_category_report(
            "cs.AI", crawl_date="2026-09-03", retries=1, client=client
        )

        self.assertEqual(client.calls, 2)
        self.assertEqual(result.metrics["retry_count"], 1)
        self.assertEqual(
            [event["event"] for event in result.attempt_events],
            ["attempt", "attempt_failed", "attempt", "http_succeeded"],
        )
        self.assertEqual(result.attempt_events[1]["error_code"], "network_timeout")

        redirect_client = FakeClient(
            FakeResponse(
                _html(total=0),
                url="https://evil.example/login?token=secret&date=2026-09-03",
            )
        )
        with self.assertRaises(CrawlFetchError) as caught:
            fetch_category_report(
                "cs.AI", crawl_date="2026-09-03", retries=2, client=redirect_client
            )
        self.assertEqual(caught.exception.error_code, "unexpected_redirect")
        self.assertEqual(redirect_client.calls, 1)
        self.assertNotIn("secret", str(caught.exception.metrics))

        with self.assertRaises(CrawlFetchError) as http_error:
            fetch_category_report(
                "cs.AI",
                crawl_date="2026-09-03",
                retries=0,
                client=FakeClient(FakeResponse("service unavailable", status_code=503)),
            )
        self.assertEqual(http_error.exception.error_code, "network_http_error")

        safe_result = fetch_category_report(
            "cs.AI",
            sort_param="sort=1&token=secret",
            crawl_date="2026-09-03",
            retries=0,
            client=FakeClient(FakeResponse(_html(total=0))),
        )
        self.assertNotIn("secret", str(safe_result.metrics))
        self.assertNotIn("secret", str(safe_result.attempt_events))

    def test_unexpected_parser_exception_is_not_misclassified_or_retried(self):
        client = FakeClient(FakeResponse(_html(total=1)))
        with patch(
            "daily_coolpapers.crawler._analyze_category_response",
            side_effect=ValueError("broken parser"),
        ):
            result = fetch_category_report(
                "cs.AI", crawl_date="2026-09-03", retries=2, client=client
            )

        self.assertEqual(client.calls, 1)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metrics["primary_error_code"], "parse_incomplete")
        self.assertEqual(
            [event["event"] for event in result.attempt_events],
            ["attempt", "http_succeeded"],
        )

    def test_failure_logs_do_not_include_sensitive_exception_urls(self):
        with self.assertLogs("daily_coolpapers.crawler", level="WARNING") as captured:
            with self.assertRaises(CrawlFetchError):
                fetch_category_report(
                    "cs.AI", retries=0, sort_param="sort=1&token=SECRET",
                    client=FakeClient(httpx.ReadTimeout("https://papers.cool/?token=SECRET")),
                )
        self.assertNotIn("SECRET", "\n".join(captured.output))

    def test_callback_error_chain_and_url_userinfo_are_redacted(self):
        def broken_callback(_event):
            raise RuntimeError("database locked")
        with self.assertLogs("daily_coolpapers.crawler", level="WARNING") as captured:
            with self.assertRaises(CrawlFetchError):
                fetch_category_report(
                    'cs.AI', retries=0, attempt_progress=broken_callback,
                    client=FakeClient(httpx.ReadTimeout('https://papers.cool/?token=SECRET')),
                )
        self.assertNotIn('SECRET', '\n'.join(captured.output))
        result = fetch_category_report('cs.AI', crawl_date='2026-09-03', retries=0,
            client=FakeClient(FakeResponse(_html(total=0), url='https://user:SECRET@papers.cool/arxiv/cs.AI')))
        self.assertNotIn('SECRET', str(result.metrics))
        self.assertNotIn('user:', str(result.attempt_events))


class CrawlPersistenceAndEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "crawl.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()

    @staticmethod
    def _pipeline_payload() -> dict:
        return {
            "trigger_source": "manual_latest",
            "target_date": "2026-09-03",
            "categories": [
                {"id": 1, "category": "cs.AI", "top_n": 30, "sort": "sort=1"},
                {"id": 2, "category": "cs.LG", "top_n": 30, "sort": "sort=1"},
            ],
        }

    def test_persist_stats_distinguish_new_updated_and_duplicate(self):
        first = db.upsert_papers_with_stats(
            [_paper("2609.00011", "First"), _paper("2609.00011", "Last"), _paper("2609.00012")],
            "cs.AI",
            "2026-09-03",
        )
        second = db.upsert_papers_with_stats(
            [_paper("2609.00011", "Updated"), _paper("2609.00012")],
            "cs.AI",
            "2026-09-03",
        )

        self.assertEqual(first.paper_ids[0], first.paper_ids[1])
        self.assertEqual(first.metrics()["new_count"], 2)
        self.assertEqual(first.metrics()["duplicate_count"], 1)
        self.assertEqual(first.metrics()["membership_new_count"], 2)
        self.assertEqual(db.get_paper(first.paper_ids[0])["title"], "Updated")
        self.assertEqual(second.metrics()["new_count"], 0)
        self.assertEqual(second.metrics()["updated_count"], 1)
        self.assertEqual(second.metrics()["duplicate_count"], 1)
        self.assertEqual(second.metrics()["membership_updated_count"], 2)

    def test_http_events_are_persisted_before_fetch_finishes(self):
        job_id, _ = db.create_daily_pipeline_job(self._pipeline_payload(), idempotency_key="realtime")
        reached, release = Event(), Event()
        categories = [{"id": 1, "category": "cs.AI", "top_n": 30}]

        def fetch(_category, _date, callback, _client):
            callback({"event": "attempt_failed", "attempt": 1, "error_code": "network_timeout"})
            reached.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return CategoryFetchResult([], "empty_success", (), {"page_date": "2026-09-03"}, ())

        with (
            patch.object(services.db, "list_categories", return_value=categories),
            patch.object(services, "_crawler_client_from_settings", return_value=nullcontext(object())),
            patch.object(services, "_fetch_category_from_config", side_effect=fetch),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(services.crawl_all_categories, crawl_date="2026-09-03", pipeline_job_id=job_id)
            try:
                self.assertTrue(reached.wait(5))
                self.assertEqual(db.count_job_events(job_id, event_type="crawl.http_failed"), 1)
                self.assertEqual(db.count_job_events(job_id, event_type="crawl.category_completed"), 0)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["status"], "success")

    def test_event_retention_preserves_active_jobs_and_summary(self):
        terminal_id = db.create_job("crawl", {})
        active_id = db.create_job("crawl", {})
        db.update_job(terminal_id, "running")
        db.update_job(terminal_id, "success")
        for job_id in (terminal_id, active_id):
            db.append_job_event(job_id, event_key=f"old:{job_id}", stage="plan", event_type="test")
        with db.connect() as conn:
            conn.execute("UPDATE job_events SET created_at = '2026-01-01T00:00:00+00:00'")
        db.append_job_event(terminal_id, event_key="new", stage="plan", event_type="test")
        current = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.assertEqual(db.delete_expired_job_events(-1, current_time=current), 0)
        self.assertEqual(db.delete_expired_job_events(current_time=current), 1)
        self.assertEqual(db.count_job_events(active_id), 1)
        self.assertEqual(db.count_job_events(terminal_id), 1)
        self.assertEqual(db.get_job(terminal_id)["status"], "success")
        with (
            patch.object(cache_manager, "ensure_directories"),
            patch.object(cache_manager, "cleanup_directory", return_value=0),
            patch.object(cache_manager, "delete_expired_job_events", return_value=2) as cleanup,
        ):
            self.assertEqual(cache_manager.cleanup_caches()["job_events_deleted"], 2)
        cleanup.assert_called_once_with()

    def test_category_failure_is_isolated_and_every_unit_has_one_terminal_event(self):
        job_id, _ = db.create_daily_pipeline_job(
            self._pipeline_payload(), idempotency_key="crawl-events"
        )
        categories = [
            {"id": 1, "category": "cs.AI", "name": "AI", "top_n": 30, "sort_param": "sort=1"},
            {"id": 2, "category": "cs.LG", "name": "ML", "top_n": 30, "sort_param": "sort=1"},
        ]

        def fake_fetch(category, *_args, **_kwargs):
            name = category["category"]
            attempt_callback = _args[1]
            if name == "cs.LG":
                failed_attempts = (
                    {"event": "attempt", "attempt": 1, "max_attempts": 1},
                    {
                        "event": "attempt_failed",
                        "attempt": 1,
                        "max_attempts": 1,
                        "error_code": "network_timeout",
                        "metrics": {"retry_count": 0},
                    },
                )
                for event in failed_attempts:
                    attempt_callback(event)
                raise CrawlFetchError(
                    "抓取 cs.LG 失败（network_timeout）",
                    error_code="network_timeout",
                    metrics={"target_date": "2026-09-03", "category": name, "retry_count": 2},
                    attempt_events=failed_attempts,
                )
            successful_attempts = (
                {"event": "attempt", "attempt": 1, "max_attempts": 2},
                {
                    "event": "attempt_failed",
                    "attempt": 1,
                    "max_attempts": 2,
                    "error_code": "network_timeout",
                    "metrics": {"retry_count": 0},
                },
                {"event": "attempt", "attempt": 2, "max_attempts": 2},
                {
                    "event": "http_succeeded",
                    "attempt": 2,
                    "max_attempts": 2,
                    "metrics": {"http_status": 200, "response_bytes": 100, "retry_count": 1},
                },
            )
            for event in successful_attempts:
                attempt_callback(event)
            return CategoryFetchResult(
                papers=[_paper("2609.00021")],
                status="success",
                error_codes=(),
                metrics={
                    "target_date": "2026-09-03",
                    "page_date": "2026-09-03",
                    "category": name,
                    "declared_total": 1,
                    "expected_count": 1,
                    "parsed_count": 1,
                    "valid_arxiv_count": 1,
                    "retry_count": 1,
                },
                attempt_events=successful_attempts,
            )

        with (
            patch.object(services.db, "list_categories", return_value=categories),
            patch.object(services, "_crawler_client_from_settings", return_value=nullcontext(object())),
            patch.object(services, "_fetch_category_from_config", side_effect=fake_fetch),
        ):
            result = services.crawl_all_categories(
                crawl_date="2026-09-03", pipeline_job_id=job_id
            )

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["saved"], 1)
        terminal_events = [
            event
            for event in db.list_job_events(job_id, event_type="crawl.category_completed")
        ]
        self.assertEqual(len(terminal_events), 2)
        self.assertEqual(
            {event["event_key"] for event in terminal_events},
            {
                f"crawl:{job_id}:2026-09-03:cs.AI:terminal",
                f"crawl:{job_id}:2026-09-03:cs.LG:terminal",
            },
        )
        terminal_by_category = {event["category"]: event for event in terminal_events}
        self.assertEqual(terminal_by_category["cs.AI"]["metrics"]["new_count"], 1)
        self.assertEqual(terminal_by_category["cs.AI"]["attempt"], 2)
        self.assertEqual(terminal_by_category["cs.LG"]["error_code"], "network_timeout")
        ai_types = [
            event["event_type"]
            for event in db.list_job_events(job_id)
            if event["category"] == "cs.AI"
        ]
        self.assertEqual(
            ai_types,
            [
                "crawl.category_started",
                "crawl.http_failed",
                "crawl.http_retrying",
                "crawl.http_succeeded",
                "crawl.parse_completed",
                "crawl.persist_completed",
                "crawl.category_completed",
            ],
        )

    def test_warning_makes_partial_success_and_all_legal_empty_is_success(self):
        warning_result = CategoryFetchResult(
            papers=[_paper("2609.00031")],
            status="warning",
            error_codes=("declared_total_unknown",),
            metrics={"page_date": "2026-09-03", "parsed_count": 1},
            attempt_events=(),
        )
        empty_result = CategoryFetchResult(
            papers=[],
            status="empty_success",
            error_codes=(),
            metrics={"page_date": "2026-09-03", "declared_total": 0, "parsed_count": 0},
            attempt_events=(),
        )
        categories = [
            {"id": 1, "category": "cs.AI", "name": "AI", "top_n": 30, "sort_param": "sort=1"}
        ]
        with (
            patch.object(services.db, "list_categories", return_value=categories),
            patch.object(services, "_crawler_client_from_settings", return_value=nullcontext(object())),
            patch.object(services, "_fetch_category_from_config", return_value=warning_result),
        ):
            warning_run = services.crawl_all_categories(crawl_date="2026-09-03")
        with (
            patch.object(services.db, "list_categories", return_value=categories),
            patch.object(services, "_crawler_client_from_settings", return_value=nullcontext(object())),
            patch.object(services, "_fetch_category_from_config", return_value=empty_result),
        ):
            empty_run = services.crawl_all_categories(crawl_date="2026-09-03")

        self.assertEqual(warning_run["status"], "partial_success")
        self.assertEqual(empty_run["status"], "success")
        self.assertEqual(empty_run["summary"]["empty_success"], 1)

    def test_database_failure_is_isolated_with_stable_terminal_error(self):
        payload = self._pipeline_payload()
        payload["categories"] = payload["categories"][:1]
        job_id, _ = db.create_daily_pipeline_job(
            payload, idempotency_key="crawl-database-failure"
        )
        categories = [
            {"id": 1, "category": "cs.AI", "name": "AI", "top_n": 30, "sort_param": "sort=1"}
        ]
        fetched = CategoryFetchResult(
            papers=[_paper("2609.00041")],
            status="success",
            error_codes=(),
            metrics={"page_date": "2026-09-03", "declared_total": 1, "parsed_count": 1},
            attempt_events=(),
        )
        with (
            patch.object(services.db, "list_categories", return_value=categories),
            patch.object(services, "_crawler_client_from_settings", return_value=nullcontext(object())),
            patch.object(services, "_fetch_category_from_config", return_value=fetched),
            patch.object(services.db, "upsert_papers_with_stats", side_effect=RuntimeError("db down")),
        ):
            result = services.crawl_all_categories(
                crawl_date="2026-09-03", pipeline_job_id=job_id
            )

        self.assertEqual(result["status"], "failed")
        terminal = db.list_job_events(job_id, event_type="crawl.category_completed")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["error_code"], "database_write_failed")
        self.assertEqual(terminal[0]["metrics"]["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
