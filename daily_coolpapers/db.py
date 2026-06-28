import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH, DEFAULT_CATEGORIES, DEFAULT_SETTINGS, LLM_PROFILES_DB_PATH, ensure_directories
from .default_prompts import DEFAULT_ABSTRACT_PROMPT, DEFAULT_FULLTEXT_PROMPT


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso() -> str:
    return date.today().isoformat()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: Path | None = None) -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(path or DB_PATH, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def connect_llm_profiles(path: Path | None = None) -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(path or LLM_PROFILES_DB_PATH, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def init_db() -> None:
    try:
        _init_db_once()
    except sqlite3.OperationalError as exc:
        if "disk I/O error" not in str(exc) or not DB_PATH.exists() or DB_PATH.stat().st_size != 0:
            raise
        for suffix in ["", "-journal", "-wal", "-shm"]:
            path = Path(str(DB_PATH) + suffix)
            if path.exists():
                path.unlink()
        _init_db_once()


def init_llm_profiles_db() -> None:
    with connect_llm_profiles() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                encrypted_api_key_ref TEXT,
                custom_headers TEXT NOT NULL DEFAULT '{}',
                temperature REAL NOT NULL DEFAULT 0.2,
                max_output_tokens INTEGER NOT NULL DEFAULT 2000,
                context_window_tokens INTEGER NOT NULL DEFAULT 128000,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_default_abstract INTEGER NOT NULL DEFAULT 0,
                is_default_fulltext INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_llm_profiles_enabled_default_abstract
            ON llm_profiles(enabled, is_default_abstract);

            CREATE INDEX IF NOT EXISTS idx_llm_profiles_enabled_default_fulltext
            ON llm_profiles(enabled, is_default_fulltext);
            """
        )


def migrate_llm_profiles_from_main_db() -> None:
    with connect() as main_conn:
        row = main_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_profiles'"
        ).fetchone()
        if not row:
            return

        profiles = main_conn.execute("SELECT * FROM llm_profiles").fetchall()

    init_llm_profiles_db()

    if profiles:
        columns = [
            "id", "name", "provider", "base_url", "model",
            "encrypted_api_key_ref", "custom_headers", "temperature",
            "max_output_tokens", "context_window_tokens", "timeout_seconds",
            "enabled", "is_default_abstract", "is_default_fulltext",
            "created_at", "updated_at",
        ]
        placeholders = ",".join("?" for _ in columns)
        with connect_llm_profiles() as llm_conn:
            for profile in profiles:
                llm_conn.execute(
                    f"INSERT INTO llm_profiles({','.join(columns)}) VALUES ({placeholders})",
                    tuple(profile[col] for col in columns),
                )

    with connect() as main_conn:
        main_conn.execute("ALTER TABLE llm_profiles RENAME TO llm_profiles_legacy")


def _init_db_once() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                top_n INTEGER NOT NULL DEFAULT 30,
                sort_param TEXT NOT NULL DEFAULT 'sort=1',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arxiv_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                authors TEXT NOT NULL DEFAULT '[]',
                abstract TEXT NOT NULL DEFAULT '',
                subjects TEXT NOT NULL DEFAULT '[]',
                published_at TEXT,
                pdf_url TEXT,
                abs_url TEXT,
                papers_cool_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                crawl_date TEXT NOT NULL,
                rank INTEGER,
                reading_stars INTEGER NOT NULL DEFAULT 0,
                pdf_clicks INTEGER NOT NULL DEFAULT 0,
                kimi_clicks INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(paper_id, category, crawl_date)
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                template TEXT NOT NULL,
                llm_profile_id INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                evaluation_type TEXT NOT NULL,
                prompt_id INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
                prompt_version INTEGER,
                llm_profile_id INTEGER,
                model TEXT,
                status TEXT NOT NULL,
                result_json TEXT,
                raw_output TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT,
                progress_details_json TEXT,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        ensure_schema_migrations(conn)
        ensure_indexes(conn)
        seed_defaults(conn)


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    migrations = {
        "progress_current": "ALTER TABLE jobs ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0",
        "progress_total": "ALTER TABLE jobs ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0",
        "progress_message": "ALTER TABLE jobs ADD COLUMN progress_message TEXT",
        "progress_details_json": "ALTER TABLE jobs ADD COLUMN progress_details_json TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)


def ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_paper_categories_crawl_rank
        ON paper_categories(crawl_date, category, rank);

        CREATE INDEX IF NOT EXISTS idx_paper_categories_paper_date
        ON paper_categories(paper_id, crawl_date DESC, category);

        CREATE INDEX IF NOT EXISTS idx_evaluations_latest
        ON evaluations(paper_id, evaluation_type, created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_evaluations_latest_success
        ON evaluations(paper_id, evaluation_type, status, created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_jobs_recent
        ON jobs(created_at DESC, id DESC);
        """
    )


def seed_defaults(conn: sqlite3.Connection) -> None:
    now = now_iso()
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, json.dumps(value, ensure_ascii=False), now),
        )

    for category, name in DEFAULT_CATEGORIES:
        conn.execute(
            """
            INSERT OR IGNORE INTO categories(
                category, name, enabled, top_n, sort_param, created_at, updated_at
            )
            VALUES (?, ?, 1, 30, 'sort=1', ?, ?)
            """,
            (category, name, now, now),
        )

    existing_prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    if existing_prompts == 0:
        conn.execute(
            """
            INSERT INTO prompts(
                name, type, template, llm_profile_id, version, is_default,
                enabled, created_at, updated_at
            )
            VALUES (?, 'abstract_review', ?, NULL, 1, 1, 1, ?, ?)
            """,
            ("默认摘要评估 Prompt", DEFAULT_ABSTRACT_PROMPT, now, now),
        )
        conn.execute(
            """
            INSERT INTO prompts(
                name, type, template, llm_profile_id, version, is_default,
                enabled, created_at, updated_at
            )
            VALUES (?, 'fulltext_review', ?, NULL, 1, 1, 1, ?, ?)
            """,
            ("默认全文阅读 Prompt", DEFAULT_FULLTEXT_PROMPT, now, now),
        )


def get_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), now_iso()),
        )


def get_bool_setting(key: str, default: bool = False) -> bool:
    value = get_setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def list_categories(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM categories"
    params: list[Any] = []
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY category"
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_category(category_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone())


def save_category(data: dict[str, Any]) -> int:
    now = now_iso()
    enabled = 1 if data.get("enabled") else 0
    category_id = data.get("id")
    with connect() as conn:
        if category_id:
            conn.execute(
                """
                UPDATE categories
                SET category = ?, name = ?, enabled = ?, top_n = ?, sort_param = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data["category"],
                    data["name"],
                    enabled,
                    int(data.get("top_n") or 30),
                    data.get("sort_param") or "sort=1",
                    now,
                    category_id,
                ),
            )
            return int(category_id)
        cur = conn.execute(
            """
            INSERT INTO categories(category, name, enabled, top_n, sort_param, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["category"],
                data["name"],
                enabled,
                int(data.get("top_n") or 30),
                data.get("sort_param") or "sort=1",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def upsert_paper(paper: dict[str, Any], category: str, crawl_date: str) -> int:
    return upsert_papers([paper], category, crawl_date)[0]


def upsert_papers(papers: Iterable[dict[str, Any]], category: str, crawl_date: str) -> list[int]:
    paper_items = list(papers)
    if not paper_items:
        return []
    now = now_iso()
    paper_ids: list[int] = []
    with connect() as conn:
        for paper in paper_items:
            paper_ids.append(_upsert_paper_with_conn(conn, paper, category, crawl_date, now))
    return paper_ids


def _upsert_paper_with_conn(
    conn: sqlite3.Connection,
    paper: dict[str, Any],
    category: str,
    crawl_date: str,
    now: str,
) -> int:
    authors = paper.get("authors", [])
    subjects = paper.get("subjects", [])
    conn.execute(
        """
        INSERT INTO papers(
            arxiv_id, title, authors, abstract, subjects, published_at,
            pdf_url, abs_url, papers_cool_url, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title = excluded.title,
            authors = excluded.authors,
            abstract = excluded.abstract,
            subjects = excluded.subjects,
            published_at = excluded.published_at,
            pdf_url = excluded.pdf_url,
            abs_url = excluded.abs_url,
            papers_cool_url = excluded.papers_cool_url,
            updated_at = excluded.updated_at
        """,
        (
            paper["arxiv_id"],
            paper["title"],
            json.dumps(authors, ensure_ascii=False),
            paper.get("abstract", ""),
            json.dumps(subjects, ensure_ascii=False),
            paper.get("published_at"),
            paper.get("pdf_url"),
            paper.get("abs_url"),
            paper.get("papers_cool_url"),
            now,
            now,
        ),
    )
    paper_id = conn.execute(
        "SELECT id FROM papers WHERE arxiv_id = ?",
        (paper["arxiv_id"],),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO paper_categories(
            paper_id, category, crawl_date, rank, reading_stars,
            pdf_clicks, kimi_clicks, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, category, crawl_date) DO UPDATE SET
            rank = excluded.rank,
            reading_stars = excluded.reading_stars,
            pdf_clicks = excluded.pdf_clicks,
            kimi_clicks = excluded.kimi_clicks
        """,
        (
            paper_id,
            category,
            crawl_date,
            paper.get("rank"),
            int(paper.get("reading_stars") or 0),
            int(paper.get("pdf_clicks") or 0),
            int(paper.get("kimi_clicks") or 0),
            now,
        ),
    )
    return int(paper_id)


def get_latest_crawl_date() -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(crawl_date) AS crawl_date FROM paper_categories").fetchone()
    return row["crawl_date"] if row and row["crawl_date"] else None


def get_latest_crawl_date_on_or_before(max_date: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(crawl_date) AS crawl_date FROM paper_categories WHERE crawl_date <= ?",
            (max_date,),
        ).fetchone()
    return row["crawl_date"] if row and row["crawl_date"] else None


def list_paper_rows(
    crawl_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    attention: str | None = None,
    sort: str = "rank",
) -> list[dict[str, Any]]:
    use_date_range = bool(date_from or date_to)
    crawl_date = None if use_date_range else (crawl_date or get_latest_crawl_date())
    if not crawl_date and not use_date_range:
        return []

    params: list[Any] = []
    sql = """
        SELECT
            pc.id AS paper_category_id,
            pc.category,
            pc.crawl_date,
            pc.rank,
            pc.reading_stars,
            pc.pdf_clicks,
            pc.kimi_clicks,
            p.*
        FROM paper_categories pc
        JOIN papers p ON p.id = pc.paper_id
        WHERE 1 = 1
    """
    if use_date_range:
        if date_from:
            sql += " AND pc.crawl_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND pc.crawl_date <= ?"
            params.append(date_to)
    else:
        sql += " AND pc.crawl_date = ?"
        params.append(crawl_date)

    if use_date_range:
        order_by = {
            "rank_desc": "pc.rank DESC, pc.crawl_date DESC, pc.category ASC",
            "stars": "pc.reading_stars DESC, pc.crawl_date DESC, pc.rank ASC",
            "stars_desc": "pc.reading_stars DESC, pc.crawl_date DESC, pc.rank ASC",
            "title": "p.title COLLATE NOCASE ASC, pc.crawl_date DESC",
            "updated": "p.updated_at DESC, pc.crawl_date DESC",
            "rank": "pc.crawl_date DESC, pc.category ASC, pc.rank ASC",
        }.get(sort, "pc.crawl_date DESC, pc.category ASC, pc.rank ASC")
    else:
        order_by = {
            "rank_desc": "pc.category ASC, pc.rank DESC",
            "stars": "pc.reading_stars DESC, pc.rank ASC",
            "stars_desc": "pc.reading_stars DESC, pc.rank ASC",
            "title": "p.title COLLATE NOCASE ASC",
            "updated": "p.updated_at DESC",
            "rank": "pc.category ASC, pc.rank ASC",
        }.get(sort, "pc.category ASC, pc.rank ASC")
    sql += f" ORDER BY {order_by}"

    with connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    rows = _dedupe_paper_category_rows(rows, category, sort)

    paper_ids = [int(row["id"]) for row in rows]
    latest_abstract_evals = list_latest_evaluations(paper_ids, "abstract_review")
    latest_fulltext_evals = list_latest_evaluations(paper_ids, "fulltext_review")
    latest_successful_fulltext_evals = list_latest_evaluations(
        paper_ids,
        "fulltext_review",
        success_only=True,
    )

    hydrated = []
    for row in rows:
        paper_id = int(row["id"])
        row["authors_list"] = loads_json(row.get("authors"), [])
        row["subjects_list"] = loads_json(row.get("subjects"), [])
        row["latest_abstract_eval"] = latest_abstract_evals.get(paper_id)
        row["latest_fulltext_eval"] = latest_fulltext_evals.get(paper_id)
        row["latest_successful_fulltext_eval"] = latest_successful_fulltext_evals.get(paper_id)
        if attention:
            result = row["latest_abstract_eval"] or {}
            if ((result.get("result") or {}).get("attention") or "") != attention:
                continue
        hydrated.append(row)
    return hydrated


def _dedupe_paper_category_rows(
    rows: list[dict[str, Any]],
    category: str | None = None,
    sort: str = "rank",
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["id"]), []).append(row)

    deduped: list[dict[str, Any]] = []
    for paper_rows in grouped.values():
        memberships = _category_memberships(paper_rows)
        if category and not any(item["category"] == category for item in memberships):
            continue
        representative = dict(_representative_category_row(paper_rows, sort))
        representative["category_memberships"] = memberships
        representative["category"] = ", ".join(_unique_values(item["category"] for item in memberships))
        representative["category_count"] = len(_unique_values(item["category"] for item in memberships))
        deduped.append(representative)

    _sort_deduped_paper_rows(deduped, sort)
    return deduped


def _category_memberships(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    memberships: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("crawl_date") or ""), str(item.get("category") or "")), reverse=True):
        key = (str(row.get("crawl_date") or ""), str(row.get("category") or ""))
        if key in seen:
            continue
        seen.add(key)
        memberships.append(
            {
                "category": row.get("category") or "",
                "crawl_date": row.get("crawl_date") or "",
                "rank": row.get("rank"),
                "reading_stars": int(row.get("reading_stars") or 0),
                "pdf_clicks": int(row.get("pdf_clicks") or 0),
                "kimi_clicks": int(row.get("kimi_clicks") or 0),
            }
        )
    return memberships


def _representative_category_row(rows: list[dict[str, Any]], sort: str) -> dict[str, Any]:
    if sort in {"stars", "stars_desc"}:
        return max(
            rows,
            key=lambda row: (
                int(row.get("reading_stars") or 0),
                _date_sort_value(row.get("crawl_date")),
                -_rank_or_large(row),
            ),
        )
    if sort == "rank_desc":
        return max(
            rows,
            key=lambda row: (
                int(row.get("rank") or 0),
                _date_sort_value(row.get("crawl_date")),
                int(row.get("reading_stars") or 0),
            ),
        )
    if sort == "updated":
        return max(rows, key=lambda row: str(row.get("updated_at") or ""))
    return min(
        rows,
        key=lambda row: (
            _rank_or_large(row),
            str(row.get("category") or ""),
            -_date_sort_value(row.get("crawl_date")),
        ),
    )


def _sort_deduped_paper_rows(rows: list[dict[str, Any]], sort: str) -> None:
    if sort in {"stars", "stars_desc"}:
        rows.sort(
            key=lambda row: (
                int(row.get("reading_stars") or 0),
                _date_sort_value(row.get("crawl_date")),
                -_rank_or_large(row),
            ),
            reverse=True,
        )
    elif sort == "rank_desc":
        rows.sort(
            key=lambda row: (
                int(row.get("rank") or 0),
                _date_sort_value(row.get("crawl_date")),
                int(row.get("reading_stars") or 0),
            ),
            reverse=True,
        )
    elif sort == "title":
        rows.sort(key=lambda row: str(row.get("title") or "").lower())
    elif sort == "updated":
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    else:
        rows.sort(
            key=lambda row: (
                -_date_sort_value(row.get("crawl_date")),
                str(row.get("category") or ""),
                _rank_or_large(row),
            )
        )


def _rank_or_large(row: dict[str, Any]) -> int:
    try:
        return int(row.get("rank"))
    except (TypeError, ValueError):
        return 999999


def _date_sort_value(value: Any) -> int:
    try:
        return int(str(value or "").replace("-", ""))
    except ValueError:
        return 0


def _unique_values(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def _unique_ints(values: Iterable[Any]) -> list[int]:
    seen: set[int] = set()
    unique: list[int] = []
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _chunks(items: list[int], size: int = 500) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def list_fulltext_reviewed_papers(sort: str = "evaluated_desc") -> list[dict[str, Any]]:
    sql = """
        WITH latest_fulltext AS (
            SELECT *
            FROM (
                SELECT
                    e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.paper_id
                        ORDER BY e.created_at DESC, e.id DESC
                    ) AS rn
                FROM evaluations e
                WHERE e.evaluation_type = 'fulltext_review'
                  AND e.status = 'success'
            )
            WHERE rn = 1
        )
        SELECT
            p.*,
            e.id AS fulltext_evaluation_id,
            e.created_at AS fulltext_evaluated_at,
            e.model AS fulltext_model,
            e.result_json AS fulltext_result_json
        FROM papers p
        JOIN latest_fulltext e ON e.paper_id = p.id
        ORDER BY e.created_at DESC, e.id DESC
    """
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(sql).fetchall()]

    categories_by_paper = list_paper_categories_for_papers(int(row["id"]) for row in rows)
    hydrated = []
    for row in rows:
        paper_id = int(row["id"])
        row["authors_list"] = loads_json(row.get("authors"), [])
        row["subjects_list"] = loads_json(row.get("subjects"), [])
        row["fulltext_result"] = loads_json(row.get("fulltext_result_json"), {})
        row["categories"] = categories_by_paper.get(paper_id, [])
        row["latest_category"] = row["categories"][0] if row["categories"] else {}
        hydrated.append(row)

    if sort == "score_desc":
        hydrated.sort(key=lambda row: (_score_value(row), row.get("fulltext_evaluated_at") or ""), reverse=True)
    elif sort == "title":
        hydrated.sort(key=lambda row: str(row.get("title") or "").lower())
    elif sort == "rank":
        hydrated.sort(
            key=lambda row: (
                str((row.get("latest_category") or {}).get("category") or ""),
                _rank_value(row),
            )
        )
    return hydrated


def _score_value(row: dict[str, Any]) -> int:
    try:
        return int((row.get("fulltext_result") or {}).get("score"))
    except (TypeError, ValueError):
        return -1


def _rank_value(row: dict[str, Any]) -> int:
    try:
        return int((row.get("latest_category") or {}).get("rank"))
    except (TypeError, ValueError):
        return 999999


def get_paper(paper_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        paper = row_to_dict(conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone())
    if paper:
        paper["authors_list"] = loads_json(paper.get("authors"), [])
        paper["subjects_list"] = loads_json(paper.get("subjects"), [])
    return paper


def get_paper_categories(paper_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM paper_categories WHERE paper_id = ? ORDER BY crawl_date DESC, category",
                (paper_id,),
            ).fetchall()
        ]


def list_paper_categories_for_papers(paper_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = _unique_ints(paper_ids)
    if not ids:
        return {}

    categories = {paper_id: [] for paper_id in ids}
    with connect() as conn:
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT *
                FROM paper_categories
                WHERE paper_id IN ({placeholders})
                ORDER BY paper_id, crawl_date DESC, category
                """,
                chunk,
            ).fetchall()
            for row in rows:
                categories.setdefault(int(row["paper_id"]), []).append(dict(row))
    return categories


def list_papers_missing_evaluation(eval_type: str, limit: int = 200) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.id
            FROM papers p
            JOIN paper_categories pc ON pc.paper_id = p.id
            WHERE pc.crawl_date = COALESCE((SELECT MAX(crawl_date) FROM paper_categories), pc.crawl_date)
              AND NOT EXISTS (
                  SELECT 1 FROM evaluations e
                  WHERE e.paper_id = p.id AND e.evaluation_type = ? AND e.status = 'success'
              )
            ORDER BY pc.category, pc.rank
            LIMIT ?
            """,
            (eval_type, limit),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def list_llm_profiles(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM llm_profiles"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY enabled DESC, name"
    with connect_llm_profiles() as conn:
        return [dict(row) for row in conn.execute(sql).fetchall()]


def get_llm_profile(profile_id: int | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    with connect_llm_profiles() as conn:
        return row_to_dict(conn.execute("SELECT * FROM llm_profiles WHERE id = ?", (profile_id,)).fetchone())


def get_default_llm_profile(eval_type: str) -> dict[str, Any] | None:
    flag = "is_default_fulltext" if eval_type == "fulltext_review" else "is_default_abstract"
    with connect_llm_profiles() as conn:
        row = conn.execute(
            f"SELECT * FROM llm_profiles WHERE enabled = 1 AND {flag} = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM llm_profiles WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    return row_to_dict(row)


def save_llm_profile(data: dict[str, Any]) -> int:
    now = now_iso()
    profile_id = data.get("id")
    enabled = 1 if data.get("enabled") else 0
    default_abstract = 1 if data.get("is_default_abstract") else 0
    default_fulltext = 1 if data.get("is_default_fulltext") else 0
    with connect_llm_profiles() as conn:
        if default_abstract:
            conn.execute("UPDATE llm_profiles SET is_default_abstract = 0")
        if default_fulltext:
            conn.execute("UPDATE llm_profiles SET is_default_fulltext = 0")

        values = (
            data["name"],
            data["provider"],
            data["base_url"],
            data["model"],
            data.get("encrypted_api_key_ref"),
            data.get("custom_headers") or "{}",
            float(data.get("temperature") or 0.2),
            int(data.get("max_output_tokens") or 2000),
            int(data.get("context_window_tokens") or 128000),
            int(data.get("timeout_seconds") or 120),
            enabled,
            default_abstract,
            default_fulltext,
            now,
        )
        if profile_id:
            conn.execute(
                """
                UPDATE llm_profiles
                SET name = ?, provider = ?, base_url = ?, model = ?,
                    encrypted_api_key_ref = COALESCE(?, encrypted_api_key_ref),
                    custom_headers = ?, temperature = ?, max_output_tokens = ?,
                    context_window_tokens = ?, timeout_seconds = ?, enabled = ?,
                    is_default_abstract = ?, is_default_fulltext = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, profile_id),
            )
            return int(profile_id)
        cur = conn.execute(
            """
            INSERT INTO llm_profiles(
                name, provider, base_url, model, encrypted_api_key_ref, custom_headers,
                temperature, max_output_tokens, context_window_tokens, timeout_seconds,
                enabled, is_default_abstract, is_default_fulltext, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["provider"],
                data["base_url"],
                data["model"],
                data.get("encrypted_api_key_ref"),
                data.get("custom_headers") or "{}",
                float(data.get("temperature") or 0.2),
                int(data.get("max_output_tokens") or 2000),
                int(data.get("context_window_tokens") or 128000),
                int(data.get("timeout_seconds") or 120),
                enabled,
                default_abstract,
                default_fulltext,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def list_prompts(prompt_type: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM prompts WHERE 1=1"
    params: list[Any] = []
    if prompt_type:
        sql += " AND type = ?"
        params.append(prompt_type)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY type, is_default DESC, name"
    with connect() as conn:
        prompts = [dict(row) for row in conn.execute(sql, params).fetchall()]

    profile_ids = _unique_ints(p.get("llm_profile_id") for p in prompts if p.get("llm_profile_id"))
    profiles: dict[int, sqlite3.Row] = {}
    if profile_ids:
        with connect_llm_profiles() as conn:
            for chunk in _chunks(profile_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT id, name, model FROM llm_profiles WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                profiles.update({row["id"]: row for row in rows})

    for prompt in prompts:
        profile = profiles.get(prompt.get("llm_profile_id"))
        prompt["llm_profile_name"] = profile["name"] if profile else None
        prompt["llm_model"] = profile["model"] if profile else None
    return prompts


def get_prompt(prompt_id: int | None) -> dict[str, Any] | None:
    if not prompt_id:
        return None
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone())


def get_default_prompt(prompt_type: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM prompts
            WHERE type = ? AND enabled = 1 AND is_default = 1
            ORDER BY id LIMIT 1
            """,
            (prompt_type,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM prompts WHERE type = ? AND enabled = 1 ORDER BY id LIMIT 1",
                (prompt_type,),
            ).fetchone()
    return row_to_dict(row)


def save_prompt(data: dict[str, Any]) -> int:
    now = now_iso()
    prompt_id = data.get("id")
    is_default = 1 if data.get("is_default") else 0
    enabled = 1 if data.get("enabled") else 0
    llm_profile_id = data.get("llm_profile_id") or None
    with connect() as conn:
        if is_default:
            conn.execute("UPDATE prompts SET is_default = 0 WHERE type = ?", (data["type"],))
        if prompt_id:
            current = conn.execute("SELECT version FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
            next_version = int(current["version"]) + 1 if current else 1
            conn.execute(
                """
                UPDATE prompts
                SET name = ?, type = ?, template = ?, llm_profile_id = ?,
                    version = ?, is_default = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data["name"],
                    data["type"],
                    data["template"],
                    llm_profile_id,
                    next_version,
                    is_default,
                    enabled,
                    now,
                    prompt_id,
                ),
            )
            return int(prompt_id)
        cur = conn.execute(
            """
            INSERT INTO prompts(
                name, type, template, llm_profile_id, version, is_default,
                enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["type"],
                data["template"],
                llm_profile_id,
                is_default,
                enabled,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_prompt_llm_profile(prompt_id: int, llm_profile_id: int | None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE prompts
            SET llm_profile_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (llm_profile_id, now_iso(), prompt_id),
        )


def create_evaluation(
    paper_id: int,
    evaluation_type: str,
    prompt_id: int | None,
    prompt_version: int | None,
    llm_profile_id: int | None,
    model: str | None,
    status: str,
    result: dict[str, Any] | None,
    raw_output: str | None,
    error_message: str | None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO evaluations(
                paper_id, evaluation_type, prompt_id, prompt_version, llm_profile_id,
                model, status, result_json, raw_output, error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                evaluation_type,
                prompt_id,
                prompt_version,
                llm_profile_id,
                model,
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                raw_output,
                error_message,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def _hydrate_evaluation_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    item = row_to_dict(row) if isinstance(row, sqlite3.Row) else (dict(row) if row else None)
    if item:
        item.pop("rn", None)
        item["result"] = loads_json(item.get("result_json"), {})
    return item


def list_latest_evaluations(
    paper_ids: Iterable[int],
    evaluation_type: str,
    success_only: bool = False,
) -> dict[int, dict[str, Any]]:
    ids = _unique_ints(paper_ids)
    if not ids:
        return {}

    latest: dict[int, dict[str, Any]] = {}
    status_filter = "AND e.status = 'success'" if success_only else ""
    with connect() as conn:
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT
                        e.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY e.paper_id
                            ORDER BY e.created_at DESC, e.id DESC
                        ) AS rn
                    FROM evaluations e
                    WHERE e.paper_id IN ({placeholders})
                      AND e.evaluation_type = ?
                      {status_filter}
                )
                WHERE rn = 1
                """,
                [*chunk, evaluation_type],
            ).fetchall()
            for row in rows:
                item = _hydrate_evaluation_row(row)
                if item:
                    latest[int(item["paper_id"])] = item
    return latest


def get_latest_evaluation(paper_id: int, evaluation_type: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM evaluations
            WHERE paper_id = ? AND evaluation_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (paper_id, evaluation_type),
        ).fetchone()
    return _hydrate_evaluation_row(row)


def get_latest_successful_evaluation(paper_id: int, evaluation_type: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM evaluations
            WHERE paper_id = ? AND evaluation_type = ? AND status = 'success'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (paper_id, evaluation_type),
        ).fetchone()
    return _hydrate_evaluation_row(row)


def list_evaluations(paper_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.*, p.name AS prompt_name
            FROM evaluations e
            LEFT JOIN prompts p ON p.id = e.prompt_id
            WHERE e.paper_id = ?
            ORDER BY e.created_at DESC, e.id DESC
            """,
            (paper_id,),
        ).fetchall()
    items = [dict(row) for row in rows]

    profile_ids = _unique_ints(item.get("llm_profile_id") for item in items if item.get("llm_profile_id"))
    profiles: dict[int, sqlite3.Row] = {}
    if profile_ids:
        with connect_llm_profiles() as conn:
            for chunk in _chunks(profile_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT id, name FROM llm_profiles WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                profiles.update({row["id"]: row for row in rows})

    for item in items:
        item["result"] = loads_json(item.get("result_json"), {})
        profile = profiles.get(item.get("llm_profile_id"))
        item["llm_profile_name"] = profile["name"] if profile else None
    return items


def create_job(job_type: str, payload: dict[str, Any] | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs(type, status, payload, created_at)
            VALUES (?, 'pending', ?, ?)
            """,
            (job_type, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
        )
        return int(cur.lastrowid)


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    item = row_to_dict(row)
    if item:
        item["payload_data"] = loads_json(item.get("payload"), {})
        hydrate_job_progress(item)
    return item


def update_job(job_id: int, status: str, error_message: str | None = None) -> None:
    fields = ["status = ?"]
    params: list[Any] = [status]
    if status == "running":
        fields.append("started_at = ?")
        params.append(now_iso())
    if status in {"success", "failed"}:
        fields.append("finished_at = ?")
        params.append(now_iso())
    if status == "success":
        fields.append(
            """
            progress_current = CASE
                WHEN progress_total > 0 THEN progress_total
                ELSE progress_current
            END
            """
        )
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    params.append(job_id)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", params)


def update_job_progress(
    job_id: int,
    current: int,
    total: int,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    current = max(0, int(current))
    total = max(0, int(total))
    if total and current > total:
        current = total
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET progress_current = ?,
                progress_total = ?,
                progress_message = ?,
                progress_details_json = ?
            WHERE id = ?
            """,
            (
                current,
                total,
                message,
                json.dumps(details, ensure_ascii=False) if details else None,
                job_id,
            ),
        )


def mark_pending_jobs_interrupted_except(active_job_ids: set[int], message: str) -> int:
    params: list[Any] = [message, now_iso()]
    sql = """
        UPDATE jobs
        SET status = 'failed',
            error_message = ?,
            finished_at = ?
        WHERE status = 'pending'
    """
    if active_job_ids:
        placeholders = ",".join("?" for _ in active_job_ids)
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(sorted(active_job_ids))
    with connect() as conn:
        cur = conn.execute(sql, params)
        return int(cur.rowcount or 0)


def mark_unfinished_jobs_interrupted() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error_message = COALESCE(error_message, '服务重启，任务中断'),
                finished_at = COALESCE(finished_at, ?)
            WHERE status IN ('pending', 'running')
            """,
            (now_iso(),),
        )


def hydrate_job_progress(job: dict[str, Any]) -> dict[str, Any]:
    current = int(job.get("progress_current") or 0)
    total = int(job.get("progress_total") or 0)
    percent = int(round((current / total) * 100)) if total else 0
    job["progress_current"] = current
    job["progress_total"] = total
    job["progress_percent"] = max(0, min(100, percent))
    job["progress_message"] = job.get("progress_message") or ""
    job["progress_details"] = loads_json(job.get("progress_details_json"), {})
    return job


def list_jobs(limit: int = 80) -> list[dict[str, Any]]:
    with connect() as conn:
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    return [hydrate_job_progress(job) for job in jobs]
