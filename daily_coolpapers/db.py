import json
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .config import DB_PATH, DEFAULT_CATEGORIES, DEFAULT_SETTINGS, LLM_PROFILES_DB_PATH, ensure_directories
from .default_prompts import DEFAULT_ABSTRACT_PROMPT, DEFAULT_FULLTEXT_PROMPT, DEFAULT_CLASSIFICATION_PROMPT, DEFAULT_MEMO_PROMPT
from .form_commands import (InvestmentThemeCommand, FormValidationError, parse_theme_ids, parse_choice,
                            ResearchEntityCommand, TeamTrackingCommand, normalized_research_name,
                            AUTHOR_CATEGORIES, ORGANIZATION_TYPES, parse_int, research_text)


DAILY_PIPELINE_JOB_TYPE = "daily_pipeline"
CRAWL_JOB_TYPES = (DAILY_PIPELINE_JOB_TYPE, "crawl", "crawl_catch_up")
PIPELINE_TRIGGER_SOURCES = frozenset({"scheduled", "manual_latest", "manual_catch_up"})
JOB_STATUSES = frozenset(
    {"pending", "running", "success", "partial_success", "failed", "interrupted"}
)
JOB_ACTIVE_STATUSES = frozenset({"pending", "running"})
JOB_TERMINAL_STATUSES = JOB_STATUSES - JOB_ACTIVE_STATUSES
JOB_EVENT_LEVELS = frozenset({"info", "warning", "error"})
PAPER_DECISIONS = frozenset({'favorite', 'skipped', 'clear'})
PAPER_DECISION_FILTERS = frozenset({'all', 'undecided', 'favorite', 'skipped'})
JOB_STATUS_TRANSITIONS = {
    "pending": frozenset({"running", "failed", "interrupted"}),
    "running": frozenset({"success", "partial_success", "failed", "interrupted"}),
}


@dataclass(frozen=True)
class PaperUpsertResult:
    paper_ids: list[int]
    parsed_count: int
    unique_count: int
    persisted_count: int
    new_count: int
    updated_count: int
    duplicate_count: int
    membership_new_count: int
    membership_updated_count: int
    failed_count: int = 0
    new_paper_ids: list[int] = field(default_factory=list)

    def metrics(self) -> dict[str, int]:
        return {
            "persist_input_count": self.parsed_count,
            "unique_count": self.unique_count,
            "persisted_count": self.persisted_count,
            "new_count": self.new_count,
            "updated_count": self.updated_count,
            "duplicate_count": self.duplicate_count,
            "membership_new_count": self.membership_new_count,
            "membership_updated_count": self.membership_updated_count,
            "failed_count": self.failed_count,
        }


class PaperNotFoundError(LookupError):
    pass


class FulltextRequiredError(ValueError):
    pass


class InvestmentThemeNotFoundError(LookupError):
    pass


class ArchivedThemeError(ValueError):
    pass


class DirectionNotFoundError(LookupError):
    pass


class DirectionConflictError(ValueError):
    pass


class ResearchEntityNotFoundError(LookupError):
    pass


class ResearchEntityConflictError(ValueError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__('作者或机构已存在或已归档，请核对后明确复用或先恢复；本次未保存任何改动')


RESEARCH_ENTITY_TABLES = {'author': 'research_authors', 'organization': 'research_organizations'}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso() -> str:
    return date.today().isoformat()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def connect_readonly(path: Path | None = None) -> sqlite3.Connection:
    target = (path or DB_PATH).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"数据库不存在，拒绝创建只读审计目标: {target}")
    uri = f"{target.as_uri()}?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=30,
        factory=ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def connect_llm_profiles(path: Path | None = None) -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(path or LLM_PROFILES_DB_PATH, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _configure_journal_mode(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode = DELETE")


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
        _configure_journal_mode(conn)
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


        columns = {row['name'] for row in conn.execute('PRAGMA table_info(llm_profiles)')}
        if 'is_default_classification' not in columns:
            conn.execute('ALTER TABLE llm_profiles ADD COLUMN is_default_classification INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_profiles_classification ON llm_profiles(enabled, is_default_classification)')
        if 'is_default_memo' not in columns:
            conn.execute('ALTER TABLE llm_profiles ADD COLUMN is_default_memo INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_profiles_memo ON llm_profiles(enabled,is_default_memo)')


def migrate_llm_profiles_from_main_db() -> None:
    init_llm_profiles_db()
    with connect() as main_conn:
        main_conn.execute("BEGIN IMMEDIATE")
        source_exists = _table_exists(main_conn, "llm_profiles")
        legacy_exists = _table_exists(main_conn, "llm_profiles_legacy")
        if not source_exists:
            return
        if legacy_exists:
            raise RuntimeError("LLM Profile 迁移冲突：源表和 legacy 表同时存在")

        columns = [
            "id", "name", "provider", "base_url", "model",
            "encrypted_api_key_ref", "custom_headers", "temperature",
            "max_output_tokens", "context_window_tokens", "timeout_seconds",
            "enabled", "is_default_abstract", "is_default_fulltext",
            "created_at", "updated_at",
        ]
        profiles = main_conn.execute(
            f"SELECT {','.join(columns)} FROM llm_profiles ORDER BY id"
        ).fetchall()
        if profiles:
            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "id")
            expected = {
                int(profile["id"]): tuple(profile[column] for column in columns)
                for profile in profiles
            }
            with connect_llm_profiles() as llm_conn:
                llm_conn.executemany(
                    f"""
                    INSERT INTO llm_profiles({','.join(columns)}) VALUES ({placeholders})
                    ON CONFLICT(id) DO UPDATE SET {updates}
                    """,
                    [tuple(profile[column] for column in columns) for profile in profiles],
                )
                migrated = {
                    int(row["id"]): tuple(row[column] for column in columns)
                    for row in llm_conn.execute(
                        f"SELECT {','.join(columns)} FROM llm_profiles",
                    ).fetchall()
                    if int(row["id"]) in expected
                }
            if migrated != expected:
                raise RuntimeError("LLM Profile 迁移校验失败，源表保持不变")
        _archive_legacy_llm_profiles(main_conn)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _archive_legacy_llm_profiles(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE llm_profiles RENAME TO llm_profiles_legacy")


def _init_db_once() -> None:
    with connect() as conn:
        _configure_journal_mode(conn)
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

            CREATE TABLE IF NOT EXISTS paper_dispositions (
                paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
                decision TEXT NOT NULL CHECK(decision IN ('favorite', 'skipped')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS investment_themes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
                normalized_name TEXT NOT NULL UNIQUE CHECK(length(normalized_name) > 0),
                description TEXT NOT NULL DEFAULT '' CHECK(length(description) <= 500),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_investment_themes (
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                theme_id INTEGER NOT NULL REFERENCES investment_themes(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(paper_id, theme_id)
            );

            CREATE TABLE IF NOT EXISTS research_authors (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK(length(name) > 0),
                normalized_name TEXT NOT NULL UNIQUE CHECK(length(normalized_name) > 0),
                author_category TEXT NOT NULL DEFAULT 'unknown' CHECK(author_category IN ('academic','industry','hybrid','unknown')),
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_organizations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK(length(name) > 0),
                normalized_name TEXT NOT NULL UNIQUE CHECK(length(normalized_name) > 0),
                organization_type TEXT NOT NULL CHECK(organization_type IN ('university','research_institute','company','other')),
                region TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_team_tracking (
                id INTEGER PRIMARY KEY,
                paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
                lead_author_id INTEGER NOT NULL REFERENCES research_authors(id) ON DELETE RESTRICT,
                organization_id INTEGER NOT NULL REFERENCES research_organizations(id) ON DELETE RESTRICT,
                status TEXT NOT NULL DEFAULT 'tracking' CHECK(status IN ('tracking','archived')),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_paper_team_tracking_status_updated ON paper_team_tracking(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_paper_team_tracking_author ON paper_team_tracking(lead_author_id);
            CREATE INDEX IF NOT EXISTS idx_paper_team_tracking_organization ON paper_team_tracking(organization_id);

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
                pipeline_job_id INTEGER REFERENCES jobs(id),
                evaluation_type TEXT NOT NULL,
                prompt_id INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
                prompt_version INTEGER,
                llm_profile_id INTEGER,
                model TEXT,
                status TEXT NOT NULL,
                result_json TEXT,
                raw_output TEXT,
                error_message TEXT,
                error_code TEXT,
                error_retryable INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT,
                retry_of_job_id INTEGER REFERENCES jobs(id),
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

            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                stage TEXT NOT NULL,
                event_type TEXT NOT NULL,
                level TEXT NOT NULL CHECK(level IN ('info', 'warning', 'error')),
                category TEXT,
                crawl_date TEXT,
                paper_id INTEGER REFERENCES papers(id),
                arxiv_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evaluation_claims (
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                evaluation_type TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                job_id INTEGER REFERENCES jobs(id),
                pipeline_job_id INTEGER REFERENCES jobs(id),
                provider_started INTEGER NOT NULL DEFAULT 0,
                last_evaluation_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(paper_id, evaluation_type)
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
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS attention_directions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, normalized_name TEXT NOT NULL, scope_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
            created_at TEXT NOT NULL, status_updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_active_direction_name
            ON attention_directions(normalized_name) WHERE status='active';
        CREATE TABLE IF NOT EXISTS paper_direction_results (
            paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            direction_id INTEGER NOT NULL REFERENCES attention_directions(id) ON DELETE RESTRICT,
            model_decision TEXT CHECK(model_decision IN ('matched','possible','unmatched','failed')),
            model_reason TEXT NOT NULL DEFAULT '',
            classification_evaluation_id INTEGER REFERENCES evaluations(id),
            classification_source TEXT CHECK(classification_source IN ('daily','historical_backfill')),
            manual_decision TEXT CHECK(manual_decision IN ('confirmed','rejected')),
            manual_updated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(paper_id,direction_id)
        );
        CREATE INDEX IF NOT EXISTS idx_direction_model ON paper_direction_results(direction_id,model_decision);
        CREATE INDEX IF NOT EXISTS idx_direction_paper ON paper_direction_results(paper_id);
        CREATE INDEX IF NOT EXISTS idx_direction_manual ON paper_direction_results(manual_decision);
        CREATE TABLE IF NOT EXISTS classification_claims (
            paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE, job_id INTEGER NOT NULL REFERENCES jobs(id),
            created_at TEXT NOT NULL
        );
    ''')
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    job_migrations = {
        "idempotency_key": "ALTER TABLE jobs ADD COLUMN idempotency_key TEXT",
        "retry_of_job_id": (
            "ALTER TABLE jobs ADD COLUMN retry_of_job_id INTEGER REFERENCES jobs(id)"
        ),
        "progress_current": "ALTER TABLE jobs ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0",
        "progress_total": "ALTER TABLE jobs ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0",
        "progress_message": "ALTER TABLE jobs ADD COLUMN progress_message TEXT",
        "progress_details_json": "ALTER TABLE jobs ADD COLUMN progress_details_json TEXT",
    }
    for column, sql in job_migrations.items():
        if column not in job_columns:
            conn.execute(sql)
    evaluation_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()
    }
    evaluation_migrations = {
        **{column: f'ALTER TABLE evaluations ADD COLUMN {column} TEXT' for column in
           ('classification_source', 'direction_snapshot_json', 'input_snapshot_json',
            'input_fingerprint', 'config_snapshot_json')},
        'attempt': 'ALTER TABLE evaluations ADD COLUMN attempt INTEGER',
        "pipeline_job_id": (
            "ALTER TABLE evaluations ADD COLUMN pipeline_job_id INTEGER REFERENCES jobs(id)"
        ),
        "error_code": "ALTER TABLE evaluations ADD COLUMN error_code TEXT",
        "error_retryable": (
            "ALTER TABLE evaluations ADD COLUMN error_retryable INTEGER NOT NULL DEFAULT 0"
        ),
    }
    for column, sql in evaluation_migrations.items():
        if column not in evaluation_columns:
            conn.execute(sql)
    from .memo_db import init_schema as init_memo_schema
    init_memo_schema(conn)


def ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_paper_categories_crawl_rank
        ON paper_categories(crawl_date, category, rank);

        CREATE INDEX IF NOT EXISTS idx_paper_categories_paper_date
        ON paper_categories(paper_id, crawl_date DESC, category);

        CREATE INDEX IF NOT EXISTS idx_paper_dispositions_decision_updated
        ON paper_dispositions(decision, updated_at);

        CREATE INDEX IF NOT EXISTS idx_paper_investment_themes_theme_created
        ON paper_investment_themes(theme_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_paper_investment_themes_paper
        ON paper_investment_themes(paper_id);

        CREATE INDEX IF NOT EXISTS idx_evaluations_latest
        ON evaluations(paper_id, evaluation_type, created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_evaluations_latest_success
        ON evaluations(paper_id, evaluation_type, status, created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_evaluations_pipeline_job
        ON evaluations(pipeline_job_id, id);

        CREATE INDEX IF NOT EXISTS idx_jobs_recent
        ON jobs(created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_jobs_status_recent
        ON jobs(status, created_at DESC, id DESC);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key
        ON jobs(idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_jobs_retry_of
        ON jobs(retry_of_job_id, created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_job_events_job_timeline
        ON job_events(job_id, id);

        CREATE INDEX IF NOT EXISTS idx_job_events_created_at
        ON job_events(created_at);

        CREATE INDEX IF NOT EXISTS idx_job_events_unit_filter
        ON job_events(job_id, crawl_date, category, stage, id);

        CREATE INDEX IF NOT EXISTS idx_job_events_job_level
        ON job_events(job_id, level, id);

        CREATE INDEX IF NOT EXISTS idx_job_events_job_stage
        ON job_events(job_id, stage, id);

        CREATE INDEX IF NOT EXISTS idx_job_events_job_stage_type
        ON job_events(job_id, stage, event_type, id);
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

    existing_prompts = conn.execute("SELECT COUNT(*) FROM prompts WHERE type != 'direction_classification'").fetchone()[0]
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


    if not conn.execute("SELECT 1 FROM prompts WHERE type='direction_classification'").fetchone():
        conn.execute('''INSERT INTO prompts(name,type,template,version,is_default,enabled,created_at,updated_at)
            VALUES (?, 'direction_classification', ?, 1, 1, 1, ?, ?)''',
                     ('默认关注方向分类 Prompt', DEFAULT_CLASSIFICATION_PROMPT, now, now))


    if not conn.execute("SELECT 1 FROM prompts WHERE type='investment_memo'").fetchone():
        conn.execute('''INSERT INTO prompts(name,type,template,version,is_default,enabled,created_at,updated_at)
            VALUES (?, 'investment_memo', ?, 1, 1, 1, ?, ?)''',
                     ('默认研究备忘录 Prompt', DEFAULT_MEMO_PROMPT, now, now))


def list_attention_directions(active_only: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM attention_directions" + (" WHERE status='active'" if active_only else '') +
            ' ORDER BY id')]


def require_direction(conn, direction_id: int, *, active: bool = False) -> dict:
    row = conn.execute('SELECT * FROM attention_directions WHERE id=?', (direction_id,)).fetchone() if 0 < direction_id <= 2**63-1 else None
    if row is None:
        raise DirectionNotFoundError('关注方向不存在')
    if active and row['status'] != 'active':
        raise DirectionConflictError('该关注方向已归档，不可恢复或用于新的分类')
    return dict(row)


def create_attention_direction(name: str, scope_text: str) -> int:
    name = research_text(name, 'name', required=True)
    scope_text = research_text(scope_text, 'scope_text', required=True)
    normalized = normalized_research_name(name)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM attention_directions WHERE normalized_name=? AND status='active'", (normalized,)).fetchone():
            raise DirectionConflictError('未归档方向中已存在同名定义，请直接使用或先归档旧方向')
        now = now_iso()
        return int(conn.execute('''INSERT INTO attention_directions
            (name,normalized_name,scope_text,created_at,status_updated_at) VALUES (?,?,?,?,?)''',
            (name, normalized, scope_text, now, now)).lastrowid)


def archive_attention_direction(direction_id: int) -> None:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        direction = require_direction(conn, direction_id)
        if direction['status'] == 'active':
            conn.execute("UPDATE attention_directions SET status='archived', status_updated_at=? WHERE id=?", (now_iso(), direction_id))


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
        _set_setting_with_conn(conn, key, value, now_iso())


def save_settings(values: dict[str, Any]) -> None:
    now = now_iso()
    with connect() as conn:
        for key, value in values.items():
            _set_setting_with_conn(conn, key, value, now)


def _set_setting_with_conn(
    conn: sqlite3.Connection,
    key: str,
    value: Any,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), updated_at),
    )


def get_settings(defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    if not defaults:
        return result
    placeholders = ",".join("?" for _ in defaults)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            list(defaults),
        ).fetchall()
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            result[row["key"]] = row["value"]
    return result


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
    return upsert_papers_with_stats(papers, category, crawl_date).paper_ids


def upsert_papers_with_stats(
    papers: Iterable[dict[str, Any]],
    category: str,
    crawl_date: str,
) -> PaperUpsertResult:
    paper_items = list(papers)
    if not paper_items:
        return PaperUpsertResult([], 0, 0, 0, 0, 0, 0, 0, 0)
    now = now_iso()
    # Serialize every item before opening a transaction so invalid input cannot
    # leave a partially written batch. The last occurrence remains canonical.
    serialized_by_arxiv: dict[str, tuple[Any, ...]] = {}
    paper_by_arxiv: dict[str, dict[str, Any]] = {}
    input_arxiv_ids: list[str] = []
    for paper in paper_items:
        arxiv_id = str(paper["arxiv_id"])
        input_arxiv_ids.append(arxiv_id)
        serialized_by_arxiv[arxiv_id] = _paper_upsert_values(paper, now)
        paper_by_arxiv[arxiv_id] = paper
    arxiv_ids = list(paper_by_arxiv)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_metadata: dict[str, tuple[Any, ...]] = {}
        for chunk_start in range(0, len(arxiv_ids), 500):
            chunk = arxiv_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT arxiv_id, title, authors, abstract, subjects, published_at,
                       pdf_url, abs_url, papers_cool_url
                FROM papers WHERE arxiv_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            existing_metadata.update(
                {
                    str(row["arxiv_id"]): (
                        row["title"],
                        row["authors"],
                        row["abstract"],
                        row["subjects"],
                        row["published_at"],
                        row["pdf_url"],
                        row["abs_url"],
                        row["papers_cool_url"],
                    )
                    for row in rows
                }
            )

        conn.executemany(
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
            [serialized_by_arxiv[arxiv_id] for arxiv_id in arxiv_ids],
        )
        ids_by_arxiv: dict[str, int] = {}
        for chunk_start in range(0, len(arxiv_ids), 500):
            chunk = arxiv_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id, arxiv_id FROM papers WHERE arxiv_id IN ({placeholders})",
                chunk,
            ).fetchall()
            ids_by_arxiv.update({str(row["arxiv_id"]): int(row["id"]) for row in rows})

        unique_paper_ids = [ids_by_arxiv[arxiv_id] for arxiv_id in arxiv_ids]
        existing_memberships: set[int] = set()
        for chunk_start in range(0, len(unique_paper_ids), 500):
            chunk = unique_paper_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT paper_id FROM paper_categories
                WHERE category = ? AND crawl_date = ?
                  AND paper_id IN ({placeholders})
                """,
                [category, crawl_date, *chunk],
            ).fetchall()
            existing_memberships.update(int(row["paper_id"]) for row in rows)

        conn.executemany(
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
            [
                (
                    paper_id,
                    category,
                    crawl_date,
                    paper.get("rank"),
                    int(paper.get("reading_stars") or 0),
                    int(paper.get("pdf_clicks") or 0),
                    int(paper.get("kimi_clicks") or 0),
                    now,
                )
                for arxiv_id in arxiv_ids
                for paper_id, paper in [(ids_by_arxiv[arxiv_id], paper_by_arxiv[arxiv_id])]
            ],
        )
    paper_ids = [ids_by_arxiv[arxiv_id] for arxiv_id in input_arxiv_ids]
    input_duplicate_count = len(input_arxiv_ids) - len(arxiv_ids)
    membership_updated_count = len(existing_memberships)
    existing_arxiv_ids = set(existing_metadata)
    changed_arxiv_ids = {
        arxiv_id
        for arxiv_id in existing_arxiv_ids
        if tuple(serialized_by_arxiv[arxiv_id][1:9]) != existing_metadata[arxiv_id]
    }
    unchanged_arxiv_ids = existing_arxiv_ids - changed_arxiv_ids
    return PaperUpsertResult(
        paper_ids=paper_ids,
        parsed_count=len(paper_items),
        unique_count=len(arxiv_ids),
        persisted_count=len(arxiv_ids),
        new_count=len(set(arxiv_ids) - existing_arxiv_ids),
        updated_count=len(changed_arxiv_ids),
        duplicate_count=input_duplicate_count + len(unchanged_arxiv_ids),
        membership_new_count=len(arxiv_ids) - membership_updated_count,
        membership_updated_count=membership_updated_count,
        new_paper_ids=[ids_by_arxiv[key] for key in arxiv_ids if key not in existing_arxiv_ids],
    )


def _paper_upsert_values(paper: dict[str, Any], now: str) -> tuple[Any, ...]:
    return (
        paper["arxiv_id"],
        paper["title"],
        json.dumps(paper.get("authors", []), ensure_ascii=False),
        paper.get("abstract", ""),
        json.dumps(paper.get("subjects", []), ensure_ascii=False),
        paper.get("published_at"),
        paper.get("pdf_url"),
        paper.get("abs_url"),
        paper.get("papers_cool_url"),
        now,
        now,
    )


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

PAPER_DIGEST_SORTS = {"rank", "rank_desc", "stars", "stars_desc", "title", "updated"}


@dataclass(frozen=True)
class PaperDigestQuery:
    selected_date: str = ""
    date_from: str = ""
    date_to: str = ""
    category: str = ""
    attention: str = ""
    sort: str = "rank"

    @classmethod
    def from_raw(
        cls,
        date_value: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
        attention: str | None = None,
        sort: str | None = None,
        latest_crawl_date: str | None = None,
    ) -> "PaperDigestQuery":
        start, end = _normalized_digest_date_range(date_from, date_to)
        use_date_range = bool(start or end)
        selected_date = "" if use_date_range else (_valid_digest_date(date_value) or latest_crawl_date or "")
        return cls(
            selected_date=selected_date,
            date_from=start,
            date_to=end,
            category=(category or "").strip(),
            attention=(attention or "").strip(),
            sort=_normalized_digest_sort(sort),
        )

    @property
    def use_date_range(self) -> bool:
        return bool(self.date_from or self.date_to)

    def url_args(self, sort: str | None = None) -> dict[str, str]:
        return {
            "date": self.selected_date,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "category": self.category,
            "attention": self.attention,
            "sort": _normalized_digest_sort(sort) if sort is not None else self.sort,
        }


def _normalized_digest_date_range(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    start = _valid_digest_date(date_from)
    end = _valid_digest_date(date_to)
    if start and end and start > end:
        start, end = end, start
    return start, end


def _valid_digest_date(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if cleaned.isdigit() and len(cleaned) == 8:
        cleaned = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    else:
        cleaned = cleaned.replace("/", "-").replace(".", "-")
    try:
        date.fromisoformat(cleaned)
    except ValueError:
        return ""
    return cleaned


def _normalized_digest_sort(value: str | None) -> str:
    candidate = (value or "rank").strip()
    return candidate if candidate in PAPER_DIGEST_SORTS else "rank"


def list_paper_rows(
    crawl_date: str | PaperDigestQuery | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    attention: str | None = None,
    sort: str = "rank",
) -> list[dict[str, Any]]:
    if isinstance(crawl_date, PaperDigestQuery):
        query = crawl_date
        crawl_date = query.selected_date or None
        date_from = query.date_from or None
        date_to = query.date_to or None
        category = query.category or None
        attention = query.attention or None
        sort = query.sort

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


def list_paper_page(
    crawl_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    attention: str | None = None,
    sort: str = "rank",
    page: int = 1,
    page_size: int = 50,
    direction_id: int | None = None, model_state: str = '', manual_state: str = '', direction_view: str = 'focused',
) -> dict[str, Any]:
    """Return one stable page after paper/category rows have been deduplicated."""
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    use_date_range = bool(date_from or date_to)
    crawl_date = None if use_date_range else (crawl_date or get_latest_crawl_date())
    if not crawl_date and not use_date_range:
        return _paper_page_result([], 0, page, page_size)

    scope_sql, scope_params = _paper_scope_sql(crawl_date, date_from, date_to)
    representative_order, final_order = _paper_page_sort_sql(sort)
    filters: list[str] = ["rn = 1"]
    filter_params: list[Any] = []
    direction_sql, direction_params = direction_filter_sql('ranked.paper_id',direction_id,model_state,manual_state,direction_view)
    filters.append(direction_sql)
    filter_params.extend(direction_params)
    if category:
        filters.append(
            "EXISTS (SELECT 1 FROM scoped membership "
            "WHERE membership.paper_id = ranked.paper_id AND membership.category = ?)"
        )
        filter_params.append(category)
    if attention:
        filters.append(
            "CASE WHEN json_valid(latest_abstract.result_json) "
            "THEN json_extract(latest_abstract.result_json, '$.attention') END = ?"
        )
        filter_params.append(attention)

    candidate_ctes = f"""
        WITH scoped AS (
            SELECT pc.*, p.title, p.updated_at
            FROM paper_categories pc
            JOIN papers p ON p.id = pc.paper_id
            WHERE {scope_sql}
        ),
        ranked AS (
            SELECT
                scoped.*,
                ROW_NUMBER() OVER (
                    PARTITION BY paper_id
                    ORDER BY {representative_order}
                ) AS rn
            FROM scoped
        ),
        latest_abstract AS (
            SELECT paper_id, result_json
            FROM (
                SELECT
                    e.paper_id,
                    e.result_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.paper_id
                        ORDER BY e.created_at DESC, e.id DESC
                    ) AS eval_rn
                FROM evaluations e
                WHERE e.evaluation_type = 'abstract_review'
            )
            WHERE eval_rn = 1
        ),
        candidates AS (
            SELECT ranked.*
            FROM ranked
            LEFT JOIN latest_abstract ON latest_abstract.paper_id = ranked.paper_id
            WHERE {' AND '.join(filters)}
        )
    """
    all_params = [*scope_params, *filter_params]
    offset = (page - 1) * page_size
    with connect() as conn:
        total = int(
            conn.execute(
                candidate_ctes + " SELECT COUNT(*) AS total FROM candidates",
                all_params,
            ).fetchone()["total"]
        )
        candidate_rows = conn.execute(
            candidate_ctes
            + f" SELECT paper_id FROM candidates ORDER BY {final_order} LIMIT ? OFFSET ?",
            [*all_params, page_size, offset],
        ).fetchall()
        paper_ids = [int(row["paper_id"]) for row in candidate_rows]
        rows = _list_scoped_paper_rows_with_conn(conn, paper_ids, scope_sql, scope_params)

    items = _hydrate_paper_rows(_dedupe_paper_category_rows(rows, None, sort), attention=None)
    directions = paper_direction_results([item['id'] for item in items])
    for item in items:
        item['direction_results'] = directions.get(item['id'], [])
    return _paper_page_result(items, total, page, page_size)


def _paper_page_result(
    items: list[dict[str, Any]],
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "has_previous": page > 1,
        "has_next": page * page_size < total,
    }


def _paper_scope_sql(
    crawl_date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[Any]]:
    if date_from or date_to:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if date_from:
            clauses.append("pc.crawl_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("pc.crawl_date <= ?")
            params.append(date_to)
        return " AND ".join(clauses), params
    return "pc.crawl_date = ?", [crawl_date]


def _paper_page_sort_sql(sort: str) -> tuple[str, str]:
    rank_asc = "CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC"
    if sort in {"stars", "stars_desc"}:
        representative = f"reading_stars DESC, crawl_date DESC, {rank_asc}, id ASC"
        final = "reading_stars DESC, crawl_date DESC, rank ASC, paper_id ASC"
    elif sort == "rank_desc":
        representative = "COALESCE(rank, 0) DESC, crawl_date DESC, reading_stars DESC, id ASC"
        final = "COALESCE(rank, 0) DESC, crawl_date DESC, reading_stars DESC, paper_id ASC"
    elif sort == "title":
        representative = f"{rank_asc}, category ASC, crawl_date DESC, id ASC"
        final = "title COLLATE NOCASE ASC, paper_id ASC"
    elif sort == "updated":
        representative = "updated_at DESC, id ASC"
        final = "updated_at DESC, paper_id ASC"
    else:
        representative = f"{rank_asc}, category ASC, crawl_date DESC, id ASC"
        final = "crawl_date DESC, category ASC, rank ASC, paper_id ASC"
    return representative, final


def _list_scoped_paper_rows_with_conn(
    conn: sqlite3.Connection,
    paper_ids: list[int],
    scope_sql: str,
    scope_params: list[Any],
) -> list[dict[str, Any]]:
    if not paper_ids:
        return []
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(
        f"""
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
        WHERE {scope_sql} AND p.id IN ({placeholders})
        """,
        [*scope_params, *paper_ids],
    ).fetchall()
    return [dict(row) for row in rows]


def _hydrate_paper_rows(
    rows: list[dict[str, Any]],
    attention: str | None = None,
) -> list[dict[str, Any]]:
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


def has_successful_fulltext(paper_id: int, *, conn: sqlite3.Connection | None = None) -> bool:
    """Shared eligibility rule for detail UI and transactional paper-level writes."""
    if conn is None:
        with connect() as connection:
            return has_successful_fulltext(paper_id, conn=connection)
    return bool(conn.execute("""SELECT EXISTS(SELECT 1 FROM evaluations
        WHERE paper_id=? AND evaluation_type='fulltext_review' AND status='success')""",
        (paper_id,)).fetchone()[0])


def get_paper_decision_state(paper_id: int) -> dict[str, Any]:
    with connect() as conn:
        conn.execute('BEGIN')
        eligible = has_successful_fulltext(paper_id, conn=conn)
        row = conn.execute('SELECT * FROM paper_dispositions WHERE paper_id=?', (paper_id,)).fetchone()
    return {'eligible': eligible, 'decision': row['decision'] if row else 'undecided',
            'created_at': row['created_at'] if row else None, 'updated_at': row['updated_at'] if row else None}


def set_paper_decision(paper_id: int, decision: str) -> None:
    if decision not in PAPER_DECISIONS:
        raise ValueError('无效的个人决策')
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_paper_fulltext(conn, paper_id)
        if decision == 'clear':
            conn.execute('DELETE FROM paper_dispositions WHERE paper_id=?', (paper_id,))
        else:
            now = now_iso()
            conn.execute("""INSERT INTO paper_dispositions(paper_id,decision,created_at,updated_at)
                VALUES (?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
                decision=excluded.decision, updated_at=excluded.updated_at
                WHERE paper_dispositions.decision != excluded.decision""", (paper_id, decision, now, now))


def _require_paper_fulltext(conn: sqlite3.Connection, paper_id: int) -> None:
    if not 0 < paper_id <= 2**63-1 or conn.execute('SELECT 1 FROM papers WHERE id=?', (paper_id,)).fetchone() is None:
        raise PaperNotFoundError('论文不存在')
    if not has_successful_fulltext(paper_id, conn=conn):
        raise FulltextRequiredError('需要至少一次成功全文评估后才能整理此论文')


def _require_theme(conn: sqlite3.Connection, theme_id: int) -> dict[str, Any]:
    row = conn.execute('SELECT * FROM investment_themes WHERE id=?', (theme_id,)).fetchone() if 0 < theme_id <= 2**63-1 else None
    if row is None:
        raise InvestmentThemeNotFoundError('投资主题不存在')
    return dict(row)


def get_investment_theme(theme_id: int) -> dict[str, Any]:
    with connect() as conn:
        return _require_theme(conn, theme_id)


def list_investment_themes() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("""SELECT t.*, COALESCE(m.paper_count,0) AS paper_count
            FROM investment_themes t LEFT JOIN
                (SELECT theme_id, COUNT(*) AS paper_count FROM paper_investment_themes GROUP BY theme_id) m
                ON m.theme_id=t.id ORDER BY t.status, t.updated_at DESC, t.id DESC""")]


def _check_theme_name(conn: sqlite3.Connection, normalized_name: str, theme_id: int | None = None) -> None:
    row = conn.execute('SELECT id FROM investment_themes WHERE normalized_name=?', (normalized_name,)).fetchone()
    if row and row['id'] != theme_id:
        raise FormValidationError({'name': '同名投资主题已存在（包含已归档主题），请复用或恢复原主题'})


def create_investment_theme(name: str, description: str = '') -> int:
    command = InvestmentThemeCommand.from_values(name, description)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _check_theme_name(conn, command.normalized_name)
        now = now_iso()
        return int(conn.execute("""INSERT INTO investment_themes(name,normalized_name,description,created_at,updated_at)
            VALUES (?,?,?,?,?)""", (command.name, command.normalized_name, command.description, now, now)).lastrowid)


def update_investment_theme(theme_id: int, action: str, name: str | None = None, description: str = '') -> None:
    action = parse_choice(action, 'action', {'update','archive','restore'})
    command = InvestmentThemeCommand.from_values(name, description) if action == 'update' else None
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = _require_theme(conn, theme_id)
        if command:
            _check_theme_name(conn, command.normalized_name, theme_id)
            if (old['name'], old['description']) != (command.name, command.description):
                conn.execute('UPDATE investment_themes SET name=?,normalized_name=?,description=?,updated_at=? WHERE id=?',
                             (command.name, command.normalized_name, command.description, now_iso(), theme_id))
        else:
            status = 'archived' if action == 'archive' else 'active'
            if old['status'] != status:
                conn.execute('UPDATE investment_themes SET status=?,updated_at=? WHERE id=?', (status, now_iso(), theme_id))


def list_paper_investment_themes(paper_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = _unique_ints(paper_ids)
    result = {paper_id: [] for paper_id in ids}
    if not ids:
        return result
    with connect() as conn:
        for chunk in _chunks(ids):
            placeholders = ','.join('?' for _ in chunk)
            rows = conn.execute(f"""SELECT t.id,t.name,t.status,m.paper_id,m.created_at AS added_at
                FROM paper_investment_themes m JOIN investment_themes t ON t.id=m.theme_id
                WHERE m.paper_id IN ({placeholders}) ORDER BY t.status,m.created_at DESC,t.id""", chunk)
            for row in rows:
                result[row['paper_id']].append(dict(row))
    return result


def set_paper_investment_themes(paper_id: int, theme_ids: Iterable[int]) -> None:
    ids = parse_theme_ids(theme_ids)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_paper_fulltext(conn, paper_id)
        requested = {}
        for chunk in _chunks(ids):
            placeholders = ','.join('?' for _ in chunk)
            requested.update({row['id']: row['status'] for row in conn.execute(
                f'SELECT id,status FROM investment_themes WHERE id IN ({placeholders})', chunk)})
        if len(requested) != len(ids):
            raise InvestmentThemeNotFoundError('所选投资主题不存在，请刷新后重试')
        if any(status != 'active' for status in requested.values()):
            raise ArchivedThemeError('所选主题已归档，请刷新后重试；已有归档关系不会被普通保存删除')
        current = {row['theme_id'] for row in conn.execute("""SELECT m.theme_id FROM paper_investment_themes m
            JOIN investment_themes t ON t.id=m.theme_id WHERE m.paper_id=? AND t.status='active'""", (paper_id,))}
        desired = set(ids)
        conn.executemany('DELETE FROM paper_investment_themes WHERE paper_id=? AND theme_id=?',
                         [(paper_id, theme_id) for theme_id in current-desired])
        now = now_iso()
        conn.executemany('INSERT INTO paper_investment_themes(paper_id,theme_id,created_at) VALUES (?,?,?)',
                         [(paper_id, theme_id, now) for theme_id in desired-current])


def paper_investment_theme_options(paper_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("""SELECT t.id,t.name,t.description,t.status,m.created_at AS added_at
            FROM investment_themes t LEFT JOIN paper_investment_themes m ON m.theme_id=t.id AND m.paper_id=?
            WHERE t.status='active' OR m.paper_id IS NOT NULL ORDER BY t.status,t.normalized_name,t.id""", (paper_id,))]


def remove_paper_investment_theme(paper_id: int, theme_id: int) -> None:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_paper_fulltext(conn, paper_id)
        _require_theme(conn, theme_id)
        conn.execute('DELETE FROM paper_investment_themes WHERE paper_id=? AND theme_id=?', (paper_id, theme_id))


def _research_table(kind: str) -> str:
    return RESEARCH_ENTITY_TABLES[parse_choice(kind, 'kind', set(RESEARCH_ENTITY_TABLES))]


def _require_research_entity(conn: sqlite3.Connection, kind: str, entity_id: int) -> dict[str, Any]:
    table = _research_table(kind)
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (entity_id,)).fetchone() if 0 < entity_id <= 2**63-1 else None
    if row is None:
        raise ResearchEntityNotFoundError('作者或机构不存在，请刷新后重新选择')
    return dict(row)


def get_research_entity(kind: str, entity_id: int) -> dict[str, Any]:
    with connect() as conn:
        return _require_research_entity(conn, kind, entity_id)


def _research_conflict(kind: str, row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {'kind': kind, 'id': row['id'], 'name': row['name'], 'status': row['status'], 'reason': reason}


def _insert_research_entity(conn: sqlite3.Connection, command: ResearchEntityCommand) -> int:
    values = {**command.values, 'created_at': now_iso(), 'updated_at': now_iso()}
    columns = ','.join(values)
    placeholders = ','.join('?' for _ in values)
    return int(conn.execute(f'INSERT INTO {_research_table(command.kind)}({columns}) VALUES ({placeholders})',
                            list(values.values())).lastrowid)


def save_paper_team_tracking(paper_id: int, form: Mapping[str, Any]) -> int:
    command = TeamTrackingCommand.from_form(form)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_paper_fulltext(conn, paper_id)
        ids, conflicts = {}, []
        # Resolve both entities before the first DML, including exact duplicates.
        for kind in ('author', 'organization'):
            value = getattr(command, kind)
            if isinstance(value, int):
                row = _require_research_entity(conn, kind, value)
                if row['status'] != 'active':
                    conflicts.append(_research_conflict(kind, row, 'archived'))
                ids[kind] = value
            else:
                row = conn.execute(f'SELECT * FROM {_research_table(kind)} WHERE normalized_name=?',
                                   (value.values['normalized_name'],)).fetchone()
                if row:
                    conflicts.append(_research_conflict(kind, row, 'duplicate'))
        if conflicts:
            raise ResearchEntityConflictError(conflicts)
        for kind in ('author', 'organization'):
            if kind not in ids:
                ids[kind] = _insert_research_entity(conn, getattr(command, kind))
        now = now_iso()
        conn.execute("""INSERT INTO paper_team_tracking(paper_id,lead_author_id,organization_id,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
                lead_author_id=excluded.lead_author_id,organization_id=excluded.organization_id,
                notes=excluded.notes,status='tracking',updated_at=excluded.updated_at
            WHERE paper_team_tracking.lead_author_id != excluded.lead_author_id
                OR paper_team_tracking.organization_id != excluded.organization_id
                OR paper_team_tracking.notes != excluded.notes OR paper_team_tracking.status != 'tracking'""",
            (paper_id, ids['author'], ids['organization'], command.notes, now, now))
        return int(conn.execute('SELECT id FROM paper_team_tracking WHERE paper_id=?', (paper_id,)).fetchone()[0])


def archive_paper_team_tracking(paper_id: int) -> None:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_paper_fulltext(conn, paper_id)
        conn.execute("UPDATE paper_team_tracking SET status='archived',updated_at=? WHERE paper_id=? AND status='tracking'",
                     (now_iso(), paper_id))


def update_research_entity(kind: str, entity_id: int, action: str, form: Mapping[str, Any] | None = None) -> None:
    table = _research_table(kind)
    action = parse_choice(action, 'action', {'update', 'archive', 'restore'})
    command = ResearchEntityCommand.from_form(kind, form or {}) if action == 'update' else None
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = _require_research_entity(conn, kind, entity_id)
        if command:
            row = conn.execute(f'SELECT * FROM {table} WHERE normalized_name=? AND id!=?',
                               (command.values['normalized_name'], entity_id)).fetchone()
            if row:
                raise ResearchEntityConflictError([_research_conflict(kind, row, 'duplicate')])
            values = command.values
        else:
            values = {'status': 'archived' if action == 'archive' else 'active'}
        if any(old[key] != value for key, value in values.items()):
            updates = {**values, 'updated_at': now_iso()}
            conn.execute(f"UPDATE {table} SET {','.join(key+'=?' for key in updates)} WHERE id=?",
                         [*updates.values(), entity_id])


def get_paper_team_tracking(paper_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("""SELECT t.*,a.name AS author_name,a.status AS author_status,
            a.author_category,o.name AS organization_name,o.status AS organization_status,o.organization_type
            FROM paper_team_tracking t JOIN research_authors a ON a.id=t.lead_author_id
            JOIN research_organizations o ON o.id=t.organization_id WHERE t.paper_id=?""", (paper_id,)).fetchone())


def research_entity_options(kind: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT id,name FROM {_research_table(kind)} WHERE status='active' ORDER BY normalized_name,id")]


def _research_filters(author_category: str, organization_type: str) -> tuple[str, str]:
    if author_category:
        author_category = parse_choice(author_category, 'author_category', set(AUTHOR_CATEGORIES))
    if organization_type:
        organization_type = parse_choice(organization_type, 'organization_type', set(ORGANIZATION_TYPES))
    return author_category, organization_type


def list_team_tracking(*, query: str = '', status: str = 'tracking', author_category: str = '',
                       organization_type: str = '', author_id: int | None = None,
                       organization_id: int | None = None) -> list[dict[str, Any]]:
    status = parse_choice(status, 'status', {'all', 'tracking', 'archived'})
    author_category, organization_type = _research_filters(author_category, organization_type)
    clauses, params = [], []
    if status != 'all':
        clauses.append('t.status=?')
        params.append(status)
    if query.strip():
        clauses.append('(instr(a.normalized_name,?)>0 OR instr(o.normalized_name,?)>0 OR instr(lower(p.title),?)>0)')
        normalized = normalized_research_name(query)
        params.extend([normalized, normalized, query.strip().lower()])
    for column, value in [('a.author_category', author_category), ('o.organization_type', organization_type)]:
        if value:
            clauses.append(column+'=?')
            params.append(value)
    for column, value in [('t.lead_author_id', author_id), ('t.organization_id', organization_id)]:
        if value is not None:
            clauses.append(column+'=?')
            params.append(parse_int(value, column, minimum=1, maximum=2**63-1))
    where = ' AND '.join(clauses) or '1=1'
    with connect() as conn:
        return [dict(row) for row in conn.execute(f"""SELECT t.*,p.title,p.arxiv_id,p.published_at,
            a.name AS author_name,a.author_category,a.status AS author_status,
            o.name AS organization_name,o.organization_type,o.region,o.status AS organization_status
            FROM paper_team_tracking t JOIN papers p ON p.id=t.paper_id
            JOIN research_authors a ON a.id=t.lead_author_id
            JOIN research_organizations o ON o.id=t.organization_id
            WHERE {where} ORDER BY t.updated_at DESC,t.id DESC""", params)]


def list_research_entities(kind: str, *, query: str = '', status: str = 'active',
                           author_category: str = '', organization_type: str = '') -> list[dict[str, Any]]:
    table = _research_table(kind)
    status = parse_choice(status, 'status', {'all', 'active', 'archived'})
    author_category, organization_type = _research_filters(author_category, organization_type)
    foreign_key, other_key, other_table = (('lead_author_id', 'organization_id', 'research_organizations')
        if kind == 'author' else ('organization_id', 'lead_author_id', 'research_authors'))
    clauses, params = [], []
    if status != 'all':
        clauses.append('e.status=?')
        params.append(status)
    if query.strip():
        clauses.append('instr(e.normalized_name,?)>0')
        params.append(normalized_research_name(query))
    for column, value, own in [('author_category', author_category, kind == 'author'),
                                ('organization_type', organization_type, kind == 'organization')]:
        if value:
            clauses.append(f'e.{column}=?' if own else f'EXISTS(SELECT 1 FROM paper_team_tracking tf JOIN {other_table} ot ON ot.id=tf.{other_key} WHERE tf.{foreign_key}=e.id AND ot.{column}=?)')
            params.append(value)
    where = ' AND '.join(clauses) or '1=1'
    with connect() as conn:
        conn.execute('BEGIN')
        rows = [dict(row) for row in conn.execute(f"""WITH counts AS (
            SELECT {foreign_key} AS entity_id,COUNT(*) AS paper_count,COUNT(DISTINCT {other_key}) AS related_count,
                SUM(status='tracking') AS tracking_count FROM paper_team_tracking GROUP BY {foreign_key})
            SELECT e.*,COALESCE(c.paper_count,0) AS paper_count,COALESCE(c.related_count,0) AS related_count,
                COALESCE(c.tracking_count,0) AS tracking_count
            FROM {table} e LEFT JOIN counts c ON c.entity_id=e.id WHERE {where}
            ORDER BY e.updated_at DESC,e.id DESC""", params)]
        by_id = {row['id']: row for row in rows}
        for row in rows:
            row.update(recent_papers=[], related_entities=[])
        for chunk in _chunks(list(by_id)):
            placeholders = ','.join('?' for _ in chunk)
            recent = conn.execute(f"""WITH ranked AS (
                SELECT t.{foreign_key} AS entity_id,p.id,p.title,p.published_at,t.status,
                    ROW_NUMBER() OVER(PARTITION BY t.{foreign_key} ORDER BY COALESCE(p.published_at,'') DESC,t.created_at DESC,t.id DESC) AS rn
                FROM paper_team_tracking t JOIN papers p ON p.id=t.paper_id WHERE t.{foreign_key} IN ({placeholders}))
                SELECT * FROM ranked WHERE rn<=3 ORDER BY entity_id,rn""", chunk)
            for row in recent:
                by_id[row['entity_id']]['recent_papers'].append(dict(row))
            partners = conn.execute(f"""WITH links AS (
                SELECT DISTINCT t.{foreign_key} AS entity_id,o.id,o.name,o.normalized_name,o.status
                FROM paper_team_tracking t JOIN {other_table} o ON o.id=t.{other_key}
                WHERE t.{foreign_key} IN ({placeholders})),ranked AS (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY entity_id ORDER BY normalized_name,id) AS rn FROM links)
                SELECT * FROM ranked WHERE rn<=3 ORDER BY entity_id,rn""", chunk)
            for row in partners:
                by_id[row['entity_id']]['related_entities'].append(dict(row))
        return rows


def list_fulltext_reviewed_papers(sort: str = "evaluated_desc", *, decision: str = 'all', theme_id: int | None = None) -> list[dict[str, Any]]:
    if decision not in PAPER_DECISION_FILTERS:
        raise ValueError('无效的个人决策筛选')
    theme_join = 'JOIN paper_investment_themes tm ON tm.paper_id=p.id AND tm.theme_id=?' if theme_id is not None else ''
    theme_column = 'tm.created_at' if theme_id is not None else 'NULL'
    evaluation_join = 'LEFT JOIN' if theme_id is not None else 'JOIN'
    sql = f"""
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
            e.result_json AS fulltext_result_json,
            COALESCE(d.decision, 'undecided') AS decision,
            d.updated_at AS decision_updated_at,
            {theme_column} AS theme_added_at
        FROM papers p
        {evaluation_join} latest_fulltext e ON e.paper_id = p.id
        {theme_join}
        LEFT JOIN paper_dispositions d ON d.paper_id = p.id
        WHERE (? = 'all' OR (? = 'undecided' AND d.paper_id IS NULL) OR d.decision = ?)
        ORDER BY e.created_at DESC, e.id DESC
    """
    with connect() as conn:
        params = ([theme_id] if theme_id is not None else []) + [decision, decision, decision]
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    categories_by_paper = list_paper_categories_for_papers(int(row["id"]) for row in rows)
    themes_by_paper = list_paper_investment_themes(int(row['id']) for row in rows)
    hydrated = []
    for row in rows:
        paper_id = int(row["id"])
        row["authors_list"] = loads_json(row.get("authors"), [])
        row["subjects_list"] = loads_json(row.get("subjects"), [])
        row["fulltext_result"] = loads_json_object(row.get("fulltext_result_json"))
        row["categories"] = categories_by_paper.get(paper_id, [])
        row['investment_themes'] = themes_by_paper.get(paper_id, [])
        row["latest_category"] = row["categories"][0] if row["categories"] else {}
        hydrated.append(row)

    if sort == 'added_desc':
        hydrated.sort(key=lambda row: (row.get('theme_added_at') or '', row['id']), reverse=True)
    elif sort == "score_desc":
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


def list_papers_for_abstract_audit(
    limit: int = 200,
    offset: int = 0,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    limit = min(1000, max(1, int(limit)))
    offset = max(0, int(offset))
    with connect_readonly(path) as conn:
        rows = conn.execute(
            """
            SELECT id, arxiv_id, title, authors, abstract, subjects, updated_at
            FROM papers
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    papers = []
    for row in rows:
        paper = dict(row)
        paper["authors_list"] = loads_json(paper.get("authors"), [])
        paper["subjects_list"] = loads_json(paper.get("subjects"), [])
        papers.append(paper)
    return papers


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


def list_papers_missing_evaluation(
    eval_type: str,
    limit: int = 200,
    include_terminal_failures: bool = False,
) -> list[int]:
    terminal_filter = ""
    if eval_type == "fulltext_review" and not include_terminal_failures:
        terminal_filter = """
          AND NOT (
              latest_eval.error_code = 'preparation_failed'
              AND latest_eval.error_retryable = 0
          )
        """
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH latest_eval AS (
                SELECT *
                FROM (
                    SELECT
                        e.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY e.paper_id
                            ORDER BY e.created_at DESC, e.id DESC
                        ) AS rn
                    FROM evaluations e
                    WHERE e.evaluation_type = ?
                )
                WHERE rn = 1
            )
            SELECT DISTINCT p.id
            FROM papers p
            JOIN paper_categories pc ON pc.paper_id = p.id
            LEFT JOIN latest_eval ON latest_eval.paper_id = p.id
            WHERE pc.crawl_date = COALESCE((SELECT MAX(crawl_date) FROM paper_categories), pc.crawl_date)
              AND NOT EXISTS (
                  SELECT 1 FROM evaluations e
                  WHERE e.paper_id = p.id AND e.evaluation_type = ? AND e.status = 'success'
              )
              {terminal_filter}
            ORDER BY pc.category, pc.rank
            LIMIT ?
            """,
            (eval_type, eval_type, limit),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def loads_json_object(value: str | None) -> dict[str, Any]:
    parsed = loads_json(value, {})
    return parsed if isinstance(parsed, dict) else {}


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
    flag = {'direction_classification': 'is_default_classification',
            'investment_memo': 'is_default_memo',
            'fulltext_review': 'is_default_fulltext'}.get(eval_type, 'is_default_abstract')
    with connect_llm_profiles() as conn:
        row = conn.execute(
            f"SELECT * FROM llm_profiles WHERE enabled = 1 AND {flag} = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None and eval_type not in {'direction_classification','investment_memo'}:
            row = conn.execute("SELECT * FROM llm_profiles WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    return row_to_dict(row)


def save_llm_profile(data: dict[str, Any]) -> int:
    now = now_iso()
    profile_id = data.get("id")
    enabled = 1 if data.get("enabled") else 0
    default_abstract = 1 if data.get("is_default_abstract") else 0
    default_fulltext = 1 if data.get("is_default_fulltext") else 0
    default_classification = 1 if data.get('is_default_classification') else 0
    default_memo = 1 if data.get('is_default_memo') else 0
    with connect_llm_profiles() as conn:
        if default_abstract:
            conn.execute("UPDATE llm_profiles SET is_default_abstract = 0")
        if default_fulltext:
            conn.execute("UPDATE llm_profiles SET is_default_fulltext = 0")
        if default_classification:
            conn.execute('UPDATE llm_profiles SET is_default_classification = 0')
        if default_memo:
            conn.execute('UPDATE llm_profiles SET is_default_memo = 0')

        values = (
            data["name"],
            data["provider"],
            data["base_url"],
            data["model"],
            data.get("encrypted_api_key_ref"),
            data.get("custom_headers") or "{}",
            float(data["temperature"] if data.get("temperature") is not None else 0.2),
            int(data.get("max_output_tokens") or 2000),
            int(data.get("context_window_tokens") or 128000),
            int(data.get("timeout_seconds") or 120),
            enabled,
            default_abstract,
            default_fulltext,
            default_classification,
            default_memo,
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
                    is_default_abstract = ?, is_default_fulltext = ?, is_default_classification = ?, is_default_memo = ?, updated_at = ?
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
                enabled, is_default_abstract, is_default_fulltext, is_default_classification, is_default_memo, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["provider"],
                data["base_url"],
                data["model"],
                data.get("encrypted_api_key_ref"),
                data.get("custom_headers") or "{}",
                float(data['temperature'] if data.get('temperature') is not None else 0.2),
                int(data.get("max_output_tokens") or 2000),
                int(data.get("context_window_tokens") or 128000),
                int(data.get("timeout_seconds") or 120),
                enabled,
                default_abstract,
                default_fulltext,
                default_classification,
                default_memo,
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
                    f"SELECT id, name, model, enabled FROM llm_profiles WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                profiles.update({row["id"]: row for row in rows})

    for prompt in prompts:
        profile = profiles.get(prompt.get("llm_profile_id"))
        prompt["llm_profile_name"] = profile["name"] if profile else None
        prompt["llm_model"] = profile["model"] if profile else None
        prompt["llm_profile_enabled"] = bool(profile["enabled"]) if profile else None
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


def direction_filter_sql(paper_column, direction_id=None, model_state='', manual_state='', direction_view='focused'):
    # paper_column is supplied only by internal SQL callers, never request input.
    parse_choice(direction_view,'direction_view',{'focused','all'})
    if model_state:
        parse_choice(model_state,'model_state',{'matched','possible','unmatched','failed'})
    if manual_state:
        parse_choice(manual_state,'manual_state',{'confirmed','rejected','pending'})
    if direction_id is not None:
        with connect() as conn:
            require_direction(conn,direction_id)
    clauses, params = [f'r.paper_id={paper_column}'], []
    if direction_id is not None:
        clauses.append('r.direction_id=?')
        params.append(direction_id)
    if model_state:
        clauses.append('r.model_decision=?')
        params.append(model_state)
    if manual_state:
        clauses.append("r.manual_decision IS NULL AND r.model_decision='possible'" if manual_state == 'pending' else 'r.manual_decision=?')
        if manual_state != 'pending':
            params.append(manual_state)
    if not model_state and not manual_state and direction_view == 'focused':
        clauses.append("(r.manual_decision='confirmed' OR (r.manual_decision IS NULL AND r.model_decision IN ('matched','possible')))")
        if direction_id is None:
            clauses.append("d.status='active'")
    if direction_view == 'all' and direction_id is None and not model_state and not manual_state:
        return '1=1', []
    sql = 'EXISTS (SELECT 1 FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id WHERE ' + ' AND '.join(clauses) + ')'
    if direction_id is None and not model_state and not manual_state:
        sql = "(NOT EXISTS (SELECT 1 FROM attention_directions WHERE status='active') OR " + sql + ')'
    return sql, params


def filter_papers_by_directions(papers, **filters):
    sql, params = direction_filter_sql('p.id',**filters)
    allowed = set()
    with connect() as conn:
        for chunk in _chunks([p['id'] for p in papers]):
            allowed.update(row[0] for row in conn.execute(f"SELECT p.id FROM papers p WHERE p.id IN ({','.join('?' for _ in chunk)}) AND {sql}", [*chunk,*params]))
    return [paper for paper in papers if paper['id'] in allowed]


def set_direction_decision(paper_id, direction_id, decision):
    decision = parse_choice(decision,'decision',{'confirmed','rejected'})
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not (0 < paper_id <= 2**63-1) or not conn.execute('SELECT 1 FROM papers WHERE id=?',(paper_id,)).fetchone():
            raise PaperNotFoundError('论文不存在')
        require_direction(conn,direction_id,active=True)
        now = now_iso()
        conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,manual_decision,manual_updated_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(paper_id,direction_id) DO UPDATE SET manual_decision=excluded.manual_decision,
            manual_updated_at=excluded.manual_updated_at,updated_at=excluded.updated_at
            WHERE paper_direction_results.manual_decision IS NOT excluded.manual_decision''',(paper_id,direction_id,decision,now,now,now))


def paper_direction_results(paper_ids: Iterable[int]) -> dict[int, list[dict]]:
    results = {}
    with connect() as conn:
        for chunk in _chunks(_unique_ints(paper_ids)):
            rows = conn.execute(f'''SELECT r.*, d.name,d.scope_text,d.status AS direction_status,
                e.prompt_id,e.prompt_version,e.llm_profile_id,e.model,e.created_at AS classified_at
                FROM paper_direction_results r JOIN attention_directions d ON d.id=r.direction_id
                LEFT JOIN evaluations e ON e.id=r.classification_evaluation_id
                WHERE r.paper_id IN ({','.join('?' for _ in chunk)}) ORDER BY r.direction_id''', chunk)
            for row in rows:
                item = dict(row)
                item['effective'] = item['manual_decision'] == 'confirmed' or (item['manual_decision'] is None and item['model_decision'] == 'matched')
                item['pending'] = item['manual_decision'] is None and item['model_decision'] == 'possible'
                results.setdefault(item['paper_id'], []).append(item)
    return results


def classification_inputs(paper_ids: Iterable[int], *, dates=None, categories=None,
                          date_from=None, date_to=None) -> dict[int, dict]:
    inputs = {}
    with connect() as conn:
        conn.execute('BEGIN')
        for chunk in _chunks(_unique_ints(paper_ids)):
            placeholders = ','.join('?' for _ in chunk)
            for row in conn.execute(f'SELECT id,title,abstract,subjects FROM papers WHERE id IN ({placeholders})', chunk):
                inputs[row['id']] = {'title': row['title'], 'abstract': row['abstract'],
                                     'subjects': loads_json(row['subjects'], []), 'categories': []}
            sql = f'SELECT DISTINCT paper_id,category FROM paper_categories WHERE paper_id IN ({placeholders})'
            params = list(chunk)
            if dates is not None:
                sql += f" AND crawl_date IN ({','.join('?' for _ in dates)})"
                params.extend(dates)
            if categories is not None:
                sql += f" AND category IN ({','.join('?' for _ in categories)})"
                params.extend(categories)
            if date_from is not None:
                sql += ' AND crawl_date BETWEEN ? AND ?'
                params.extend([date_from, date_to])
            for row in conn.execute(sql + ' ORDER BY category', params):
                inputs[row['paper_id']]['categories'].append(row['category'])
    return inputs


def preview_direction_backfill(direction_id, date_from, date_to):
    with connect() as conn:
        conn.execute('BEGIN')
        direction = require_direction(conn,direction_id,active=True)
        rows = conn.execute('''SELECT p.id,p.title,p.abstract,p.subjects,r.model_decision,
            EXISTS(SELECT 1 FROM evaluations e WHERE e.paper_id=p.id AND e.evaluation_type='abstract_review' AND e.status='success') AS has_abstract
            FROM papers p LEFT JOIN paper_direction_results r ON r.paper_id=p.id AND r.direction_id=?
            WHERE EXISTS(SELECT 1 FROM paper_categories pc WHERE pc.paper_id=p.id AND pc.crawl_date BETWEEN ? AND ?)
            ORDER BY p.id''',(direction_id,date_from,date_to)).fetchall()
        categories = {}
        for row in conn.execute('''SELECT DISTINCT paper_id,category FROM paper_categories
                                   WHERE crawl_date BETWEEN ? AND ? ORDER BY paper_id,category''',(date_from,date_to)):
            categories.setdefault(row['paper_id'],[]).append(row['category'])
        counts = {key:0 for key in ('total','executable','input_incomplete','already_classified','already_abstract','max_additional_abstract')}
        inputs = {}
        for row in rows:
            key = row['id']
            inputs[key] = {'title':row['title'],'abstract':row['abstract'],'subjects':loads_json(row['subjects'],[]),'categories':categories.get(key,[])}
            counts['total'] += 1
            complete = all(str(row[k] or '').strip() for k in ('title','abstract'))
            successful = row['model_decision'] in {'matched','possible','unmatched'}
            counts['input_incomplete'] += int(not complete)
            counts['already_classified'] += int(successful)
            counts['executable'] += int(complete and not successful)
            counts['already_abstract'] += int(row['has_abstract'])
            counts['max_additional_abstract'] += int(complete and not row['has_abstract'] and row['model_decision'] != 'unmatched')
    return {'direction':direction,'date_from':date_from,'date_to':date_to,'paper_ids':list(inputs),'inputs':inputs,'counts':counts}


def create_direction_backfill_job(plan):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_direction(conn,plan['directions'][0]['id'],active=True)
        job_id = int(conn.execute("INSERT INTO jobs(type,status,payload,created_at) VALUES ('direction_backfill','pending',?,?)",
                     (json.dumps(plan,ensure_ascii=False),now_iso())).lastrowid)
        _insert_job_event(conn,_normalize_job_event(job_id,f'backfill:{job_id}:preview','direction_backfill',
            'direction_backfill.previewed',metrics=plan['preview'],message='用户已确认日期范围与模型调用上限'))
    return job_id


def claim_classification(paper_id: int, direction_ids: list[int], job_id: int, *, daily=False):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('SELECT direction_id,model_decision FROM paper_direction_results WHERE paper_id=?', (paper_id,)).fetchall()
        successful = {row['direction_id'] for row in rows if row['model_decision'] in {'matched','possible','unmatched'}}
        if daily and conn.execute("SELECT 1 FROM evaluations WHERE paper_id=? AND evaluation_type='direction_classification' LIMIT 1", (paper_id,)).fetchone():
            return None, [], 'already_classified'
        missing = [key for key in direction_ids if key not in successful]
        if not missing:
            return None, [], 'already_classified'
        if conn.execute('SELECT 1 FROM classification_claims WHERE paper_id=?', (paper_id,)).fetchone():
            return None, [], 'classification_already_running'
        token = uuid4().hex
        conn.execute('INSERT INTO classification_claims(paper_id,token,job_id,created_at) VALUES (?,?,?,?)', (paper_id,token,job_id,now_iso()))
        return token, missing, None


def start_classification_attempt(paper_id, job_id, source, directions, metadata, config, attempt):
    encoded_input = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    # Configuration contains only the frozen public fields and a transport binding hash.
    prompt, profile = (config or {}).get('prompt', {}), (config or {}).get('profile', {})
    with connect() as conn:
        return int(conn.execute('''INSERT INTO evaluations(paper_id,pipeline_job_id,evaluation_type,
            prompt_id,prompt_version,llm_profile_id,model,status,classification_source,
            direction_snapshot_json,input_snapshot_json,input_fingerprint,config_snapshot_json,attempt,created_at)
            VALUES (?,?,'direction_classification',?,?,?,?,'running',?,?,?,?,?,?,?)''',
            (paper_id,job_id,prompt.get('id'),prompt.get('version'),profile.get('id'),profile.get('model'),source,
             json.dumps(directions,ensure_ascii=False),encoded_input,hashlib.sha256(encoded_input.encode()).hexdigest(),
             json.dumps(config or {},ensure_ascii=False),attempt,now_iso())).lastrowid)


def _write_direction_models(conn, evaluation, results):
    now = now_iso()
    for result in results:
        conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,model_reason,
            classification_evaluation_id,classification_source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(paper_id,direction_id) DO UPDATE SET model_decision=excluded.model_decision,
            model_reason=excluded.model_reason,classification_evaluation_id=excluded.classification_evaluation_id,
            classification_source=excluded.classification_source,updated_at=excluded.updated_at
            WHERE paper_direction_results.model_decision IS NULL OR paper_direction_results.model_decision='failed' ''',
            (evaluation['paper_id'],result['direction_id'],result['decision'],result['reason'],evaluation['id'],
             evaluation['classification_source'],now,now))


def finish_classification_attempt(evaluation_id, *, result=None, raw_output=None, error_code=None,
                                  retryable=False, terminal=True, usage=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        evaluation = conn.execute('SELECT * FROM evaluations WHERE id=?', (evaluation_id,)).fetchone()
        if not evaluation or evaluation['status'] != 'running':
            raise RuntimeError('classification_attempt_not_running')
        conn.execute('''UPDATE evaluations SET status=?,result_json=?,raw_output=?,error_code=?,error_message=?,
            error_retryable=? WHERE id=?''',
            ('success' if result else 'failed',json.dumps({'directions':result,'usage':usage},ensure_ascii=False) if result else None,
             raw_output,error_code,error_code,int(retryable),evaluation_id))
        if result:
            _write_direction_models(conn,evaluation,result)
        elif terminal:
            _write_direction_models(conn,evaluation,[{'direction_id':d['id'],'decision':'failed','reason':error_code or 'classification_failed'}
                                                    for d in loads_json(evaluation['direction_snapshot_json'],[])])


def release_classification_claim(token):
    with connect() as conn:
        conn.execute('DELETE FROM classification_claims WHERE token=?',(token,))


def daily_abstract_attempted(paper_ids):
    """Persisted evaluation facts, not expiring job events, stop daily failure loops."""
    attempted = set()
    with connect() as conn:
        for chunk in _chunks(_unique_ints(paper_ids)):
            attempted.update(row[0] for row in conn.execute(f'''SELECT DISTINCT paper_id FROM evaluations
                WHERE paper_id IN ({','.join('?' for _ in chunk)}) AND evaluation_type='abstract_review'
                AND status='failed' ''', chunk))
    return attempted


def claim_abstract_evaluation(
    paper_id: int, *, skip_success: bool = True, job_id: int | None = None,
    pipeline_job_id: int | None = None,
) -> tuple[str | None, str | None]:
    """Atomic admission shared by manual, repair and pipeline evaluations."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if skip_success and conn.execute(
            "SELECT 1 FROM evaluations WHERE paper_id=? AND evaluation_type='abstract_review' AND status='success' LIMIT 1",
            (paper_id,),
        ).fetchone():
            return None, "already_successful"
        active = conn.execute(
            "SELECT 1 FROM evaluation_claims WHERE paper_id=? AND evaluation_type='abstract_review'",
            (paper_id,),
        ).fetchone()
        queued = conn.execute(
            """SELECT 1 FROM jobs WHERE type='abstract_eval' AND status IN ('pending','running')
               AND id != COALESCE(?, -1) AND json_extract(payload, '$.paper_id')=? LIMIT 1""",
            (job_id, paper_id),
        ).fetchone()
        if active or queued:
            return None, "evaluation_already_running"
        token = uuid4().hex
        conn.execute(
            """INSERT INTO evaluation_claims(paper_id,evaluation_type,token,job_id,pipeline_job_id,last_evaluation_id,created_at)
               VALUES (?, 'abstract_review', ?, ?, ?,
               (SELECT COALESCE(MAX(id),0) FROM evaluations WHERE paper_id=? AND evaluation_type='abstract_review'), ?)""",
            (paper_id, token, job_id, pipeline_job_id, paper_id, now_iso()),
        )
        return token, None


def mark_evaluation_provider_started(token: str) -> None:
    with connect() as conn:
        conn.execute("""UPDATE evaluation_claims SET provider_started=1,
            last_evaluation_id=(SELECT COALESCE(MAX(id),0) FROM evaluations e
                WHERE e.paper_id=evaluation_claims.paper_id AND e.evaluation_type=evaluation_claims.evaluation_type)
            WHERE token=?""", (token,))


def release_evaluation_claim(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM evaluation_claims WHERE token=?", (token,))


def get_latest_completed_pipeline_date(max_date: str, categories: Iterable[str] | None = None) -> str | None:
    """Use completed units, including legal empty days, instead of latest membership."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT payload, progress_details_json FROM jobs WHERE type='daily_pipeline' AND status='success' ORDER BY id DESC"
        ).fetchall()
        wanted = set(categories or [])
        plans = [loads_json(row['payload'], {}) for row in rows]
        dates = [day for plan in plans
                 if wanted <= {item['category'] for item in plan.get('categories', [])}
                 for day in plan.get('dates', []) if day <= max_date]
        has_pipeline = conn.execute("SELECT 1 FROM jobs WHERE type='daily_pipeline' LIMIT 1").fetchone()
    if dates:
        return max(dates)
    # Only legacy installs bootstrap from memberships; failed pipelines must not advance the cursor.
    return None if has_pipeline else get_latest_crawl_date_on_or_before(max_date)


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
    error_code: str | None = None,
    error_retryable: bool = False,
    pipeline_job_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO evaluations(
                paper_id, pipeline_job_id, evaluation_type,
                prompt_id, prompt_version, llm_profile_id,
                model, status, result_json, raw_output, error_message,
                error_code, error_retryable, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                pipeline_job_id,
                evaluation_type,
                prompt_id,
                prompt_version,
                llm_profile_id,
                model,
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                raw_output,
                error_message,
                error_code,
                1 if error_retryable else 0,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def _hydrate_evaluation_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    item = row_to_dict(row) if isinstance(row, sqlite3.Row) else (dict(row) if row else None)
    if item:
        item.pop("rn", None)
        item["result"] = loads_json_object(item.get("result_json"))
        item["error_retryable"] = bool(item.get("error_retryable"))
        item["outcome"] = {
            "status": item.get("status"),
            "error_code": item.get("error_code"),
            "retryable": item["error_retryable"],
        }
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
        item["result"] = loads_json_object(item.get("result_json"))
        profile = profiles.get(item.get("llm_profile_id"))
        item["llm_profile_name"] = profile["name"] if profile else None
    return items


def list_pipeline_evaluations(
    pipeline_job_id: int,
    evaluation_type: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [int(pipeline_job_id)]
    where = "pipeline_job_id = ?"
    if evaluation_type is not None:
        where += " AND evaluation_type = ?"
        params.append(str(evaluation_type))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM evaluations
            WHERE {where}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()
    return [
        item
        for row in rows
        if (item := _hydrate_evaluation_row(row)) is not None
    ]


def create_job(
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    retry_of_job_id: int | None = None,
) -> int:
    job_type = str(job_type or "").strip()
    if not job_type:
        raise ValueError("job_type 不能为空")
    normalized_key = str(idempotency_key or "").strip() or None
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs(
                type, status, idempotency_key, retry_of_job_id, payload, created_at
            )
            VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (
                job_type,
                normalized_key,
                retry_of_job_id,
                json.dumps(payload or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def create_daily_pipeline_job(
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    retry_of_job_id: int | None = None,
) -> tuple[int, bool]:
    payload_data = dict(payload or {})
    trigger_source = str(payload_data.get("trigger_source") or "")
    if trigger_source not in PIPELINE_TRIGGER_SOURCES:
        raise ValueError(f"未知流水线触发来源: {trigger_source or '-'}")
    normalized_key = str(idempotency_key or "").strip() or None
    encoded_payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if normalized_key:
            existing = conn.execute(
                "SELECT id, type, payload, retry_of_job_id FROM jobs WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing:
                existing_payload = json.dumps(
                    loads_json(existing["payload"], {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    existing["type"] != DAILY_PIPELINE_JOB_TYPE
                    or existing_payload != encoded_payload
                    or existing["retry_of_job_id"] != retry_of_job_id
                ):
                    raise ValueError("idempotency_key 已用于不同的流水线请求")
                return int(existing["id"]), False

        active = conn.execute(
            f"""
            SELECT id FROM jobs
            WHERE type IN ({','.join('?' for _ in CRAWL_JOB_TYPES)})
              AND status IN ('pending', 'running')
            ORDER BY created_at, id
            LIMIT 1
            """,
            CRAWL_JOB_TYPES,
        ).fetchone()
        if active:
            return int(active["id"]), False

        if retry_of_job_id is not None:
            original = conn.execute(
                "SELECT type, status FROM jobs WHERE id = ?",
                (retry_of_job_id,),
            ).fetchone()
            if not original:
                raise ValueError("retry_of_job_id 指向的任务不存在")
            if original["status"] not in JOB_TERMINAL_STATUSES:
                raise ValueError("只能重试已经结束的任务")
            if original["type"] != DAILY_PIPELINE_JOB_TYPE:
                raise ValueError("只能重试历史每日情报流水线任务")

        cur = conn.execute(
            """
            INSERT INTO jobs(
                type, status, idempotency_key, retry_of_job_id, payload, created_at
            )
            VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (
                DAILY_PIPELINE_JOB_TYPE,
                normalized_key,
                retry_of_job_id,
                encoded_payload,
                now_iso(),
            ),
        )
        job_id = int(cur.lastrowid)
        plan_event = _normalize_job_event(
            job_id,
            f"pipeline:{job_id}:plan_created",
            "plan",
            "pipeline.plan_created",
            metrics={
                "trigger_source": trigger_source,
                "category_count": len(payload_data.get("categories") or []),
            },
            message="每日情报流水线计划已创建",
        )
        _insert_job_event(conn, plan_event)
        return job_id, True


def get_active_crawl_job() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE type IN ({','.join('?' for _ in CRAWL_JOB_TYPES)})
              AND status IN ('pending', 'running')
            ORDER BY created_at, id
            LIMIT 1
            """,
            CRAWL_JOB_TYPES,
        ).fetchone()
    item = row_to_dict(row)
    if item:
        item["payload_data"] = loads_json(item.get("payload"), {})
        hydrate_job_progress(item)
    return item


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    item = row_to_dict(row)
    if item:
        item["payload_data"] = loads_json(item.get("payload"), {})
        hydrate_job_progress(item)
    return item


def _normalize_job_event(
    job_id: int,
    event_key: str,
    stage: str,
    event_type: str,
    *,
    level: str = "info",
    category: str | None = None,
    crawl_date: str | None = None,
    paper_id: int | None = None,
    arxiv_id: str | None = None,
    attempt: int = 1,
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str = "",
) -> dict[str, Any]:
    normalized = {
        "job_id": int(job_id),
        "event_key": str(event_key or "").strip(),
        "stage": str(stage or "").strip(),
        "event_type": str(event_type or "").strip(),
        "level": str(level or "").strip(),
        "category": str(category).strip() if category is not None else None,
        "crawl_date": str(crawl_date).strip() if crawl_date is not None else None,
        "paper_id": int(paper_id) if paper_id is not None else None,
        "arxiv_id": str(arxiv_id).strip() if arxiv_id is not None else None,
        "attempt": int(attempt),
        "metrics": dict(metrics or {}),
        "error_code": str(error_code).strip() if error_code is not None else None,
        "message": str(message or ""),
    }
    if normalized["job_id"] <= 0:
        raise ValueError("job_id 必须为正整数")
    for field in ("event_key", "stage", "event_type"):
        if not normalized[field]:
            raise ValueError(f"{field} 不能为空")
    if normalized["level"] not in JOB_EVENT_LEVELS:
        raise ValueError(f"未知事件级别: {normalized['level']}")
    if normalized["attempt"] < 1:
        raise ValueError("attempt 必须大于等于 1")
    return normalized


def _job_event_comparable(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "job_id": int(item["job_id"]),
        "event_key": str(item["event_key"]),
        "stage": str(item["stage"]),
        "event_type": str(item["event_type"]),
        "level": str(item["level"]),
        "category": item.get("category"),
        "crawl_date": item.get("crawl_date"),
        "paper_id": item.get("paper_id"),
        "arxiv_id": item.get("arxiv_id"),
        "attempt": int(item["attempt"]),
        "metrics": loads_json_object(item.get("metrics_json")),
        "error_code": item.get("error_code"),
        "message": str(item.get("message") or ""),
    }


def _insert_job_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
) -> tuple[int, bool]:
    existing = conn.execute(
        "SELECT * FROM job_events WHERE event_key = ?",
        (event["event_key"],),
    ).fetchone()
    if existing:
        if _job_event_comparable(existing) != event:
            raise ValueError("event_key 已用于不同的结构化事件")
        return int(existing["id"]), False
    cur = conn.execute(
        """
        INSERT INTO job_events(
            event_key, job_id, stage, event_type, level, category, crawl_date,
            paper_id, arxiv_id, attempt, metrics_json, error_code, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_key"],
            event["job_id"],
            event["stage"],
            event["event_type"],
            event["level"],
            event["category"],
            event["crawl_date"],
            event["paper_id"],
            event["arxiv_id"],
            event["attempt"],
            json.dumps(
                event["metrics"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            event["error_code"],
            event["message"],
            utc_now_iso(),
        ),
    )
    return int(cur.lastrowid), True


def append_job_event(
    job_id: int,
    event_key: str,
    stage: str,
    event_type: str,
    *,
    level: str = "info",
    category: str | None = None,
    crawl_date: str | None = None,
    paper_id: int | None = None,
    arxiv_id: str | None = None,
    attempt: int = 1,
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str = "",
) -> tuple[int, bool]:
    event = _normalize_job_event(
        job_id,
        event_key,
        stage,
        event_type,
        level=level,
        category=category,
        crawl_date=crawl_date,
        paper_id=paper_id,
        arxiv_id=arxiv_id,
        attempt=attempt,
        metrics=metrics,
        error_code=error_code,
        message=message,
    )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _insert_job_event(conn, event)


def _hydrate_job_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["metrics"] = loads_json_object(item.get("metrics_json"))
    return item


def list_job_events(
    job_id: int,
    *,
    after_id: int = 0,
    stage: str | None = None,
    event_type: str | None = None,
    level: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    limit = max(0, min(int(limit), 5000))
    if limit == 0:
        return []
    clauses = ["job_id = ?", "id > ?"]
    params: list[Any] = [int(job_id), max(0, int(after_id))]
    for column, value in (("stage", stage), ("event_type", event_type), ("level", level)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_hydrate_job_event(row) for row in rows]


def job_observability_batch(job_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Read-only, bounded-query task summaries; never load per-paper event bodies."""
    ids = _unique_ints(job_ids)
    if not ids:
        return {}
    keys = ('parsed_count', 'persisted_count', 'new_count', 'updated_count', 'duplicate_count',
            'failed_count', 'retry_count', 'candidate_count', 'unique_count', 'terminal_failed')
    projections = ', '.join(
        f"SUM(COALESCE(json_extract(metrics_json, '$.{('terminal_failure' if key == 'terminal_failed' else key)}'),0)) AS {key}"
        for key in keys
    )
    results = {}
    with connect() as conn:
        conn.execute('BEGIN')
        for chunk in _chunks(ids):
            placeholders = ','.join('?' for _ in chunk)
            for row in conn.execute(f'''SELECT j.id,j.payload,j.progress_details_json,e.metrics_json AS classification_json
                FROM jobs j LEFT JOIN job_events e ON e.job_id=j.id AND e.event_type='classification.stage_completed'
                WHERE j.id IN ({placeholders})''', chunk):
                results[row['id']] = {'plan': loads_json_object(row['payload']),
                                      'classification': loads_json_object(row['classification_json']),
                                      'retained': loads_json_object(row['progress_details_json']), 'groups': [], 'last': None}
            for row in conn.execute(f"""SELECT job_id,event_type,
                    COALESCE(json_extract(metrics_json,'$.skip_reason'),json_extract(metrics_json,'$.final_status'),json_extract(metrics_json,'$.status'),'') AS outcome,
                    COUNT(*) AS count, {projections}
                FROM job_events WHERE job_id IN ({placeholders})
                GROUP BY job_id,event_type,outcome""", chunk):
                results[row['job_id']]['groups'].append(dict(row))
            for row in conn.execute(f"""SELECT e.job_id,e.stage,e.event_type,e.created_at
                FROM job_events e JOIN (SELECT job_id,MAX(id) AS id FROM job_events
                    WHERE job_id IN ({placeholders}) GROUP BY job_id) latest ON latest.id=e.id""", chunk):
                results[row['job_id']]['last'] = dict(row)
    return results


def list_job_event_page(
    job_id: int, *, severity: str = 'issues', category: str = '', crawl_date: str = '',
    stage: str = '', view: str = 'grouped', page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    clauses, params = ['e.job_id = ?'], [int(job_id)]
    if severity == 'issues':
        clauses.append("e.level IN ('warning','error')")
    elif severity in {'warning', 'error'}:
        clauses.append('e.level = ?')
        params.append(severity)
    elif severity != 'all':
        raise ValueError('无效的事件级别')
    for column, value in (('category', category), ('crawl_date', crawl_date), ('stage', stage)):
        if value:
            clauses.append(f'e.{column} = ?')
            params.append(value)
    where = ' AND '.join(clauses)
    order = """COALESCE(e.crawl_date,''),COALESCE(e.category,''),
        CASE e.stage WHEN 'plan' THEN 0 WHEN 'crawl_http' THEN 1 WHEN 'crawl_parse' THEN 2
        WHEN 'persist' THEN 3 WHEN 'direction_backfill' THEN 4 WHEN 'classification' THEN 5 WHEN 'abstract_plan' THEN 6 WHEN 'abstract_eval' THEN 7
        WHEN 'investment_memo' THEN 8 WHEN 'finalize' THEN 9 ELSE 10 END,e.id""" if view == 'grouped' else 'e.id'
    page_size = min(100, max(1, int(page_size)))
    with connect() as conn:
        conn.execute('BEGIN')
        total = conn.execute(f'SELECT COUNT(*) FROM job_events e WHERE {where}', params).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(pages, max(1, int(page)))
        rows = conn.execute(f"""SELECT e.*,p.title AS paper_title,p.id AS existing_paper_id
            FROM job_events e LEFT JOIN papers p ON p.id=e.paper_id
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", [*params, page_size, (page-1)*page_size]).fetchall()
    return {'items': [_hydrate_job_event(row) for row in rows], 'page': page, 'pages': pages,
            'total': total, 'page_size': page_size}


def list_all_job_events(job_id: int, event_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after_id = 0
    while True:
        page = list_job_events(job_id, event_type=event_type, after_id=after_id, limit=5000)
        events.extend(page)
        if len(page) < 5000:
            return events
        after_id = page[-1]['id']


def pipeline_retry_units(job_id: int) -> list[dict[str, Any]]:
    """Read the retention marker and all unit outcomes from one SQLite snapshot."""
    with connect() as conn:
        rows = conn.execute("""SELECT * FROM job_events WHERE job_id=?
            AND event_type IN ('pipeline.plan_created','crawl.category_completed') ORDER BY id""",
            (job_id,)).fetchall()
    if not any(row['event_type'] == 'pipeline.plan_created' for row in rows):
        raise ValueError('原任务事件已清理或不完整，无法安全规划重试；请使用新的抓取或评估缺失摘要入口')
    return [_hydrate_job_event(row) for row in rows if row['event_type'] == 'crawl.category_completed']


def count_job_events(
    job_id: int,
    *,
    stage: str | None = None,
    event_type: str | None = None,
    level: str | None = None,
) -> int:
    clauses = ["job_id = ?"]
    params: list[Any] = [int(job_id)]
    for column, value in (("stage", stage), ("event_type", event_type), ("level", level)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM job_events WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return int(row["count"] if row else 0)


def delete_expired_job_events(
    retention_days: int | None = None, *, current_time: datetime | None = None
) -> int:
    """Prune terminal-job diagnostics; active jobs keep their full event trail."""
    days = get_int_setting("job_events.retention_days", 30) if retention_days is None else int(retention_days)
    if days < 0:
        return 0
    current = current_time or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current.astimezone(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    with connect() as conn:
        cursor = conn.execute(
            """DELETE FROM job_events WHERE created_at < ?
               AND job_id NOT IN (SELECT id FROM jobs WHERE status IN ('pending', 'running'))""",
            (cutoff,),
        )
        return max(0, cursor.rowcount)


def _update_job_in_connection(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    error_message: str | None = None,
) -> bool:
    if status not in JOB_STATUSES:
        raise ValueError(f"未知任务状态: {status}")
    current = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not current:
        return False
    current_status = str(current["status"])
    if current_status == status:
        return False
    allowed = JOB_STATUS_TRANSITIONS.get(current_status, frozenset())
    if status not in allowed:
        raise ValueError(f"不允许的任务状态转换: {current_status} -> {status}")

    fields = ["status = ?"]
    params: list[Any] = [status]
    if status == "running":
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(now_iso())
    if status in JOB_TERMINAL_STATUSES:
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
    conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", params)
    return True


def update_job(job_id: int, status: str, error_message: str | None = None) -> None:
    with connect() as conn:
        _update_job_in_connection(conn, job_id, status, error_message)


def update_job_with_event(
    job_id: int,
    status: str,
    *,
    event_key: str,
    stage: str,
    event_type: str,
    level: str = "info",
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str = "",
    error_message: str | None = None,
) -> tuple[int, bool]:
    event = _normalize_job_event(
        job_id,
        event_key,
        stage,
        event_type,
        level=level,
        metrics=metrics,
        error_code=error_code,
        message=message,
    )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _update_job_in_connection(conn, job_id, status, error_message)
        return _insert_job_event(conn, event)


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
    params: list[Any] = []
    where = "status = 'pending'"
    if active_job_ids:
        placeholders = ",".join("?" for _ in active_job_ids)
        where += f" AND id NOT IN ({placeholders})"
        params.extend(sorted(active_job_ids))
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id, type, status FROM jobs WHERE {where}",
            params,
        ).fetchall()
        if not rows:
            return 0
        job_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(
            f"""
            UPDATE jobs
            SET status = 'interrupted',
                error_message = COALESCE(error_message, ?),
                finished_at = COALESCE(finished_at, ?)
            WHERE id IN ({placeholders})
            """,
            (message, now_iso(), *job_ids),
        )
        for row in rows:
            if row["type"] == DAILY_PIPELINE_JOB_TYPE:
                event = _normalize_job_event(
                    int(row["id"]),
                    f"pipeline:{row['id']}:interrupted",
                    "finalize",
                    "pipeline.interrupted",
                    level="warning",
                    metrics={"previous_status": row["status"]},
                    error_code="pipeline_interrupted",
                    message=message,
                )
                _insert_job_event(conn, event)
        from .memo_db import recover_versions
        recover_versions(conn,job_ids)
        return len(rows)


def mark_unfinished_jobs_interrupted() -> int:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        from .memo_db import recover_versions
        memo_interrupted = recover_versions(conn)
        for evaluation in conn.execute("SELECT * FROM evaluations WHERE evaluation_type='direction_classification' AND status='running'").fetchall():
            conn.execute("UPDATE evaluations SET status='failed',error_code='external_outcome_unknown',error_message='服务中断，重试可能产生额外费用',error_retryable=1 WHERE id=?", (evaluation['id'],))
            _write_direction_models(conn,evaluation,[{'direction_id':d['id'],'decision':'failed','reason':'external_outcome_unknown'}
                                                    for d in loads_json(evaluation['direction_snapshot_json'],[])])
            if evaluation['pipeline_job_id']:
                _insert_job_event(conn, _normalize_job_event(evaluation['pipeline_job_id'],
                    f"classification:{evaluation['pipeline_job_id']}:{evaluation['paper_id']}:terminal", 'classification',
                    'classification.paper_failed', level='warning', paper_id=evaluation['paper_id'],
                    error_code='external_outcome_unknown', metrics={'failed':1,'evaluation_id':evaluation['id']},
                    message='分类调用中断；显式重试可能产生额外费用'))
        # A retryable failed attempt may be sleeping between requests. The claim,
        # not only a running evaluation, defines an unfinished logical task.
        for claim in conn.execute('SELECT * FROM classification_claims').fetchall():
            evaluation = conn.execute("SELECT * FROM evaluations WHERE paper_id=? AND pipeline_job_id=? AND evaluation_type='direction_classification' ORDER BY id DESC LIMIT 1",
                                      (claim['paper_id'],claim['job_id'])).fetchone()
            if evaluation is None:
                job = conn.execute('SELECT payload FROM jobs WHERE id=?',(claim['job_id'],)).fetchone()
                plan = loads_json(job['payload'],{}) if job else {}
                cur = conn.execute('''INSERT INTO evaluations(paper_id,pipeline_job_id,evaluation_type,status,error_code,
                    classification_source,direction_snapshot_json,attempt,created_at)
                    VALUES (?,?,'direction_classification','failed','pipeline_interrupted',?,?,0,?)''',
                    (claim['paper_id'],claim['job_id'],'historical_backfill' if plan.get('trigger_source')=='historical_backfill' else 'daily',
                     json.dumps(plan.get('directions',[]),ensure_ascii=False),now_iso()))
                evaluation = conn.execute('SELECT * FROM evaluations WHERE id=?',(cur.lastrowid,)).fetchone()
            succeeded = evaluation['status'] == 'success'
            if not succeeded:
                _write_direction_models(conn,evaluation,[{'direction_id':d['id'],'decision':'failed','reason':evaluation['error_code'] or 'pipeline_interrupted'}
                                                        for d in loads_json(evaluation['direction_snapshot_json'],[])])
            event_key = f"classification:{claim['job_id']}:{claim['paper_id']}:terminal"
            if not conn.execute('SELECT 1 FROM job_events WHERE event_key=?',(event_key,)).fetchone():
                _insert_job_event(conn,_normalize_job_event(claim['job_id'],event_key,
                    'classification','classification.paper_succeeded' if succeeded else 'classification.paper_failed',
                    paper_id=claim['paper_id'],level='info' if succeeded else 'warning',
                    error_code=None if succeeded else 'pipeline_interrupted',metrics={'success' if succeeded else 'failed':1,'evaluation_id':evaluation['id'],'recovered':True},
                    message='服务重启，从分类尝试恢复逻辑任务终态'))
        conn.execute('DELETE FROM classification_claims')
        claims = conn.execute("SELECT * FROM evaluation_claims").fetchall()
        for claim in claims:
            known = conn.execute(
                "SELECT id,status FROM evaluations WHERE paper_id=? AND evaluation_type=? AND id>? ORDER BY id DESC LIMIT 1",
                (claim['paper_id'], claim['evaluation_type'], claim['last_evaluation_id']),
            ).fetchone()
            if claim['provider_started'] and not known:
                conn.execute(
                    """INSERT INTO evaluations(paper_id,evaluation_type,pipeline_job_id,status,error_code,error_retryable,error_message,created_at)
                       VALUES (?, ?, ?, 'failed', 'external_outcome_unknown', 1, ?, ?)""",
                    (claim['paper_id'], claim['evaluation_type'], claim['pipeline_job_id'],
                     '服务中断，外部调用结果未知；重试可能产生额外费用', now_iso()),
                )
                if claim['pipeline_job_id']:
                    _insert_job_event(conn, _normalize_job_event(
                        claim['pipeline_job_id'], f"abstract:{claim['pipeline_job_id']}:{claim['paper_id']}:terminal",
                        'abstract_eval', 'abstract.paper_failed', level='warning', paper_id=claim['paper_id'],
                        error_code='external_outcome_unknown', metrics={'status': 'failed'},
                        message='外部调用结果未知，重试可能产生额外费用',
                    ))
            elif claim['pipeline_job_id']:
                event_key = f"abstract:{claim['pipeline_job_id']}:{claim['paper_id']}:terminal"
                if not conn.execute('SELECT 1 FROM job_events WHERE event_key=?', (event_key,)).fetchone():
                    succeeded = known and known['status'] == 'success'
                    _insert_job_event(conn, _normalize_job_event(
                        claim['pipeline_job_id'], event_key, 'abstract_eval',
                        'abstract.paper_succeeded' if succeeded else 'abstract.paper_failed',
                        paper_id=claim['paper_id'], level='info' if succeeded else 'warning',
                        metrics={'status': 'success' if succeeded else 'failed',
                                 'evaluation_id': known['id'] if known else None, 'recovered': True},
                        error_code=None if succeeded else 'pipeline_interrupted',
                        message='服务重启，从已持久化记录恢复摘要终态',
                    ))
        conn.execute("DELETE FROM evaluation_claims")
        rows = conn.execute(
            "SELECT id, type, status FROM jobs WHERE status IN ('pending', 'running')"
        ).fetchall()
        if not rows:
            return memo_interrupted
        job_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in job_ids)
        message = "服务重启，任务中断"
        conn.execute(
            f"""
            UPDATE jobs
            SET status = 'interrupted',
                error_message = COALESCE(error_message, ?),
                finished_at = COALESCE(finished_at, ?)
            WHERE id IN ({placeholders})
            """,
            (message, now_iso(), *job_ids),
        )
        for row in rows:
            if row["type"] == DAILY_PIPELINE_JOB_TYPE:
                plan = conn.execute("SELECT metrics_json FROM job_events WHERE job_id=? AND event_type='abstract.plan_created' LIMIT 1", (row['id'],)).fetchone()
                for paper_id in loads_json(plan['metrics_json'], {}).get('paper_ids', []) if plan else []:
                    event_key = f"abstract:{row['id']}:{paper_id}:terminal"
                    if not conn.execute('SELECT 1 FROM job_events WHERE event_key=?', (event_key,)).fetchone():
                        _insert_job_event(conn, _normalize_job_event(
                            row['id'], event_key, 'abstract_eval', 'abstract.paper_failed', paper_id=paper_id,
                            level='warning', metrics={'status': 'failed'}, error_code='pipeline_interrupted',
                            message='摘要评估阶段中断，尚未形成终态',
                        ))
                event = _normalize_job_event(
                    int(row["id"]),
                    f"pipeline:{row['id']}:interrupted",
                    "finalize",
                    "pipeline.interrupted",
                    level="warning",
                    metrics={"previous_status": row["status"]},
                    error_code="pipeline_interrupted",
                    message=message,
                )
                _insert_job_event(conn, event)
        return len(rows)+memo_interrupted


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


JOB_PROGRESS_COLUMNS = """
    id, type, status, idempotency_key, retry_of_job_id,
    progress_current, progress_total, progress_message,
    progress_details_json, error_message, started_at, finished_at, created_at
"""


def list_job_summaries(limit: int = 80, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
    limit = max(0, int(limit))
    if limit == 0:
        return []
    params: list[Any] = []
    where = ""
    if statuses:
        status_list = list(statuses)
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        where = f"WHERE status IN ({placeholders})"
        params.extend(status_list)
    params.append(limit)
    with connect() as conn:
        jobs = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT {JOB_PROGRESS_COLUMNS}
                FROM jobs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        ]
    return [hydrate_job_progress(job) for job in jobs]


def list_active_job_progress(limit: int = 12) -> list[dict[str, Any]]:
    return list_job_summaries(limit, statuses=("pending", "running"))


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
