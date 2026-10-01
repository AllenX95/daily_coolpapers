import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db, schedule_db
from daily_coolpapers.jobs import JobRunner


class ScheduleBoundaryTests(unittest.TestCase):
    """Failure and restart boundaries for the persistent S2 scheduler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "schedule.sqlite3"
        self.patch_db = patch.object(db, "DB_PATH", self.db_path)
        self.patch_dirs = patch.object(db, "ensure_directories")
        self.patch_db.start()
        self.patch_dirs.start()
        self.addCleanup(self.patch_dirs.stop)
        self.addCleanup(self.patch_db.stop)
        db.init_db()
        self.plan_calls: list[datetime | None] = []
        self.plan_lock = threading.Lock()

    @staticmethod
    def now(hour: int, minute: int = 0, day: int = 5) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=schedule_db.SHANGHAI_TZ)

    def plan(self, source, *, now=None):
        with self.plan_lock:
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

    def reconcile(self, now, times="10:30,12:00", *, builder=None, **kwargs):
        return schedule_db.reconcile_daily_schedule(
            now,
            enabled=True,
            daily_times=times,
            plan_builder=self.plan if builder is None else builder,
            **kwargs,
        )

    @staticmethod
    def pipeline_rows():
        return schedule_db.list_occurrences(
            schedule_kind=schedule_db.SCHEDULE_KIND_DAILY_PIPELINE,
            local_date="2026-09-05",
        )

    def test_concurrent_reconcile_dispatches_one_job_for_one_slot(self):
        barrier = threading.Barrier(3)

        def reconcile_once():
            barrier.wait(timeout=3)
            return self.reconcile(self.now(13), times="12:00", cleanup_daily=False)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reconcile_once) for _ in range(2)]
            barrier.wait(timeout=3)
            results = [future.result(timeout=5) for future in futures]

        with db.connect() as conn:
            jobs = conn.execute(
                "SELECT id, idempotency_key FROM jobs WHERE type=?",
                (db.DAILY_PIPELINE_JOB_TYPE,),
            ).fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][1], "scheduled:2026-09-05 12:00")
        self.assertEqual(sum(result.queue_job for result in results), 1)
        self.assertEqual(len(self.plan_calls), 1)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "dispatched")
        self.assertEqual(row["job_id"], jobs[0][0])

    def test_mark_dispatched_failure_rolls_back_job_event_and_coalescing(self):
        # Create the due rows in a committed, non-dispatching poll first.
        self.reconcile(self.now(9), cleanup_daily=False)
        real_mark = schedule_db._mark_dispatched

        def mark_then_fail(*args, **kwargs):
            real_mark(*args, **kwargs)
            raise RuntimeError("mark failed after transition")

        with patch.object(schedule_db, "_mark_dispatched", side_effect=mark_then_fail):
            with self.assertRaisesRegex(RuntimeError, "mark failed"):
                self.reconcile(self.now(13), cleanup_daily=False)

        self.assertEqual([row["state"] for row in self.pipeline_rows()], ["due", "due"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0)

    def test_required_plan_event_failure_rolls_back_job_and_schedule_transition(self):
        self.reconcile(self.now(9), times="12:00", cleanup_daily=False)

        def fail_event(_conn, _event):
            raise RuntimeError("event persistence failed")

        with patch.object(db, "_insert_job_event", side_effect=fail_event):
            with self.assertRaisesRegex(RuntimeError, "event persistence"):
                self.reconcile(self.now(13), times="12:00", cleanup_daily=False)

        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "due")
        self.assertIsNone(row["job_id"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0)

    def test_committed_pending_job_interrupted_is_not_rescheduled(self):
        first = self.reconcile(self.now(13), times="12:00", cleanup_daily=False)
        self.assertTrue(first.queue_job)
        self.assertEqual(db.get_job(first.job_id)["status"], "pending")

        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 1)
        self.assertEqual(db.get_job(first.job_id)["status"], "interrupted")
        before_calls = len(self.plan_calls)
        second = self.reconcile(self.now(13), times="12:00", cleanup_daily=False)

        self.assertIsNone(second.job_id)
        self.assertEqual(len(self.plan_calls), before_calls)
        self.assertEqual(schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")["state"], "dispatched")
        with db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM jobs WHERE type=?", (db.DAILY_PIPELINE_JOB_TYPE,)).fetchone()[0],
                1,
            )

    def test_unchanged_poll_has_no_business_changes_or_timestamp_update(self):
        first = self.reconcile(self.now(9), times="12:00", cleanup_daily=False)
        self.assertIsNone(first.job_id)
        with db.connect() as conn:
            before = {
                "occurrences": conn.execute("SELECT COUNT(*) FROM schedule_occurrences").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
                "metadata": conn.execute("SELECT COUNT(*) FROM schedule_metadata").fetchone()[0],
                "updated_at": conn.execute(
                    "SELECT updated_at FROM schedule_occurrences WHERE schedule_kind='daily_pipeline'"
                ).fetchone()[0],
                "snapshot": conn.execute(
                    "SELECT config_snapshot FROM schedule_occurrences WHERE schedule_kind='daily_pipeline'"
                ).fetchone()[0],
            }

        observed_total_changes: list[int] = []
        real_connect = db.connect

        class TrackingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection.__enter__()

            def __exit__(self, *args):
                observed_total_changes.append(self.connection.total_changes)
                return self.connection.__exit__(*args)

        def tracked_connect(*args, **kwargs):
            return TrackingConnection(real_connect(*args, **kwargs))

        with patch.object(db, "connect", side_effect=tracked_connect):
            second = self.reconcile(self.now(9, minute=1), times="12:00", cleanup_daily=False)
        self.assertIsNone(second.job_id)
        self.assertEqual(observed_total_changes, [0])
        with db.connect() as conn:
            after = {
                "occurrences": conn.execute("SELECT COUNT(*) FROM schedule_occurrences").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
                "metadata": conn.execute("SELECT COUNT(*) FROM schedule_metadata").fetchone()[0],
                "updated_at": conn.execute(
                    "SELECT updated_at FROM schedule_occurrences WHERE schedule_kind='daily_pipeline'"
                ).fetchone()[0],
                "snapshot": conn.execute(
                    "SELECT config_snapshot FROM schedule_occurrences WHERE schedule_kind='daily_pipeline'"
                ).fetchone()[0],
            }
        self.assertEqual(after, before)
        self.assertEqual(self.plan_calls, [])

    def test_failed_dispatched_job_and_config_change_do_not_rebuild_plan(self):
        first = self.reconcile(self.now(13), times="10:30,12:00", cleanup_daily=False)
        self.assertTrue(first.queue_job)
        db.update_job(first.job_id, "failed", "provider failed")
        before_calls = len(self.plan_calls)

        # Removing the already-coalesced earlier slot changes configuration,
        # but must not reopen the dispatched slot or invoke the builder.
        second = self.reconcile(self.now(13), times="12:00", cleanup_daily=False)
        self.assertIsNone(second.job_id)
        self.assertEqual(len(self.plan_calls), before_calls)
        rows = self.pipeline_rows()
        self.assertEqual([(row["scheduled_time"], row["state"]) for row in rows], [
            ("10:30", "coalesced"),
            ("12:00", "dispatched"),
        ])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_active_block_then_removed_target_is_not_dispatched_after_release(self):
        active = db.create_job("crawl", {})
        blocked = self.reconcile(self.now(13), times="10:30,12:00", cleanup_daily=False)
        self.assertIsNone(blocked.job_id)
        self.assertEqual([row["state"] for row in self.pipeline_rows()], ["due", "due"])

        removed = self.reconcile(self.now(13), times="10:30", cleanup_daily=False)
        self.assertIsNone(removed.job_id)
        self.assertEqual(
            [(row["scheduled_time"], row["state"]) for row in self.pipeline_rows()],
            [("10:30", "due"), ("12:00", "cancelled")],
        )

        db.update_job(active, "running")
        db.update_job(active, "success")
        released = self.reconcile(self.now(13), times="10:30", cleanup_daily=False)
        self.assertTrue(released.queue_job)
        self.assertEqual(
            [(row["scheduled_time"], row["state"]) for row in self.pipeline_rows()],
            [("10:30", "dispatched"), ("12:00", "cancelled")],
        )
        self.assertEqual(len(self.plan_calls), 1)

    def test_removed_and_readded_coalesced_or_dispatched_slots_do_not_revive(self):
        first = self.reconcile(self.now(13), times="10:30,12:00", cleanup_daily=False)
        self.assertTrue(first.queue_job)
        self.assertEqual(
            [(row["scheduled_time"], row["state"]) for row in self.pipeline_rows()],
            [("10:30", "coalesced"), ("12:00", "dispatched")],
        )
        before_ids = {
            row["scheduled_time"]: row["id"] for row in self.pipeline_rows()
        }

        self.assertIsNone(self.reconcile(self.now(13), times="", cleanup_daily=False).job_id)
        readded = self.reconcile(self.now(13), times="10:30,12:00", cleanup_daily=False)
        self.assertIsNone(readded.job_id)
        self.assertEqual(
            [(row["scheduled_time"], row["state"]) for row in self.pipeline_rows()],
            [("10:30", "coalesced"), ("12:00", "dispatched")],
        )
        self.assertEqual(
            {row["scheduled_time"]: row["id"] for row in self.pipeline_rows()},
            before_ids,
        )
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_second_slot_dispatch_keeps_one_cleanup_job_for_the_day(self):
        first = self.reconcile(self.now(11), times="10:30,12:00", cleanup_daily=True)
        self.assertTrue(first.queue_job)
        self.assertTrue(first.queue_cleanup_job)
        db.update_job(first.job_id, "running")
        db.update_job(first.job_id, "success")

        second = self.reconcile(self.now(13), times="10:30,12:00", cleanup_daily=True)
        self.assertTrue(second.queue_job)
        self.assertFalse(second.queue_cleanup_job)
        self.assertNotEqual(second.job_id, first.job_id)
        rows = schedule_db.list_occurrences(local_date="2026-09-05")
        cleanup_rows = [
            row for row in rows if row["schedule_kind"] == schedule_db.SCHEDULE_KIND_CACHE_CLEANUP
        ]
        self.assertEqual(len(cleanup_rows), 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE type='cleanup'").fetchone()[0], 1)

    def test_cancelled_future_slot_restores_same_record_and_catches_up(self):
        initial = self.reconcile(self.now(9), times="12:00", cleanup_daily=False)
        self.assertIsNone(initial.job_id)
        original = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(original["state"], "due")

        cancelled = self.reconcile(self.now(9), times="", cleanup_daily=False)
        self.assertIsNone(cancelled.job_id)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["id"], original["id"])

        restored = self.reconcile(self.now(13), times="12:00", cleanup_daily=False)
        self.assertTrue(restored.queue_job)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["state"], "dispatched")
        self.assertEqual(row["id"], original["id"])
        db.update_job(restored.job_id, "running")
        db.update_job(restored.job_id, "success")

        # In a separate set of daily slots, several days of downtime only
        # catch up the current day; older due rows become expired.
        self.assertIsNone(self.reconcile(self.now(9, day=4), times="10:30", cleanup_daily=False).job_id)
        self.assertIsNone(self.reconcile(self.now(9, day=5), times="10:30", cleanup_daily=False).job_id)
        current = self.reconcile(self.now(13, day=6), times="10:30", cleanup_daily=False)
        self.assertTrue(current.queue_job)

        self.assertEqual(
            schedule_db.get_occurrence("daily_pipeline", "2026-09-04", "10:30")["state"],
            "expired",
        )
        self.assertEqual(
            schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "10:30")["state"],
            "expired",
        )
        self.assertEqual(
            schedule_db.get_occurrence("daily_pipeline", "2026-09-06", "10:30")["state"],
            "dispatched",
        )
        with db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM jobs WHERE type=?", (db.DAILY_PIPELINE_JOB_TYPE,)).fetchone()[0],
                2,
            )

    def test_committed_schedule_is_protected_from_orphan_recovery_before_queue(self):
        runner = JobRunner(clock=lambda: self.now(13))
        runner._started = True
        schedule_returned = threading.Event()
        release_schedule = threading.Event()
        orphan_started = threading.Event()
        orphan_finished = threading.Event()
        errors: list[BaseException] = []
        orphan_result: dict[str, int] = {}
        real_reconcile = schedule_db.reconcile_daily_schedule

        def reconcile_then_pause(*args, **kwargs):
            result = real_reconcile(*args, **kwargs)
            # The real call has exited its DB context here: the Job is
            # committed, while _maybe_schedule_daily_work still owns the
            # runner state lock and has not queued it yet.
            schedule_returned.set()
            if not release_schedule.wait(3):
                raise TimeoutError("schedule queue handoff was not released")
            return result

        def run_schedule():
            try:
                runner._maybe_schedule_daily_work()
            except BaseException as exc:  # pass thread failures to the test
                errors.append(exc)

        def run_orphan_recovery():
            orphan_started.set()
            try:
                orphan_result["marked"] = runner.reconcile_orphaned_pending_jobs()
            except BaseException as exc:  # pass thread failures to the test
                errors.append(exc)
            finally:
                orphan_finished.set()

        def bool_setting(key, default=False):
            if key == "scheduler.enabled":
                return True
            if key == "cache.cleanup_daily":
                return False
            return default

        def setting(key, default=None):
            if key == "scheduler.daily_times":
                return "12:00"
            return default

        schedule_thread = threading.Thread(target=run_schedule)
        orphan_thread = threading.Thread(target=run_orphan_recovery)
        with (
            patch.object(schedule_db, "reconcile_daily_schedule", side_effect=reconcile_then_pause),
            patch.object(db, "get_bool_setting", side_effect=bool_setting),
            patch.object(db, "get_setting", side_effect=setting),
            patch("daily_coolpapers.jobs.build_daily_pipeline_plan", side_effect=self.plan),
        ):
            schedule_thread.start()
            self.assertTrue(schedule_returned.wait(3))
            orphan_thread.start()
            self.assertTrue(orphan_started.wait(1))
            self.assertFalse(orphan_finished.wait(0.1))
            release_schedule.set()
            schedule_thread.join(3)
            orphan_thread.join(3)

        self.assertFalse(schedule_thread.is_alive())
        self.assertFalse(orphan_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(orphan_result, {"marked": 0})
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status FROM jobs WHERE type=?",
                (db.DAILY_PIPELINE_JOB_TYPE,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(runner.active_job_ids(), {int(row["id"])})
        self.assertEqual(runner.queue.qsize(), 1)

    def test_naive_now_is_interpreted_as_shanghai_local_time(self):
        before_slot = self.reconcile(datetime(2026, 9, 5, 9, 0), times="12:00", cleanup_daily=False)
        self.assertIsNone(before_slot.job_id)
        self.assertEqual(self.plan_calls, [])

        result = self.reconcile(datetime(2026, 9, 5, 13, 0), times="12:00", cleanup_daily=False)

        self.assertTrue(result.queue_job)
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.plan_calls[0].tzinfo, schedule_db.SHANGHAI_TZ)
        self.assertEqual(self.plan_calls[0].hour, 13)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["timezone"], "Asia/Shanghai")
        self.assertEqual(row["state"], "dispatched")

    def test_legacy_migration_tolerates_non_dict_payload_bad_date_and_duplicate_key(self):
        first_id = db.create_job(
            db.DAILY_PIPELINE_JOB_TYPE,
            ["legacy payload is not a mapping"],
            idempotency_key="scheduled:2026-09-05 12:00",
        )
        second_id = db.create_job(
            db.DAILY_PIPELINE_JOB_TYPE,
            "also not a mapping",
            idempotency_key="scheduled:2026-09-05:12:00",
        )
        malformed_id = db.create_job(
            db.DAILY_PIPELINE_JOB_TYPE,
            {"trigger_source": "scheduled"},
            idempotency_key="scheduled:2026-99-99 12:00",
        )

        migrated = schedule_db.migrate_legacy_scheduled_jobs(self.now(13))
        self.assertEqual(migrated, 1)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["job_id"], first_id)
        self.assertEqual(row["state"], "dispatched")
        self.assertIsInstance(row["config_snapshot_data"], dict)
        self.assertNotEqual(row["job_id"], second_id)
        self.assertIsNone(schedule_db.get_occurrence("daily_pipeline", "2026-99-99", "12:00"))
        self.assertIsNotNone(db.get_job(malformed_id))
        self.assertEqual(schedule_db.migrate_legacy_scheduled_jobs(self.now(13)), 0)

    def test_legacy_migration_rolls_back_marker_and_can_retry(self):
        job_id = db.create_job(
            db.DAILY_PIPELINE_JOB_TYPE,
            {"trigger_source": "scheduled"},
            idempotency_key="scheduled:2026-09-05 12:00",
        )
        real_migrate = schedule_db._migrate_legacy_scheduled_jobs

        def migrate_then_fail(conn, now):
            result = real_migrate(conn, now)
            self.assertEqual(result, 1)
            raise RuntimeError("migration transaction failed")

        with patch.object(schedule_db, "_migrate_legacy_scheduled_jobs", side_effect=migrate_then_fail):
            with self.assertRaisesRegex(RuntimeError, "migration transaction"):
                schedule_db.migrate_legacy_scheduled_jobs(self.now(13))

        self.assertIsNone(schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00"))
        with db.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT value FROM schedule_metadata WHERE key='legacy_scheduled_jobs_v1'"
            ).fetchone())

        self.assertEqual(schedule_db.migrate_legacy_scheduled_jobs(self.now(13)), 1)
        row = schedule_db.get_occurrence("daily_pipeline", "2026-09-05", "12:00")
        self.assertEqual(row["job_id"], job_id)


if __name__ == "__main__":
    unittest.main()
