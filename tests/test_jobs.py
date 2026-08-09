import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import db
from daily_coolpapers.jobs import JobRunner


class JobRunnerConcurrencyTests(unittest.TestCase):
    def test_reconcile_cannot_interrupt_job_during_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "jobs.sqlite3"):
                db.init_db()
                runner = JobRunner()
                runner._started = True
                progress_entered = threading.Event()
                release_progress = threading.Event()
                reconcile_started = threading.Event()
                mark_entered = threading.Event()
                result: dict[str, int] = {}
                real_update_progress = db.update_job_progress
                real_mark_pending = db.mark_pending_jobs_interrupted_except

                def delayed_progress(*args, **kwargs):
                    progress_entered.set()
                    if not release_progress.wait(2):
                        raise TimeoutError("test did not release enqueue")
                    return real_update_progress(*args, **kwargs)

                def enqueue():
                    result["job_id"] = runner.enqueue("cleanup", {})

                def reconcile():
                    reconcile_started.set()
                    result["marked"] = runner.reconcile_orphaned_pending_jobs()

                def observed_mark(*args, **kwargs):
                    mark_entered.set()
                    return real_mark_pending(*args, **kwargs)

                with (
                    patch.object(db, "update_job_progress", side_effect=delayed_progress),
                    patch.object(db, "mark_pending_jobs_interrupted_except", side_effect=observed_mark),
                ):
                    enqueue_thread = threading.Thread(target=enqueue)
                    enqueue_thread.start()
                    self.assertTrue(progress_entered.wait(2))

                    reconcile_thread = threading.Thread(target=reconcile)
                    reconcile_thread.start()
                    self.assertTrue(reconcile_started.wait(2))
                    self.assertFalse(mark_entered.wait(0.1))
                    release_progress.set()
                    enqueue_thread.join(2)
                    reconcile_thread.join(2)

                self.assertFalse(enqueue_thread.is_alive())
                self.assertFalse(reconcile_thread.is_alive())
                self.assertTrue(mark_entered.is_set())
                self.assertEqual(result["marked"], 0)
                self.assertEqual(runner.active_job_ids(), {result["job_id"]})
                self.assertEqual(runner.queue.qsize(), 1)
                self.assertEqual(db.get_job(result["job_id"])["status"], "pending")


if __name__ == "__main__":
    unittest.main()
