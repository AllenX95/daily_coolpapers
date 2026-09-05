import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db
from daily_coolpapers.jobs import JobExecutionResult, JobRunner


class PipelineFoundationTests(unittest.TestCase):
    def _db_path(self, directory: str) -> Path:
        return Path(directory) / "pipeline.sqlite3"

    def _pipeline_payload(self, source: str = "manual_latest") -> dict:
        return {
            "trigger_source": source,
            "timezone": "Asia/Shanghai",
            "plan_created_at": "2026-09-03T07:00:00+00:00",
            "categories": [{"id": 1, "category": "cs.AI", "top_n": 30, "sort": "sort=1"}],
        }

    def test_init_migrates_old_job_and_evaluation_tables_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '{}',
                        error_message TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE evaluations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        paper_id INTEGER NOT NULL,
                        evaluation_type TEXT NOT NULL,
                        prompt_id INTEGER,
                        prompt_version INTEGER,
                        llm_profile_id INTEGER,
                        model TEXT,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        raw_output TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO jobs(type, status, payload, created_at)
                    VALUES ('crawl', 'success', '{}', '2026-09-01 10:00:00');
                    """
                )

            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.init_db()
                with db.connect() as conn:
                    job_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(jobs)")
                    }
                    evaluation_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")
                    }
                    event_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(job_events)")
                    }
                    indexes = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        )
                    }
                    evaluation_foreign_keys = {
                        row["from"]: row["table"]
                        for row in conn.execute("PRAGMA foreign_key_list(evaluations)")
                    }
                    old_job = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()

            self.assertEqual(old_job["status"], "success")
            self.assertTrue({"idempotency_key", "retry_of_job_id"} <= job_columns)
            self.assertIn("pipeline_job_id", evaluation_columns)
            self.assertEqual(evaluation_foreign_keys["pipeline_job_id"], "jobs")
            self.assertTrue(
                {
                    "event_key",
                    "job_id",
                    "stage",
                    "event_type",
                    "level",
                    "metrics_json",
                }
                <= event_columns
            )
            self.assertIn("idx_jobs_idempotency_key", indexes)
            self.assertIn("idx_evaluations_pipeline_job", indexes)
            self.assertIn("idx_job_events_job_timeline", indexes)

    def test_pipeline_creation_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                payload = self._pipeline_payload()
                first_id, first_created = db.create_daily_pipeline_job(
                    payload,
                    idempotency_key="manual:2026-09-03",
                )
                repeated_id, repeated_created = db.create_daily_pipeline_job(
                    payload,
                    idempotency_key="manual:2026-09-03",
                )
                active_id, active_created = db.create_daily_pipeline_job(
                    {**payload, "plan_created_at": "2026-09-03T07:01:00+00:00"},
                    idempotency_key="manual:2026-09-03:second",
                )
                payload["categories"][0]["top_n"] = 999

                self.assertEqual((repeated_id, repeated_created), (first_id, False))
                self.assertEqual((active_id, active_created), (first_id, False))
                self.assertTrue(first_created)
                first_job = db.get_job(first_id)
                self.assertEqual(first_job["type"], db.DAILY_PIPELINE_JOB_TYPE)
                self.assertEqual(first_job["payload_data"]["categories"][0]["top_n"], 30)
                self.assertEqual(db.get_active_crawl_job()["id"], first_id)
                self.assertEqual(
                    [event["event_type"] for event in db.list_job_events(first_id)],
                    ["pipeline.plan_created"],
                )

                with self.assertRaisesRegex(ValueError, "不同的流水线请求"):
                    db.create_daily_pipeline_job(
                        {**payload, "timezone": "UTC"},
                        idempotency_key="manual:2026-09-03",
                    )

                with self.assertRaisesRegex(ValueError, "未知流水线触发来源"):
                    db.create_daily_pipeline_job({"trigger_source": "unknown"})

                db.update_job(first_id, "running")
                db.update_job(first_id, "success")
                pending_retry_target = db.create_job("abstract_eval", {})
                with self.assertRaisesRegex(ValueError, "只能重试已经结束的任务"):
                    db.create_daily_pipeline_job(
                        {**payload, "plan_created_at": "2026-09-04T06:59:00+00:00"},
                        idempotency_key="manual:pending-retry",
                        retry_of_job_id=pending_retry_target,
                    )
                db.update_job(pending_retry_target, "running")
                db.update_job(pending_retry_target, "success")
                with self.assertRaisesRegex(ValueError, "历史每日情报流水线"):
                    db.create_daily_pipeline_job(
                        {**payload, "plan_created_at": "2026-09-04T06:59:30+00:00"},
                        idempotency_key="manual:wrong-retry-type",
                        retry_of_job_id=pending_retry_target,
                    )
                next_id, next_created = db.create_daily_pipeline_job(
                    {**payload, "plan_created_at": "2026-09-04T07:00:00+00:00"},
                    idempotency_key="manual:2026-09-04",
                    retry_of_job_id=first_id,
                )

            self.assertTrue(next_created)
            self.assertNotEqual(next_id, first_id)
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                self.assertEqual(db.get_job(next_id)["retry_of_job_id"], first_id)

    def test_concurrent_pipeline_creation_returns_one_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                barrier = threading.Barrier(2)
                results: list[tuple[int, bool]] = []
                errors: list[BaseException] = []

                def create(key: str) -> None:
                    try:
                        barrier.wait(2)
                        results.append(
                            db.create_daily_pipeline_job(
                                self._pipeline_payload(),
                                idempotency_key=key,
                            )
                        )
                    except BaseException as exc:  # pragma: no cover - assertion reports details
                        errors.append(exc)

                threads = [
                    threading.Thread(target=create, args=("concurrent:a",)),
                    threading.Thread(target=create, args=("concurrent:b",)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)

                self.assertFalse(errors)
                self.assertEqual(len(results), 2)
                self.assertEqual({job_id for job_id, _created in results}, {results[0][0]})
                self.assertEqual(sum(1 for _job_id, created in results if created), 1)

    def test_job_events_are_append_only_idempotent_and_filterable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                job_id = db.create_job("crawl", {})
                first_id, first_created = db.append_job_event(
                    job_id,
                    f"crawl:{job_id}:2026-09-03:cs.AI:terminal",
                    "persist",
                    "crawl.category_completed",
                    category="cs.AI",
                    crawl_date="2026-09-03",
                    metrics={"saved": 3},
                    message="抓取完成",
                )
                repeated_id, repeated_created = db.append_job_event(
                    job_id,
                    f"crawl:{job_id}:2026-09-03:cs.AI:terminal",
                    "persist",
                    "crawl.category_completed",
                    category="cs.AI",
                    crawl_date="2026-09-03",
                    metrics={"saved": 3},
                    message="抓取完成",
                )
                db.append_job_event(
                    job_id,
                    f"crawl:{job_id}:warning:1",
                    "crawl_parse",
                    "crawl.parse_anomaly",
                    level="warning",
                    metrics={"parsed": 0},
                )

                events = db.list_job_events(job_id)
                warnings = db.list_job_events(job_id, level="warning")
                warning_count = db.count_job_events(job_id, level="warning")

                self.assertEqual((repeated_id, repeated_created), (first_id, False))
                self.assertTrue(first_created)
                self.assertEqual([event["id"] for event in events], sorted(event["id"] for event in events))
                self.assertEqual(events[0]["metrics"], {"saved": 3})
                self.assertTrue(events[0]["created_at"].endswith("+00:00"))
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warning_count, 1)

                with self.assertRaisesRegex(ValueError, "不同的结构化事件"):
                    db.append_job_event(
                        job_id,
                        f"crawl:{job_id}:2026-09-03:cs.AI:terminal",
                        "persist",
                        "crawl.category_completed",
                        metrics={"saved": 4},
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.append_job_event(
                        999999,
                        "unknown-job:event",
                        "plan",
                        "pipeline.started",
                    )

    def test_job_status_and_event_update_roll_back_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                first_job = db.create_job("crawl", {})
                second_job = db.create_job("crawl", {})
                db.append_job_event(
                    first_job,
                    "shared-event-key",
                    "plan",
                    "pipeline.started",
                )

                with self.assertRaisesRegex(ValueError, "不同的结构化事件"):
                    db.update_job_with_event(
                        second_job,
                        "running",
                        event_key="shared-event-key",
                        stage="plan",
                        event_type="pipeline.started",
                    )

                self.assertEqual(db.get_job(second_job)["status"], "pending")

    def test_partial_success_and_interrupted_are_terminal_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                partial_id = db.create_job("crawl", {})
                db.update_job(partial_id, "running")
                db.update_job(partial_id, "partial_success")
                partial = db.get_job(partial_id)

                self.assertIsNotNone(partial["finished_at"])
                with self.assertRaisesRegex(ValueError, "不允许的任务状态转换"):
                    db.update_job(partial_id, "success")

                pending_pipeline, _created = db.create_daily_pipeline_job(
                    self._pipeline_payload("scheduled"),
                    idempotency_key="scheduled:2026-09-03:10:30",
                )
                running_job = db.create_job("abstract_eval", {})
                db.update_job(running_job, "running")
                changed = db.mark_unfinished_jobs_interrupted()

                self.assertEqual(changed, 2)
                self.assertEqual(db.get_job(pending_pipeline)["status"], "interrupted")
                self.assertEqual(db.get_job(running_job)["status"], "interrupted")
                events = db.list_job_events(pending_pipeline)

            self.assertEqual(
                [event["event_type"] for event in events],
                ["pipeline.plan_created", "pipeline.interrupted"],
            )
            self.assertEqual(events[1]["metrics"]["previous_status"], "pending")

    def test_evaluation_can_link_to_pipeline_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                paper_id = db.upsert_papers(
                    [
                        {
                            "arxiv_id": "2609.00001",
                            "title": "Pipeline Paper",
                            "authors": ["Ada"],
                            "abstract": "Abstract",
                            "subjects": ["cs.AI"],
                        }
                    ],
                    "cs.AI",
                    "2026-09-03",
                )[0]
                pipeline_id, _created = db.create_daily_pipeline_job(
                    self._pipeline_payload(),
                    idempotency_key="manual:paper-link",
                )
                linked_id = db.create_evaluation(
                    paper_id,
                    "abstract_review",
                    None,
                    None,
                    None,
                    "model-x",
                    "success",
                    {"score": 80},
                    "{}",
                    None,
                    pipeline_job_id=pipeline_id,
                )
                legacy_id = db.create_evaluation(
                    paper_id,
                    "fulltext_review",
                    None,
                    None,
                    None,
                    "model-x",
                    "success",
                    {"score": 90},
                    "{}",
                    None,
                )
                with db.connect() as conn:
                    rows = {
                        row["id"]: row["pipeline_job_id"]
                        for row in conn.execute(
                            "SELECT id, pipeline_job_id FROM evaluations WHERE id IN (?, ?)",
                            (linked_id, legacy_id),
                        ).fetchall()
                    }
                linked = db.list_pipeline_evaluations(pipeline_id)

            self.assertEqual(rows[linked_id], pipeline_id)
            self.assertIsNone(rows[legacy_id])
            self.assertEqual([item["id"] for item in linked], [linked_id])

    def test_job_runner_accepts_partial_success_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", self._db_path(tmp)):
                db.init_db()
                job_id = db.create_job("cleanup", {})
                runner = JobRunner()
                with patch.object(
                    runner,
                    "_dispatch",
                    return_value=JobExecutionResult({"completed": 2}, "partial_success"),
                ):
                    runner._run_job(job_id)

                job = db.get_job(job_id)

            self.assertEqual(job["status"], "partial_success")
            self.assertIsNotNone(job["finished_at"])


if __name__ == "__main__":
    unittest.main()
