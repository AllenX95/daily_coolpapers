"""Minimal, request-level persistence for provider HTTP calls.

The caller opens an operation scope around one logical evaluation.  Each
business retry gets its own business-attempt scope; the low-level HTTP client
records every physical POST inside that scope without changing call_llm's API.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator
from uuid import uuid4

from . import db


@dataclass
class _Operation:
    operation_id: str
    call_type: str
    job_id: int | None = None
    paper_id: int | None = None
    evaluation_type: str | None = None
    memo_version_id: int | None = None
    prompt_id: int | None = None
    prompt_version: int | None = None
    profile_id: int | None = None
    model: str | None = None
    input_schema_version: str | None = None
    output_schema_version: str | None = None
    next_attempt_no: int = 1
    business_attempt_no: int = 0
    retry_reason: str | None = None
    current_ids: list[int] = field(default_factory=list)


_operation: contextvars.ContextVar[_Operation | None] = contextvars.ContextVar(
    "llm_call_operation", default=None
)

CALL_ATTEMPTS_MIGRATION = "llm_call_attempts_v1"


def init_schema(conn) -> None:
    """Create the append-oriented attempt ledger as an idempotent migration."""
    conn.execute("SAVEPOINT llm_call_attempts_v1")
    try:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS llm_call_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                business_attempt_no INTEGER NOT NULL CHECK(business_attempt_no > 0),
                attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
                call_type TEXT NOT NULL,
                evaluation_type TEXT,
                job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
                evaluation_id INTEGER REFERENCES evaluations(id) ON DELETE SET NULL,
                memo_version_id INTEGER REFERENCES investment_memo_versions(id) ON DELETE SET NULL,
                prompt_id INTEGER,
                prompt_version INTEGER,
                profile_id INTEGER,
                input_schema_version TEXT,
                output_schema_version TEXT,
                provider TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL CHECK(status IN ('provider_started','succeeded','failed','external_outcome_unknown')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                http_status INTEGER,
                input_hash TEXT NOT NULL,
                request_config_json TEXT NOT NULL DEFAULT '{}',
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                usage_source TEXT,
                usage_json TEXT NOT NULL DEFAULT '{}',
                usage_missing_reason TEXT,
                error_code TEXT,
                retry_reason TEXT,
                fallback_reason TEXT,
                UNIQUE(operation_id, attempt_no)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_llm_call_attempts_job ON llm_call_attempts(job_id, started_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_call_attempts_paper ON llm_call_attempts(paper_id, started_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_call_attempts_memo ON llm_call_attempts(memo_version_id, started_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_call_attempts_incomplete ON llm_call_attempts(started_at, id) WHERE status='provider_started'",
            "CREATE INDEX IF NOT EXISTS idx_llm_call_attempts_evaluation ON llm_call_attempts(evaluation_id)",
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            conn.execute(statement)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_call_attempts)")}
        for column in ("input_schema_version", "output_schema_version"):
            if column not in columns:
                conn.execute(f"ALTER TABLE llm_call_attempts ADD COLUMN {column} TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (CALL_ATTEMPTS_MIGRATION, db.now_iso()),
        )
        conn.execute("RELEASE SAVEPOINT llm_call_attempts_v1")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT llm_call_attempts_v1")
        conn.execute("RELEASE SAVEPOINT llm_call_attempts_v1")
        raise


@contextmanager
def operation_context(
    call_type: str,
    *,
    job_id: int | None = None,
    paper_id: int | None = None,
    evaluation_type: str | None = None,
    memo_version_id: int | None = None,
    prompt_id: int | None = None,
    prompt_version: int | None = None,
    profile_id: int | None = None,
    model: str | None = None,
    input_schema_version: str | None = None,
    output_schema_version: str | None = None,
) -> Iterator[str]:
    """Track requests inside this context; calls outside it remain untracked."""
    operation = _Operation(
        operation_id=uuid4().hex,
        call_type=str(call_type),
        job_id=_as_int(job_id),
        paper_id=_as_int(paper_id),
        evaluation_type=evaluation_type,
        memo_version_id=_as_int(memo_version_id),
        prompt_id=_as_int(prompt_id),
        prompt_version=_as_int(prompt_version),
        profile_id=_as_int(profile_id),
        model=_safe_model(model),
        input_schema_version=_safe_schema_version(input_schema_version),
        output_schema_version=_safe_schema_version(output_schema_version),
    )
    token = _operation.set(operation)
    try:
        yield operation.operation_id
    finally:
        _operation.reset(token)


@contextmanager
def business_attempt(*, retry_reason: str | None = None) -> Iterator[tuple[int, ...]]:
    """Start one business try and collect all its physical HTTP attempt IDs."""
    operation = _operation.get()
    if operation is None:
        yield ()
        return
    operation.business_attempt_no += 1
    operation.retry_reason = _safe_reason(retry_reason)
    operation.current_ids = []
    try:
        yield tuple(operation.current_ids)
    finally:
        operation.retry_reason = None


def current_operation_id() -> str | None:
    operation = _operation.get()
    return operation.operation_id if operation else None


def current_attempt_ids() -> tuple[int, ...]:
    operation = _operation.get()
    return tuple(operation.current_ids) if operation else ()


def begin_provider_request(
    provider: str,
    payload: dict[str, Any],
    *,
    fallback_reason: str | None = None,
    transport_retry_reason: str | None = None,
) -> tuple[int | None, float]:
    """Commit provider_started before the transport is allowed to send."""
    operation = _operation.get()
    started = time.perf_counter()
    if operation is None:
        return None, started
    if operation.business_attempt_no == 0:
        # Keep direct uses inside a declared operation safe and attributable.
        operation.business_attempt_no = 1
        operation.current_ids = []
    request_input = _input_payload(provider, payload)
    input_hash = hashlib.sha256(
        json.dumps(request_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    request_config = _request_config(payload)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt_no = operation.next_attempt_no
        operation.next_attempt_no += 1
        cur = conn.execute(
            """INSERT INTO llm_call_attempts(
                operation_id,business_attempt_no,attempt_no,call_type,evaluation_type,
                job_id,paper_id,memo_version_id,prompt_id,prompt_version,profile_id,
                input_schema_version,output_schema_version,provider,model,status,started_at,input_hash,request_config_json,
                retry_reason,fallback_reason,usage_missing_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'provider_started',?,?,?,?,?,?)""",
            (
                operation.operation_id, operation.business_attempt_no, attempt_no,
                operation.call_type, operation.evaluation_type or operation.call_type,
                operation.job_id, operation.paper_id, operation.memo_version_id,
                operation.prompt_id, operation.prompt_version, operation.profile_id,
                operation.input_schema_version, operation.output_schema_version,
                _safe_provider(provider), _safe_model(payload.get("model") or operation.model),
                db.now_iso(), input_hash,
                json.dumps(request_config, ensure_ascii=False, sort_keys=True),
                _safe_reason(transport_retry_reason) or operation.retry_reason,
                _safe_reason(fallback_reason), "response_not_received",
            ),
        )
        if operation.memo_version_id is not None:
            version = conn.execute(
                "SELECT status,provider_started,call_operation_id FROM investment_memo_versions WHERE id=?",
                (operation.memo_version_id,),
            ).fetchone()
            if not version or version["status"] != "running":
                raise RuntimeError("memo_not_running")
            if version["call_operation_id"] not in (None, operation.operation_id):
                raise RuntimeError("memo_operation_mismatch")
            conn.execute(
                "UPDATE investment_memo_versions SET provider_started=1,call_operation_id=? WHERE id=?",
                (operation.operation_id, operation.memo_version_id),
            )
        attempt_id = int(cur.lastrowid)
    operation.current_ids.append(attempt_id)
    return attempt_id, started


def finish_provider_response(
    attempt_id: int | None,
    started: float,
    provider: str,
    response: Any,
) -> dict[str, Any] | None:
    """Save a received HTTP result and whitelisted usage before parsing it."""
    if attempt_id is None:
        return None
    duration_ms = max(0, round((time.perf_counter() - started) * 1000))
    status_code = _http_status(response)
    status = ("external_outcome_unknown" if status_code is None else
              "succeeded" if status_code < 400 else "failed")
    usage = _response_usage(response, provider)
    missing_reason = None if usage["input_tokens"] is not None or usage["output_tokens"] is not None else "usage_not_returned"
    error_code = (None if status == "succeeded" else
                  f"http_{status_code}" if status_code is not None else "response_status_missing")
    with db.connect() as conn:
        conn.execute(
            """UPDATE llm_call_attempts SET status=?,finished_at=?,duration_ms=?,http_status=?,
                input_tokens=?,output_tokens=?,total_tokens=?,usage_source=?,usage_json=?,
                usage_missing_reason=?,error_code=? WHERE id=? AND status='provider_started'""",
            (
                status, db.now_iso(), duration_ms, status_code,
                usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
                usage["usage_source"], json.dumps(usage["details"], ensure_ascii=False, sort_keys=True),
                missing_reason, error_code, attempt_id,
            ),
        )
    return usage


def finish_provider_transport_error(attempt_id: int | None, started: float, error: BaseException) -> None:
    """Transport failures are conservatively unknown unless an HTTP response exists."""
    if attempt_id is None:
        return
    with db.connect() as conn:
        conn.execute(
            """UPDATE llm_call_attempts SET status='external_outcome_unknown',finished_at=?,
                duration_ms=?,error_code=?,usage_missing_reason='response_not_received'
                WHERE id=? AND status='provider_started'""",
            (db.now_iso(), max(0, round((time.perf_counter() - started) * 1000)),
             _safe_error_code(error), attempt_id),
        )


def associate_current_attempts(
    conn,
    *,
    evaluation_id: int | None = None,
    memo_version_id: int | None = None,
) -> int:
    """Associate this business try's physical requests in its result transaction."""
    ids = current_attempt_ids()
    if not ids:
        return 0
    if evaluation_id is None and memo_version_id is None:
        raise ValueError("attempt association requires an evaluation or memo version")
    assignments, values = [], []
    if evaluation_id is not None:
        assignments.append("evaluation_id=?")
        values.append(int(evaluation_id))
    if memo_version_id is not None:
        assignments.append("memo_version_id=?")
        values.append(int(memo_version_id))
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE llm_call_attempts SET {','.join(assignments)} WHERE id IN ({marks})",
        (*values, *ids),
    )
    return cur.rowcount


def mark_incomplete_unknown(conn) -> int:
    """Recover each unfinished physical request, including rows without claims."""
    cur = conn.execute(
        """UPDATE llm_call_attempts SET status='external_outcome_unknown',finished_at=?,
            error_code='process_interrupted',usage_missing_reason='response_not_received',
            duration_ms=MAX(0,CAST((julianday(?) - julianday(started_at))*86400000 AS INTEGER))
            WHERE status='provider_started'""",
        (db.now_iso(), db.now_iso()),
    )
    return cur.rowcount


def details_for_job(conn, job_id: int) -> dict[str, Any]:
    return _details(conn, "job_id=?", (int(job_id),))


def details_for_paper(conn, paper_id: int) -> dict[str, Any]:
    return _details(conn, "paper_id=?", (int(paper_id),))


def details_for_memo(conn, memo_version_id: int) -> dict[str, Any]:
    return _details(conn, "memo_version_id=?", (int(memo_version_id),))


def attempts_for_operation(conn, operation_id: str) -> list[dict[str, Any]]:
    return _items(conn, "operation_id=?", (str(operation_id),))


def _details(conn, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    try:
        row = conn.execute(
            f"""SELECT COUNT(*) AS calls,
                COALESCE(SUM(input_tokens),0) AS known_input_tokens,
                COALESCE(SUM(output_tokens),0) AS known_output_tokens,
                COALESCE(SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL THEN 1 ELSE 0 END),0) AS usage_missing_calls,
                COALESCE(SUM(CASE WHEN (input_tokens IS NULL) != (output_tokens IS NULL) THEN 1 ELSE 0 END),0) AS usage_partial_calls,
                COALESCE(SUM(CASE WHEN status='external_outcome_unknown' THEN 1 ELSE 0 END),0) AS external_outcome_unknown_calls,
                COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed_calls,
                COALESCE(SUM(duration_ms),0) AS duration_ms,
                GROUP_CONCAT(DISTINCT model) AS models
                FROM llm_call_attempts WHERE {where}""",
            params,
        ).fetchone()
        summary = dict(row)
        summary["models"] = sorted(filter(None, (summary.get("models") or "").split(",")))
        items = _items(conn, where, params)
        summary["items_truncated"] = summary["calls"] > len(items)
        return {"summary": summary, "items": items}
    finally:
        if started_transaction:
            conn.rollback()


def _items(conn, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT id,operation_id,business_attempt_no,attempt_no,call_type,evaluation_type,
            job_id,paper_id,evaluation_id,memo_version_id,provider,model,status,started_at,
            finished_at,duration_ms,input_tokens,output_tokens,total_tokens,usage_source,
            usage_missing_reason,http_status,error_code,retry_reason,fallback_reason,input_hash,
            input_schema_version,output_schema_version
            FROM llm_call_attempts WHERE {where} ORDER BY started_at DESC,id DESC LIMIT 100""",
        params,
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _input_payload(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    if _safe_provider(provider) == "anthropic":
        return {key: payload.get(key) for key in ("system", "messages") if key in payload}
    return {"messages": payload.get("messages")}


def _request_config(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    model = _safe_model(payload.get("model"))
    if model is not None:
        result["model"] = model
    for key in ("temperature", "max_tokens"):
        value = payload.get(key)
        if _finite_config_number(value):
            result[key] = value
    response_format = payload.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema", "text"}:
        result["response_format"] = {"type": response_format["type"]}
    return result


def _response_usage(response: Any, provider: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = None
    raw = normalize_usage(data.get("usage") if isinstance(data, dict) else None) or {}
    input_value = raw.get("input_tokens", raw.get("prompt_tokens"))
    output_value = raw.get("output_tokens", raw.get("completion_tokens"))
    total_value = raw.get("total_tokens")
    details: dict[str, int] = {}
    for key, value in (
        ("input_tokens", input_value), ("output_tokens", output_value), ("total_tokens", total_value),
        ("cache_creation_input_tokens", raw.get("cache_creation_input_tokens")),
        ("cache_read_input_tokens", raw.get("cache_read_input_tokens")),
    ):
        number = _nonnegative_int(value)
        if number is not None:
            details[key] = number
    nested = raw.get("details")
    if isinstance(nested, dict):
        for key in ("cached_tokens", "reasoning_tokens"):
            number = _nonnegative_int(nested.get(key))
            if number is not None:
                details[key] = number
    return {
        "input_tokens": _nonnegative_int(input_value),
        "output_tokens": _nonnegative_int(output_value),
        "total_tokens": _nonnegative_int(total_value),
        "usage_source": _safe_provider(provider) if raw else None,
        "details": details,
    }


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, int):
        return value if value <= 9223372036854775807 else None
    if not math.isfinite(value) or value > 9223372036854775807:
        return None
    return int(value)


def _finite_config_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return -9223372036854775808 <= value <= 9223372036854775807
    return math.isfinite(value)


def normalize_usage(value: Any) -> dict[str, Any] | None:
    """Keep recognized numeric usage aliases and supported cache subcounts."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    accepted = {
        "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }
    for key in accepted:
        number = _nonnegative_int(value.get(key))
        if number is not None:
            result[key] = number
    nested: dict[str, Any] = {}
    if isinstance(value.get("details"), dict):
        nested.update(value["details"])
    for parent in ("prompt_tokens_details", "completion_tokens_details"):
        if isinstance(value.get(parent), dict):
            nested.update(value[parent])
    details = {}
    for key in ("cached_tokens", "reasoning_tokens"):
        number = _nonnegative_int(nested.get(key))
        if number is not None:
            details[key] = number
    if details:
        result["details"] = details
    return result or None


def _http_status(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    return int(value) if isinstance(value, int) and 100 <= value <= 599 else None


def _safe_provider(value: Any) -> str:
    return str(value)[:40] if isinstance(value, str) and value in {"openai_compatible", "anthropic"} else "other"


def _safe_model(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:160]


def _safe_schema_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return value[:80] if value and all(char in allowed for char in value) else None


def _safe_reason(value: Any) -> str | None:
    allowed = {"provider_retry", "provider_concurrency_limit", "response_format_unsupported", "format_fallback", "business_retry"}
    return str(value) if isinstance(value, str) and value in allowed else None


def _safe_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.replace("_", "").isalnum():
        return code[:64]
    return type(error).__name__[:64]


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
