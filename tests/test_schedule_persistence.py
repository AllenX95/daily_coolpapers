import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db, schedule_db


class PersistentScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "main.sqlite3"
        self.patch_db = patch.object(db, "DB_PATH", self.db_path)
        self.patch_dirs = patch.object(db, "ensure_directories")
        self.patch_db.start()
        self.patch_dirs.start()
        self.addCleanup(self.patch_dirs.stop)
        self.addCleanup(self.patch_db.stop)
        db.init_db()
        self.plan_calls = []

    @staticmethod
    def now(hour: int, minute: int = 0, day: int = 5) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=schedule_db.SHANGHAI_TZ)

    def plan(self, source, *, now=None):
        self.plan_calls.append(now)
        return {
            "trigger_source": source,
            "timezone": "Asia/Shanghai",
            "dates": ["2026-09-05"],
            "target_date": "2026-09-05",
            "start_date": "2026-09-05",
            "end_date": "2026-09-05",
            "categories": [],
        }

    def reconcile(self, now, times="10:30,12:00", **kwargs):
        plan_builder = kwargs.pop("plan_builder", self.plan)
        return schedule_db.reconcile_daily_schedule(
            now,
            enabled=True,
            daily_times=times,
            plan_builder=plan_builder,
            **kwargs,
        )

    def test_late_start_coalesces_and_cleanup_is_independently_deduplicated(self):
        result = self.reconcile(self.now(13))
        self.assertTrue(result.queue_job)
        self.assertTrue(result.queue_cleanup_job)
        self.assertEqual(len(self.plan_calls), 1)
        rows = schedule_db.list_occurrences(local_date="2026-09-05")
        pipeline = [row for row in rows if row["schedule_kind"] == "daily_pipeline"]
        self.assertEqual([(row["scheduled_time"], row["state"]) for row in pipeline], [
            ("10:30", "coalesced"),
            ("12:00", "dispatched"),
        ])
        self.assertEqual(len([row for row in rows if row["schedule_kind"] == "cache_cleanup"]), 1)
        with db.connect() as conn:
            job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            updated_at = conn.execute(
                "SELECT updated_at FROM schedule_occurrences WHERE schedule_kind='daily_pipeline' AND scheduled_time='12:00'"
            ).fetchone()[0]
        self.assertEqual(job_count, 2)
        second = self.reconcile(self.now(13))
        self.assertIsNone(second.job_id)
        self.assertEqual(len(self.plan_calls), 1)
        with db.connect() as conn:
            self.assertEqual(
                updated_at,
                conn.execute(
                    "SELECT updated_at FROM schedule_occurrences WHERE schedule_kind='daily_pipeline' AND scheduled_time='12:00'"
                ).fetchone()[0],
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_active_job_keeps_due_and_builder_failure_does_not_coalesce(self):
        active = db.create_job("crawl", {})
        blocked = self.reconcile(self.now(13))
        self.assertIsNone(blocked.job_id)
        rows = schedule_db.list_occurrences(schedule_kind="daily_pipeline", local_date="2026-09-05")
        self.assertEqual([row["state"] for row in rows], ["due", "due"])
        db.update_job(active, "running")
        db.update_job(active, "success")

        def fail(*_args, **_kwargs):
            raise ValueError("invalid plan")

        with self.assertRaisesRegex(ValueError, "invalid plan"):
            self.reconcile(self.now(13), plan_builder=fail)

        rows = schedule_db.list_occurrences(schedule_kind="daily_pipeline", local_date="2026-09-05")
        self.assertEqual([row["state"] for row in rows], ["due", "due"])

    def test_builder_failure_rolls_back_new_occurrences_and_migration_marker(self):
        def fail(*_args, **_kwargs):
            raise ValueError("invalid plan")

        with self.assertRaisesRegex(ValueError, "invalid plan"):
            self.reconcile(self.now(13), plan_builder=fail)

        self.assertEqual(
            schedule_db.list_occurrences(
                schedule_kind="daily_pipeline", local_date="2026-09-05"
            ),
            [],
        )
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertIsNone(
                conn.execute(
                    "SELECT value FROM schedule_metadata WHERE key='legacy_scheduled_jobs_v1'"
                ).fetchone()
            )

    def test_legacy_migration_fails_closed_when_schedule_schema_is_missing(self):
        # Startup tests mock init_db to isolate ordering.  A real migration
        # must fail when schema initialization was skipped rather than
        # silently claiming the one-time migration completed.
        missing_path = Path(self.tmp.name) / "missing-schema.sqlite3"
        with patch.object(db, "DB_PATH", missing_path):
            with db.connect():
                pass
            with self.assertRaises(sqlite3.OperationalError):
                schedule_db.migrate_legacy_scheduled_jobs(self.now(13))

    def test_old_scheduled_key_is_migrated_once_without_rebuilding_plan(self):
        payload = {"trigger_source": "scheduled", "timezone": "Asia/Shanghai", "categories": []}
        job_id, created = db.create_daily_pipeline_job(
            payload,
            idempotency_key="scheduled:2026-09-05:12:00",
        )
        self.assertTrue(created)
        result = self.reconcile(self.now(13), times="12:00")
        self.assertIsNone(result.job_id)
        self.assertFalse(result.queue_job)
        self.assertEqual(self.plan_calls, [])
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "dispatched")
        self.assertEqual(row["job_id"], job_id)
        with db.connect() as conn:
            marker = conn.execute(
                "SELECT value FROM schedule_metadata WHERE key='legacy_scheduled_jobs_v1'"
            ).fetchone()
            self.assertEqual(marker[0], "completed")
        self.assertEqual(schedule_db.migrate_legacy_scheduled_jobs(self.now(13)), 0)

    def test_disabled_reenable_catches_up_missed_current_day_slots_and_keeps_future(self):
        disabled = schedule_db.reconcile_daily_schedule(
            self.now(13),
            enabled=False,
            daily_times="10:30,12:00,14:00",
            cleanup_daily=True,
            plan_builder=self.plan,
        )
        self.assertIsNone(disabled.job_id)
        reenabled = self.reconcile(self.now(13), times="10:30,12:00,14:00")
        self.assertIsNotNone(reenabled.job_id)
        rows = schedule_db.list_occurrences(schedule_kind="daily_pipeline", local_date="2026-09-05")
        self.assertEqual([(row["scheduled_time"], row["state"]) for row in rows], [
            ("10:30", "coalesced"), ("12:00", "dispatched"), ("14:00", "due")
        ])

    def test_config_remove_readd_reuses_cancelled_slot_and_clock_rollback_is_safe(self):
        first = self.reconcile(self.now(11), times="10:30,12:00")
        self.assertTrue(first.queue_job)
        removed = self.reconcile(self.now(11), times="10:30")
        self.assertIsNone(removed.job_id)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "cancelled")
        db.update_job(first.job_id, "running")
        db.update_job(first.job_id, "success")
        restored = self.reconcile(self.now(13), times="10:30,12:00")
        self.assertTrue(restored.queue_job)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "dispatched")
        rollback = self.reconcile(self.now(11), times="10:30,12:00")
        self.assertIsNone(rollback.job_id)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE type='daily_pipeline'").fetchone()[0], 2)

    def test_previous_day_due_is_expired_without_dispatch(self):
        active = db.create_job("crawl", {})
        self.reconcile(self.now(13, day=4), times="10:30")
        db.update_job(active, "running")
        db.update_job(active, "success")
        # There is no active job now, but the previous date is no longer
        # eligible when the next day is reconciled.
        result = self.reconcile(self.now(9, day=5), times="10:30")
        self.assertIsNone(result.job_id)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-04", "10:30")
        self.assertEqual(row["state"], "expired")


if __name__ == "__main__":
    unittest.main()
