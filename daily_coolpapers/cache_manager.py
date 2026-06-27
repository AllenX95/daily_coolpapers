import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import MARKDOWN_CACHE_DIR, PDF_CACHE_DIR, ensure_directories
from .db import get_bool_setting, get_int_setting, get_setting
from .network import httpx_proxy_kwargs

logger = logging.getLogger(__name__)

ALLOWED_PDF_DOMAINS = {"arxiv.org", "export.arxiv.org"}


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
    return markdown_path(arxiv_id).exists()


def touch(path: Path) -> None:
    if path.exists():
        try:
            os.utime(path, None)
        except OSError:
            logger.debug("Failed touching cache file %s", path, exc_info=True)


def download_pdf(arxiv_id: str, url: str, timeout_seconds: int = 120, retries: int = 2) -> Path:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain not in ALLOWED_PDF_DOMAINS:
        raise ValueError(f"不允许下载非可信 PDF 域名: {domain}")

    path = pdf_path(arxiv_id)
    if _is_valid_pdf(path):
        touch(path)
        logger.info("Using cached PDF %s", path)
        return path
    if path.exists():
        logger.warning("Cached PDF is missing or incomplete, overwriting: %s", path)

    last_error: Exception | None = None
    with httpx.Client(**_pdf_client_kwargs(timeout_seconds)) as client:
        for attempt in range(max(0, retries) + 1):
            try:
                logger.info("Downloading PDF %s -> %s attempt=%s", url, path, attempt + 1)
                _download_pdf_to_path(url, path, client)
                if not _is_valid_pdf(path):
                    raise RuntimeError("PDF download result is incomplete or invalid")
                return path
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    break
                delay = min(8.0, 1.5 * (attempt + 1))
                logger.warning("PDF download failed for %s attempt=%s error=%s", arxiv_id, attempt + 1, exc)
                time.sleep(delay)
    raise RuntimeError(f"PDF 下载失败 {arxiv_id}: {last_error}") from last_error


def _pdf_client_kwargs(timeout_seconds: int) -> dict[str, object]:
    client_kwargs = {
        "timeout": timeout_seconds,
        "follow_redirects": True,
    }
    client_kwargs.update(
        httpx_proxy_kwargs(
            explicit_proxy_url=str(get_setting("crawler.proxy_url", "") or ""),
            use_system_proxy=get_bool_setting("crawler.trust_env_proxy", False),
        )
    )
    return client_kwargs


def _download_pdf_to_path(url: str, path: Path, client: httpx.Client) -> None:
    with client.stream("GET", url, headers={"User-Agent": "DailyCoolPapers/0.1"}) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            logger.warning("PDF response has unexpected content-type: %s", content_type)
        with path.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
    if path.stat().st_size <= 0:
        raise RuntimeError("PDF 下载结果为空")


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


def cleanup_caches(
    pdf_retention_days: int | None = None,
    markdown_retention_days: int | None = None,
) -> dict[str, int]:
    ensure_directories()
    pdf_days = pdf_retention_days if pdf_retention_days is not None else get_int_setting("cache.pdf_retention_days", 5)
    md_days = (
        markdown_retention_days
        if markdown_retention_days is not None
        else get_int_setting("cache.markdown_retention_days", 7)
    )
    result = {
        "pdf_deleted": cleanup_directory(PDF_CACHE_DIR, "*.pdf", pdf_days),
        "pdf_tmp_deleted": cleanup_directory(PDF_CACHE_DIR, "*.tmp", 1),
        "markdown_deleted": cleanup_directory(MARKDOWN_CACHE_DIR, "*.md", md_days),
    }
    logger.info("Cache cleanup finished: %s", result)
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
