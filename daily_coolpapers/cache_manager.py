import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse

import httpx

from .config import MARKDOWN_CACHE_DIR, PDF_CACHE_DIR, ensure_directories
from .db import delete_expired_job_events, get_bool_setting, get_int_setting, get_setting
from . import db, cache_db
from .network import httpx_proxy_kwargs

logger = logging.getLogger(__name__)

ALLOWED_PDF_DOMAINS = {"arxiv.org", "export.arxiv.org"}
MAX_PDF_REDIRECTS = 5
_cache_locks_guard = threading.Lock()
_cache_locks: dict[str, threading.RLock] = {}


def safe_arxiv_filename(arxiv_id: str, suffix: str) -> str:
    return arxiv_id.replace("/", "_").replace("\\", "_") + suffix


def pdf_path(arxiv_id: str) -> Path:
    ensure_directories()
    return PDF_CACHE_DIR / safe_arxiv_filename(arxiv_id, ".pdf")


def markdown_path(arxiv_id: str) -> Path:
    ensure_directories()
    return MARKDOWN_CACHE_DIR / safe_arxiv_filename(arxiv_id, ".md")


def has_pdf(arxiv_id: str) -> bool:
    return _is_valid_pdf(pdf_path(arxiv_id))


def has_markdown(arxiv_id: str) -> bool:
    path = markdown_path(arxiv_id)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


@contextmanager
def cache_lock(arxiv_id: str) -> Iterator[None]:
    key = safe_arxiv_filename(arxiv_id, "")
    with _cache_locks_guard:
        lock = _cache_locks.setdefault(key, threading.RLock())
    with lock:
        yield


def touch(path: Path) -> None:
    if path.exists():
        try:
            os.utime(path, None)
        except OSError:
            logger.debug("Failed touching cache file %s", path, exc_info=True)


def download_pdf(arxiv_id: str, url: str, timeout_seconds: int = 120, retries: int = 2) -> Path:
    _validate_pdf_url(url)
    with cache_lock(arxiv_id):
        path = pdf_path(arxiv_id)
        if _is_valid_pdf(path):
            logger.info("Using cached PDF %s", path)
            return path
        if path.exists():
            logger.warning("Cached PDF is missing or incomplete, preserving until replacement: %s", path)

        last_error: Exception | None = None
        with httpx.Client(**_pdf_client_kwargs(timeout_seconds)) as client:
            for attempt in range(max(0, retries) + 1):
                tmp = _temporary_path(path.parent, f".{path.name}.")
                downloaded = False
                try:
                    logger.info("Downloading PDF %s -> %s attempt=%s", url, path, attempt + 1)
                    _download_pdf_to_path(url, tmp, client)
                    if not _is_valid_pdf(tmp):
                        raise RuntimeError("PDF download result is incomplete or invalid")
                    _replace_with_retries(tmp, path)
                    downloaded = True
                except Exception as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    delay = min(8.0, 1.5 * (attempt + 1))
                    logger.warning("PDF download failed for %s attempt=%s error=%s", arxiv_id, attempt + 1, exc)
                    time.sleep(delay)
                finally:
                    _safe_unlink(tmp)
                if downloaded:
                    # A registry failure must not repeat a successful HTTP download.
                    record_cache_use(arxiv_id, "pdf", path, generated=True)
                    return path
        raise RuntimeError(f"PDF 下载失败 {arxiv_id}: {last_error}") from last_error


def _pdf_client_kwargs(timeout_seconds: int) -> dict[str, object]:
    client_kwargs = {
        "timeout": timeout_seconds,
        "follow_redirects": False,
    }
    client_kwargs.update(
        httpx_proxy_kwargs(
            explicit_proxy_url=str(get_setting("crawler.proxy_url", "") or ""),
            use_system_proxy=get_bool_setting("crawler.trust_env_proxy", False),
        )
    )
    return client_kwargs


def _download_pdf_to_path(url: str, path: Path, client: httpx.Client) -> None:
    current_url = _validate_pdf_url(url)
    for redirect_count in range(MAX_PDF_REDIRECTS + 1):
        with client.stream("GET", current_url, headers={"User-Agent": "DailyCoolPapers/0.1"}) as response:
            response_url = _validate_pdf_url(str(getattr(response, "url", current_url)))
            status_code = int(getattr(response, "status_code", 0))
            if 300 <= status_code < 400:
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError("PDF 重定向缺少 Location")
                if redirect_count >= MAX_PDF_REDIRECTS:
                    raise RuntimeError("PDF 重定向次数过多")
                current_url = _validate_pdf_url(urljoin(response_url, location))
                continue

            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not response_url.lower().endswith(".pdf"):
                logger.warning("PDF response has unexpected content-type: %s", content_type)
            with path.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if path.stat().st_size <= 0:
                raise RuntimeError("PDF 下载结果为空")
            return
    raise RuntimeError("PDF 重定向次数过多")


def _validate_pdf_url(url: str) -> str:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PDF URL 端口无效") from exc
    domain = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise ValueError("PDF URL 必须使用 HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("PDF URL 不允许包含认证信息")
    if domain not in ALLOWED_PDF_DOMAINS or port not in {None, 443}:
        raise ValueError(f"不允许下载非可信 PDF 地址: {domain}")
    return url


def _temporary_path(directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    os.close(descriptor)
    return Path(name)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = _temporary_path(path.parent, f".{path.name}.")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retries(tmp, path)
    finally:
        _safe_unlink(tmp)


def _is_valid_pdf(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as handle:
            head = handle.read(5)
            if head != b"%PDF-":
                return False
            handle.seek(max(0, path.stat().st_size - 65536))
            tail = handle.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def _replace_with_retries(tmp: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    raise last_error or RuntimeError(f"无法保存 PDF: {path}")


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Failed removing temporary cache file %s", path)


def _owned_workspace(path: Path, kind: str) -> bool:
    # Standalone converters/tests must never open the application's real database.
    directory = 'pdf' if kind == 'pdf' else 'markdown'
    expected = db.DB_PATH.parent.parent / 'cache' / directory
    return (db.DB_PATH.is_file() and not path.is_symlink()
            and path.absolute().parent == expected.resolve()
            and path.resolve().parent == expected.resolve())


def _workspace_directory(directory: Path, kind: str) -> bool:
    expected = db.DB_PATH.parent.parent / 'cache' / ('pdf' if kind == 'pdf' else 'markdown')
    return db.DB_PATH.is_file() and directory.absolute() == expected.resolve()


def record_cache_use(arxiv_id: str, kind: str, path: Path, *, generated=False) -> None:
    if not _owned_workspace(path, kind):
        return
    with cache_lock(arxiv_id), db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        stat = path.stat()
        cache_db.register_artifact(conn, arxiv_id, kind, stat.st_size,
            generated_at=cache_db.timestamp(datetime.fromtimestamp(stat.st_mtime, cache_db.timezone.utc)),
            used=True, generated=generated)


def read_cached_markdown(arxiv_id: str, path: Path | None = None) -> str:
    with cache_lock(arxiv_id):
        path = path or markdown_path(arxiv_id)
        text = path.read_text(encoding='utf-8')
        record_cache_use(arxiv_id, 'markdown', path)
        return text


def register_legacy_caches() -> int:
    """One-time grace for known, valid files; never infer expiry from mtime."""
    registered = 0
    now = cache_db.utc_now()
    for kind, directory, suffix in [('pdf', PDF_CACHE_DIR, '.pdf'), ('markdown', MARKDOWN_CACHE_DIR, '.md')]:
        if not _workspace_directory(directory, kind):
            continue
        paths = directory.glob('*' + suffix)
        batch = []
        def flush(items):
            nonlocal registered
            if not items:
                return
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cache_db.require_schema(conn)
                marks = ','.join('?' for _ in items)
                keys = [item[0] for item in items]
                known = dict(conn.execute('SELECT arxiv_id,id FROM papers WHERE arxiv_id IN (' + marks + ')', keys))
                before = conn.total_changes
                conn.executemany("""INSERT OR IGNORE INTO paper_cache_artifacts
                    (arxiv_id,artifact_kind,paper_id,source_version,generated_at,migration_grace_at,last_known_size_bytes)
                    VALUES(?,?,?,'unknown',?,?,?)""", [(key,kind,known[key],
                        cache_db.timestamp(datetime.fromtimestamp(stat.st_mtime, cache_db.timezone.utc)),
                        cache_db.timestamp(now),stat.st_size) for key,path,stat in items if key in known])
                registered += conn.total_changes - before
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            if kind == 'pdf' and not _is_valid_pdf(path):
                continue
            stat = path.stat()
            if not stat.st_size:
                continue
            batch.append((path.stem.replace('_', '/'), path, stat))
            if len(batch) >= 100:
                flush(batch)
                batch = []
        flush(batch)
    return registered


def cache_status(arxiv_id: str) -> dict:
    result = {'tier': 'unknown', 'reasons': [], 'artifacts': []}
    if not _owned_workspace(PDF_CACHE_DIR / safe_arxiv_filename(arxiv_id, '.pdf'), 'pdf'):
        return result
    with db.connect_readonly() as conn:
        cache_db.require_schema(conn)
        settings = cache_db.retention_settings(conn)
        reasons = cache_db.core_reasons(conn, [arxiv_id])[arxiv_id]
        result.update(tier='core' if reasons else 'ordinary', reasons=reasons)
        for row in conn.execute('SELECT * FROM paper_cache_artifacts WHERE arxiv_id=?', (arxiv_id,)):
            kind = row['artifact_kind']
            path = (PDF_CACHE_DIR if kind == 'pdf' else MARKDOWN_CACHE_DIR) / safe_arxiv_filename(arxiv_id, '.pdf' if kind == 'pdf' else '.md')
            days = settings[f"cache.{result['tier']}_{kind}_retention_days"]
            result['artifacts'].append({'kind': kind, 'exists': path.is_file(), 'last_used_at': row['last_used_at'],
                'expires_at': cache_db.timestamp(cache_db.expiry(row, bool(reasons), days))})
    return result


def _expired_candidates(kind, directory, suffix, settings, now, result):
    batch = []
    def select(items):
        with db.connect_readonly() as conn:
            keys = [item[0] for item in items]
            reasons = cache_db.core_reasons(conn, keys)
            rows = {row['arxiv_id']: row for row in conn.execute(
                'SELECT * FROM paper_cache_artifacts WHERE artifact_kind=? AND arxiv_id IN (' + ','.join('?' for _ in keys) + ')', [kind] + keys)}
            selected = []
            for key,path,identity in items:
                row = rows.get(key)
                if row is None:
                    result['orphan_skipped'] += 1
                    continue
                core = bool(reasons[key])
                tier = 'core' if core else 'ordinary'
                if cache_db.expiry(row, core, settings[f'cache.{tier}_{kind}_retention_days']) <= now:
                    selected.append((key,path,identity))
                else:
                    result['protected_skipped'] += 1
            return selected
    if not _workspace_directory(directory, kind):
        return
    for path in directory.glob('*' + suffix):
        result['scanned'] += 1
        if path.is_symlink() or not path.is_file():
            result['skipped'] += 1
            continue
        stat = path.stat()
        if not stat.st_size or (kind == 'pdf' and not _is_valid_pdf(path)):
            result['invalid_skipped'] += 1
            continue
        batch.append((path.stem.replace('_', '/'),path,(stat.st_size,stat.st_mtime_ns,stat.st_ino)))
        if len(batch) == 100:
            yield from select(batch)
            batch = []
    if batch:
        yield from select(batch)


def _cleanup_temporary_files(kind, directory, suffix, now):
    deleted = 0
    for path in directory.glob('*.tmp'):
        if not _owned_workspace(path, kind):
            continue
        marker = suffix + '.'
        if not path.name.startswith('.') or marker not in path.name:
            continue  # Unknown naming/source stays untouched.
        key = path.name[1:].split(marker, 1)[0].replace('_', '/')
        with cache_lock(key):
            try:
                if (now.timestamp() - path.stat().st_mtime) >= 86400:
                    path.unlink()
                    deleted += 1
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning('Temporary cache cleanup failed for %s', path.name)
    return deleted


def _reconcile_missing_artifacts(settings, now) -> int:
    """Repair file/SQLite commit divergence on the next inventory, in bounded batches."""
    cutoff = cache_db.timestamp(now - timedelta(days=min(settings.values())))
    last_key = ('', '')
    removed = 0
    while True:
        with db.connect_readonly() as conn:
            rows = list(conn.execute("""SELECT arxiv_id,artifact_kind FROM paper_cache_artifacts
                WHERE (arxiv_id,artifact_kind) > (?,?)
                AND julianday(COALESCE(last_used_at,generated_at)) <= julianday(?)
                ORDER BY arxiv_id,artifact_kind LIMIT 100""", (*last_key,cutoff)))
        if not rows:
            return removed
        last_key = tuple(rows[-1])
        for row in rows:
            key,kind = row
            directory = PDF_CACHE_DIR if kind == 'pdf' else MARKDOWN_CACHE_DIR
            path = directory / safe_arxiv_filename(key, '.pdf' if kind == 'pdf' else '.md')
            if not _owned_workspace(path,kind) or path.exists():
                continue
            with cache_lock(key), db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cache_db.require_schema(conn)
                if not path.exists():
                    removed += conn.execute('DELETE FROM paper_cache_artifacts WHERE arxiv_id=? AND artifact_kind=?', (key,kind)).rowcount


def cleanup_caches(pdf_retention_days=None, markdown_retention_days=None) -> dict:
    result = dict(pdf_deleted=0, markdown_deleted=0, pdf_tmp_deleted=0, markdown_tmp_deleted=0,
                  job_events_deleted=0, deleted_bytes=0, ordinary_deleted=0, core_deleted=0,
                  ordinary_deleted_bytes=0, core_deleted_bytes=0, missing_records_repaired=0,
                  skipped=0, errors=0, scanned=0, protected_skipped=0, orphan_skipped=0, invalid_skipped=0)
    try:
        # All prerequisites are checked before any file is removed. Legacy files get grace.
        with db.connect_readonly() as conn:
            cache_db.require_schema(conn)
            settings = cache_db.retention_settings(conn)
            cache_db.core_reasons(conn, ['__schema_probe__'])
        for kind, override in [('pdf', pdf_retention_days), ('markdown', markdown_retention_days)]:
            if override is not None:
                if isinstance(override, bool) or not isinstance(override, int) or not 1 <= override <= settings[f'cache.core_{kind}_retention_days']:
                    raise ValueError('缓存期限无效')
                settings[f'cache.ordinary_{kind}_retention_days'] = override
        register_legacy_caches()
        now = cache_db.utc_now()
        result['missing_records_repaired'] = _reconcile_missing_artifacts(settings, now)
        for kind, directory, suffix in [('pdf', PDF_CACHE_DIR, '.pdf'), ('markdown', MARKDOWN_CACHE_DIR, '.md')]:
            for key, path, identity in _expired_candidates(kind, directory, suffix, settings, now, result):
                with cache_lock(key), db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    cache_db.require_schema(conn)
                    current_settings = cache_db.retention_settings(conn)
                    if (pdf_retention_days if kind == 'pdf' else markdown_retention_days) is not None:
                        current_settings[f'cache.ordinary_{kind}_retention_days'] = settings[f'cache.ordinary_{kind}_retention_days']
                    row = conn.execute('SELECT * FROM paper_cache_artifacts WHERE arxiv_id=? AND artifact_kind=?', (key, kind)).fetchone()
                    if row is None:
                        result['skipped'] += 1
                        continue
                    core = bool(cache_db.core_reasons(conn, [key])[key])
                    tier = 'core' if core else 'ordinary'
                    if cache_db.expiry(row, core, current_settings[f'cache.{tier}_{kind}_retention_days']) > now:
                        continue
                    if not _owned_workspace(path, kind):
                        result['skipped'] += 1
                        continue
                    try:
                        stat = path.stat()
                        if identity != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                            result['protected_skipped'] += 1
                            continue
                        size = stat.st_size
                        path.unlink()
                    except FileNotFoundError:
                        size = 0
                    except OSError:
                        result['errors'] += 1
                        continue
                    conn.execute('DELETE FROM paper_cache_artifacts WHERE arxiv_id=? AND artifact_kind=?', (key, kind))
                    result[kind + '_deleted'] += 1
                    result[tier + '_deleted'] += 1
                    result['deleted_bytes'] += size
                    result[tier + '_deleted_bytes'] += size
            result[kind + '_tmp_deleted'] = _cleanup_temporary_files(kind, directory, suffix, now)
        result['job_events_deleted'] = delete_expired_job_events()
    except Exception:
        result['errors'] += 1
        logger.exception('Cache cleanup stopped; remaining files preserved')
    logger.info('Cache cleanup finished: %s', result)
    return result


def cleanup_directory(directory: Path, pattern: str, retention_days: int) -> int:
    if retention_days < 0:
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified < cutoff:
            try:
                path.unlink()
                deleted += 1
                logger.info("Deleted expired cache file %s", path)
            except OSError:
                logger.exception("Failed deleting cache file %s", path)
    return deleted


def cache_usage() -> dict:
    """Read-only storage totals, deliberately separate DB from disposable artifacts."""
    usage = {'database_bytes': db.DB_PATH.stat().st_size if db.DB_PATH.is_file() else 0,
             'pdf_bytes': 0, 'markdown_bytes': 0, 'pdf_files': 0, 'markdown_files': 0}
    for kind, directory, suffix in [('pdf', PDF_CACHE_DIR, '.pdf'), ('markdown', MARKDOWN_CACHE_DIR, '.md')]:
        if not _workspace_directory(directory, kind):
            continue
        for path in directory.glob('*' + suffix):
            if path.is_symlink():
                continue
            try:
                if path.is_file():
                    usage[kind + '_bytes'] += path.stat().st_size
                    usage[kind + '_files'] += 1
            except FileNotFoundError:
                pass
    return usage
