from datetime import datetime
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from daily_coolpapers import app as app_module
from daily_coolpapers import jobs as jobs_module
from daily_coolpapers.jobs import JobRunner
from daily_coolpapers.security import SecretStore


class RuntimeLifecycleTests(unittest.TestCase):
    def test_create_app_only_composes_dependencies(self):
        runner = JobRunner()
        store = SecretStore(Path(tempfile.gettempdir()) / "unused-runtime-test.key")
        with (
            patch.object(app_module, "ensure_directories") as ensure_dirs,
            patch.object(app_module, "setup_logging") as setup_logging,
            patch.object(app_module.db, "init_db") as init_db,
            patch.object(app_module, "cleanup_caches") as cleanup,
        ):
            app = app_module.create_app(runner=runner, store=store, secret_key="test-secret")

        ensure_dirs.assert_not_called()
        setup_logging.assert_not_called()
        init_db.assert_not_called()
        cleanup.assert_not_called()
        self.assertIs(app.extensions["daily_coolpapers.job_runner"], runner)
        self.assertIs(app.extensions["daily_coolpapers.secret_store"], store)

    def test_two_apps_do_not_share_injected_runtime_dependencies(self):
        first_runner = JobRunner()
        second_runner = JobRunner()
        first = app_module.create_app(runner=first_runner, secret_key="first")
        second = app_module.create_app(runner=second_runner, secret_key="second")

        self.assertIsNot(
            first.extensions["daily_coolpapers.job_runner"],
            second.extensions["daily_coolpapers.job_runner"],
        )
        self.assertNotEqual(first.secret_key, second.secret_key)

    def test_start_runtime_owns_side_effects_and_returns_stoppable_handle(self):
        runner = Mock(spec=JobRunner)
        with (
            tempfile.TemporaryDirectory() as runtime_dir,
            patch.object(app_module.db, 'DB_PATH', Path(runtime_dir) / 'main.sqlite3'),
            patch.object(app_module, "ensure_directories") as ensure_dirs,
            patch.object(app_module.db, "init_db") as init_db,
            patch.object(app_module.db, "init_llm_profiles_db") as init_profiles,
            patch.object(app_module.db, "migrate_llm_profiles_from_main_db") as migrate,
            patch.object(app_module.schedule_db, "migrate_legacy_scheduled_jobs") as migrate_schedule,
            patch.object(app_module.db, "mark_unfinished_jobs_interrupted") as mark_jobs,
            patch.object(app_module.db, "get_bool_setting", side_effect=[False, False]),
            patch.object(app_module, "setup_logging") as setup_logging,
            patch.object(app_module, "cleanup_caches") as cleanup,
        ):
            handle = app_module.start_runtime(runner=runner, start_worker=True)
            handle.stop()
            handle.stop()

        ensure_dirs.assert_called_once()
        init_db.assert_called_once()
        init_profiles.assert_called_once()
        migrate.assert_called_once()
        migrate_schedule.assert_called_once()
        mark_jobs.assert_called_once()
        setup_logging.assert_called_once_with(clear_on_start=False)
        cleanup.assert_not_called()
        runner.start.assert_called_once()
        runner.stop.assert_called_once_with(timeout_seconds=5.0)

    def test_job_runner_start_and_stop_are_idempotent(self):
        runner = JobRunner()
        with (patch.object(runner, "_maybe_schedule_daily_work", return_value=None),
              patch.object(runner, 'reconcile_orphaned_pending_jobs', return_value=0)):
            runner.start()
            runner.start()
            runner.stop()
            runner.stop()

        self.assertFalse(runner._started)
        self.assertIsNone(runner._worker_thread)
        self.assertIsNone(runner._scheduler_thread)

    def test_scheduler_loop_rechecks_after_time_becomes_due(self):
        with tempfile.TemporaryDirectory() as runtime_dir, patch.object(
            app_module.db, "DB_PATH", Path(runtime_dir) / "main.sqlite3"
        ), patch.object(app_module.db, "ensure_directories"):
            app_module.db.init_db()
            current = [
                datetime(2026, 9, 20, 10, 29, 50, tzinfo=app_module.schedule_db.SHANGHAI_TZ)
            ]
            runner: JobRunner | None = None
            waits: list[float] = []

            def scheduler_wait(seconds: float) -> bool:
                waits.append(seconds)
                if len(waits) == 1:
                    current[0] = datetime(
                        2026, 9, 20, 10, 30, 20, tzinfo=app_module.schedule_db.SHANGHAI_TZ
                    )
                    return False
                assert runner is not None
                runner._stop_event.set()
                return True

            runner = JobRunner(clock=lambda: current[0], scheduler_wait=scheduler_wait)
            runner._started = True
            plan = {
                "trigger_source": "scheduled",
                "timezone": "Asia/Shanghai",
                "dates": ["2026-09-20"],
                "target_date": "2026-09-20",
                "start_date": "2026-09-20",
                "end_date": "2026-09-20",
                "categories": [],
            }
            with patch.object(jobs_module, "build_daily_pipeline_plan", return_value=plan):
                runner._scheduler_loop()

            self.assertEqual(waits, [30, 30])
            queued = list(runner._queued_job_ids)
            self.assertEqual(len(queued), 2)  # pipeline and independently deduplicated cleanup
            pipeline = app_module.db.get_job(next(
                job_id for job_id in queued
                if app_module.db.get_job(job_id)["type"] == app_module.db.DAILY_PIPELINE_JOB_TYPE
            ))
            self.assertIsNotNone(pipeline)
            occurrence = app_module.schedule_db.get_occurrence(
                "daily_pipeline", "2026-09-20", "10:30"
            )
            self.assertEqual(occurrence["state"], "dispatched")
            self.assertEqual(occurrence["job_id"], pipeline["id"])

    def test_scheduler_queue_registration_completes_before_stop(self):
        runner = JobRunner()
        runner._started = True
        reconcile_entered = threading.Event()
        release_reconcile = threading.Event()
        stop_finished = threading.Event()

        def delayed_reconcile(*_args, **_kwargs):
            reconcile_entered.set()
            self.assertTrue(release_reconcile.wait(2))
            return app_module.schedule_db.ScheduleDispatch(job_id=101, queue_job=True)

        with (
            patch.object(jobs_module.db, "get_bool_setting", side_effect=[True, False]),
            patch.object(jobs_module.db, "get_setting", return_value="10:30"),
            patch.object(
                jobs_module.schedule_db,
                "reconcile_daily_schedule",
                side_effect=delayed_reconcile,
            ),
        ):
            scheduling = threading.Thread(target=runner._maybe_schedule_daily_work)
            scheduling.start()
            self.assertTrue(reconcile_entered.wait(2))

            stopping = threading.Thread(
                target=lambda: (runner.stop(timeout_seconds=2), stop_finished.set())
            )
            stopping.start()
            self.assertFalse(stop_finished.wait(0.1))
            release_reconcile.set()
            scheduling.join(2)
            stopping.join(2)

        self.assertFalse(scheduling.is_alive())
        self.assertFalse(stopping.is_alive())
        self.assertTrue(stop_finished.is_set())
        self.assertEqual(runner.queue.get_nowait(), 101)

    def test_scheduler_rejects_after_stop_without_database_or_queue_write(self):
        runner = JobRunner()
        runner.stop()
        with (
            patch.object(jobs_module.db, "get_bool_setting") as get_bool,
            patch.object(jobs_module.db, "get_setting") as get_setting,
            patch.object(jobs_module.schedule_db, "reconcile_daily_schedule") as reconcile,
        ):
            with self.assertRaises(RuntimeError):
                runner._maybe_schedule_daily_work()
        get_bool.assert_not_called()
        get_setting.assert_not_called()
        reconcile.assert_not_called()
        self.assertTrue(runner.queue.empty())


if __name__ == "__main__":
    unittest.main()
