import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from daily_coolpapers import app as app_module
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
            patch.object(app_module, "ensure_directories") as ensure_dirs,
            patch.object(app_module.db, "init_db") as init_db,
            patch.object(app_module.db, "init_llm_profiles_db") as init_profiles,
            patch.object(app_module.db, "migrate_llm_profiles_from_main_db") as migrate,
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
        mark_jobs.assert_called_once()
        setup_logging.assert_called_once_with(clear_on_start=False)
        cleanup.assert_not_called()
        runner.start.assert_called_once()
        runner.stop.assert_called_once()

    def test_job_runner_start_and_stop_are_idempotent(self):
        runner = JobRunner()
        with patch.object(runner, "_maybe_schedule_daily_work", return_value=None):
            runner.start()
            runner.start()
            runner.stop()
            runner.stop()

        self.assertFalse(runner._started)
        self.assertIsNone(runner._worker_thread)
        self.assertIsNone(runner._scheduler_thread)


if __name__ == "__main__":
    unittest.main()
