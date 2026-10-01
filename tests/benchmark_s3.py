"""Focused S3 before/after benchmark.

This runner measures the HTML paths that S3 migrates while retaining one
full-materialization reference for the two old service contracts.  It uses
only temporary SQLite databases and the deterministic fixture from
``benchmark_architecture``::

    python -B -m tests.benchmark_s3
    python -B -m tests.benchmark_s3 --sizes 10000 --case reviewed/html
    python -B -m tests.benchmark_s3 --report S3_BASELINE.json

The default run is intentionally a small representative matrix: first page
at 30 items and a deep page at 100 items for each ordinary collection, plus
one global and one selective memo-candidate HTML page.  The legacy reviewed
and memo JSON references run only at 10,000 papers.  A 100,000-paper run
measures the new HTML cases too; the slow old full-list cases are left in the
S0 report.  Each size first populates an isolated old-style database without
the projection artifacts, records the first and second projection ensures,
then runs the HTML cases on that same migrated fixture.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import time
from typing import Any, Callable, Iterator
from unittest.mock import patch

from daily_coolpapers import db, memo_candidates, memo_db

from tests.benchmark_architecture import (
    isolated_database,
    measure,
    populate,
)


DEFAULT_REFERENCE_SIZE = 10_000
_PROJECTION_TRIGGERS = (
    "trg_fulltext_projection_insert",
    "trg_fulltext_projection_update",
    "trg_fulltext_projection_delete",
)


def _register_benchmark_query_functions(conn: sqlite3.Connection) -> None:
    """Register the read-only UDFs used by paginated collection SQL.

    ``benchmark_architecture.measure`` opens a fresh connection for
    ``EXPLAIN QUERY PLAN``.  The application registers these functions on the
    query connection itself, so this benchmark must register them on its
    temporary measurement connections too; otherwise a valid plan is reported
    as ``UNAVAILABLE: no such function``.
    """

    conn.create_function("collection_score_int", 2, db._collection_page_score)
    conn.create_function("collection_rank_int", 1, db._collection_page_rank)
    conn.create_function("collection_lower", 1, db._collection_page_lower)
    memo_candidates.register_functions(conn)


@contextmanager
def _benchmark_connections_with_functions() -> Iterator[None]:
    """Patch only this benchmark's temporary connections with collection UDFs."""

    original_connect = db.connect

    def connect(path: Path | None = None) -> sqlite3.Connection:
        conn = original_connect(path)
        _register_benchmark_query_functions(conn)
        return conn

    with patch.object(db, "connect", connect):
        yield


@contextmanager
def isolated_s3_database() -> Iterator[tuple[Any, Path]]:
    """Yield the S0 isolated app and initialize the profile store for memo HTML.

    ``benchmark_architecture.isolated_database`` already patches every module
    bound to the temporary paths and blocks network/runtime startup.  The
    memo page additionally reads the separate profile database, so initialize
    it here and insert one harmless synthetic profile.
    """

    with isolated_database() as (application, root):
        db.init_llm_profiles_db()
        db.save_llm_profile(
            {
                "name": "S3 synthetic profile",
                "provider": "synthetic",
                "base_url": "https://synthetic.invalid/v1",
                "model": "synthetic-s3",
                "custom_headers": "{}",
                "temperature": 0.2,
                "max_output_tokens": 2000,
                "context_window_tokens": 128000,
                "timeout_seconds": 10,
                "enabled": 1,
                "is_default_memo": 1,
            }
        )
        with _benchmark_connections_with_functions():
            yield application, root


def _assert_isolated_database(root: Path) -> Path:
    """Return the expected temporary DB path and reject accidental real DB use."""

    expected = (root / "data" / "main.sqlite3").resolve()
    actual = Path(db.DB_PATH).resolve()
    if actual != expected:
        raise RuntimeError(f"S3 benchmark database escaped its temporary root: {actual}")
    return expected


def _database_storage(root: Path) -> dict[str, Any]:
    """Capture the main SQLite file and its possible journal sidecars."""

    database_path = _assert_isolated_database(root)
    sizes: dict[str, int] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(database_path) + suffix)
        sizes[suffix or "main"] = path.stat().st_size if path.exists() else 0
    return {"bytes": sum(sizes.values()), "files": sizes}


def _prepare_legacy_projection(root: Path) -> None:
    """Remove only projection artifacts so populate() writes an old-style DB."""

    _assert_isolated_database(root)
    with db.connect() as conn:
        for trigger in _PROJECTION_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP INDEX IF EXISTS idx_fulltext_projection_latest")
        conn.execute("DROP TABLE IF EXISTS fulltext_evaluation_projection")
        conn.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            (db.FULLTEXT_EVALUATION_PROJECTION_MIGRATION,),
        )
        remaining = conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name IN ('fulltext_evaluation_projection', "
            "'trg_fulltext_projection_insert', 'trg_fulltext_projection_update', "
            "'trg_fulltext_projection_delete')"
        ).fetchall()
        marker = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (db.FULLTEXT_EVALUATION_PROJECTION_MIGRATION,),
        ).fetchone()
        if remaining or marker is not None:
            raise AssertionError("failed to create the isolated old-style projection state")


def _digest_rows(rows: Any) -> tuple[int, int, str]:
    """Hash a cursor without materializing large evaluation payloads in Python."""

    digest = hashlib.sha256()
    count = 0
    paper_ids: set[int] = set()
    for row in rows:
        values = tuple(row)
        digest.update(json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        count += 1
        if len(values) > 1 and values[1] is not None:
            paper_ids.add(int(values[1]))
    return count, len(paper_ids), digest.hexdigest()


def _successful_evaluation_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    count, papers, digest = _digest_rows(
        conn.execute(
            """
            SELECT id, paper_id, created_at
            FROM evaluations
            WHERE evaluation_type = 'fulltext_review' AND status = 'success'
            ORDER BY id
            """
        )
    )
    return {"rows": count, "distinct_papers": papers, "sha256": digest}


def _projection_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'fulltext_evaluation_projection'"
    ).fetchone()
    if table_exists is None:
        raise AssertionError("projection table was not created by migration")
    count, papers, digest = _digest_rows(
        conn.execute(
            """
            SELECT evaluation_id, paper_id, created_at, score_type, score_scalar
            FROM fulltext_evaluation_projection
            ORDER BY evaluation_id
            """
        )
    )
    marker = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
        (db.FULLTEXT_EVALUATION_PROJECTION_MIGRATION,),
    ).fetchone()[0]
    return {
        "rows": count,
        "distinct_papers": papers,
        "sha256": digest,
        "schema_marker_count": int(marker),
    }


def _migration_query_plans(conn: sqlite3.Connection, statements: list[str]) -> list[dict[str, Any]]:
    """Explain each distinct DML shape emitted by the first migration."""

    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for statement in statements:
        sql = statement.strip()
        upper = sql.upper()
        if not upper.startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
            continue
        shape = re.sub(r"\b\d+\b", "?", sql)
        if shape in seen:
            continue
        seen.add(shape)
        try:
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        except sqlite3.Error as exc:
            plan = ["UNAVAILABLE: " + str(exc)]
        plans.append({"query": shape[:1200], "plan": plan})
    return plans


def _measure_projection_migration(root: Path) -> dict[str, Any]:
    """Measure first backfill and a second idempotent ensure on one old fixture."""

    _assert_isolated_database(root)
    storage_before = _database_storage(root)
    with db.connect() as conn:
        evaluations = _successful_evaluation_snapshot(conn)

    first_statements: list[str] = []
    first_started = time.perf_counter()
    with db.connect() as conn:
        conn.set_trace_callback(
            lambda sql: first_statements.append(sql)
            if sql.lstrip().upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE"))
            else None
        )
        db._ensure_fulltext_evaluation_projection(conn)
        conn.set_trace_callback(None)
    first_elapsed_ms = (time.perf_counter() - first_started) * 1000
    storage_after_first = _database_storage(root)

    with db.connect() as conn:
        first_projection = _projection_snapshot(conn)
        first_plans = _migration_query_plans(conn, first_statements)

    second_statements: list[str] = []
    second_started = time.perf_counter()
    with db.connect() as conn:
        conn.set_trace_callback(
            lambda sql: second_statements.append(sql)
            if sql.lstrip().upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE"))
            else None
        )
        db._ensure_fulltext_evaluation_projection(conn)
        conn.set_trace_callback(None)
    second_elapsed_ms = (time.perf_counter() - second_started) * 1000
    storage_after_second = _database_storage(root)

    with db.connect() as conn:
        second_projection = _projection_snapshot(conn)

    quantity_consistent = (
        evaluations["rows"] == first_projection["rows"] == second_projection["rows"]
        and evaluations["distinct_papers"]
        == first_projection["distinct_papers"]
        == second_projection["distinct_papers"]
    )
    idempotent = (
        first_projection["sha256"] == second_projection["sha256"]
        and first_projection["schema_marker_count"] == second_projection["schema_marker_count"] == 1
    )
    if not quantity_consistent:
        raise AssertionError(
            "projection row count mismatch: "
            f"evaluations={evaluations}, first={first_projection}, second={second_projection}"
        )
    if not idempotent:
        raise AssertionError(
            f"second projection ensure changed derived data: first={first_projection}, second={second_projection}"
        )

    return {
        "successful_fulltext_evaluations": evaluations,
        "first": {
            **first_projection,
            "elapsed_ms": round(first_elapsed_ms, 2),
        },
        "second": {
            **second_projection,
            "elapsed_ms": round(second_elapsed_ms, 2),
        },
        "storage": {
            "before": storage_before,
            "after_first": storage_after_first,
            "after_second": storage_after_second,
            "first_delta_bytes": storage_after_first["bytes"] - storage_before["bytes"],
            "second_delta_bytes": storage_after_second["bytes"] - storage_after_first["bytes"],
        },
        "quantity_consistent": quantity_consistent,
        "idempotent": idempotent,
        "sql_plans": first_plans,
    }


def _page_count(total: int, page_size: int) -> int:
    return max(1, math.ceil(total / page_size))


def _collection_total(count: int, collection: str) -> int:
    # populate() creates one favorite per five papers.  Theme 1 is attached
    # to every synthetic paper; every paper has a successful full-text row.
    if collection == "favorites":
        return count // 5
    return count


def _html_case(
    client: Any,
    name: str,
    url: str,
    *,
    marker: bytes,
    page_size: int,
) -> tuple[str, Callable[[], bytes]]:
    def run() -> bytes:
        response = client.get(url)
        if response.status_code != 200:
            raise AssertionError((url, response.status_code, response.get_data(as_text=True)[:500]))
        payload = response.data
        if not payload:
            raise AssertionError(f"empty HTML response for {url}")
        card_count = payload.count(marker)
        if not 0 < card_count <= page_size:
            raise AssertionError(
                f"unexpected card count for {url}: {card_count}, expected 1..{page_size}"
            )
        return payload

    return name, run


def _html_cases(application: Any, count: int, *, include_memo: bool) -> list[tuple[str, Callable[[], bytes]]]:
    """Build a deliberately non-Cartesian representative HTML matrix.

    Each collection has a page-30 first-page case and a page-100 deep-page
    case.  This covers both supported page sizes and both shallow/deep access
    without repeating every size/position combination.  The synthetic data
    has 20% favorites and full theme/reviewed populations, so the cases also
    exercise different selectivity.
    """

    client = application.test_client()
    cases: list[tuple[str, Callable[[], bytes]]] = []
    collection_urls = {
        "favorites": ("/favorites", b'class="favorite-item"'),
        "reviewed": ("/reviewed-papers?decision=all", b'class="favorite-item"'),
        "theme": ("/investment-themes/1/papers", b'class="favorite-item"'),
    }
    for collection, (base_url, marker) in collection_urls.items():
        total = _collection_total(count, collection)
        deep_page = max(1, _page_count(total, 100) // 2)
        sort_by_position = {
            "favorites": {"first": "rank"},
            "reviewed": {"first": "score_desc"},
            "theme": {"deep": "title"},
        }
        for page, page_size, position in ((1, 30, "first"), (deep_page, 100, "deep")):
            separator = "&" if "?" in base_url else "?"
            sort = sort_by_position.get(collection, {}).get(position)
            sort_query = f"&sort={sort}" if sort else ""
            url = f"{base_url}{separator}page={page}&page_size={page_size}{sort_query}"
            cases.append(
                _html_case(
                    client,
                    f"{collection}/html/{position}/page{page_size}"
                    + (f"/{sort}" if sort else ""),
                    url,
                    marker=marker,
                    page_size=page_size,
                )
            )

    if include_memo:
        # The fixture puts every ``Rare`` paper at an id divisible by 100;
        # those ids are also favorites, so this is a selective page with
        # count//100 candidates. Select the deepest non-empty page at
        # page_size=100 so the 100k run exercises a real filtered offset.
        rare_page = _page_count(count // 100, 100)
        cases.append(
            _html_case(
                client,
                "memo/html/first/page30",
                "/investment-memos/new?source_mode=manual&page=1&page_size=30",
                marker=b'data-testid="memo-candidate"',
                page_size=30,
            )
        )
        cases.append(
            _html_case(
                client,
                "memo/html/rare/deep/page100",
                f"/investment-memos/new?source_mode=manual&query=Rare&page={rare_page}&page_size=100",
                marker=b'data-testid="memo-candidate"',
                page_size=100,
            )
        )
    return cases


def _json_model(call: Callable[[], Any]) -> Callable[[], bytes]:
    def run() -> bytes:
        return json.dumps(call(), ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")

    return run


def _legacy_cases() -> list[tuple[str, Callable[[], bytes]]]:
    """Return the two old full-materialization references used at 10k only."""

    from daily_coolpapers import services

    reviewed = _json_model(lambda: services.reviewed_papers_page_model(decision="all"))

    def memo() -> dict[str, Any]:
        with db.connect() as conn:
            candidates, counts = memo_db.candidate_data(
                conn,
                {"mode": "manual", "id": None},
                {},
            )
        return {"candidates": candidates, "counts": counts}

    return [
        ("legacy/reviewed model JSON (full)", reviewed),
        ("legacy/memo candidates JSON (full)", _json_model(memo)),
    ]


def _selected_cases(
    application: Any,
    count: int,
    *,
    filters: list[str] | None,
    include_memo: bool,
    include_legacy: bool,
) -> list[tuple[str, Callable[[], bytes]]]:
    cases = _html_cases(application, count, include_memo=include_memo)
    if include_legacy:
        cases.extend(_legacy_cases())
    if not filters:
        return cases
    return [
        item
        for item in cases
        if any(value.casefold() in item[0].casefold() for value in filters)
    ]


def _source_hashes() -> dict[str, str]:
    paths = [
        *sorted(Path("daily_coolpapers").glob("*.py")),
        Path("daily_coolpapers/templates/favorites.html"),
        Path("daily_coolpapers/templates/memo_new.html"),
        Path(__file__),
    ]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _environment(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "python": platform.python_version(),
        "executable": os.sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "seed": 20260905,
        "argv": os.sys.argv,
        "parameters": {**vars(args), "report": str(args.report)},
        "source_sha256": _source_hashes(),
    }


def _write_markdown(path: Path, environment: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    lines = [
        "# S3 性能对照",
        "",
        "本报告只测量临时 SQLite 数据库。旧 `model JSON (full)` 是全量物化参考，",
        "与新 HTML 页面响应时间不作一一等价比较；10 万规模只运行新 HTML 代表用例。",
        "",
        "## Environment",
        "",
        "```json",
        json.dumps(environment, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    by_size: dict[int, list[dict[str, Any]]] = {}
    migration_by_size: dict[int, dict[str, Any]] = {}
    for run in runs:
        by_size.setdefault(int(run["size"]), []).extend(run["results"])
        migration_by_size[int(run["size"])] = run["projection_migration"]
    for size, results in by_size.items():
        lines.extend(
            [
                f"## {size:,} papers",
                "",
                "Projection migration (same isolated fixture): "
                f"first {migration_by_size[size]['first']['elapsed_ms']} ms, "
                f"second {migration_by_size[size]['second']['elapsed_ms']} ms; "
                f"first space Δ {migration_by_size[size]['storage']['first_delta_bytes']} bytes, "
                f"second space Δ {migration_by_size[size]['storage']['second_delta_bytes']} bytes; "
                f"rows consistent={migration_by_size[size]['quantity_consistent']}, "
                f"idempotent={migration_by_size[size]['idempotent']}.",
                "",
                "| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for result in results:
            lines.append(
                f"| {result['case']} | {result['p50_ms']} | {result['p95_ms']} | "
                f"{result['payload_bytes']} | {result['python_peak_mib']} | {result['select_count']} |"
            )
        lines.extend(
            [
                "",
                "<details><summary>Exact results and EXPLAIN QUERY PLAN</summary>",
                "",
                "```json",
                json.dumps(
                    {
                        "projection_migration": migration_by_size[size],
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "</details>",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_json(path: Path, environment: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps({"environment": environment, "runs": runs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10_000, 100_000])
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--case", action="append", help="Only cases containing this substring; repeatable")
    parser.add_argument("--report", type=Path, default=Path("S3_BASELINE.md"))
    args = parser.parse_args()
    if args.samples < 1 or args.warmup < 0 or any(size < 1 for size in args.sizes):
        parser.error("sizes/samples must be positive and warmup must be nonnegative")

    environment = _environment(args)
    runs: list[dict[str, Any]] = []
    for size in args.sizes:
        print(f"Generating {size:,} papers", flush=True)
        with isolated_s3_database() as (application, _root):
            _prepare_legacy_projection(_root)
            started = time.perf_counter()
            populate(size)
            generation_seconds = time.perf_counter() - started
            projection_migration = _measure_projection_migration(_root)
            include_legacy = size == DEFAULT_REFERENCE_SIZE
            include_memo = True
            selected = _selected_cases(
                application,
                size,
                filters=args.case,
                include_memo=include_memo,
                include_legacy=include_legacy,
            )
            results: list[dict[str, Any]] = []
            print(f"{size}: {len(selected)} selected cases", flush=True)
            for name, call in selected:
                print(f"{size}: {name}", flush=True)
                results.append(measure(name, call, args.warmup, args.samples))
            runs.append(
                {
                    "size": size,
                    "papers": size,
                    "evaluations": 3 * size,
                    "favorites": size // 5,
                    "generation_seconds": round(generation_seconds, 3),
                    "projection_migration": projection_migration,
                    "results": results,
                }
            )

    if args.report.suffix.casefold() == ".json":
        _write_json(args.report, environment, runs)
    else:
        _write_markdown(args.report, environment, runs)
    print(f"Wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
