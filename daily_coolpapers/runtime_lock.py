"""OS-owned workspace lock. The lock file is never removed or used as a PID lease."""
import errno
import os
from pathlib import Path


class RuntimeAlreadyRunningError(RuntimeError):
    def __init__(self):
        super().__init__('此数据工作区已有 Daily Cool Papers 实例运行，请使用已打开的服务。')


class WorkspaceLock:
    def __init__(self, database_path: Path):
        # Resolve the database itself as well as directory junctions/symlinks.
        self.path = Path(database_path).resolve().parent / '.daily-coolpapers.lock'
        self._file = None

    def acquire(self):
        if self._file is not None:
            raise RuntimeError('运行锁不能重复获取')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                # Windows permits locking a byte beyond EOF; no marker write needed.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeAlreadyRunningError() from None
            raise
        except BaseException:
            handle.close()
            raise
        self._file = handle
        return self

    def release(self):
        if self._file is not None:
            # Closing the owning descriptor releases the OS lock, including at exit.
            self._file.close()
            self._file = None
