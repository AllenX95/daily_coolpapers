import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import httpx

from . import db
from .crawler import ARXIV_ID_RE


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_SUBJECT_RE = re.compile(
    r"(?:^|\s)((?:(?:cs|stat|math|eess|physics|q-bio|q-fin|econ)\.[A-Za-z0-9.-]+"
    r"(?:[\s,;|]+|$))+)$",
    re.I,
)


@dataclass(frozen=True)
class AbstractAuditFinding:
    paper_id: int
    arxiv_id: str
    title: str
    signals: tuple[str, ...]
    stored_abstract: str
    proposed_abstract: str | None = None
    proposal_source: str | None = None

    @property
    def would_update(self) -> bool:
        return bool(
            self.proposed_abstract
            and _normalize_text(self.proposed_abstract) != _normalize_text(self.stored_abstract)
        )

    def to_dict(self) -> dict[str, Any]:
        item = asdict(self)
        item["signals"] = list(self.signals)
        item["would_update"] = self.would_update
        return item


def scan_abstracts(
    papers: Iterable[dict[str, Any]],
    canonical_abstracts: Mapping[str, str] | None = None,
) -> list[AbstractAuditFinding]:
    """Find likely metadata contamination without mutating stored paper data."""
    canonical_abstracts = canonical_abstracts or {}
    findings: list[AbstractAuditFinding] = []
    for paper in papers:
        abstract = _normalize_text(str(paper.get("abstract") or ""))
        if not abstract:
            continue
        authors = _string_list(paper.get("authors_list") or paper.get("authors"))
        subjects = _string_list(paper.get("subjects_list") or paper.get("subjects"))
        signals = _contamination_signals(abstract, authors, subjects)
        canonical = _normalize_text(canonical_abstracts.get(_base_arxiv_id(paper["arxiv_id"]), ""))
        if not signals and not canonical:
            continue
        if canonical and canonical == abstract:
            continue
        findings.append(
            AbstractAuditFinding(
                paper_id=int(paper["id"]),
                arxiv_id=str(paper["arxiv_id"]),
                title=str(paper.get("title") or ""),
                signals=tuple(signals or ["canonical_mismatch"]),
                stored_abstract=abstract,
                proposed_abstract=canonical or None,
                proposal_source="arxiv" if canonical else None,
            )
        )
    return findings


def fetch_arxiv_abstracts(
    arxiv_ids: Iterable[str],
    client: httpx.Client | None = None,
    batch_size: int = 40,
) -> dict[str, str]:
    """Fetch canonical abstracts through an injectable HTTP client."""
    ids = list(dict.fromkeys(_base_arxiv_id(item) for item in arxiv_ids if item))
    invalid_ids = [item for item in ids if ARXIV_ID_RE.fullmatch(item) is None]
    if invalid_ids:
        raise ValueError(f"包含非法 arXiv ID: {invalid_ids[0]}")
    if not ids:
        return {}
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=30, follow_redirects=False)
    abstracts: dict[str, str] = {}
    try:
        for start in range(0, len(ids), max(1, batch_size)):
            batch = ids[start : start + max(1, batch_size)]
            query = urlencode({"id_list": ",".join(batch), "max_results": len(batch)})
            response = client.get(
                f"{ARXIV_API_URL}?{query}",
                headers={"User-Agent": "DailyCoolPapers/0.1 abstract-audit"},
            )
            response.raise_for_status()
            if len(response.text.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("arXiv API 响应超过 2 MiB，已拒绝解析")
            abstracts.update(_parse_arxiv_atom(response.text))
    finally:
        if owns_client:
            client.close()
    return abstracts


def audit_database(
    limit: int = 200,
    offset: int = 0,
    verify_arxiv: bool = False,
    client: httpx.Client | None = None,
    db_path: Path | None = None,
) -> list[AbstractAuditFinding]:
    papers = db.list_papers_for_abstract_audit(limit=limit, offset=offset, path=db_path)
    local_findings = scan_abstracts(papers)
    if not verify_arxiv or not local_findings:
        return local_findings
    candidate_ids = {finding.paper_id for finding in local_findings}
    candidates = [paper for paper in papers if int(paper["id"]) in candidate_ids]
    canonical = fetch_arxiv_abstracts(
        (str(paper["arxiv_id"]) for paper in candidates),
        client=client,
    )
    return scan_abstracts(candidates, canonical)


def _contamination_signals(
    abstract: str,
    authors: list[str],
    subjects: list[str],
) -> list[str]:
    signals: list[str] = []
    folded = abstract.casefold()
    if authors:
        author_prefixes = {
            _normalize_text(separator.join(authors)).casefold()
            for separator in (" ", ", ", "; ")
        }
        if any(prefix and folded.startswith(prefix) for prefix in author_prefixes):
            signals.append("author_prefix")
    if subjects:
        subject_tails = {
            _normalize_text(separator.join(subjects)).casefold()
            for separator in (" ", ", ", "; ")
        }
        if any(suffix and folded.endswith(suffix) for suffix in subject_tails):
            signals.append("subject_suffix")
    if ARXIV_SUBJECT_RE.search(abstract):
        signals.append("subject_code_tail")
    return list(dict.fromkeys(signals))


def _parse_arxiv_atom(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    abstracts: dict[str, str] = {}
    for entry in root.findall("atom:entry", namespace):
        identifier = entry.findtext("atom:id", default="", namespaces=namespace)
        summary = entry.findtext("atom:summary", default="", namespaces=namespace)
        match = re.search(r"/abs/([^/?#]+)", identifier)
        if match and summary:
            abstracts[_base_arxiv_id(match.group(1))] = _normalize_text(summary)
    return abstracts


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
        value = parsed
    if not isinstance(value, list):
        return []
    return [_normalize_text(str(item)) for item in value if _normalize_text(str(item))]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _base_arxiv_id(value: str) -> str:
    return re.sub(r"v\d+$", "", str(value).strip(), flags=re.I)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读扫描历史 Abstract 元数据污染，并输出 dry-run 修复建议。",
    )
    parser.add_argument("--limit", type=int, default=200, help="最多扫描论文数，最大 1000")
    parser.add_argument("--offset", type=int, default=0, help="从第几条论文开始")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="要审计的现有 SQLite 文件；省略时使用应用默认数据库",
    )
    parser.add_argument(
        "--verify-arxiv",
        action="store_true",
        help="从 arXiv API 获取权威摘要，仅用于生成建议，不写数据库",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit 必须在 1 到 1000 之间")
    if args.offset < 0:
        parser.error("--offset 不能小于 0")

    exit_code = 0
    try:
        findings = audit_database(
            limit=args.limit,
            offset=args.offset,
            verify_arxiv=args.verify_arxiv,
            db_path=args.db,
        )
    except Exception as exc:
        if not args.verify_arxiv:
            print(f"审计失败：{exc}", file=sys.stderr)
            return 2
        print(f"arXiv 复核失败，保留本地扫描结果：{exc}", file=sys.stderr)
        try:
            findings = audit_database(
                limit=args.limit,
                offset=args.offset,
                verify_arxiv=False,
                db_path=args.db,
            )
        except Exception as local_exc:
            print(f"本地审计失败：{local_exc}", file=sys.stderr)
            return 2
        exit_code = 2
    if args.json:
        print(json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2))
    else:
        print(f"扫描完成：发现 {len(findings)} 条候选；数据库未修改。")
        for finding in findings:
            proposal = "可生成替换建议" if finding.would_update else "需 --verify-arxiv 复核"
            print(
                f"#{finding.paper_id} {finding.arxiv_id} "
                f"[{','.join(finding.signals)}] {proposal} — {finding.title}"
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
