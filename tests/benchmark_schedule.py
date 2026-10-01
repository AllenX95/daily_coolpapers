"""Benchmark the S2 persistent schedule migration and idle reconciliation.

The benchmark uses a temporary SQLite database and inserts terminal legacy
``daily_pipeline`` jobs directly.  It never starts the application runtime,
calls a plan/model provider, or accesses the network.

Examples::

    python -m tests.benchmark_schedule
    python -m tests.benchmark_schedule --sizes 10000 100000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daily_coolpapers import db, schedule_db  # noqa: E402


MIGRATION_MARKER = "legacy_scheduled_jobs_v1"
BASE_DATE = datetime(2020, 1, 1)
SCHEDULE_TIMES = tuple(f"{hour:02d}:{minute:02d}" for hour in range(0, 20, 1) for minute in (30,))
PAYLOAD = json.dumps(
    {"trigger_source": "scheduled", "timezone": "Asia/Shanghai"},
    ensure_ascii=False,
    separators=(",", ":"),
)


@contextmanager
def isolated_database() -> Iterator[Path]:
    """Patch the DB location and network for one benchmark case."""

    with tempfile.TemporaryDirectory(prefix="coolpapers-s2-schedule-") as temp:
        root = Path(temp)
        database_path = root / "main.sqlite3"
        with ExitStack() as stack:
            stack.enter_context(patch.object(db, "DB_PATH", database_path))
            stack.enter_context(patch.object(db, "ensure_directories", lambda: None))
            stack.enter_context(
                patch.object(
                    socket.socket,
                    "connect",
                    side_effect=AssertionError("network access is forbidden in this benchmark"),
                )
            )
            db.init_db()
            yield database_path


def _rows(count: int) -> Iterator[tuple[str, str, str, str, str]]:
    """Yield deterministic, unique terminal pre-S2 jobs across many slots."""

    slots_per_day = len(SCHEDULE_TIMES)
    for index in range(count):
        day_offset, slot_index = divmod(index, slots_per_day)
        local_date = (BASE_DATE + timedelta(days=day_offset)).date().isoformat()
        scheduled_time = SCHEDULE_TIMES[slot_index]
        created_at = f"{local_date} {scheduled_time}:00"
        idempotency_key = f"scheduled:{local_date} {scheduled_time}"
        yield (db.DAILY_PIPELINE_JOB_TYPE, "success", idempotency_key, PAYLOAD, created_at)


def _insert_legacy_jobs(count: int) -> None:
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT INTO jobs(type, status, idempotency_key, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            _rows(count),
        )


def _last_case_now(count: int) -> datetime:
    day_offset = (count - 1) // len(SCHEDULE_TIMES)
    last_date = (BASE_DATE + timedelta(days=day_offset)).date()
    return datetime(
        last_date.year,
        last_date.month,
        last_date.day,
        0,
        0,
        tzinfo=schedule_db.SHANGHAI_TZ,
    )


def _checkpoint_size(database_path: Path) -> dict[str, int]:
    """Checkpoint WAL and return the main DB/WAL sizes."""

    with db.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    wal_path = Path(f"{database_path}-wal")
    return {
        "main_bytes": database_path.stat().st_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }


def _counts() -> dict[str, int]:
    with db.connect() as conn:
        return {
            "jobs": int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
            "daily_pipeline_jobs": int(
                conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE type=?",
                    (db.DAILY_PIPELINE_JOB_TYPE,),
                ).fetchone()[0]
            ),
            "occurrences": int(
                conn.execute(
                    "SELECT COUNT(*) FROM schedule_occurrences WHERE schedule_kind=?",
                    (schedule_db.SCHEDULE_KIND_DAILY_PIPELINE,),
                ).fetchone()[0]
            ),
            "dispatched_occurrences": int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM schedule_occurrences
                    WHERE schedule_kind=? AND state='dispatched'
                    """,
                    (schedule_db.SCHEDULE_KIND_DAILY_PIPELINE,),
                ).fetchone()[0]
            ),
        }


def _integrity_check() -> str:
    with db.connect() as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile used by the S0 baseline."""

    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _measure_idle_reconcile(
    now: datetime,
    daily_times: tuple[str, ...],
    warmup: int,
    samples: int,
) -> tuple[dict[str, float], int]:
    builder_calls = 0

    def forbidden_builder(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("idle reconciliation attempted to build a business job")

    def reconcile() -> None:
        schedule_db.reconcile_daily_schedule(
            now,
            enabled=True,
            daily_times=",".join(daily_times),
            cleanup_daily=False,
            plan_builder=forbidden_builder,
        )

    for _ in range(warmup):
        reconcile()
    timings: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        reconcile()
        timings.append((time.perf_counter() - start) * 1000)
    return {
        "p50_ms": round(_percentile(timings, 0.50), 3),
        "p95_ms": round(_percentile(timings, 0.95), 3),
        "min_ms": round(min(timings), 3),
        "max_ms": round(max(timings), 3),
        "mean_ms": round(statistics.fmean(timings), 3),
    }, builder_calls


def run_case(count: int, warmup: int, samples: int) -> dict[str, Any]:
    if count < 1:
        raise ValueError("size must be positive")
    if warmup < 0 or samples < 1:
        raise ValueError("warmup must be nonnegative and samples must be positive")

    with isolated_database() as database_path:
        _insert_legacy_jobs(count)
        with db.connect() as conn:
            conn.execute(
                "DELETE FROM schedule_metadata WHERE key=?",
                (MIGRATION_MARKER,),
            )
        before = _checkpoint_size(database_path)
        now = _last_case_now(count)

        first_start = time.perf_counter()
        migrated = schedule_db.migrate_legacy_scheduled_jobs(now)
        first_ms = (time.perf_counter() - first_start) * 1000
        after = _checkpoint_size(database_path)

        second_start = time.perf_counter()
        migrated_again = schedule_db.migrate_legacy_scheduled_jobs(now)
        second_ms = (time.perf_counter() - second_start) * 1000

        counts_after_migration = _counts()
        if migrated != count:
            raise AssertionError(f"expected {count} migrated jobs, got {migrated}")
        if migrated_again != 0:
            raise AssertionError(f"second migration was not idempotent: {migrated_again}")
        if counts_after_migration["daily_pipeline_jobs"] != count:
            raise AssertionError("migration changed the legacy job count")
        if counts_after_migration["occurrences"] != count:
            raise AssertionError("migration did not create one occurrence per legacy job")
        if counts_after_migration["dispatched_occurrences"] != count:
            raise AssertionError("migrated occurrences are not all dispatched")

        idle_before = counts_after_migration
        remainder = count % len(SCHEDULE_TIMES)
        idle_times = SCHEDULE_TIMES[:remainder] if remainder else SCHEDULE_TIMES
        idle_metrics, builder_calls = _measure_idle_reconcile(
            now,
            idle_times,
            warmup,
            samples,
        )
        idle_after = _counts()
        if builder_calls != 0:
            raise AssertionError(f"unexpected plan builder calls: {builder_calls}")
        if idle_after != idle_before:
            raise AssertionError(
                f"idle reconcile changed persistent counts: before={idle_before}, after={idle_after}"
            )

        integrity = _integrity_check()
        if integrity.lower() != "ok":
            raise AssertionError(f"PRAGMA integrity_check failed: {integrity}")

        return {
            "size": count,
            "days": (count + len(SCHEDULE_TIMES) - 1) // len(SCHEDULE_TIMES),
            "slots_per_day": len(SCHEDULE_TIMES),
            "idle_slots_on_last_day": len(idle_times),
            "first_migration_ms": round(first_ms, 3),
            "migrated": migrated,
            "db_before_checkpoint_bytes": before["main_bytes"],
            "db_after_checkpoint_bytes": after["main_bytes"],
            "db_checkpoint_delta_bytes": after["main_bytes"] - before["main_bytes"],
            "wal_after_checkpoint_bytes": after["wal_bytes"],
            "second_migration_ms": round(second_ms, 3),
            "second_migrated": migrated_again,
            "idle_warmup": warmup,
            "idle_samples": samples,
            "idle_reconcile_ms": idle_metrics,
            "business_or_model_calls": builder_calls,
            "integrity_check": integrity,
            "counts": idle_after,
        }


def _environment(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(__file__)
    hashed_sources = {
        "benchmark_schedule.py": source,
        "schedule_db.py": PROJECT_ROOT / "daily_coolpapers" / "schedule_db.py",
        "db.py": PROJECT_ROOT / "daily_coolpapers" / "db.py",
        "jobs.py": PROJECT_ROOT / "daily_coolpapers" / "jobs.py",
    }
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "python": platform.python_version(),
        "executable": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "warmup": args.warmup,
        "samples": args.samples,
        "sizes": args.sizes,
        "script": str(source),
        "source_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in hashed_sources.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10000])
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args(argv)
    if any(size < 1 for size in args.sizes):
        parser.error("sizes must be positive")
    if args.warmup < 0 or args.samples < 1:
        parser.error("warmup must be nonnegative and samples must be positive")

    results = []
    for size in args.sizes:
        print(f"benchmarking {size:,} legacy scheduled jobs", file=sys.stderr, flush=True)
        results.append(run_case(size, args.warmup, args.samples))
    print(
        json.dumps(
            {"environment": _environment(args), "results": results},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
