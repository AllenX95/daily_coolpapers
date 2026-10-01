"""Persistent daily-schedule state and atomic dispatch helpers.

The scheduler is deliberately kept separate from :mod:`jobs`' in-memory
queue.  A queue entry is only an execution hint; ``schedule_occurrences`` is
the source of truth for whether a scheduled slot has already been dispatched.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import db

SCHEDULE_KIND_DAILY_PIPELINE = "daily_pipeline"
SCHEDULE_KIND_CACHE_CLEANUP = "cache_cleanup"
SCHEDULE_TIME_DAILY = "daily"
SCHEDULE_TIMEZONE = "Asia/Shanghai"
SHANGHAI_TZ = timezone(timedelta(hours=8), SCHEDULE_TIMEZONE)
OCCURRENCE_STATES = frozenset({"due", "dispatched", "coalesced", "expired", "cancelled"})
_LEGACY_MIGRATION_KEY = "legacy_scheduled_jobs_v1"
_LEGACY_SCHEDULE_KEY = re.compile(
    r"^scheduled:(?P<date>\d{4}-\d{2}-\d{2})(?:[ T]|:)(?P<time>\d{2}:\d{2})$"
)
_CLOCK_TIME = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


@dataclass(frozen=True)
class ScheduleDispatch:
    """Jobs created by one reconciliation pass.

    ``queue_job``/``cleanup_queue_job`` are true only for rows newly created
    in this transaction.  An existing historical Job is linked for
    idempotency, but is intentionally not re-enqueued automatically.
    """

    job_id: int | None = None
    cleanup_job_id: int | None = None
    queue_job: bool = False
    queue_cleanup_job: bool = False
    occurrence_id: int | None = None
    coalesced_occurrence_ids: tuple[int, ...] = ()


def parse_daily_times(value: Any) -> list[str]:
    """Return valid, canonical, sorted ``HH:MM`` values from settings.

    Settings predating persistent scheduling were free-form text.  Invalid
    entries are ignored so a malformed one cannot make the scheduler loop
    fail; a user can still correct the value from the settings page.
    """

    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = ()
    result: set[str] = set()
    for raw in values:
        item = str(raw or "").strip()
        match = _CLOCK_TIME.fullmatch(item)
        if match:
            result.add(f"{int(match.group('hour')):02d}:{int(match.group('minute')):02d}")
    return sorted(result)


def _as_local_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        # Existing plan builders interpret naive injected values as Shanghai
        # local time, so preserve that convention here as well.
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _now_text(now: datetime) -> str:
    current = _as_local_now(now).replace(tzinfo=None, microsecond=0)
    return current.isoformat(sep=" ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


def _snapshot(times: list[str], scheduled_time: str, *, reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schedule_kind": SCHEDULE_KIND_DAILY_PIPELINE,
        "timezone": SCHEDULE_TIMEZONE,
        "scheduled_time": scheduled_time,
        "daily_times": list(times),
    }
    if reason:
        result["cancel_reason"] = reason
    return result


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["config_snapshot_data"] = _load_json(item.get("config_snapshot"), {})
    return item


def list_occurrences(
    *,
    schedule_kind: str | None = None,
    local_date: str | None = None,
    states: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if schedule_kind is not None:
        clauses.append("schedule_kind = ?")
        params.append(str(schedule_kind))
    if local_date is not None:
        clauses.append("local_date = ?")
        params.append(str(local_date))
    if states is not None:
        values = [str(value) for value in states]
        if not values:
            return []
        clauses.append(f"state IN ({','.join('?' for _ in values)})")
        params.extend(values)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM schedule_occurrences
            {where}
            ORDER BY local_date, scheduled_time, id
            """,
            params,
        ).fetchall()
    return [_row(item) for item in rows if item is not None]


def get_occurrence(
    schedule_kind: str,
    local_date: str,
    scheduled_time: str,
) -> dict[str, Any] | None:
    with db.connect() as conn:
        item = conn.execute(
            """
            SELECT * FROM schedule_occurrences
            WHERE schedule_kind=? AND local_date=? AND scheduled_time=?
            """,
            (schedule_kind, local_date, scheduled_time),
        ).fetchone()
    return _row(item)


def _legacy_schedule_key(local_date: str, scheduled_time: str) -> tuple[str, str]:
    return (
        f"scheduled:{local_date} {scheduled_time}",
        f"scheduled:{local_date}:{scheduled_time}",
    )


def _parse_legacy_key(value: Any) -> tuple[str, str] | None:
    match = _LEGACY_SCHEDULE_KEY.fullmatch(str(value or ""))
    if not match:
        return None
    local_date = match.group("date")
    scheduled_time = match.group("time")
    try:
        datetime.strptime(f"{local_date} {scheduled_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return local_date, scheduled_time


def _migrate_legacy_scheduled_jobs(conn: sqlite3.Connection, now: datetime) -> int:
    """Link recognizable pre-S2 scheduled jobs exactly once.

    The metadata marker is written in the caller's transaction.  A failure
    rolls back both the links and the marker, so a later startup can retry;
    normal scheduler polls only read the marker and never scan the Job table.
    """

    marker = conn.execute(
        "SELECT value FROM schedule_metadata WHERE key=?",
        (_LEGACY_MIGRATION_KEY,),
    ).fetchone()
    if marker:
        return 0
    migrated = 0
    for job in conn.execute(
        """
        SELECT id, type, idempotency_key, payload, created_at
        FROM jobs
        WHERE type=? AND idempotency_key LIKE 'scheduled:%'
        ORDER BY id
        """,
        (db.DAILY_PIPELINE_JOB_TYPE,),
    ):
        parsed = _parse_legacy_key(job["idempotency_key"])
        if parsed is None:
            continue
        local_date, scheduled_time = parsed
        existing = conn.execute(
            """
            SELECT id, state, job_id FROM schedule_occurrences
            WHERE schedule_kind=? AND local_date=? AND scheduled_time=?
            """,
            (SCHEDULE_KIND_DAILY_PIPELINE, local_date, scheduled_time),
        ).fetchone()
        payload = _load_json(job["payload"], {})
        if not isinstance(payload, dict):
            payload = {}
        snapshot = {
            "schedule_kind": SCHEDULE_KIND_DAILY_PIPELINE,
            "timezone": str(payload.get("timezone") or SCHEDULE_TIMEZONE),
            "scheduled_time": scheduled_time,
            "migrated_from_job_id": int(job["id"]),
            "trigger_source": payload.get("trigger_source", "scheduled"),
        }
        created_at = str(job["created_at"] or _now_text(now))
        if existing is None:
            conn.execute(
                """
                INSERT INTO schedule_occurrences(
                    schedule_kind, local_date, scheduled_time, timezone,
                    config_snapshot, state, job_id, created_at,
                    dispatched_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'dispatched', ?, ?, ?, ?)
                """,
                (
                    SCHEDULE_KIND_DAILY_PIPELINE,
                    local_date,
                    scheduled_time,
                    snapshot["timezone"],
                    _json(snapshot),
                    int(job["id"]),
                    created_at,
                    created_at,
                    _now_text(now),
                ),
            )
            migrated += 1
        elif existing["job_id"] is None and existing["state"] in {"due", "cancelled"}:
            conn.execute(
                """
                UPDATE schedule_occurrences
                SET timezone=?, config_snapshot=?, state='dispatched', job_id=?,
                    dispatched_at=COALESCE(dispatched_at, ?), updated_at=?
                WHERE id=?
                """,
                (
                    snapshot["timezone"],
                    _json(snapshot),
                    int(job["id"]),
                    created_at,
                    _now_text(now),
                    int(existing["id"]),
                ),
            )
            migrated += 1
    conn.execute(
        """
        INSERT INTO schedule_metadata(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (_LEGACY_MIGRATION_KEY, "completed", _now_text(now)),
    )
    return migrated


def migrate_legacy_scheduled_jobs(now: datetime | None = None) -> int:
    current = _as_local_now(now or datetime.now(SHANGHAI_TZ))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _migrate_legacy_scheduled_jobs(conn, current)


def _ensure_daily_occurrence(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    scheduled_time: str,
    times: list[str],
    now_text: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT * FROM schedule_occurrences
        WHERE schedule_kind=? AND local_date=? AND scheduled_time=?
        """,
        (SCHEDULE_KIND_DAILY_PIPELINE, local_date, scheduled_time),
    ).fetchone()
    snapshot = _snapshot(times, scheduled_time)
    encoded = _json(snapshot)
    if row is None:
        conn.execute(
            """
            INSERT INTO schedule_occurrences(
                schedule_kind, local_date, scheduled_time, timezone,
                config_snapshot, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'due', ?, ?)
            """,
            (
                SCHEDULE_KIND_DAILY_PIPELINE,
                local_date,
                scheduled_time,
                SCHEDULE_TIMEZONE,
                encoded,
                now_text,
                now_text,
            ),
        )
        return None

    item = dict(row)
    state = str(item["state"])
    old_snapshot = _load_json(item.get("config_snapshot"), {})
    if state == "due":
        if str(item.get("config_snapshot") or "") != encoded:
            conn.execute(
                "UPDATE schedule_occurrences SET config_snapshot=?, timezone=?, updated_at=? WHERE id=?",
                (encoded, SCHEDULE_TIMEZONE, now_text, int(item["id"])),
            )
    elif state == "cancelled":
        conn.execute(
            """
            UPDATE schedule_occurrences
            SET config_snapshot=?, timezone=?, state='due',
                job_id=NULL, coalesced_into_id=NULL, dispatched_at=NULL, updated_at=?
            WHERE id=?
            """,
            (encoded, SCHEDULE_TIMEZONE, now_text, int(item["id"])),
        )
    return int(item["id"])


def _reconcile_daily_rows(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    current_time: str,
    times: list[str],
    now_text: str,
) -> None:
    wanted = set(times)
    existing = conn.execute(
        """
        SELECT * FROM schedule_occurrences
        WHERE schedule_kind=? AND local_date=?
        """,
        (SCHEDULE_KIND_DAILY_PIPELINE, local_date),
    ).fetchall()
    for row in existing:
        scheduled_time = str(row["scheduled_time"])
        if scheduled_time not in wanted and row["state"] == "due":
            old_snapshot = _load_json(row["config_snapshot"], {})
            snapshot = dict(old_snapshot) if isinstance(old_snapshot, dict) else {}
            snapshot["cancel_reason"] = "configuration"
            conn.execute(
                "UPDATE schedule_occurrences SET state='cancelled', config_snapshot=?, updated_at=? WHERE id=?",
                (_json(snapshot), now_text, int(row["id"])),
            )
    for scheduled_time in times:
        _ensure_daily_occurrence(
            conn,
            local_date=local_date,
            scheduled_time=scheduled_time,
            times=times,
            now_text=now_text,
        )


def _cancel_current_schedule(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    now_text: str,
) -> None:
    """Cancel every current-day due slot when the configured list is empty."""

    rows = conn.execute(
        """
        SELECT id, config_snapshot FROM schedule_occurrences
        WHERE schedule_kind=? AND local_date=? AND state='due'
        """,
        (SCHEDULE_KIND_DAILY_PIPELINE, local_date),
    ).fetchall()
    for row in rows:
        snapshot = _load_json(row["config_snapshot"], {})
        snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        snapshot["cancel_reason"] = "configuration"
        conn.execute(
            "UPDATE schedule_occurrences SET state='cancelled', config_snapshot=?, updated_at=? WHERE id=?",
            (_json(snapshot), now_text, int(row["id"])),
        )


def _active_crawl_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in db.CRAWL_JOB_TYPES)
    return conn.execute(
        f"""
        SELECT id, type, status FROM jobs
        WHERE type IN ({placeholders}) AND status IN ('pending', 'running')
        ORDER BY created_at, id LIMIT 1
        """,
        db.CRAWL_JOB_TYPES,
    ).fetchone()


def _find_existing_scheduled_job(
    conn: sqlite3.Connection,
    local_date: str,
    scheduled_time: str,
) -> sqlite3.Row | None:
    keys = _legacy_schedule_key(local_date, scheduled_time)
    placeholders = ",".join("?" for _ in keys)
    return conn.execute(
        f"""
        SELECT id, type, status, idempotency_key FROM jobs
        WHERE type=? AND idempotency_key IN ({placeholders})
        ORDER BY id LIMIT 1
        """,
        (db.DAILY_PIPELINE_JOB_TYPE, *keys),
    ).fetchone()


def _mark_dispatched(
    conn: sqlite3.Connection,
    *,
    target: sqlite3.Row,
    job_id: int,
    now_text: str,
) -> tuple[int, ...]:
    target_id = int(target["id"])
    conn.execute(
        """
        UPDATE schedule_occurrences
        SET state='dispatched', job_id=?, dispatched_at=COALESCE(dispatched_at, ?),
            coalesced_into_id=NULL, updated_at=?
        WHERE id=?
        """,
        (int(job_id), now_text, now_text, target_id),
    )
    rows = conn.execute(
        """
        SELECT id FROM schedule_occurrences
        WHERE schedule_kind=? AND local_date=? AND state='due'
          AND scheduled_time < ?
        ORDER BY scheduled_time, id
        """,
        (SCHEDULE_KIND_DAILY_PIPELINE, target["local_date"], target["scheduled_time"]),
    ).fetchall()
    coalesced = tuple(int(row["id"]) for row in rows)
    if coalesced:
        placeholders = ",".join("?" for _ in coalesced)
        conn.execute(
            f"""
            UPDATE schedule_occurrences
            SET state='coalesced', coalesced_into_id=?, updated_at=?
            WHERE id IN ({placeholders})
            """,
            (target_id, now_text, *coalesced),
        )
    return coalesced


def _create_cleanup_job(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    now_text: str,
) -> tuple[int, bool]:
    idempotency_key = f"cleanup:{local_date}"
    existing = conn.execute(
        "SELECT id, type FROM jobs WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        if existing["type"] != "cleanup":
            raise ValueError("缓存清理幂等键已用于不同任务")
        return int(existing["id"]), False
    cur = conn.execute(
        """
        INSERT INTO jobs(type, status, idempotency_key, payload, created_at,
                         progress_total, progress_message)
        VALUES ('cleanup', 'pending', ?, '{}', ?, 1, '缓存清理等待执行')
        """,
        (idempotency_key, now_text),
    )
    return int(cur.lastrowid), True


def _ensure_cleanup_occurrence(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    now_text: str,
) -> tuple[int | None, bool]:
    row = conn.execute(
        """
        SELECT * FROM schedule_occurrences
        WHERE schedule_kind=? AND local_date=? AND scheduled_time=?
        """,
        (SCHEDULE_KIND_CACHE_CLEANUP, local_date, SCHEDULE_TIME_DAILY),
    ).fetchone()
    if row is not None and row["state"] in {"dispatched", "coalesced", "expired"}:
        return None, False
    if row is None:
        conn.execute(
            """
            INSERT INTO schedule_occurrences(
                schedule_kind, local_date, scheduled_time, timezone,
                config_snapshot, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'due', ?, ?)
            """,
            (
                SCHEDULE_KIND_CACHE_CLEANUP,
                local_date,
                SCHEDULE_TIME_DAILY,
                SCHEDULE_TIMEZONE,
                _json({"schedule_kind": SCHEDULE_KIND_CACHE_CLEANUP, "timezone": SCHEDULE_TIMEZONE}),
                now_text,
                now_text,
            ),
        )
        row = conn.execute("SELECT * FROM schedule_occurrences WHERE id=last_insert_rowid()").fetchone()
    job_id, created = _create_cleanup_job(conn, local_date=local_date, now_text=now_text)
    conn.execute(
        """
        UPDATE schedule_occurrences
        SET state='dispatched', job_id=?, dispatched_at=COALESCE(dispatched_at, ?), updated_at=?
        WHERE id=?
        """,
        (job_id, now_text, now_text, int(row["id"])),
    )
    return job_id, created


def reconcile_daily_schedule(
    now: datetime,
    *,
    enabled: bool,
    daily_times: Any,
    cleanup_daily: bool = True,
    plan_builder: Callable[..., dict[str, Any]] | None = None,
) -> ScheduleDispatch:
    """Reconcile today's slots and atomically dispatch at most one pipeline.

    Current-day due slots always follow the finite catch-up policy when the
    scheduler is enabled again.
    """

    current = _as_local_now(now)
    local_date = current.date().isoformat()
    current_time = current.strftime("%H:%M")
    times = parse_daily_times(daily_times)
    now_text = _now_text(current)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _migrate_legacy_scheduled_jobs(conn, current)
        conn.execute(
            """
            UPDATE schedule_occurrences
            SET state='expired', updated_at=?
            WHERE schedule_kind=? AND state='due' AND local_date < ?
            """,
            (now_text, SCHEDULE_KIND_DAILY_PIPELINE, local_date),
        )
        if not enabled:
            return ScheduleDispatch()
        if not times:
            _cancel_current_schedule(conn, local_date=local_date, now_text=now_text)
            return ScheduleDispatch()
        _reconcile_daily_rows(
            conn,
            local_date=local_date,
            current_time=current_time,
            times=times,
            now_text=now_text,
        )
        due = conn.execute(
            """
            SELECT * FROM schedule_occurrences
            WHERE schedule_kind=? AND local_date=? AND state='due'
              AND scheduled_time <= ?
            ORDER BY scheduled_time DESC, id DESC
            """,
            (SCHEDULE_KIND_DAILY_PIPELINE, local_date, current_time),
        ).fetchall()
        if not due:
            return ScheduleDispatch()
        target = due[0]

        existing_job = _find_existing_scheduled_job(conn, local_date, target["scheduled_time"])
        if existing_job is not None:
            coalesced = _mark_dispatched(
                conn,
                target=target,
                job_id=int(existing_job["id"]),
                now_text=now_text,
            )
            return ScheduleDispatch(
                job_id=int(existing_job["id"]),
                occurrence_id=int(target["id"]),
                coalesced_occurrence_ids=coalesced,
            )

        if _active_crawl_job(conn) is not None:
            return ScheduleDispatch()

        builder = plan_builder
        if builder is None:
            from .services import build_daily_pipeline_plan
            builder = build_daily_pipeline_plan
        # Build the frozen plan before changing any occurrence state.  Any
        # validation/configuration failure therefore rolls back this pass;
        # pre-existing due rows remain due and newly observed rows are
        # reconstructed on the next poll.
        plan = builder("scheduled", now=current)
        idempotency_key = f"scheduled:{local_date} {target['scheduled_time']}"
        job_id, created = db._create_daily_pipeline_job_in_connection(
            conn,
            plan,
            idempotency_key=idempotency_key,
        )
        if not created:
            # The helper can return an active Job if another caller won before
            # this transaction.  Keep the occurrence due unless the exact
            # idempotency key already existed and was therefore found above.
            return ScheduleDispatch()
        conn.execute(
            """
            UPDATE jobs SET progress_total=1, progress_message='流水线等待执行'
            WHERE id=?
            """,
            (job_id,),
        )
        coalesced = _mark_dispatched(
            conn,
            target=target,
            job_id=job_id,
            now_text=now_text,
        )
        cleanup_job_id: int | None = None
        cleanup_created = False
        if cleanup_daily:
            cleanup_job_id, cleanup_created = _ensure_cleanup_occurrence(
                conn,
                local_date=local_date,
                now_text=now_text,
            )
        return ScheduleDispatch(
            job_id=job_id,
            cleanup_job_id=cleanup_job_id,
            queue_job=True,
            queue_cleanup_job=cleanup_created,
            occurrence_id=int(target["id"]),
            coalesced_occurrence_ids=coalesced,
        )


__all__ = [
    "OCCURRENCE_STATES",
    "SCHEDULE_KIND_CACHE_CLEANUP",
    "SCHEDULE_KIND_DAILY_PIPELINE",
    "SCHEDULE_TIME_DAILY",
    "ScheduleDispatch",
    "get_occurrence",
    "list_occurrences",
    "migrate_legacy_scheduled_jobs",
    "parse_daily_times",
    "reconcile_daily_schedule",
]
