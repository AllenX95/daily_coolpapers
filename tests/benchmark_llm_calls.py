"""Offline S4 local call-recording overhead measurement.

Run once after S4 code is frozen with the repository's isolated test Python:
    tmp/a4-test-env/Scripts/python.exe -B -m tests.benchmark_llm_calls

The benchmark uses a temporary SQLite database and an httpx MockTransport.
It never calls a real provider and records its report only after all checks pass.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from typing import Any
from unittest.mock import patch

import httpx

from daily_coolpapers import config, db


WARMUP = 5
SAMPLES = 30
API_KEY_SENTINEL = "S4_BENCHMARK_API_KEY_MUST_NOT_BE_PERSISTED"
PROMPT = "Return the fixed JSON object {\"ok\": true} for the offline S4 benchmark."


@contextmanager
def isolated_database():
    """Mirror the S0 benchmark's temporary-path and no-runtime isolation."""
    with tempfile.TemporaryDirectory(prefix="coolpapers-s4-") as temporary_root, ExitStack() as stack:
        root = Path(temporary_root)
        paths = {
            "BASE_DIR": root,
            "INSTANCE_DIR": root / "instance",
            "DATA_DIR": root / "data",
            "CACHE_DIR": root / "cache",
            "PDF_CACHE_DIR": root / "cache" / "pdf",
            "MARKDOWN_CACHE_DIR": root / "cache" / "markdown",
            "LOG_DIR": root / "logs",
            "CURRENT_LOG": root / "logs" / "current.log",
            "DB_PATH": root / "data" / "main.sqlite3",
            "LLM_PROFILES_DB_PATH": root / "instance" / "profiles.sqlite3",
        }
        for name, value in paths.items():
            stack.enter_context(patch.object(config, name, value))

        from daily_coolpapers import app, call_attempts, llm, security

        for module in (db, app, call_attempts, llm):
            for name, value in paths.items():
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, value))

        secret_store = security.SecretStore(paths["INSTANCE_DIR"] / "fernet.key")
        for module in (app, call_attempts, llm, security):
            if hasattr(module, "secret_store"):
                stack.enter_context(patch.object(module, "secret_store", secret_store))

        # MockTransport must be the only network path. Startup is also forbidden.
        stack.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        )
        stack.enter_context(
            patch.object(app, "start_runtime", side_effect=AssertionError("runtime forbidden"))
        )
        db.init_db()
        yield {
            "app": app,
            "attempts": call_attempts,
            "db": db,
            "llm": llm,
            "paths": paths,
            "root": root,
            "secret_store": secret_store,
        }


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def schema_snapshot(database_module: Any) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with database_module.connect() as conn:
        migrations = [
            tuple(row)
            for row in conn.execute(
                "SELECT name, applied_at FROM schema_migrations ORDER BY name"
            )
        ]
        objects = [
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    return migrations, objects


def assert_migrations_idempotent(database_module: Any) -> dict[str, Any]:
    before = schema_snapshot(database_module)
    with database_module.connect() as conn:
        attempts_before = conn.execute("SELECT COUNT(*) FROM llm_call_attempts").fetchone()[0]

    # Re-enter the normal initializer twice, as on repeated application startup.
    database_module.init_db()
    database_module.init_db()
    after = schema_snapshot(database_module)
    with database_module.connect() as conn:
        attempts_after = conn.execute("SELECT COUNT(*) FROM llm_call_attempts").fetchone()[0]

    if before != after or attempts_before != attempts_after:
        raise AssertionError("re-running db.init_db changed the schema or attempt rows")
    return {
        "initializer_runs": 3,
        "schema_and_migration_rows_unchanged": True,
        "migration_rows": len(after[0]),
        "attempt_rows_before": attempts_before,
        "attempt_rows_after": attempts_after,
    }


def mock_client_factory(request_signatures: list[str], expected_api_key: str):
    """Build real httpx clients whose transport returns a fixed provider response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/v1/chat/completions":
            raise AssertionError("unexpected mock provider request")
        if request.headers.get("authorization") != f"Bearer {expected_api_key}":
            raise AssertionError("mock request did not carry the expected ephemeral API key")

        # Keep only a non-reversible request fingerprint in memory and the report.
        body = request.content
        body_hash = hashlib.sha256(body).hexdigest()
        request_signatures.append(
            hashlib.sha256(
                f"{request.method}\n{request.url.path}\n{body_hash}".encode("utf-8")
            ).hexdigest()
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            },
            request=request,
        )

    def factory(_profile: dict[str, Any]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


def make_profile(secret_store: Any) -> dict[str, Any]:
    return {
        "id": 91001,
        "provider": "openai_compatible",
        "base_url": "https://provider.invalid/v1",
        "model": "s4-offline-fixture",
        "encrypted_api_key_ref": secret_store.encrypt(API_KEY_SENTINEL),
        "temperature": 0,
        "max_output_tokens": 64,
    }


def invoke_once(components: dict[str, Any], profile: dict[str, Any], tracked: bool) -> dict[str, Any]:
    """Call the production llm.call_llm path and return semantic response facts."""
    llm = components["llm"]
    attempts = components["attempts"]

    def call() -> dict[str, Any]:
        response = llm.call_llm(profile, PROMPT)
        if response.result_json != {"ok": True}:
            raise AssertionError("mock LLM response did not parse as expected")
        usage = response.usage if isinstance(response.usage, dict) else {}
        semantic_usage = {
            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
            "total_tokens": usage.get("total_tokens"),
        }
        if semantic_usage != {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}:
            raise AssertionError(f"mock LLM usage differs from expected semantic counts: {semantic_usage}")
        return {"result_json": response.result_json, "usage": semantic_usage}

    if tracked:
        with attempts.operation_context(
            "abstract_review",
            evaluation_type="abstract_review",
            profile_id=profile["id"],
            model=profile["model"],
        ):
            with attempts.business_attempt():
                return call()
    return call()


def measure_case(tracked: bool) -> dict[str, Any]:
    request_signatures: list[str] = []
    with isolated_database() as components:
        database_module = components["db"]
        llm = components["llm"]
        profile = make_profile(components["secret_store"])

        with patch.object(
            llm,
            "make_llm_client",
            mock_client_factory(request_signatures, API_KEY_SENTINEL),
        ):
            if tracked:
                migration = assert_migrations_idempotent(database_module)
            else:
                migration = None

            timings_ms: list[float] = []
            response_facts: list[dict[str, Any]] = []
            for _ in range(WARMUP):
                response_facts.append(invoke_once(components, profile, tracked))
            for index in range(SAMPLES):
                started = time.perf_counter_ns()
                response_facts.append(invoke_once(components, profile, tracked))
                timings_ms.append((time.perf_counter_ns() - started) / 1_000_000)
                if (index + 1) % 10 == 0:
                    print(
                        f"  {'tracked' if tracked else 'untracked'}: {index + 1}/{SAMPLES}",
                        flush=True,
                    )

        with database_module.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM llm_call_attempts ORDER BY id")
            ]
        expected_rows = WARMUP + SAMPLES if tracked else 0
        if len(rows) != expected_rows:
            raise AssertionError(f"expected {expected_rows} attempt rows, found {len(rows)}")
        if tracked and any(row.get("status") != "succeeded" for row in rows):
            raise AssertionError("not all mocked provider attempts reached succeeded")

        secret_values = (API_KEY_SENTINEL, f"Bearer {API_KEY_SENTINEL}")
        attempt_dump = json.dumps(rows, ensure_ascii=False, default=str)
        if any(secret in attempt_dump for secret in secret_values):
            raise AssertionError("an API-key or Authorization sentinel was persisted in attempts")

        db_bytes = {
            path.name: path.stat().st_size
            for path in components["root"].rglob("main.sqlite3*")
            if path.is_file()
        }
        return {
            "tracked": tracked,
            "timings_ms": timings_ms,
            "response_facts": response_facts,
            "request_signatures": request_signatures,
            "attempt_rows": len(rows),
            "attempt_rows_status": sorted({str(row.get("status")) for row in rows}),
            "secrets_absent_from_attempt_rows": True,
            "database_bytes": db_bytes,
            "migration": migration,
        }


def environment_metadata() -> dict[str, Any]:
    source_paths = [
        Path("daily_coolpapers/llm.py"),
        Path("daily_coolpapers/call_attempts.py"),
        Path("daily_coolpapers/db.py"),
        Path(__file__),
    ]
    sources = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_paths
        if path.exists()
    }
    return {
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "executable": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "warmup": WARMUP,
        "samples": SAMPLES,
        "mock_transport": "httpx.MockTransport; no external provider/network",
        "source_sha256": sources,
    }


def summarize(case: dict[str, Any]) -> dict[str, Any]:
    values = case["timings_ms"]
    return {
        "mean_ms": statistics.fmean(values),
        "p50_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
        "timed_samples": len(values),
        "mock_requests_including_warmup": len(case["request_signatures"]),
        "attempt_rows": case["attempt_rows"],
        "database_bytes": case["database_bytes"],
        "attempt_rows_status": case["attempt_rows_status"],
        "secrets_absent_from_attempt_rows": case["secrets_absent_from_attempt_rows"],
        "migration": case["migration"],
    }


def build_report(untracked: dict[str, Any], tracked: dict[str, Any]) -> str:
    untracked_summary = summarize(untracked)
    tracked_summary = summarize(tracked)
    if len(untracked["request_signatures"]) != len(tracked["request_signatures"]):
        raise AssertionError("tracked and untracked mock request counts differ")
    same_requests = untracked["request_signatures"] == tracked["request_signatures"]
    if not same_requests:
        raise AssertionError("tracked and untracked request fingerprints differ")
    same_responses = untracked["response_facts"] == tracked["response_facts"]
    if not same_responses:
        raise AssertionError("tracked and untracked response or normalized usage facts differ")

    overhead_ms = tracked_summary["mean_ms"] - untracked_summary["mean_ms"]
    if overhead_ms > 50:
        status = "FAIL"
    else:
        status = "PASS"

    environment = environment_metadata()
    output = [
        "# S4 local LLM call-recording overhead baseline",
        "",
        f"Measurement time: {environment['measured_at']}",
        "",
        "This offline measurement uses a temporary SQLite database and the production `llm.call_llm` path with `httpx.MockTransport`. No real provider or external network is called.",
        "",
        "## Environment and settings",
        "",
        "```json",
        json.dumps(environment, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Results",
        "",
        "| Path | Mean ms | p50 ms | p95 ms | Timed samples | Mock requests incl. warmup | Attempt rows |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, summary in (("Without tracing", untracked_summary), ("With tracing", tracked_summary)):
        output.append(
            f"| {name} | {summary['mean_ms']:.3f} | {summary['p50_ms']:.3f} | "
            f"{summary['p95_ms']:.3f} | {summary['timed_samples']} | "
            f"{summary['mock_requests_including_warmup']} | {summary['attempt_rows']} |"
        )
    output.extend(
        [
            "",
            f"Mean additional local overhead per physical attempt: **{overhead_ms:.3f} ms** (mean with tracing minus mean without tracing; each invocation sent one mock HTTP request; target ≤50 ms): **{status}**.",
            f"Request equivalence: **{'PASS' if same_requests else 'FAIL'}**; each path issued {len(untracked['request_signatures'])} mock requests including warmup, and all request fingerprints matched.",
            f"Response and normalized usage equivalence: **{'PASS' if same_responses else 'FAIL'}**; both paths returned the same result and input/output/total token counts.",
            "",
            "## Migration and persistence checks",
            "",
            f"- Repeated `db.init_db()` was idempotent: **{tracked_summary['migration']['schema_and_migration_rows_unchanged']}** across {tracked_summary['migration']['initializer_runs']} initializer runs; migration rows remained {tracked_summary['migration']['migration_rows']} and attempt rows remained {tracked_summary['migration']['attempt_rows_before']} before measurement.",
            f"- Tracked path created {tracked_summary['attempt_rows']} successful attempt rows; untracked path created {untracked_summary['attempt_rows']}.",
            f"- API-key and Authorization sentinels were absent from the attempt rows: **{tracked_summary['secrets_absent_from_attempt_rows']}**. Check scope is only the new `llm_call_attempts` records.",
            f"- Temporary SQLite files (tracked fixture): `{json.dumps(tracked_summary['database_bytes'], sort_keys=True)}` bytes by file.",
            "",
            "Request fingerprints are SHA-256 digests of method, path, and request-body digest; no prompt or credential is included in this report.",
            "",
        ]
    )
    return "\n".join(output)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "doc" / "S4_BASELINE.md")
    args = parser.parse_args()
    if args.report.exists():
        parser.error(f"report already exists; choose a new --report path: {args.report}")

    print("Measuring untracked mock LLM calls", flush=True)
    untracked = measure_case(tracked=False)
    print("Measuring tracked mock LLM calls", flush=True)
    tracked = measure_case(tracked=True)
    report = build_report(untracked, tracked)
    args.report.write_text(report, encoding="utf-8")
    print(f"Wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
