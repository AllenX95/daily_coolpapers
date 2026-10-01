"""Isolated process probe for the runtime-lock integration tests (no network)."""
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import app, db
from daily_coolpapers.jobs import JobRunner
from daily_coolpapers.runtime_lock import RuntimeAlreadyRunningError, WorkspaceLock


def main():
    root = Path(sys.argv[1])
    mode = sys.argv[2]
    if len(sys.argv) > 3:
        gate = Path(sys.argv[3])
        deadline = time.monotonic() + 15
        while not gate.exists():
            if time.monotonic() > deadline:
                raise RuntimeError('probe gate timeout')
            time.sleep(.01)
    with ExitStack() as stack:
        if mode == 'runtime':
            def effect(name, callback=None):
                def run(*args, **kwargs):
                    with (root / 'effects.txt').open('a', encoding='utf-8') as stream:
                        stream.write(name + '\n')
                    if callback:
                        return callback(*args, **kwargs)
                return run
            stack.enter_context(patch.object(db, 'DB_PATH', root / 'main.sqlite3'))
            stack.enter_context(patch.object(db, 'LLM_PROFILES_DB_PATH', root / 'profiles.sqlite3'))
            stack.enter_context(patch.object(db, 'ensure_directories', lambda: None))
            for owner, name in ((app, 'ensure_directories'), (db, 'init_db'),
                                (db, 'init_llm_profiles_db'), (db, 'migrate_llm_profiles_from_main_db'),
                                (app.schedule_db, 'migrate_legacy_scheduled_jobs'),
                                (db, 'mark_unfinished_jobs_interrupted'), (app, 'setup_logging'),
                                (app, 'cleanup_caches')):
                original = getattr(owner, name) if owner is db else None
                stack.enter_context(patch.object(owner, name, side_effect=effect(name, original)))
            try:
                handle = app.start_runtime(runner=JobRunner(), start_worker=False)
            except RuntimeAlreadyRunningError as exc:
                print(str(exc), file=sys.stderr, flush=True)
                return 2
            release = handle.stop
        else:
            try:
                lock = WorkspaceLock(root / 'main.sqlite3').acquire()
            except RuntimeAlreadyRunningError as exc:
                print(str(exc), file=sys.stderr, flush=True)
                return 2
            release = lock.release
        print('READY', flush=True)
        try:
            command = sys.stdin.readline().strip()
            if command == 'crash':
                os._exit(17)
        finally:
            release()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
