import logging
import re
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
    url = build_category_url(category, sort_param, top_n, crawl_date=crawl_date)
    last_error: Exception | None = None
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
        try:
            logger.info("Fetching %s attempt=%s", url, attempt + 1)
            if attempt_progress:
                attempt_progress(
                    {
                        "event": "attempt",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "url": url,
                    }
                )
            response = client.get(url, headers={"User-Agent": user_agent})
            response.raise_for_status()
            page_date = extract_page_date(response.text) or crawl_date
            papers = parse_papers(response.text, category, top_n, url)
            paper_dicts = [paper.to_dict() for paper in papers]
            if page_date:
                for paper in paper_dicts:
                    paper["_crawl_date"] = page_date
            logger.info("Fetched %s papers for %s", len(papers), category)
            if owns_client:
                client.close()
            return paper_dicts
        except Exception as exc:
            last_error = exc
            logger.warning("Fetch failed for %s attempt=%s error=%s", category, attempt + 1, exc)
            if attempt_progress:
                attempt_progress(
                    {
                        "event": "attempt_failed",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "url": url,
                        "error": str(exc),
                    }
                )
    if owns_client:
        client.close()
    raise RuntimeError(f"抓取 {category} 失败: {last_error}") from last_error


def parse_papers(html: str, category: str, top_n: int, source_url: str) -> list[CrawledPaper]:
    soup = BeautifulSoup(html, "html.parser")
    headings = [heading for heading in soup.find_all("h2") if _rank_from_text(heading.get_text(" ", strip=True))]
    papers: list[CrawledPaper] = []
    for heading in headings:
        paper = _parse_heading_block(heading, source_url)
        if paper:
            papers.append(paper)
        if len(papers) >= top_n:
            break
    return papers


def extract_page_date(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    date_link = soup.select_one("a.date")
    if date_link:
        match = re.search(r"\d{4}-\d{2}-\d{2}", date_link.get_text(" ", strip=True))
        if match:
            return match.group(0)
    return None


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
    return match.group("id")


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
