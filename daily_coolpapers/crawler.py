import hashlib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup, Tag

from .network import httpx_proxy_kwargs

logger = logging.getLogger(__name__)

PAPERS_COOL_BASE = "https://papers.cool"
ARXIV_ID_RE = re.compile(r"(?P<id>(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7})|(?:\d{4}\.\d{4,5})(?:v\d+)?)")
ARXIV_DISABLED_DATES = {
    date.fromisoformat(item)
    for item in [
        "2024-01-16",
        "2024-05-23",
        "2024-06-20",
        "2024-07-05",
        "2024-09-03",
        "2024-10-09",
        "2024-11-29",
        "2024-12-26",
        "2024-12-27",
        "2025-01-01",
        "2025-01-02",
        "2025-01-21",
        "2025-06-20",
        "2025-07-07",
        "2025-09-02",
        "2025-11-28",
        "2025-12-26",
        "2025-12-31",
        "2026-01-02",
        "2026-01-20",
        "2026-06-20",
        "2026-07-04",
        "2026-09-08",
        "2026-11-27",
        "2026-12-26",
        "2026-12-30",
        "2027-01-01",
    ]
}


@dataclass
class CrawledPaper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    subjects: list[str]
    published_at: str | None
    pdf_url: str | None
    abs_url: str | None
    papers_cool_url: str | None
    rank: int | None
    reading_stars: int
    pdf_clicks: int
    kimi_clicks: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ParsedPapersResult:
    papers: list[CrawledPaper]
    metrics: dict[str, int]


@dataclass(frozen=True)
class CategoryFetchResult:
    papers: list[dict[str, Any]]
    status: str
    error_codes: tuple[str, ...]
    metrics: dict[str, Any]
    attempt_events: tuple[dict[str, Any], ...]


class CrawlFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        metrics: dict[str, Any] | None = None,
        attempt_events: Iterable[dict[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.metrics = dict(metrics or {})
        self.attempt_events = tuple(dict(event) for event in attempt_events)


@dataclass(frozen=True)
class _PaperBlock:
    text: str
    items: tuple[str, ...] = ()


def build_category_url(
    category: str,
    sort_param: str = "sort=1",
    top_n: int = 30,
    crawl_date: str | None = None,
) -> str:
    base = f"{PAPERS_COOL_BASE}/arxiv/{category}"
    query = dict(parse_qsl(sort_param or "sort=1"))
    query.setdefault("sort", "1")
    query.setdefault("show", str(max(top_n, 30)))
    if crawl_date:
        query["date"] = crawl_date
    return urlunparse((*urlparse(base)[:4], urlencode(query), ""))


def fetch_category(
    category: str,
    top_n: int = 30,
    sort_param: str = "sort=1",
    timeout_seconds: int = 20,
    retries: int = 2,
    user_agent: str = "DailyCoolPapers/0.1",
    trust_env_proxy: bool = False,
    proxy_url: str = "",
    crawl_date: str | None = None,
    attempt_progress: Callable[[dict[str, Any]], None] | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    result = fetch_category_report(
        category,
        top_n=top_n,
        sort_param=sort_param,
        timeout_seconds=timeout_seconds,
        retries=retries,
        user_agent=user_agent,
        trust_env_proxy=trust_env_proxy,
        proxy_url=proxy_url,
        crawl_date=crawl_date,
        attempt_progress=attempt_progress,
        client=client,
    )
    if result.status == "failed":
        error_code = str(result.metrics.get("primary_error_code") or "parse_incomplete")
        raise CrawlFetchError(
            f"抓取 {category} 完整性检查失败（{error_code}）",
            error_code=error_code,
            metrics=result.metrics,
            attempt_events=result.attempt_events,
        )
    return result.papers


def fetch_category_report(
    category: str,
    top_n: int = 30,
    sort_param: str = "sort=1",
    timeout_seconds: int = 20,
    retries: int = 2,
    user_agent: str = "DailyCoolPapers/0.1",
    trust_env_proxy: bool = False,
    proxy_url: str = "",
    crawl_date: str | None = None,
    missing_field_warning_rate: float = 0.0,
    attempt_progress: Callable[[dict[str, Any]], None] | None = None,
    client: httpx.Client | None = None,
) -> CategoryFetchResult:
    url = build_category_url(category, sort_param, top_n, crawl_date=crawl_date)
    last_error: Exception | None = None
    last_error_code = "network_http_error"
    last_metrics: dict[str, Any] = {
        "target_date": crawl_date,
        "category": category,
        "request_url": _safe_url(url),
        "top_n": int(top_n),
    }
    attempt_events: list[dict[str, Any]] = []
    max_attempts = retries + 1
    owns_client = client is None
    if client is None:
        client_kwargs = {
            "timeout": timeout_seconds,
            "follow_redirects": True,
        }
        client_kwargs.update(httpx_proxy_kwargs(proxy_url, trust_env_proxy))
        client = httpx.Client(**client_kwargs)
    for attempt in range(retries + 1):
        attempt_number = attempt + 1
        started = time.perf_counter()
        _record_attempt_event(
            attempt_events,
            attempt_progress,
            {
                "event": "attempt",
                "attempt": attempt_number,
                "max_attempts": max_attempts,
                "request_url": _safe_url(url),
                "url": _safe_url(url),
            },
        )
        try:
            logger.info("Fetching %s attempt=%s", _safe_url(url), attempt_number)
            response = client.get(url, headers={"User-Agent": user_agent})
            elapsed_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
            status_code = int(getattr(response, "status_code", 200) or 200)
            final_url = _safe_url(str(getattr(response, "url", url) or url))
            response_bytes = _response_bytes(response)
            last_metrics = {
                **last_metrics,
                "http_status": status_code,
                "response_ms": elapsed_ms,
                "final_url": final_url,
                "response_bytes": len(response_bytes),
                "content_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "retry_count": attempt,
            }
            response.raise_for_status()
            if not _is_allowed_category_url(final_url, category):
                raise CrawlFetchError(
                    f"抓取 {category} 被重定向到非预期页面",
                    error_code="unexpected_redirect",
                    metrics=last_metrics,
                    attempt_events=attempt_events,
                )
            _record_attempt_event(
                attempt_events,
                attempt_progress,
                {
                    "event": "http_succeeded",
                    "attempt": attempt_number,
                    "max_attempts": max_attempts,
                    "metrics": dict(last_metrics),
                },
            )
            try:
                result = _analyze_category_response(
                    response.text,
                    category=category,
                    top_n=top_n,
                    target_date=crawl_date,
                    request_metrics=last_metrics,
                    missing_field_warning_rate=missing_field_warning_rate,
                    attempt_events=attempt_events,
                )
            except Exception as exc:
                logger.warning("Parsing crawl response failed category=%s error_type=%s", category, type(exc).__name__)
                result = CategoryFetchResult(
                    papers=[],
                    status="failed",
                    error_codes=("parse_incomplete",),
                    metrics={
                        **last_metrics,
                        "target_date": crawl_date,
                        "category": category,
                        "declared_total": None,
                        "expected_count": None,
                        "parsed_count": 0,
                        "valid_arxiv_count": 0,
                        "failed_count": 0,
                        "error_codes": ["parse_incomplete"],
                        "primary_error_code": "parse_incomplete",
                        "integrity_status": "failed",
                    },
                    attempt_events=tuple(dict(event) for event in attempt_events),
                )
            logger.info(
                "Fetched %s papers for %s integrity_status=%s",
                len(result.papers),
                category,
                result.status,
            )
            if owns_client:
                client.close()
            return result
        except Exception as exc:
            last_error = exc
            elapsed_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
            if isinstance(exc, CrawlFetchError):
                last_error_code = exc.error_code
                last_metrics = {**last_metrics, **exc.metrics}
            else:
                last_error_code = _network_error_code(exc)
            last_metrics = {
                **last_metrics,
                "response_ms": elapsed_ms,
                "retry_count": attempt,
            }
            logger.warning(
                "Fetch failed for %s attempt=%s code=%s error_type=%s",
                category,
                attempt_number,
                last_error_code,
                type(exc).__name__,
            )
            _record_attempt_event(
                attempt_events,
                attempt_progress,
                {
                    "event": "attempt_failed",
                    "attempt": attempt_number,
                    "max_attempts": max_attempts,
                    "error_code": last_error_code,
                    "error": f"抓取失败（{last_error_code}）",
                    "metrics": dict(last_metrics),
                },
            )
            if last_error_code == "unexpected_redirect":
                break
    if owns_client:
        client.close()
    raise CrawlFetchError(
        f"抓取 {category} 失败（{last_error_code}）",
        error_code=last_error_code,
        metrics=last_metrics,
        attempt_events=attempt_events,
    ) from last_error


def parse_papers(html: str, category: str, top_n: int, source_url: str) -> list[CrawledPaper]:
    return parse_papers_with_diagnostics(html, category, top_n, source_url).papers


def parse_papers_with_diagnostics(
    html: str,
    category: str,
    top_n: int,
    source_url: str,
) -> ParsedPapersResult:
    soup = BeautifulSoup(html, "html.parser")
    headings = [heading for heading in soup.find_all("h2") if _rank_from_text(heading.get_text(" ", strip=True))]
    papers: list[CrawledPaper] = []
    missing = {
        "missing_arxiv_id": 0,
        "missing_title": 0,
        "missing_abstract": 0,
        "missing_authors": 0,
        "missing_published_at": 0,
    }
    critical_missing_entries = 0
    considered = headings[: max(0, int(top_n))]
    for heading in considered:
        paper = _parse_heading_block(heading, source_url)
        if not paper:
            missing["missing_arxiv_id"] += 1
            critical_missing_entries += 1
            continue
        entry_missing = False
        for field_name, value in (
            ("missing_title", paper.title),
            ("missing_abstract", paper.abstract),
            ("missing_authors", paper.authors),
            ("missing_published_at", paper.published_at),
        ):
            if not value:
                missing[field_name] += 1
                entry_missing = True
        if entry_missing:
            critical_missing_entries += 1
        papers.append(paper)
    return ParsedPapersResult(
        papers=papers,
        metrics={
            "heading_count": len(considered),
            "parsed_count": len(considered),
            "valid_arxiv_count": len(papers),
            "critical_missing_entries": critical_missing_entries,
            **missing,
        },
    )


def extract_page_date(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    date_link = soup.select_one("a.date")
    if date_link:
        match = re.search(r"\d{4}-\d{2}-\d{2}", date_link.get_text(" ", strip=True))
        if match:
            return match.group(0)
    return None


def extract_declared_total(html: str) -> int | None:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"\bTotal\s*[:：]\s*([\d,]+)\b", text, flags=re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _analyze_category_response(
    html: str,
    *,
    category: str,
    top_n: int,
    target_date: str | None,
    request_metrics: dict[str, Any],
    missing_field_warning_rate: float,
    attempt_events: list[dict[str, Any]],
) -> CategoryFetchResult:
    page_date = extract_page_date(html)
    declared_total = extract_declared_total(html)
    parsed = parse_papers_with_diagnostics(
        html,
        category,
        top_n,
        str(request_metrics.get("final_url") or request_metrics.get("request_url") or ""),
    )
    papers = [paper.to_dict() for paper in parsed.papers]
    effective_date = page_date or target_date
    if effective_date:
        for paper in papers:
            paper["_crawl_date"] = effective_date

    expected_count = min(max(0, int(top_n)), declared_total) if declared_total is not None else None
    candidate_count = max(1, int(parsed.metrics["heading_count"]))
    missing_rate = parsed.metrics["critical_missing_entries"] / candidate_count
    threshold = max(0.0, min(1.0, float(missing_field_warning_rate)))
    error_codes: list[str] = []
    failed = False

    if target_date and page_date and page_date != target_date:
        error_codes.append("page_date_mismatch")
        failed = True
    elif not page_date:
        error_codes.append("page_date_unknown")

    parsed_count = int(parsed.metrics["parsed_count"])
    if declared_total is None:
        error_codes.append("declared_total_unknown")
        if parsed_count == 0:
            error_codes.append("parse_zero_without_total")
            failed = True
    elif declared_total > 0 and parsed_count == 0:
        error_codes.append("parse_zero_with_nonzero_total")
        failed = True
    elif declared_total == 0 and parsed_count > 0:
        error_codes.append("declared_total_mismatch")
    elif expected_count is not None and 0 < parsed_count < expected_count:
        error_codes.append("parse_incomplete")

    if parsed.metrics["missing_arxiv_id"]:
        error_codes.append("missing_arxiv_id")
        if parsed_count > 0 and parsed.metrics["valid_arxiv_count"] == 0:
            failed = True
    if parsed.metrics["critical_missing_entries"] and missing_rate > threshold:
        error_codes.append("missing_critical_fields")

    error_codes = list(dict.fromkeys(error_codes))
    primary_error_code = _primary_integrity_error(error_codes)
    if failed:
        status = "failed"
    elif error_codes:
        status = "warning"
    elif declared_total == 0 and parsed_count == 0:
        status = "empty_success"
    else:
        status = "success"

    metrics = {
        **request_metrics,
        "target_date": target_date,
        "page_date": page_date,
        "category": category,
        "declared_total": declared_total,
        "top_n": int(top_n),
        "expected_count": expected_count,
        **parsed.metrics,
        "missing_field_rate": round(missing_rate, 6),
        "missing_field_warning_rate": threshold,
        "error_codes": error_codes,
        "primary_error_code": primary_error_code,
        "integrity_status": status,
    }
    return CategoryFetchResult(
        papers=papers,
        status=status,
        error_codes=tuple(error_codes),
        metrics=metrics,
        attempt_events=tuple(dict(event) for event in attempt_events),
    )


def _record_attempt_event(
    events: list[dict[str, Any]],
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    safe_event = dict(event)
    events.append(safe_event)
    if callback:
        try:
            callback(dict(safe_event))
        except Exception as exc:
            logger.warning("Crawl attempt progress callback failed error_type=%s", type(exc).__name__)


def _response_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    return str(getattr(response, "text", "")).encode("utf-8", errors="replace")


def _safe_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    allowed_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key in {"date", "show", "sort"}
    ]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc.rsplit('@', 1)[-1],
            parsed.path,
            "",
            urlencode(allowed_query),
            "",
        )
    )


def _is_allowed_category_url(value: str, category: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and (hostname == "papers.cool" or hostname.endswith(".papers.cool"))
        and parsed.path.rstrip("/") == f"/arxiv/{category}"
    )


def _network_error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "network_timeout"
    return "network_http_error"


def _primary_integrity_error(error_codes: list[str]) -> str | None:
    priority = (
        "page_date_mismatch",
        "parse_zero_with_nonzero_total",
        "parse_zero_without_total",
        "parse_incomplete",
        "missing_arxiv_id",
        "declared_total_mismatch",
        "declared_total_unknown",
        "page_date_unknown",
        "missing_critical_fields",
    )
    return next((code for code in priority if code in error_codes), None)


def _parse_heading_block(heading: Tag, source_url: str) -> CrawledPaper | None:
    heading_text = heading.get_text(" ", strip=True)
    rank = _rank_from_text(heading_text)
    links = heading.find_all("a")
    title = _extract_title(links, heading_text)
    abs_url = _extract_abs_url(links)
    arxiv_id = _extract_arxiv_id(abs_url or heading_text)
    if not arxiv_id:
        logger.warning("Skipping paper without arXiv id: %s", heading_text[:160])
        return None

    pdf_url = _extract_pdf_url(links) or f"https://arxiv.org/pdf/{arxiv_id}"
    if pdf_url:
        pdf_url = urljoin(source_url, pdf_url)
    if abs_url and "arxiv.org/abs/" in abs_url:
        abs_url = urljoin(source_url, abs_url)
    else:
        abs_url = f"https://arxiv.org/abs/{arxiv_id}"

    papers_cool_url = _extract_papers_cool_url(links, source_url) or f"{PAPERS_COOL_BASE}/arxiv/{arxiv_id}"
    pdf_clicks = _extract_label_count(heading_text, "PDF")
    kimi_clicks = _extract_label_count(heading_text, "Kimi")
    reading_stars = pdf_clicks + kimi_clicks

    blocks = _blocks_until_next_heading(heading)
    authors = _extract_metadata_items(blocks, "authors")
    subjects = _extract_metadata_items(blocks, "subjects")
    published_at = _extract_metadata_value(blocks, "publish")
    abstract = _extract_abstract(blocks)

    return CrawledPaper(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        subjects=subjects,
        published_at=published_at,
        pdf_url=pdf_url,
        abs_url=abs_url,
        papers_cool_url=papers_cool_url,
        rank=rank,
        reading_stars=reading_stars,
        pdf_clicks=pdf_clicks,
        kimi_clicks=kimi_clicks,
    )


def _rank_from_text(text: str) -> int | None:
    match = re.search(r"#\s*(\d+)", text)
    return int(match.group(1)) if match else None


def _extract_title(links: Iterable[Tag], heading_text: str) -> str:
    candidates: list[tuple[int, str]] = []
    for link in links:
        text = link.get_text(" ", strip=True)
        if not text:
            continue
        if _is_heading_control_text(text):
            continue
        href = link.get("href") or ""
        score = 0
        if _extract_arxiv_id(href):
            score += 10
        if "arxiv.org" not in href:
            score += 2
        score += min(len(text), 200)
        candidates.append((score, text))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    text = re.sub(r"#\s*\d+", "", heading_text)
    text = re.sub(r"\[(?:PDF|Kimi|Copy|REL|BibTeX|Code|Project)\d*\]", "", text, flags=re.I).strip()
    return text


def _is_heading_control_text(text: str) -> bool:
    normalized = re.sub(r"^\[\s*|\s*\]$", "", text.strip())
    normalized = re.sub(r"\s+", "", normalized)
    return bool(
        re.fullmatch(r"#\d+", normalized)
        or re.fullmatch(r"PDF\d*", normalized, flags=re.I)
        or re.fullmatch(r"Kimi\d*", normalized, flags=re.I)
        or re.fullmatch(r"Copy", normalized, flags=re.I)
        or re.fullmatch(r"REL", normalized, flags=re.I)
        or re.fullmatch(r"BibTeX", normalized, flags=re.I)
        or re.fullmatch(r"Code", normalized, flags=re.I)
        or re.fullmatch(r"Project", normalized, flags=re.I)
    )


def _extract_abs_url(links: Iterable[Tag]) -> str | None:
    for link in links:
        href = link.get("href") or ""
        if "arxiv.org/abs/" in href or re.search(r"/abs/\d{4}\.\d{4,5}", href):
            return href
    for link in links:
        href = link.get("href") or ""
        if _extract_arxiv_id(href):
            return href
    return None


def _extract_pdf_url(links: Iterable[Tag]) -> str | None:
    for link in links:
        text = link.get_text(" ", strip=True)
        href = link.get("href") or ""
        if text.startswith("PDF") or "/pdf/" in href:
            return href
    return None


def _extract_papers_cool_url(links: Iterable[Tag], source_url: str) -> str | None:
    for link in links:
        href = link.get("href") or ""
        full = urljoin(source_url, href)
        if urlparse(full).netloc.endswith("papers.cool"):
            return full
    return None


def _extract_arxiv_id(text: str | None) -> str | None:
    if not text:
        return None
    match = ARXIV_ID_RE.search(text)
    if not match:
        return None
    return re.sub(r"v\d+$", "", match.group("id"), flags=re.I)


def _extract_label_count(text: str, label: str) -> int:
    match = re.search(rf"{re.escape(label)}\s*(\d+)", text, flags=re.I)
    return int(match.group(1)) if match else 0


def _blocks_until_next_heading(heading: Tag) -> list[_PaperBlock]:
    blocks: list[_PaperBlock] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h2":
            break
        if isinstance(sibling, Tag):
            leaf_blocks = [
                child
                for child in sibling.find_all(["p", "li", "div", "section", "article"])
                if not child.find(["p", "li", "div", "section", "article"], recursive=False)
            ]
            elements = leaf_blocks or [sibling]
            for element in elements:
                text = _clean_text(element.get_text(" ", strip=True))
                if not text:
                    continue
                links = tuple(
                    link_text
                    for link in element.find_all("a")
                    if (link_text := _clean_text(link.get_text(" ", strip=True)))
                )
                spans = tuple(
                    span_text
                    for span in element.find_all("span")
                    if not span.find("span")
                    and (span_text := _clean_text(span.get_text(" ", strip=True)))
                    and span_text != text
                )
                blocks.append(_PaperBlock(text, links or spans))
        else:
            text = _clean_text(str(sibling))
            if text:
                blocks.append(_PaperBlock(text))
    return blocks


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


_METADATA_LABELS = {
    "authors": re.compile(r"^(?:authors?|author\s*\(s\))\s*(?::|：|$)\s*", re.I),
    "subjects": re.compile(r"^(?:subjects?|categories)\s*(?::|：|$)\s*", re.I),
    "publish": re.compile(r"^(?:publish|published)\s*(?::|：|$)\s*", re.I),
    "abstract": re.compile(r"^(?:abstract|abs)\s*(?::|：|$)\s*", re.I),
}


def _labeled_value(block: _PaperBlock, kind: str) -> str | None:
    match = _METADATA_LABELS[kind].match(block.text)
    return block.text[match.end() :].strip() if match else None


def _extract_metadata_value(blocks: list[_PaperBlock], kind: str) -> str | None:
    for block in blocks:
        value = _labeled_value(block, kind)
        if value is not None:
            return value
    return None


def _extract_metadata_items(blocks: list[_PaperBlock], kind: str) -> list[str]:
    for block in blocks:
        value = _labeled_value(block, kind)
        if value is None:
            continue
        structured_items: list[str] = []
        for item in block.items:
            item_value = _labeled_value(_PaperBlock(item), kind)
            if item_value is not None:
                if item_value:
                    structured_items.extend(_split_metadata_items(item_value))
                continue
            structured_items.append(item)
        if structured_items:
            return list(dict.fromkeys(structured_items))
        return _split_metadata_items(value)
    return []


def _split_metadata_items(value: str) -> list[str]:
    return [
            item.strip()
            for item in re.split(r",|，|;|；|\|", value)
            if item.strip()
        ]


def _extract_abstract(blocks: list[_PaperBlock]) -> str:
    abstract_lines: list[str] = []
    authors_seen = False
    abstract_closed = False
    for block in blocks:
        if _labeled_value(block, "authors") is not None:
            authors_seen = True
            continue
        if (
            _labeled_value(block, "subjects") is not None
            or _labeled_value(block, "publish") is not None
        ):
            abstract_closed = True
            continue
        explicit_abstract = _labeled_value(block, "abstract")
        if explicit_abstract is not None:
            if explicit_abstract:
                abstract_lines.append(explicit_abstract)
            continue
        if authors_seen and not abstract_closed:
            abstract_lines.append(block.text)
    return " ".join(abstract_lines).strip()


def crawl_date_from_papers(papers: list[dict]) -> str:
    for paper in papers:
        crawl_date = paper.get("_crawl_date")
        if crawl_date:
            return str(crawl_date)
        published = paper.get("published_at")
        if published:
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(published))
            if match:
                return match.group(0)
    return date.today().isoformat()


def latest_available_arxiv_date(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    local_now = now if now.tzinfo else now.astimezone()
    utc_now = local_now.astimezone(timezone.utc)
    candidate = local_now.date()
    before_daily_release = utc_now.hour * 60 + utc_now.minute < 90
    while True:
        if before_daily_release and candidate == local_now.date():
            candidate -= timedelta(days=1)
            continue
        if is_available_arxiv_date(candidate):
            return candidate.isoformat()
        candidate -= timedelta(days=1)


def available_arxiv_dates_after(start_date: str | None, end_date: str) -> list[str]:
    end = date.fromisoformat(end_date)
    if start_date:
        cursor = date.fromisoformat(start_date) + timedelta(days=1)
    else:
        cursor = end
    dates: list[str] = []
    while cursor <= end:
        if is_available_arxiv_date(cursor):
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return dates


def is_available_arxiv_date(value: date) -> bool:
    return value.weekday() < 5 and value not in ARXIV_DISABLED_DATES
