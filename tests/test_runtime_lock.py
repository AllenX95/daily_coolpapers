import os
import queue
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from daily_coolpapers import app, db
from daily_coolpapers.jobs import JobRunner
from daily_coolpapers.runtime_lock import RuntimeAlreadyRunningError, WorkspaceLock

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def isolated_startup(root):
    with ExitStack() as stack:
        stack.enter_context(patch.object(db, 'DB_PATH', root / 'main.sqlite3'))
        effects = {}
        for owner, name in ((app, 'ensure_directories'), (db, 'init_db'),
                            (db, 'init_llm_profiles_db'), (db, 'migrate_llm_profiles_from_main_db'),
                            (app.schedule_db, 'migrate_legacy_scheduled_jobs'),
                            (db, 'mark_unfinished_jobs_interrupted'), (app, 'setup_logging'),
                            (app, 'cleanup_caches')):
            effects[name] = stack.enter_context(patch.object(owner, name))
        stack.enter_context(patch.object(db, 'get_bool_setting', return_value=True))
        yield effects


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def probe(self, root=None, mode='lock', gate=None):
        command = [sys.executable, '-B', '-m', 'tests.runtime_process', str(root or self.root), mode]
        if gate:
            command.append(str(gate))
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                   env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process

    def ready(self, process):
        lines = queue.Queue()
        thread = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        thread.start()
        self.assertEqual(lines.get(timeout=15).strip(), 'READY')

    def finish(self, process, command='stop'):
        process.stdin.write(command + '\n')
        process.stdin.flush()
        process.wait(timeout=15)

    def test_duplicate_runtime_has_no_initialization_effects(self):
        first = self.probe(mode='runtime')
        self.ready(first)
        before = (self.root / 'effects.txt').read_bytes()
        second = self.probe(mode='runtime')
        stdout, stderr = second.communicate(timeout=15)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(stdout, '')
        self.assertIn('已有', stderr)
        self.assertEqual((self.root / 'effects.txt').read_bytes(), before)
        self.assertIn(b'mark_unfinished_jobs_interrupted', before)
        self.finish(first)
        self.assertEqual(first.returncode, 0)
        third = self.probe(mode='runtime')
        self.ready(third)
        self.finish(third)

    def test_simultaneous_processes_have_one_owner(self):
        gate = self.root / 'gate'
        processes = [self.probe(mode='runtime', gate=gate) for _ in range(2)]
        gate.touch()
        deadline = time.monotonic() + 15
        while all(p.poll() is None for p in processes) and time.monotonic() < deadline:
            time.sleep(.02)
        losers = [p for p in processes if p.poll() is not None]
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0].returncode, 2)
        winner = next(p for p in processes if p is not losers[0])
        self.ready(winner)
        self.assertEqual((self.root / 'effects.txt').read_text().count('ensure_directories'), 1)
        self.finish(winner)

    def test_crash_releases_lock_without_removing_file(self):
        first = self.probe()
        self.ready(first)
        self.finish(first, 'crash')
        self.assertEqual(first.returncode, 17)
        self.assertTrue((self.root / '.daily-coolpapers.lock').exists())
        second = self.probe()
        self.ready(second)
        self.finish(second)

    def test_different_workspaces_can_run_together(self):
        first = self.probe(self.root / 'one')
        second = self.probe(self.root / 'two')
        self.ready(first)
        self.ready(second)
        self.finish(first)
        self.finish(second)

    def test_relative_and_case_paths_share_lock(self):
        first = self.probe()
        self.ready(first)
        paths = [self.root / 'nested' / '..']
        if os.name == 'nt':
            paths.append(Path(str(self.root).swapcase()))
        for path in paths:
            other = self.probe(path)
            other.communicate(timeout=15)
            self.assertEqual(other.returncode, 2)
        self.finish(first)

    def test_directory_alias_cannot_bypass_lock(self):
        target = self.root / 'target'
        target.mkdir()
        alias = self.root / 'alias'
        if os.name == 'nt':
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path ' + quote(alias) + ' -Target ' + quote(target) + ' | Out-Null'],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            alias.symlink_to(target, target_is_directory=True)
        first = self.probe(target)
        self.ready(first)
        second = self.probe(alias)
        second.communicate(timeout=15)
        self.assertEqual(second.returncode, 2)
        self.finish(first)

    def test_startup_failures_release_lock_before_retry(self):
        for failing in ('ensure_directories', 'init_db', 'init_llm_profiles_db',
                        'migrate_llm_profiles_from_main_db', 'migrate_legacy_scheduled_jobs',
                        'mark_unfinished_jobs_interrupted',
                        'setup_logging', 'cleanup_caches'):
            with self.subTest(failing=failing), isolated_startup(self.root) as effects:
                effects[failing].side_effect = ValueError('synthetic startup error')
                with self.assertRaises(ValueError):
                    app.start_runtime(runner=JobRunner(), start_worker=False)
                lock = WorkspaceLock(self.root / 'main.sqlite3').acquire()
                lock.release()

    def test_lock_precedes_every_effect_even_without_worker(self):
        lock = WorkspaceLock(self.root / 'main.sqlite3').acquire()
        self.addCleanup(lock.release)
        with isolated_startup(self.root) as effects:
            with self.assertRaises(RuntimeAlreadyRunningError):
                app.start_runtime(runner=JobRunner(), start_worker=False)
            for effect in effects.values():
                effect.assert_not_called()

    def test_create_app_does_not_acquire_lock(self):
        with patch.object(app, 'WorkspaceLock') as lock:
            app.create_app(runner=JobRunner(), secret_key='test')
        lock.assert_not_called()

    def test_entrypoint_duplicate_exits_before_secrets_or_side_effects(self):
        lock = WorkspaceLock(self.root / 'main.sqlite3').acquire()
        self.addCleanup(lock.release)
        with isolated_startup(self.root) as effects, patch.object(app, 'create_app', return_value=Mock()), \
                patch.object(app, '_flask_secret') as secret:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ROOT / 'run.py'), run_name='__main__')
            self.assertIn('已有', str(raised.exception.code))
            secret.assert_not_called()
            for effect in effects.values():
                effect.assert_not_called()

    def test_entrypoint_secret_failure_releases_runtime(self):
        with isolated_startup(self.root), patch.object(app, 'job_runner', JobRunner()), \
                patch.dict(os.environ, {'DAILY_COOLPAPERS_DISABLE_WORKER': '1'}), \
                patch.object(app, 'create_app', return_value=Mock()), \
                patch.object(app, '_flask_secret', side_effect=ValueError('synthetic secret error')):
            with self.assertRaisesRegex(ValueError, 'synthetic secret'):
                runpy.run_path(str(ROOT / 'run.py'), run_name='__main__')
        WorkspaceLock(self.root / 'main.sqlite3').acquire().release()

    def test_no_worker_shutdown_closes_admission_and_allows_new_runtime(self):
        runner = JobRunner()
        with isolated_startup(self.root):
            handle = app.start_runtime(runner=runner, start_worker=False)
            handle.stop()
            with patch.object(db, 'create_job') as create:
                with self.assertRaises(RuntimeError):
                    runner.enqueue('cleanup')
                create.assert_not_called()
            with self.assertRaises(RuntimeError):
                runner.start()
            runner.queue.put(99)
            runner._queued_job_ids.add(99)
            runner._daily_runs.add('old runtime marker')
            replacement = app.start_runtime(runner=runner, start_worker=False)
            self.assertTrue(runner.queue.empty())
            self.assertFalse(runner.active_job_ids())
            self.assertFalse(runner._daily_runs)
            replacement.stop()

    def test_stopped_threads_cannot_restart_before_deferred_lock_release(self):
        worker_release = threading.Event()
        joined, finish_wait = threading.Event(), threading.Event()
        runner = JobRunner()
        wait_stopped = runner.wait_stopped
        def delayed_wait():
            wait_stopped()
            joined.set()
            finish_wait.wait(10)
        self.addCleanup(worker_release.set)
        self.addCleanup(finish_wait.set)
        with isolated_startup(self.root), patch.object(runner, '_worker_loop', side_effect=lambda: worker_release.wait(10)), \
                patch.object(runner, '_scheduler_loop', side_effect=lambda: runner._stop_event.wait(10)), \
                patch.object(runner, 'wait_stopped', side_effect=delayed_wait):
            handle = app.start_runtime(runner=runner, start_worker=True)
            handle.stop(timeout_seconds=.01)
            worker_release.set()
            self.assertTrue(joined.wait(5))
            with self.assertRaises(RuntimeError):
                runner.start()
            with self.assertRaises(RuntimeAlreadyRunningError):
                WorkspaceLock(self.root / 'main.sqlite3').acquire()
            finish_wait.set()
            deadline = time.monotonic() + 5
            while handle.worker_started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(handle.worker_started)

    def test_shutdown_timeout_holds_lock_until_worker_exits(self):
        entered, release = threading.Event(), threading.Event()
        runner = JobRunner()
        def worker():
            entered.set()
            release.wait(15)
        self.addCleanup(release.set)
        with isolated_startup(self.root), patch.object(runner, '_worker_loop', side_effect=worker), \
                patch.object(runner, '_scheduler_loop', side_effect=lambda: runner._stop_event.wait(15)):
            handle = app.start_runtime(runner=runner, start_worker=True)
            self.addCleanup(handle.stop)
            self.assertTrue(entered.wait(5))
            handle.stop(timeout_seconds=.01)
            with self.assertRaises(RuntimeAlreadyRunningError):
                WorkspaceLock(self.root / 'main.sqlite3').acquire()
            with self.assertRaises(RuntimeError):
                runner.start()
            with patch.object(db, 'create_job') as create:
                with self.assertRaises(RuntimeError):
                    runner.enqueue('cleanup')
                create.assert_not_called()
            release.set()
            deadline = time.monotonic() + 5
            while handle.worker_started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(handle.worker_started)
            lock = WorkspaceLock(self.root / 'main.sqlite3').acquire()
            lock.release()

    def test_partial_thread_start_failure_releases_after_join(self):
        runner = JobRunner()
        real_start = threading.Thread.start
        def start(thread):
            if thread.name == 'job-scheduler':
                raise RuntimeError('synthetic thread start failure')
            real_start(thread)
        with isolated_startup(self.root), patch.object(threading.Thread, 'start', start):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                app.start_runtime(runner=runner, start_worker=True)
        self.assertIsNone(runner._worker_thread)
        WorkspaceLock(self.root / 'main.sqlite3').acquire().release()
