"""Connection-scoped cache metadata. No filesystem, connections or import side effects."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re

RETENTION_DEFAULTS = {
    'cache.ordinary_pdf_retention_days': 7,
    'cache.ordinary_markdown_retention_days': 30,
    'cache.core_pdf_retention_days': 30,
    'cache.core_markdown_retention_days': 180,
}
MIGRATION = 'tiered_cache_v1'


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def init_schema(conn):
    conn.execute('SAVEPOINT tiered_cache')
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS paper_cache_artifacts (
            arxiv_id TEXT NOT NULL,
            artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('pdf','markdown')),
            paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
            source_version TEXT NOT NULL DEFAULT 'unknown',
            generated_at TEXT NOT NULL,
            last_used_at TEXT,
            migration_grace_at TEXT,
            tier_promoted_at TEXT,
            last_known_size_bytes INTEGER NOT NULL CHECK(last_known_size_bytes>=0),
            PRIMARY KEY(arxiv_id,artifact_kind)
        )''')
        conn.execute("UPDATE paper_cache_artifacts SET source_version='unknown' WHERE source_version IS NULL")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cache_used ON paper_cache_artifacts(artifact_kind,last_used_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_memo_cached_paper ON investment_memo_version_papers(paper_id,memo_version_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_memo_cached_source ON investment_memo_version_papers(paper_arxiv_id_snapshot,memo_version_id)')
        for key, value in RETENTION_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)',
                         (key,json.dumps(value),timestamp(utc_now())))
        conn.execute('INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)',
                     (MIGRATION,timestamp(utc_now())))
        conn.execute('RELEASE tiered_cache')
    except BaseException:
        conn.execute('ROLLBACK TO tiered_cache')
        conn.execute('RELEASE tiered_cache')
        raise


def require_schema(conn):
    if not conn.execute('SELECT 1 FROM schema_migrations WHERE name=?',(MIGRATION,)).fetchone():
        raise RuntimeError('缓存分层迁移未完成，停止清理')


def core_reasons(conn, arxiv_ids):
    """One bounded UNION query per 100 keys; no evaluation JSON or snapshots loaded."""
    keys = list(dict.fromkeys(arxiv_ids))
    result = {key:[] for key in keys}
    for offset in range(0,len(keys),100):
        batch = keys[offset:offset+100]
        marks = ','.join('?' for _ in batch)
        sql = f'''SELECT p.arxiv_id,'favorite' AS reason FROM papers p
            CROSS JOIN paper_dispositions d ON d.paper_id=p.id AND d.decision='favorite'
            WHERE p.arxiv_id IN ({marks})
            UNION SELECT p.arxiv_id,'theme_relation' FROM papers p
            CROSS JOIN paper_investment_themes t ON t.paper_id=p.id WHERE p.arxiv_id IN ({marks})
            UNION SELECT p.arxiv_id,'team_tracking' FROM papers p
            CROSS JOIN paper_team_tracking t ON t.paper_id=p.id AND t.status='tracking'
            WHERE p.arxiv_id IN ({marks})
            UNION SELECT p.arxiv_id,'successful_memo_reference'
            FROM papers p
            CROSS JOIN investment_memo_version_papers vp ON vp.paper_id=p.id
            CROSS JOIN investment_memo_versions v ON v.id=vp.memo_version_id AND v.status='success'
            WHERE p.arxiv_id IN ({marks})
            UNION SELECT vp.paper_arxiv_id_snapshot,'successful_memo_reference'
            FROM investment_memo_version_papers vp
            CROSS JOIN investment_memo_versions v ON v.id=vp.memo_version_id AND v.status='success'
            WHERE vp.paper_arxiv_id_snapshot IN ({marks})'''
        for row in conn.execute(sql,batch*5):
            if row[0] in result:
                result[row[0]].append(row[1])
    return result


@contextmanager
def track_tier_change(conn, paper_ids):
    ids = list(dict.fromkeys(paper_ids))
    keys = []
    for offset in range(0,len(ids),100):
        batch=ids[offset:offset+100]
        keys.extend(row[0] for row in conn.execute(
            'SELECT arxiv_id FROM papers WHERE id IN ('+','.join('?' for _ in batch)+')',batch))
    before = core_reasons(conn,keys)
    yield
    after = core_reasons(conn,keys)
    now = timestamp(utc_now())
    for key in keys:
        if not before[key] and after[key]:
            conn.execute('UPDATE paper_cache_artifacts SET tier_promoted_at=? WHERE arxiv_id=?',(now,key))


def register_artifact(conn, arxiv_id, kind, size, *, generated_at, used=False, generated=False, now=None):
    require_schema(conn)
    paper = conn.execute('SELECT id FROM papers WHERE arxiv_id=?',(arxiv_id,)).fetchone()
    if paper is None:
        return False
    now = now or utc_now()
    stamp = timestamp(now)
    version = re.search(r'v(\d+)$',arxiv_id)
    if generated:
        conn.execute("""INSERT INTO paper_cache_artifacts
            (arxiv_id,artifact_kind,paper_id,source_version,generated_at,last_used_at,last_known_size_bytes)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(arxiv_id,artifact_kind) DO UPDATE SET
            paper_id=excluded.paper_id,source_version=excluded.source_version,
            generated_at=excluded.generated_at,last_used_at=excluded.last_used_at,
            migration_grace_at=NULL,last_known_size_bytes=excluded.last_known_size_bytes""",
            (arxiv_id,kind,paper[0],version.group(1) if version else 'unknown',stamp,stamp,size))
    elif not used:
        conn.execute('''INSERT OR IGNORE INTO paper_cache_artifacts
            (arxiv_id,artifact_kind,paper_id,source_version,generated_at,migration_grace_at,last_known_size_bytes)
            VALUES(?,?,?,?,?,?,?)''',
            (arxiv_id,kind,paper[0],version.group(1) if version else 'unknown',generated_at,stamp,size))
    else:
        conn.execute('''INSERT INTO paper_cache_artifacts
            (arxiv_id,artifact_kind,paper_id,source_version,generated_at,last_used_at,last_known_size_bytes)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(arxiv_id,artifact_kind) DO UPDATE SET
            paper_id=excluded.paper_id, last_used_at=excluded.last_used_at,
            last_known_size_bytes=excluded.last_known_size_bytes
            WHERE paper_cache_artifacts.last_used_at IS NULL
            OR julianday(excluded.last_used_at)-julianday(paper_cache_artifacts.last_used_at)>=1.0/1440''',
            (arxiv_id,kind,paper[0],version.group(1) if version else 'unknown',generated_at,stamp,size))
    return True


def retention_settings(conn):
    values={}
    for key,encoded in conn.execute('SELECT key,value FROM settings WHERE key IN ('+','.join('?' for _ in RETENTION_DEFAULTS)+')',list(RETENTION_DEFAULTS)):
        value=json.loads(encoded)
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=3650:
            raise ValueError('缓存期限配置无效，停止清理')
        values[key]=value
    if values.keys() != RETENTION_DEFAULTS.keys():
        raise ValueError('缓存期限配置缺失，停止清理')
    for kind in ('pdf','markdown'):
        if values[f'cache.core_{kind}_retention_days']<values[f'cache.ordinary_{kind}_retention_days']:
            raise ValueError('核心缓存期限不得短于普通缓存')
    return values


def expiry(row, core, days):
    from datetime import timedelta
    starts = [parse_time(row['last_used_at'] or row['generated_at'])]
    if row['migration_grace_at']:
        starts.append(parse_time(row['migration_grace_at']))
    if core and row['tier_promoted_at']:
        starts.append(parse_time(row['tier_promoted_at']))
    return max(starts)+timedelta(days=days)
