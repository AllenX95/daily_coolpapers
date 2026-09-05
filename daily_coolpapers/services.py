import csv
import io
import json
import logging
import hashlib
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Lock, RLock
from time import perf_counter
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from . import db
from .crawler import (
    CategoryFetchResult,
    CrawlFetchError,
    available_arxiv_dates_after,
    crawl_date_from_papers,
    fetch_category_report,
    latest_available_arxiv_date,
)
from .fulltext import ensure_markdown
from .llm import LLMError, LLMResultError, call_llm, make_llm_client, validate_evaluation_result
from .network import httpx_proxy_kwargs
from .prompt_engine import estimate_tokens, render_prompt

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str, dict[str, Any] | None], None]

EVALUATION_TYPE_LABELS = {
    "direction_classification": "关注方向分类",
    "abstract_review": "摘要评估",
    "fulltext_review": "全文评估",
}
EVALUATION_STATUS_LABELS = {
    "pending": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
}
FAVORITE_SORT_OPTIONS = [
    {"value": "evaluated_desc", "label": "评估时间"},
    {"value": "score_desc", "label": "全文评分"},
    {"value": "rank", "label": "类目排名"},
    {"value": "title", "label": "标题"},
]
FAVORITE_SORT_VALUES = {option["value"] for option in FAVORITE_SORT_OPTIONS}
PAPER_DECISION_LABELS = {'undecided': '未处理', 'favorite': '已收藏', 'skipped': '已跳过'}


@dataclass(frozen=True)
class EvaluationRequest:
    paper_id: int
    evaluation_type: str
    prompt_id: int | None = None
    force_markdown: bool = False
    pipeline_job_id: int | None = None
    claim_token: str | None = None


@dataclass(frozen=True)
class EvaluationConfig:
    evaluation_type: str
    prompt: dict[str, Any]
    profile: dict[str, Any]

    @property
    def prompt_id(self) -> int | None:
        value = self.prompt.get("id")
        return int(value) if value is not None else None

    @property
    def prompt_version(self) -> int | None:
        value = self.prompt.get("version")
        return int(value) if value is not None else None

    @property
    def profile_id(self) -> int | None:
        value = self.profile.get("id")
        return int(value) if value is not None else None

    @property
    def model(self) -> str | None:
        value = self.profile.get("model")
        return str(value) if value is not None else None


def resolve_evaluation_config(evaluation_type: str, prompt_id: int | None = None) -> EvaluationConfig:
    prompt = db.get_prompt(prompt_id) if prompt_id else db.get_default_prompt(evaluation_type)
    if not prompt:
        raise ValueError(f"No available prompt: {evaluation_type}")
    if prompt.get("type") != evaluation_type:
        raise ValueError(
            f"Prompt {prompt.get('id')} is for {prompt.get('type')}, not {evaluation_type}"
        )
    if not int(prompt.get("enabled") or 0):
        raise ValueError(f"Prompt {prompt.get('id')} is disabled")

    profile = db.get_llm_profile(prompt.get("llm_profile_id"))
    if profile and not int(profile.get("enabled") or 0):
        profile = None
    profile = profile or db.get_default_llm_profile(evaluation_type)
    if not profile:
        raise ValueError("No available LLM profile")
    if not int(profile.get("enabled") or 0):
        raise ValueError(f"LLM profile {profile.get('id')} is disabled")
    return EvaluationConfig(evaluation_type=evaluation_type, prompt=prompt, profile=profile)


def evaluation_prompt_options(evaluation_type: str) -> list[dict[str, Any]]:
    prompts = db.list_prompts(evaluation_type, enabled_only=True)
    default_prompt = db.get_default_prompt(evaluation_type)
    default_prompt_id = int(default_prompt["id"]) if default_prompt else None
    if default_prompt_id is not None:
        default_prompt = next(
            (prompt for prompt in prompts if int(prompt["id"]) == default_prompt_id),
            default_prompt,
        )
    default_profile = db.get_default_llm_profile(evaluation_type)
    options: list[dict[str, Any]] = []

    if default_prompt:
        options.append(
            {
                "value": str(default_prompt["id"]),
                "label": f"默认：{_prompt_choice_label(default_prompt, default_profile)}",
                "is_default": True,
                "disabled": False,
            }
        )
    else:
        options.append(
            {
                "value": "",
                "label": "无可用 Prompt",
                "is_default": True,
                "disabled": True,
            }
        )

    for prompt in prompts:
        prompt_id = int(prompt["id"])
        if prompt_id == default_prompt_id:
            continue
        options.append(
            {
                "value": str(prompt_id),
                "label": _prompt_choice_label(prompt, default_profile),
                "is_default": bool(prompt.get("is_default")),
                "disabled": False,
            }
        )
    return options


def _prompt_choice_label(prompt: dict[str, Any], default_profile: dict[str, Any] | None) -> str:
    profile = _prompt_profile_label(prompt, default_profile)
    version = prompt.get("version") or 1
    return f"{prompt.get('name') or 'Prompt'} · v{version} · {profile}"


def _prompt_profile_label(prompt: dict[str, Any], default_profile: dict[str, Any] | None) -> str:
    if prompt.get("llm_profile_id") and prompt.get("llm_profile_enabled") is not False:
        name = prompt.get("llm_profile_name")
        model = prompt.get("llm_model")
        if name and model:
            return f"{name} / {model}"
        if name or model:
            return str(name or model)
    if default_profile:
        name = default_profile.get("name")
        model = default_profile.get("model")
        if name and model:
            return f"默认模型：{name} / {model}"
        if name or model:
            return f"默认模型：{name or model}"
    return "默认模型"


def evaluation_result_view(evaluation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not evaluation:
        return None
    result = evaluation.get("result") if isinstance(evaluation.get("result"), dict) else {}
    status = str(evaluation.get("status") or "pending")
    is_success = status == "success"
    vc = result.get("vc_perspective") if isinstance(result.get("vc_perspective"), dict) else {}
    score = result.get("score") if is_success else None
    attention = (result.get("attention") or "unknown") if is_success else "pending"
    prompt_name = evaluation.get("prompt_name") or "Prompt 已删除"
    model = evaluation.get("model") or ""
    profile_name = evaluation.get("llm_profile_name") or ""
    return {
        "id": evaluation.get("id"),
        "evaluation_type": evaluation.get("evaluation_type") or "",
        "type_label": EVALUATION_TYPE_LABELS.get(
            evaluation.get("evaluation_type"),
            evaluation.get("evaluation_type") or "评估",
        ),
        "status": status,
        "status_label": EVALUATION_STATUS_LABELS.get(status, status),
        "is_success": is_success,
        "is_failed": status == "failed",
        "created_at": evaluation.get("created_at") or "",
        "prompt_name": prompt_name,
        "prompt_label": prompt_name,
        "model": model,
        "profile_name": profile_name,
        "profile_label": _evaluation_profile_label(profile_name, model),
        "score": score,
        "score_text": "-" if score is None else str(score),
        "attention": str(attention),
        "summary_text": _first_text(result, "summary_zh", "one_sentence_summary", "detailed_summary_zh"),
        "one_sentence_summary": _text(result.get("one_sentence_summary")),
        "detailed_summary_zh": _text(result.get("detailed_summary_zh")),
        "problem": _text(result.get("problem")),
        "method": _text(result.get("method")),
        "novelty_assessment": _text(result.get("novelty_assessment")),
        "recommended_action": _text(result.get("recommended_action")),
        "sections": _evaluation_sections(result),
        "vc": _evaluation_vc_view(vc),
        "vc_impact": _text(vc.get("impact")),
        "tags": _string_list(result.get("tags")),
        "result_json": result,
        "has_result": bool(result),
        "error_message": evaluation.get("error_message") or "",
        "raw_output": evaluation.get("raw_output") or "",
    }


def paper_evaluation_result_model(paper_id: int) -> dict[str, Any]:
    history = [
        view
        for view in (evaluation_result_view(item) for item in db.list_evaluations(paper_id))
        if view is not None
    ]
    latest_abstract = _first_evaluation(history, "abstract_review")
    latest_fulltext = _first_evaluation(history, "fulltext_review")
    latest_successful_fulltext = _first_evaluation(history, "fulltext_review", success_only=True)
    latest_fulltext_failure = None
    if latest_fulltext and latest_fulltext["is_failed"]:
        if not latest_successful_fulltext or latest_fulltext["id"] != latest_successful_fulltext["id"]:
            latest_fulltext_failure = latest_fulltext
    return {
        "history": history,
        "latest_abstract": latest_abstract,
        "latest_fulltext": latest_fulltext,
        "latest_successful_fulltext": latest_successful_fulltext,
        "latest_fulltext_failure": latest_fulltext_failure,
    }


def build_paper_evaluation_export(
    paper: dict[str, Any],
    result_model: dict[str, Any] | None = None,
) -> str:
    result_model = result_model or paper_evaluation_result_model(int(paper["id"]))
    lines = [
        f"# {paper['title']}",
        "",
        f"- arXiv ID: {paper['arxiv_id']}",
        f"- Published: {paper.get('published_at') or ''}",
        f"- PDF: {paper.get('pdf_url') or ''}",
        "",
        "## Abstract",
        "",
        paper.get("abstract") or "",
        "",
    ]
    for evaluation in result_model.get("history") or []:
        lines.extend(_evaluation_export_block(evaluation))
    return "\n".join(lines)


def build_paper_digest_csv(papers: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "category", "rank", "stars", "score", "attention", "title", "arxiv_id", "pdf_url"])
    for paper in papers:
        abstract_eval = evaluation_result_view(paper.get("latest_abstract_eval"))
        writer.writerow(
            [
                paper.get("crawl_date"),
                paper.get("category"),
                paper.get("rank"),
                paper.get("reading_stars"),
                abstract_eval["score"] if abstract_eval else None,
                abstract_eval["attention"] if abstract_eval and abstract_eval["is_success"] else None,
                paper.get("title"),
                paper.get("arxiv_id"),
                paper.get("pdf_url"),
            ]
        )
    return output.getvalue()


def favorite_papers_page_model(sort: str | None = None) -> dict[str, Any]:
    return _reviewed_collection_model(sort, decision='favorite', collection_kind='favorites')


def reviewed_papers_page_model(sort: str | None = None, decision: str = 'all') -> dict[str, Any]:
    return _reviewed_collection_model(sort, decision=decision, collection_kind='reviewed')


def paper_decision_model(paper_id: int) -> dict[str, Any]:
    state = db.get_paper_decision_state(paper_id)
    return {**state, 'label': PAPER_DECISION_LABELS[state['decision']]}


def safe_arxiv_abstract_url(value: Any) -> str:
    """Only expose canonical arXiv abstract links, never arbitrary imported URLs."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ''
    try:
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'} or url.netloc.lower() not in {'arxiv.org', 'www.arxiv.org'}:
            return ''
        if not re.fullmatch(r'/abs/(?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?', url.path):
            return ''
        return 'https://arxiv.org' + url.path
    except ValueError:
        return ''


def paper_themes_model(paper_id: int) -> dict[str, Any]:
    options = db.paper_investment_theme_options(paper_id)
    return {'active': [{**item, 'selected': item['added_at'] is not None} for item in options if item['status'] == 'active'],
            'assigned': [item for item in options if item['added_at'] is not None]}


TEAM_FORM_FIELDS = ('author_mode', 'author_id', 'author_name', 'author_category', 'author_notes',
                    'organization_mode', 'organization_id', 'organization_name', 'organization_type',
                    'organization_region', 'organization_notes', 'tracking_notes')


def team_form_model(paper: dict, submitted: Any = None, selections: Any = None) -> dict[str, Any]:
    from .form_commands import AUTHOR_CATEGORIES, ORGANIZATION_TYPES, parse_int, FormValidationError
    tracking = db.get_paper_team_tracking(paper['id'])
    authors = paper.get('authors_list') or []
    first_author = authors[0] if isinstance(authors, list) and authors and isinstance(authors[0], str) else ''
    options = {kind: db.research_entity_options(kind) for kind in ('author', 'organization')}
    values = {field: '' for field in TEAM_FORM_FIELDS}
    values.update(author_name=first_author, author_category='unknown')
    for kind, key in (('author', 'lead_author_id'), ('organization', 'organization_id')):
        values[kind+'_mode'] = 'existing' if tracking or options[kind] else 'new'
        if tracking:
            values[kind+'_id'] = str(tracking[key])
    if tracking:
        values['tracking_notes'] = tracking['notes']
    if submitted is not None:
        values.update({field: str(submitted.get(field, '')) for field in TEAM_FORM_FIELDS})
    # Only explicit restore redirects suggest an existing ID. GET never writes.
    for kind in ('author', 'organization'):
        candidate = (selections or {}).get('team_'+kind+'_id')
        if candidate:
            try:
                entity_id = parse_int(candidate, kind+'_id', minimum=1, maximum=2**63-1)
                entity = db.get_research_entity(kind, entity_id)
                if entity['status'] == 'active':
                    values[kind+'_mode'], values[kind+'_id'] = 'existing', str(entity_id)
            except (FormValidationError, db.ResearchEntityNotFoundError):
                pass
        selected = values[kind+'_id']
        if tracking and selected == str(tracking['lead_author_id' if kind == 'author' else 'organization_id']):
            if not any(str(item['id']) == selected for item in options[kind]):
                options[kind].append({'id': int(selected), 'name': tracking[kind+'_name']+'（已归档，须先恢复）'})
    return {'tracking': tracking, 'values': values, 'first_author': first_author,
            'author_options': options['author'], 'organization_options': options['organization'],
            'author_categories': AUTHOR_CATEGORIES, 'organization_types': ORGANIZATION_TYPES}


def investment_theme_papers_model(theme_id: int, sort: str | None = None) -> dict[str, Any]:
    theme = db.get_investment_theme(theme_id)
    options = [{'value': 'added_desc', 'label': '加入时间'}, {'value': 'score_desc', 'label': '全文评分'}, {'value': 'title', 'label': '论文标题'}]
    selected_sort = sort if sort in {option['value'] for option in options} else 'added_desc'
    return {'collection_kind': 'theme', 'theme': theme, 'sort': selected_sort,
            'sort_options': [{**option, 'selected': option['value'] == selected_sort} for option in options],
            'papers': [_favorite_paper_card(row) for row in db.list_fulltext_reviewed_papers(sort=selected_sort, theme_id=theme_id)]}


def research_entities_model(args: Any) -> dict[str, Any]:
    from .form_commands import AUTHOR_CATEGORIES, ORGANIZATION_TYPES, parse_choice, parse_int
    view = parse_choice(args.get('view', 'tracking'), 'view', {'tracking', 'authors', 'organizations'})
    query = args.get('q', '').strip()
    status_options = ({'tracking': '跟踪中', 'archived': '已停止', 'all': '全部关系'} if view == 'tracking'
                      else {'active': '活跃', 'archived': '已归档', 'all': '全部实体'})
    status = parse_choice(args.get('status', next(iter(status_options))), 'status', set(status_options))
    filters = {'query': query, 'status': status, 'author_category': args.get('author_category', ''),
               'organization_type': args.get('organization_type', '')}
    scoped = {}
    if view == 'tracking':
        for kind in ('author', 'organization'):
            if args.get(kind+'_id'):
                entity_id = parse_int(args.get(kind+'_id'), kind+'_id', minimum=1, maximum=2**63-1)
                scoped[kind] = db.get_research_entity(kind, entity_id)
                filters[kind+'_id'] = entity_id
        rows = db.list_team_tracking(**filters)
    else:
        rows = db.list_research_entities('author' if view == 'authors' else 'organization', **filters)
    return {'view': view, 'query': query, 'status': status, 'status_options': status_options,
            'author_category': filters['author_category'], 'organization_type': filters['organization_type'],
            'author_categories': AUTHOR_CATEGORIES, 'organization_types': ORGANIZATION_TYPES,
            'scoped': scoped, 'rows': rows}


def _reviewed_collection_model(sort: str | None, *, decision: str, collection_kind: str) -> dict[str, Any]:
    selected_sort = _normalize_favorite_sort(sort)
    return {
        'collection_kind': collection_kind,
        'decision': decision,
        'decision_options': [{'value': key, 'label': label, 'selected': key == decision}
                             for key, label in {'all': '全部状态', **PAPER_DECISION_LABELS}.items()],
        "papers": [
            _favorite_paper_card(row)
            for row in db.list_fulltext_reviewed_papers(sort=selected_sort, decision=decision)
        ],
        "sort": selected_sort,
        "sort_options": [
            {**option, "selected": option["value"] == selected_sort}
            for option in FAVORITE_SORT_OPTIONS
        ],
    }


def _first_evaluation(
    evaluations: list[dict[str, Any]],
    evaluation_type: str,
    success_only: bool = False,
) -> dict[str, Any] | None:
    for evaluation in evaluations:
        if evaluation["evaluation_type"] != evaluation_type:
            continue
        if success_only and not evaluation["is_success"]:
            continue
        return evaluation
    return None


def _normalize_favorite_sort(sort: str | None) -> str:
    return sort if sort in FAVORITE_SORT_VALUES else "evaluated_desc"


def _favorite_paper_card(row: dict[str, Any]) -> dict[str, Any]:
    fulltext = evaluation_result_view(_fulltext_reviewed_evaluation(row))
    fulltext = fulltext or {}
    latest_category = _favorite_category_view(row.get("latest_category") or {})
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "arxiv_id": row.get("arxiv_id") or "",
        "abstract": row.get("abstract") or "",
        "abs_url": row.get("abs_url") or "",
        "category_label": latest_category["label"],
        'decision': row.get('decision', 'undecided'),
        'decision_label': PAPER_DECISION_LABELS.get(row.get('decision'), '未处理'),
        'investment_themes': row.get('investment_themes', []),
        'theme_added_at': row.get('theme_added_at'),
        'has_fulltext': row.get('fulltext_evaluation_id') is not None,
        "evaluation_label": _favorite_evaluation_label(
            str(fulltext.get("created_at") or ""),
            str(fulltext.get("profile_label") or ""),
        ),
        "score_text": fulltext.get("score_text") or "-",
        "attention": fulltext.get("attention") or "pending",
        "one_sentence_summary": fulltext.get("one_sentence_summary") or "",
        "summary_excerpt": _first_text(
            fulltext,
            "detailed_summary_zh",
            "problem",
        )
        or row.get("abstract")
        or "",
        "vc_summary": _favorite_vc_summary(fulltext.get("vc") or {}),
        "market_relevance": (fulltext.get("vc") or {}).get("market_relevance") or "",
        "recommended_action": fulltext.get("recommended_action") or "",
        "novelty_assessment": fulltext.get("novelty_assessment") or "",
        "tags": fulltext.get("tags") or [],
    }


def _fulltext_reviewed_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("fulltext_evaluation_id"),
        "evaluation_type": "fulltext_review",
        "status": "success",
        "created_at": row.get("fulltext_evaluated_at"),
        "model": row.get("fulltext_model"),
        "result": row.get("fulltext_result") or {},
    }


def _favorite_category_view(category: dict[str, Any]) -> dict[str, str]:
    name = str(category.get("category") or "")
    rank = str(category.get("rank") or "-")
    stars = str(category.get("reading_stars") or 0)
    return {
        "category": name,
        "rank": rank,
        "stars": stars,
        "label": f"{name} / Rank {rank} / Stars {stars}" if name else "",
    }


def _favorite_evaluation_label(evaluated_at: str, profile_label: str) -> str:
    if evaluated_at and profile_label:
        return f"{evaluated_at} / {profile_label}"
    return evaluated_at or profile_label


def _favorite_vc_summary(vc: dict[str, Any]) -> str:
    return (
        _text(vc.get("impact"))
        or _text(vc.get("commercialization_path"))
        or "暂无 VC 视角字段"
    )


def _evaluation_sections(result: dict[str, Any]) -> list[dict[str, str]]:
    fields = [
        ("详细总结", "detailed_summary_zh"),
        ("问题", "problem"),
        ("方法", "method"),
        ("新颖性", "novelty_assessment"),
        ("建议动作", "recommended_action"),
    ]
    return [
        {"title": title, "body": body}
        for title, key in fields
        if (body := _text(result.get(key)))
    ]


def _evaluation_vc_view(vc: dict[str, Any]) -> dict[str, Any]:
    startup_opportunities = _string_list(vc.get("startup_opportunities"))
    investment_risks = _string_list(vc.get("investment_risks"))
    view = {
        "impact": _text(vc.get("impact")),
        "market_relevance": _text(vc.get("market_relevance")),
        "commercialization_path": _text(vc.get("commercialization_path")),
        "startup_opportunities": startup_opportunities,
        "investment_risks": investment_risks,
    }
    view["has_content"] = any(
        [
            view["impact"],
            view["market_relevance"],
            view["commercialization_path"],
            startup_opportunities,
            investment_risks,
        ]
    )
    return view


def _evaluation_profile_label(profile_name: str, model: str) -> str:
    if profile_name and model:
        return f"{profile_name} / {model}"
    return profile_name or model


def _evaluation_export_block(evaluation: dict[str, Any]) -> list[str]:
    lines = [
        f"## {evaluation['type_label']} - {evaluation['status_label']} - {evaluation['created_at']}",
        "",
        f"- Prompt: {evaluation['prompt_label']}",
        f"- Model: {evaluation['profile_label']}",
        "",
    ]
    if evaluation["has_result"]:
        lines.extend(
            [
                "```json",
                json.dumps(evaluation["result_json"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    else:
        if evaluation["error_message"]:
            lines.extend(["Error:", "", evaluation["error_message"], ""])
        if evaluation["raw_output"]:
            lines.extend(["Raw output:", "", "```text", evaluation["raw_output"], "```", ""])
    return lines


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(source.get(key))
        if value:
            return value
    return ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_text(item) for item in value) if item]


def crawl_all_categories(
    category_ids: list[int] | None = None,
    crawl_date: str | None = None,
    progress: ProgressCallback | None = None,
    pipeline_job_id: int | None = None,
    category_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    crawl_started = perf_counter()
    categories = category_snapshot if category_snapshot is not None else db.list_categories(enabled_only=True)
    if category_ids:
        allowed = set(category_ids)
        categories = [category for category in categories if int(category["id"]) in allowed]
    saved = 0
    category_results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    total = len(categories)
    lock = RLock()
    completed = 0
    succeeded = 0
    warning = 0
    empty_success = 0
    failed = 0
    category_started_at: dict[str, float] = {}
    category_state = {
        category["category"]: {
            "category": category["category"],
            "name": category.get("name") or "",
            "status": "pending",
            "top_n": int(category.get("top_n") or 30),
            "sort_param": category.get("sort_param") or "sort=1",
            "papers": 0,
            "crawl_date": crawl_date or "",
            "attempt": 0,
            "max_attempts": db.get_int_setting("crawler.retries", 2) + 1,
            "url": "",
            "error_code": "",
            "error": "",
        }
        for category in categories
    }

    def snapshot() -> dict[str, Any]:
        return {
            "phase": "crawl",
            "summary": {
                "total": total,
                "completed": completed,
                "success": succeeded,
                "warning": warning,
                "empty_success": empty_success,
                "failed": failed,
                "saved": saved,
                "running": sum(1 for item in category_state.values() if item["status"] == "running"),
                "pending": sum(1 for item in category_state.values() if item["status"] == "pending"),
                "crawl_date": crawl_date or "",
            },
            "categories": [dict(item) for item in category_state.values()],
        }

    def emit(message: str) -> None:
        if progress:
            with lock:
                details = snapshot()
                current = completed
            progress(current, total, message, details)

    def set_category_attempt(category_key: str, event: dict[str, Any]) -> None:
        with lock:
            state = category_state[category_key]
            state["status"] = "running"
            state["attempt"] = event.get("attempt") or state["attempt"]
            state["max_attempts"] = event.get("max_attempts") or state["max_attempts"]
            event_metrics = event.get("metrics") or {}
            state["url"] = (
                event.get("request_url")
                or event_metrics.get("request_url")
                or state["url"]
            )
            if event.get("error_code"):
                state["error_code"] = event["error_code"]
            attempt = state["attempt"]
            max_attempts = state["max_attempts"]
        _append_http_attempt_events(
            pipeline_job_id,
            crawl_date,
            category_key,
            [event],
        )
        emit(f"正在抓取 {category_key}（第 {attempt}/{max_attempts} 次尝试）")

    if progress:
        date_label = f" {crawl_date}" if crawl_date else ""
        progress(0, total, f"准备抓取{date_label} {total} 个类目", snapshot())
    concurrency = max(1, db.get_int_setting("crawler.concurrency", 6))
    with _crawler_client_from_settings() as client:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for category in categories:
                category_started_at[category["category"]] = perf_counter()
                _append_crawl_event(
                    pipeline_job_id,
                    crawl_date,
                    category["category"],
                    "started",
                    "crawl_http",
                    "crawl.category_started",
                    metrics={
                        "target_date": crawl_date,
                        "category": category["category"],
                        "top_n": int(category.get("top_n") or 30),
                    },
                    message=f"开始抓取 {category['category']}",
                )
            futures = {
                executor.submit(
                    _fetch_category_from_config,
                    category,
                    crawl_date,
                    lambda event, key=category["category"]: set_category_attempt(key, event),
                    client,
                ): category
                for category in categories
            }
            for future in as_completed(futures):
                category = futures[future]
                category_key = category["category"]
                try:
                    fetch_result = future.result()
                except Exception as exc:
                    error_code = (
                        exc.error_code if isinstance(exc, CrawlFetchError) else "network_http_error"
                    )
                    fetch_metrics = (
                        dict(exc.metrics) if isinstance(exc, CrawlFetchError) else {}
                    )
                    terminal_metrics = {
                        **fetch_metrics,
                        "target_date": crawl_date,
                        "category": category_key,
                        "persisted_count": 0,
                        "new_count": 0,
                        "updated_count": 0,
                        "duplicate_count": 0,
                        "failed_count": 0,
                        "final_status": "failed",
                        "total_ms": _crawl_unit_elapsed_ms(
                            category_started_at, category_key, crawl_started
                        ),
                    }
                    failures.append(
                        {"category": category_key, "error": str(exc), "error_code": error_code}
                    )
                    _append_crawl_terminal_event(
                        pipeline_job_id,
                        crawl_date,
                        category_key,
                        "failed",
                        terminal_metrics,
                        error_code,
                    )
                    with lock:
                        completed += 1
                        failed += 1
                        category_state[category_key]["status"] = "failed"
                        category_state[category_key]["error_code"] = error_code
                        category_state[category_key]["error"] = str(exc)
                    category_results.append(
                        {
                            "category": category_key,
                            "crawl_date": crawl_date or "",
                            "status": "failed",
                            "error_code": error_code,
                            "metrics": terminal_metrics,
                        }
                    )
                    emit(f"抓取 {category_key} 失败：{error_code}")
                    continue

                _append_parse_events(
                    pipeline_job_id,
                    crawl_date,
                    category_key,
                    fetch_result,
                )
                saved_crawl_date = (
                    crawl_date
                    or str(fetch_result.metrics.get("page_date") or "")
                    or crawl_date_from_papers(fetch_result.papers)
                )
                persist_metrics = {
                    "persist_input_count": len(fetch_result.papers),
                    "unique_count": 0,
                    "persisted_count": 0,
                    "new_count": 0,
                    "updated_count": 0,
                    "duplicate_count": 0,
                    "membership_new_count": 0,
                    "membership_updated_count": 0,
                    "failed_count": 0,
                }
                unit_status = fetch_result.status
                error_code = (
                    str(fetch_result.metrics.get("primary_error_code") or "")
                    or (fetch_result.error_codes[0] if fetch_result.error_codes else None)
                )
                if unit_status != "failed":
                    try:
                        if fetch_result.papers:
                            persisted = db.upsert_papers_with_stats(
                                fetch_result.papers,
                                category_key,
                                saved_crawl_date,
                            )
                            persist_metrics = persisted.metrics()
                            persist_metrics["paper_ids"] = list(dict.fromkeys(persisted.paper_ids))
                            persist_metrics['new_paper_ids'] = persisted.new_paper_ids
                        _append_crawl_event(
                            pipeline_job_id,
                            saved_crawl_date,
                            category_key,
                            "persist_completed",
                            "persist",
                            "crawl.persist_completed",
                            metrics=persist_metrics,
                            message=f"{category_key} 入库完成",
                        )
                    except Exception:
                        logger.exception("Persisting crawl unit failed category=%s", category_key)
                        unit_status = "failed"
                        error_code = "database_write_failed"
                        persist_metrics = {
                            **persist_metrics,
                            "failed_count": max(1, len(fetch_result.papers)),
                        }
                terminal_metrics = {
                    **fetch_result.metrics,
                    **persist_metrics,
                    "target_date": crawl_date,
                    "page_date": fetch_result.metrics.get("page_date"),
                    "crawl_date": saved_crawl_date,
                    "category": category_key,
                    "final_status": unit_status,
                    "total_ms": _crawl_unit_elapsed_ms(
                        category_started_at, category_key, crawl_started
                    ),
                }
                _append_crawl_terminal_event(
                    pipeline_job_id,
                    saved_crawl_date,
                    category_key,
                    unit_status,
                    terminal_metrics,
                    error_code,
                )
                category_results.append(
                    {
                        "category": category_key,
                        "papers": len(fetch_result.papers),
                        "crawl_date": saved_crawl_date,
                        "status": unit_status,
                        "error_code": error_code,
                        "metrics": terminal_metrics,
                    }
                )
                with lock:
                    saved += int(persist_metrics["persisted_count"])
                    completed += 1
                    if unit_status == "success":
                        succeeded += 1
                    elif unit_status == "empty_success":
                        empty_success += 1
                    elif unit_status == "warning":
                        warning += 1
                    else:
                        failed += 1
                        failures.append(
                            {
                                "category": category_key,
                                "error": error_code or "crawl unit failed",
                                "error_code": error_code or "parse_incomplete",
                            }
                        )
                    category_state[category_key]["status"] = unit_status
                    category_state[category_key]["papers"] = len(fetch_result.papers)
                    category_state[category_key]["crawl_date"] = saved_crawl_date
                    category_state[category_key]["error_code"] = error_code or ""
                    category_state[category_key]["error"] = error_code or ""
                emit(
                    f"已处理 {category_key}：{len(fetch_result.papers)} 篇，状态 {unit_status}；"
                    f"成功 {succeeded}/{total}，warning {warning}，失败 {failed}"
                )
    overall_status = _crawl_overall_status(category_results)
    if failures and pipeline_job_id is None:
        failure_text = "；".join(f"{item['category']}: {_short_error(item['error'], 160)}" for item in failures)
        emit(f"抓取完成：成功 {succeeded}/{total}，失败 {failed}。{failure_text}")
        raise RuntimeError(f"抓取失败：{failure_text}")
    return {
        "saved": saved,
        "status": overall_status,
        "summary": {
            "total": total,
            "success": succeeded,
            "warning": warning,
            "empty_success": empty_success,
            "failed": failed,
            "saved": saved,
        },
        "categories": category_results,
    }


def _crawl_overall_status(category_results: list[dict[str, Any]]) -> str:
    statuses = [str(item.get("status") or "failed") for item in category_results]
    if not statuses or all(status in {"success", "empty_success"} for status in statuses):
        return "success"
    if all(status == "failed" for status in statuses):
        return "failed"
    return "partial_success"


def _crawl_unit_elapsed_ms(
    started_at: dict[str, float],
    category: str,
    fallback: float,
) -> int:
    return max(0, int(round((perf_counter() - started_at.get(category, fallback)) * 1000)))


def _crawl_event_key(
    pipeline_job_id: int,
    crawl_date: str | None,
    category: str,
    suffix: str,
) -> str:
    return f"crawl:{pipeline_job_id}:{crawl_date or 'latest'}:{category}:{suffix}"


def _append_crawl_event(
    pipeline_job_id: int | None,
    crawl_date: str | None,
    category: str,
    key_suffix: str,
    stage: str,
    event_type: str,
    *,
    level: str = "info",
    attempt: int = 1,
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str = "",
) -> None:
    if pipeline_job_id is None:
        return
    db.append_job_event(
        pipeline_job_id,
        _crawl_event_key(pipeline_job_id, crawl_date, category, key_suffix),
        stage,
        event_type,
        level=level,
        category=category,
        crawl_date=crawl_date,
        attempt=attempt,
        metrics=metrics or {},
        error_code=error_code,
        message=message,
    )


def _append_http_attempt_events(
    pipeline_job_id: int | None,
    crawl_date: str | None,
    category: str,
    events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> None:
    for event in events:
        attempt = max(1, int(event.get("attempt") or 1))
        max_attempts = max(attempt, int(event.get("max_attempts") or attempt))
        event_name = str(event.get("event") or "")
        if event_name in {"attempt", "attempt_started"}:
            if attempt <= 1:
                continue
            _append_crawl_event(
                pipeline_job_id,
                crawl_date,
                category,
                f"attempt:{attempt}:retrying",
                "crawl_http",
                "crawl.http_retrying",
                level="warning",
                attempt=attempt,
                metrics={
                    "request_url": event.get("request_url"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                },
                message=f"{category} 正在进行第 {attempt} 次 HTTP 尝试",
            )
        elif event_name == "http_succeeded":
            _append_crawl_event(
                pipeline_job_id,
                crawl_date,
                category,
                f"attempt:{attempt}:http_succeeded",
                "crawl_http",
                "crawl.http_succeeded",
                attempt=attempt,
                metrics=event.get("metrics") or {},
                message=f"{category} HTTP 请求成功",
            )
        elif event_name in {"attempt_failed", "http_failed"}:
            is_final = attempt >= max_attempts
            error_code = str(event.get("error_code") or "network_http_error")
            _append_crawl_event(
                pipeline_job_id,
                crawl_date,
                category,
                f"attempt:{attempt}:http_failed",
                "crawl_http",
                "crawl.http_failed",
                level="error" if is_final else "warning",
                attempt=attempt,
                metrics=event.get("metrics") or {},
                error_code=error_code,
                message=f"{category} HTTP 请求失败（{error_code}）",
            )


def _append_parse_events(
    pipeline_job_id: int | None,
    crawl_date: str | None,
    category: str,
    result: CategoryFetchResult,
) -> None:
    _append_crawl_event(
        pipeline_job_id,
        crawl_date,
        category,
        "parse_completed",
        "crawl_parse",
        "crawl.parse_completed",
        metrics=result.metrics,
        message=f"{category} 页面解析完成",
    )
    for error_code in result.error_codes:
        _append_crawl_event(
            pipeline_job_id,
            crawl_date,
            category,
            f"anomaly:{error_code}",
            "crawl_parse",
            "crawl.parse_anomaly",
            level="error" if result.status == "failed" else "warning",
            metrics=result.metrics,
            error_code=error_code,
            message=f"{category} 完整性异常（{error_code}）",
        )


def _append_crawl_terminal_event(
    pipeline_job_id: int | None,
    crawl_date: str | None,
    category: str,
    status: str,
    metrics: dict[str, Any],
    error_code: str | None,
) -> None:
    level = "error" if status == "failed" else "warning" if status == "warning" else "info"
    _append_crawl_event(
        pipeline_job_id,
        crawl_date,
        category,
        "terminal",
        "persist",
        "crawl.category_completed",
        level=level,
        attempt=max(1, int(metrics.get("retry_count") or 0) + 1),
        metrics=metrics,
        error_code=error_code,
        message=f"{category} 抓取单元结束，状态 {status}",
    )


def crawl_to_latest(
    category_ids: list[int] | None = None,
    progress: ProgressCallback | None = None,
    pipeline_job_id: int | None = None,
) -> dict[str, Any]:
    latest_db_date = db.get_latest_crawl_date()
    latest_target_date = latest_available_arxiv_date()
    latest_reference_date = db.get_latest_crawl_date_on_or_before(latest_target_date)
    plan = build_catch_up_date_plan(latest_db_date, latest_target_date, latest_reference_date)
    missing_dates = plan["missing_dates"]
    categories = db.list_categories(enabled_only=True)
    if category_ids:
        allowed = set(category_ids)
        categories = [category for category in categories if int(category["id"]) in allowed]
    per_date_total = max(1, len(categories))
    overall_total = max(1, len(missing_dates) * per_date_total)
    date_state = {
        crawl_date: {"date": crawl_date, "status": "pending", "saved": 0, "error": ""}
        for crawl_date in missing_dates
    }

    def snapshot(current_date: str | None = None, nested: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "phase": "catch_up",
            "summary": {
                "latest_db_date": latest_db_date or "",
                "latest_reference_date": latest_reference_date or "",
                "latest_target_date": latest_target_date,
                "total_dates": len(missing_dates),
                "completed_dates": sum(1 for item in date_state.values() if item["status"] == "success"),
                "failed_dates": sum(1 for item in date_state.values() if item["status"] == "failed"),
                "current_date": current_date or "",
            },
            "dates": [dict(item) for item in date_state.values()],
            "current": nested or {},
        }

    if not missing_dates:
        if progress:
            progress(
                1,
                1,
                _catch_up_status_message("metadata 已是最新", plan),
                snapshot(),
            )
        return {
            "saved": 0,
            "status": "success",
            "latest_db_date": latest_db_date,
            "latest_reference_date": latest_reference_date,
            "latest_target_date": latest_target_date,
            "dates": [],
        }

    if progress:
        progress(
            0,
            overall_total,
            f"{_catch_up_status_message('准备补抓 metadata', plan)}，共 {len(missing_dates)} 个日期",
            snapshot(),
        )

    saved_total = 0
    date_results: list[dict[str, Any]] = []
    for date_index, target_date in enumerate(missing_dates):
        date_state[target_date]["status"] = "running"

        def date_progress(
            current: int,
            total: int,
            message: str,
            details: dict[str, Any] | None = None,
            date_index: int = date_index,
            target_date: str = target_date,
        ) -> None:
            if not progress:
                return
            base = date_index * per_date_total
            translated_current = min(overall_total, base + min(current, per_date_total))
            progress(
                translated_current,
                overall_total,
                f"{target_date}：{message}",
                snapshot(target_date, details),
            )

        try:
            result = crawl_all_categories(
                category_ids,
                crawl_date=target_date,
                progress=date_progress,
                pipeline_job_id=pipeline_job_id,
            )
            saved_for_date = int(result.get("saved") or 0)
            saved_total += saved_for_date
            date_results.append(
                {
                    "date": target_date,
                    "saved": saved_for_date,
                    "status": result.get("status") or "success",
                    "categories": result.get("categories") or [],
                }
            )
            date_state[target_date]["status"] = str(result.get("status") or "success")
            date_state[target_date]["saved"] = saved_for_date
        except Exception as exc:
            date_state[target_date]["status"] = "failed"
            date_state[target_date]["error"] = str(exc)
            if progress:
                progress(
                    min(overall_total, (date_index + 1) * per_date_total),
                    overall_total,
                    f"{target_date} metadata 抓取失败：{_short_error(str(exc), 180)}",
                    snapshot(target_date),
                )
            raise

    if progress:
        progress(
            overall_total,
            overall_total,
            f"metadata 补抓完成：{len(missing_dates)} 个日期，保存 {saved_total} 条类目记录",
            snapshot(missing_dates[-1]),
        )
    return {
        "saved": saved_total,
        "status": _crawl_overall_status(
            [{"status": item.get("status") or "success"} for item in date_state.values()]
        ),
        "latest_db_date": latest_db_date,
        "latest_reference_date": latest_reference_date,
        "latest_target_date": latest_target_date,
        "dates": date_results,
    }


def build_catch_up_date_plan(
    latest_db_date: str | None,
    latest_target_date: str,
    latest_reference_date: str | None,
) -> dict[str, Any]:
    reference = latest_reference_date
    if reference and reference > latest_target_date:
        reference = None
    return {
        "latest_db_date": latest_db_date,
        "latest_reference_date": reference,
        "latest_target_date": latest_target_date,
        "missing_dates": available_arxiv_dates_after(reference, latest_target_date),
        "ignored_later_db_date": bool(latest_db_date and latest_db_date > latest_target_date),
    }


def _catch_up_status_message(prefix: str, plan: dict[str, Any]) -> str:
    reference = plan.get("latest_reference_date") or "无"
    target = plan.get("latest_target_date") or "未知"
    message = f"{prefix}：目标范围内数据库最新 {reference}，最新可抓日期 {target}"
    if plan.get("ignored_later_db_date"):
        message += f"；已忽略晚于目标范围的数据库最大日期 {plan.get('latest_db_date')}"
    return message


def _fetch_category_from_config(
    category: dict[str, Any],
    crawl_date: str | None = None,
    attempt_progress: Callable[[dict[str, Any]], None] | None = None,
    client: httpx.Client | None = None,
) -> CategoryFetchResult:
    try:
        missing_field_warning_rate = float(
            db.get_setting("crawler.missing_field_warning_rate", 0.0)
        )
    except (TypeError, ValueError):
        missing_field_warning_rate = 0.0
    return fetch_category_report(
        category["category"],
        top_n=int(category.get("top_n") or 30),
        sort_param=category.get("sort_param") or "sort=1",
        timeout_seconds=db.get_int_setting("crawler.timeout_seconds", 20),
        retries=db.get_int_setting("crawler.retries", 2),
        user_agent=str(db.get_setting("crawler.user_agent", "DailyCoolPapers/0.1")),
        trust_env_proxy=db.get_bool_setting("crawler.trust_env_proxy", False),
        proxy_url=str(db.get_setting("crawler.proxy_url", "") or ""),
        crawl_date=crawl_date,
        missing_field_warning_rate=missing_field_warning_rate,
        attempt_progress=attempt_progress,
        client=client,
    )


def _crawler_client_from_settings() -> httpx.Client:
    client_kwargs = {
        "timeout": db.get_int_setting("crawler.timeout_seconds", 20),
        "follow_redirects": True,
    }
    client_kwargs.update(
        httpx_proxy_kwargs(
            explicit_proxy_url=str(db.get_setting("crawler.proxy_url", "") or ""),
            use_system_proxy=db.get_bool_setting("crawler.trust_env_proxy", False),
        )
    )
    return httpx.Client(**client_kwargs)


def _short_error(error: str, limit: int = 120) -> str:
    cleaned = " ".join(str(error).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit].rstrip() + "..."


SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
PROFILE_SNAPSHOT_FIELDS = (
    "id", "model", "provider", "temperature", "max_output_tokens",
    "context_window_tokens", "timeout_seconds",
)


def _profile_binding(profile: dict[str, Any]) -> str:
    # Credentials and transport headers never enter job payloads or events.
    binding = {key: profile.get(key) for key in ("base_url", "custom_headers", "encrypted_api_key_ref")}
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def build_daily_pipeline_plan(
    trigger_source: str, category_ids: list[int] | None = None, *,
    start_date: str | None = None, end_date: str | None = None,
    now: datetime | None = None,
    category_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    current = current.astimezone(SHANGHAI_TZ)
    latest = latest_available_arxiv_date(current)
    categories = category_snapshot if category_snapshot is not None else db.list_categories(enabled_only=True)
    if category_ids and category_snapshot is None:
        categories = [item for item in categories if int(item['id']) in set(category_ids)]
    if not categories:
        raise ValueError("请至少启用一个抓取类目")
    if bool(start_date) != bool(end_date):
        raise ValueError("起止日期必须同时提供")
    if start_date and end_date:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end or end.isoformat() > latest:
            raise ValueError("日期范围无效或超出最新可抓日期")
        dates = available_arxiv_dates_after((start - timedelta(days=1)).isoformat(), end.isoformat())
    elif trigger_source == "manual_catch_up":
        reference = db.get_latest_completed_pipeline_date(latest, [item['category'] for item in categories])
        dates = available_arxiv_dates_after(reference, latest)
        # An interrupted first run has no completed cursor: keep its earlier planned dates.
        if reference is None:
            prior_dates = [day for job in db.list_jobs(100) if job['type'] == db.DAILY_PIPELINE_JOB_TYPE
                           for day in db.loads_json(job.get('payload'), {}).get('dates', []) if day <= latest]
            if prior_dates:
                dates = available_arxiv_dates_after(
                    (date.fromisoformat(min(prior_dates)) - timedelta(days=1)).isoformat(), latest)
    else:
        dates = [latest]
    category_plan = []
    for item in categories:
        category_plan.append({
            "id": int(item['id']), "category": item['category'], "name": item.get('name', ''),
            "top_n": int(item.get('top_n') or 30),
            "sort_param": urlencode([(key, value) for key, value in parse_qsl(item.get('sort_param') or 'sort=1') if key == 'sort']),
        })
    plan: dict[str, Any] = {
        "trigger_source": trigger_source, "timezone": "Asia/Shanghai",
        "plan_created_at": current.astimezone(timezone.utc).isoformat(),
        "dates": dates, "target_date": latest,
        "start_date": dates[0] if dates else latest, "end_date": dates[-1] if dates else latest,
        "categories": category_plan,
        "abstract_concurrency": max(1, db.get_int_setting("llm.abstract_concurrency", 4)),
        "abstract_retries": max(0, db.get_int_setting("llm.abstract_retries", 2)),
        "directions": db.list_attention_directions(active_only=True),
    }
    try:
        config = resolve_evaluation_config("abstract_review")
        plan['abstract_config'] = {
            "prompt": {key: config.prompt.get(key) for key in ('id', 'version', 'type', 'template', 'enabled')},
            "profile": {key: config.profile.get(key) for key in PROFILE_SNAPSHOT_FIELDS},
            "binding": _profile_binding(config.profile),
        }
    except ValueError:
        plan['abstract_config_error'] = 'evaluation_config_missing'
    if plan['directions']:
        try:
            plan['classification_config'] = evaluation_config_snapshot('direction_classification')
        except ValueError:
            plan['classification_config_error'] = 'evaluation_config_missing'
    return plan


def evaluation_config_snapshot(evaluation_type):
    config = resolve_evaluation_config(evaluation_type)
    profile = {key: config.profile.get(key) for key in PROFILE_SNAPSHOT_FIELDS}
    if evaluation_type in {'direction_classification','investment_memo'}:
        profile['allow_response_format_fallback'] = False
    return {'prompt': {key: config.prompt.get(key) for key in ('id','version','type','template','enabled')},
            'profile': profile,
            'binding': _profile_binding(config.profile)}


def _pipeline_evaluation_config(plan: dict[str, Any], evaluation_type='abstract_review') -> EvaluationConfig:
    key = 'classification_config' if evaluation_type == 'direction_classification' else 'abstract_config'
    snapshot = plan.get(key) or {}
    profile = db.get_llm_profile((snapshot.get('profile') or {}).get('id')) if snapshot else None
    if not profile or _profile_binding(profile) != snapshot.get('binding'):
        raise ValueError('evaluation_config_missing')
    return EvaluationConfig(evaluation_type, snapshot['prompt'], {**profile, **snapshot['profile']})


def validate_classification_result(result, direction_ids):
    def invalid():
        raise LLMError('分类结果不符合固定契约', code='invalid_classification_result', retryable=True)
    if not isinstance(result, dict) or not isinstance(result.get('directions'), list):
        invalid()
    rows, seen = [], set()
    for row in result['directions']:
        if not isinstance(row, dict):
            invalid()
        key = row.get('direction_id')
        if type(key) is not int or key not in direction_ids or key in seen:
            invalid()
        if not isinstance(row.get('decision'),str) or row['decision'] not in {'matched','possible','unmatched'} or not isinstance(row.get('reason'), str) or not row['reason'].strip():
            invalid()
        seen.add(key)
        rows.append({name:row[name] for name in ('direction_id','decision','reason')})
    if seen != set(direction_ids):
        invalid()
    return rows


def aggregate_direction_results(rows):
    states = {row['model_decision'] for row in rows}
    return {'state': next((state for state in ('matched','possible','failed','unmatched') if state in states), 'unclassified'),
            'has_partial_failure': 'failed' in states,
            'effective': any(row.get('effective') for row in rows), 'pending': any(row.get('pending') for row in rows)}


def classify_candidate(paper_id, metadata, plan, job_id, *, config=None, client=None, source='daily', can_classify=True, explicit=False):
    directions = plan.get('directions', [])
    snapshot_ids = [d['id'] for d in directions]
    def event(kind, metrics, *, attempt=1, terminal=False, code=None):
        db.append_job_event(job_id, f'classification:{job_id}:{paper_id}:' + ('terminal' if terminal else f'{attempt}:{kind}'),
                            'classification', f'classification.{kind}', paper_id=paper_id, attempt=attempt,
                            level='error' if kind == 'paper_failed' else 'info', metrics=metrics, error_code=code)
    if not metadata or not all(str(metadata.get(key) or '').strip() for key in ('title','abstract')):
        outcome = {'input_incomplete':1}
        event('paper_skipped_existing', outcome, terminal=True, code='input_incomplete')
        return outcome
    if not can_classify:
        outcome = {'already_classified':1}
        event('paper_skipped_existing', outcome, terminal=True)
        return outcome
    token, missing, reason = db.claim_classification(paper_id, snapshot_ids, job_id, daily=source == 'daily' and not explicit)
    if not token:
        outcome = {reason:1}
        event('paper_skipped_existing', outcome, terminal=True)
        return outcome
    directions = [d for d in directions if d['id'] in missing]
    started = perf_counter()
    outcome = {'calls':0,'call_success':0,'call_failed':0,'retry_count':0,'input_tokens':0,'output_tokens':0}
    try:
        for attempt in range(1,4):
            evaluation_id = db.start_classification_attempt(paper_id,job_id,source,directions,metadata,plan.get('classification_config'),attempt)
            event('paper_started', {'evaluation_id':evaluation_id, 'direction_count':len(directions)},attempt=attempt)
            response, result, code, retryable = None, None, None, False
            if not config:
                code = 'evaluation_config_missing'
            else:
                try:
                    prompt = render_prompt(config.prompt['template'], {**metadata,
                        'directions_json':json.dumps(directions,ensure_ascii=False),
                        'metadata_json':json.dumps(metadata,ensure_ascii=False)})
                    EvaluationRunner()._ensure_context_window(prompt, config.profile)
                    outcome['calls'] += 1
                    response = call_llm(config.profile,prompt,client=client)
                    result = validate_classification_result(response.result_json, missing)
                except sqlite3.DatabaseError:
                    raise
                except Exception as exc:
                    retryable = bool(getattr(exc,'retryable',False))
                    code = 'invalid_classification_result' if getattr(exc,'code',None) == 'invalid_classification_result' else ('provider_retryable_error' if retryable else 'provider_terminal_error')
            usage = getattr(response,'usage',None) or {}
            usage = usage if isinstance(usage,dict) else {}
            for key in ('input_tokens','output_tokens'):
                value = usage.get(key,usage.get('prompt_tokens' if key=='input_tokens' else 'completion_tokens',0))
                if isinstance(value,(int,float)) and not isinstance(value,bool):
                    outcome[key] += max(0,int(value))
            will_retry = bool(code and retryable and attempt < 3)
            db.finish_classification_attempt(evaluation_id, result=result, raw_output=response.raw_text if response else None,
                error_code=code,retryable=retryable,terminal=not will_retry,usage=usage)
            outcome['call_success' if result else 'call_failed'] += int(response is not None or outcome['calls'] > attempt-1)
            if will_retry:
                outcome['retry_count'] += 1
                event('paper_retrying', {'evaluation_id':evaluation_id},attempt=attempt,code=code)
                _abstract_retry_wait(attempt)
                continue
            outcome.update({'success' if result else 'failed':1, 'evaluation_id':evaluation_id,
                            'duration_ms':round((perf_counter()-started)*1000)})
            event('paper_succeeded' if result else 'paper_failed',outcome,attempt=attempt,terminal=True,code=code)
            return outcome
    finally:
        db.release_classification_claim(token)


def run_classification_stage(job_id, plan, paper_ids, inputs, *, new_ids=None, source='daily', explicit=False, progress=None):
    started = perf_counter()
    directions = plan.get('directions', [])
    summary = {key:0 for key in ('success','failed','input_incomplete','already_classified','classification_already_running',
               'calls','call_success','call_failed','retry_count','matched','possible','unmatched','failed_papers','input_tokens','output_tokens')}
    summary.update({'candidate_count':len(paper_ids),'direction_count':len(directions),'direction_ids':[d['id'] for d in directions]})
    db.append_job_event(job_id,f'classification:{job_id}:plan','classification','classification.plan_created',metrics=summary)
    if not directions:
        summary['status'] = 'skipped_no_active_directions'
        db.append_job_event(job_id,f'classification:{job_id}:skip','classification','classification.skipped_no_active_directions',
            message='当前未启用关注方向，将对全部新论文执行摘要评估。',metrics=summary)
        return paper_ids,summary
    config = None
    try:
        config = _pipeline_evaluation_config(plan,'direction_classification')
    except ValueError:
        summary['config_error'] = 'evaluation_config_missing'
    if config:
        summary.update({'prompt_id':config.prompt_id,'prompt_version':config.prompt_version,'profile_id':config.profile_id,'model':config.model})
    incomplete = set()
    with (make_llm_client(config.profile) if config else nullcontext(None)) as client:
        with ThreadPoolExecutor(max_workers=max(1,min(len(paper_ids),plan['abstract_concurrency']))) as executor:
            futures = {executor.submit(classify_candidate,paper_id,inputs.get(paper_id),plan,job_id,config=config,client=client,source=source,
                        can_classify=new_ids is None or paper_id in new_ids or explicit,explicit=explicit):paper_id for paper_id in paper_ids}
            for count,future in enumerate(as_completed(futures),1):
                outcome = future.result()
                for key in summary:
                    if key in outcome and isinstance(summary[key],int):
                        summary[key] += outcome[key]
                if outcome.get('input_incomplete'):
                    incomplete.add(futures[future])
                if progress:
                    progress(count,len(paper_ids),f'关注方向分类 {count}/{len(paper_ids)}',{'phase':'classification','classification':dict(summary)})
    results = db.paper_direction_results(paper_ids)
    eligible, partial = [], False
    for paper_id in paper_ids:
        rows = [r for r in results.get(paper_id,[]) if r['direction_id'] in summary['direction_ids']]
        aggregate = aggregate_direction_results(rows)
        partial |= aggregate['has_partial_failure']
        state = aggregate['state']
        if state in ('matched','possible','unmatched'):
            summary[state] += 1
        if state == 'failed':
            summary['failed_papers'] += 1
        if state in ('matched','possible') and paper_id not in incomplete:
            eligible.append(paper_id)
    summary['has_partial_failure'] = partial
    summary['status'] = 'failed' if summary.get('config_error') and summary['failed'] else ('partial_success' if partial or summary['failed'] else 'success')
    summary['duration_ms'] = round((perf_counter()-started)*1000)
    db.append_job_event(job_id,f'classification:{job_id}:completed','classification','classification.stage_completed',metrics=summary)
    return eligible,summary


def direction_backfill_preview(direction_id, date_from, date_to):
    from .form_commands import FormValidationError
    try:
        if not all(isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) for value in (date_from,date_to)):
            raise ValueError()
        start,end = date.fromisoformat(date_from),date.fromisoformat(date_to)
        if start > end:
            raise ValueError()
    except (ValueError,TypeError):
        raise FormValidationError({'dates':'请填写有效的开始日期与结束日期（YYYY-MM-DD），开始日期不得晚于结束日期'})
    return db.preview_direction_backfill(direction_id,start.isoformat(),end.isoformat())


def build_direction_backfill_plan(direction_id,date_from,date_to):
    preview = direction_backfill_preview(direction_id,date_from,date_to)
    plan = {'directions':[preview['direction']], 'paper_ids':preview['paper_ids'],'inputs':preview['inputs'],
            'start_date':preview['date_from'],'end_date':preview['date_to'],'preview':preview['counts'],
            'trigger_source':'historical_backfill','plan_created_at':db.now_iso(),
            'abstract_concurrency':max(1,db.get_int_setting('llm.abstract_concurrency',4)),
            'abstract_retries':min(2,max(0,db.get_int_setting('llm.abstract_retries',2)))}
    for kind,key in [('direction_classification','classification_config'),('abstract_review','abstract_config')]:
        try:
            plan[key] = evaluation_config_snapshot(kind)
        except ValueError:
            plan[key+'_error'] = 'evaluation_config_missing'
    return plan


def run_direction_backfill(job_id,plan,progress=None):
    started = perf_counter()
    db.append_job_event(job_id,f'backfill:{job_id}:start','direction_backfill','direction_backfill.started',
                        metrics={'candidate_count':len(plan['paper_ids']),'direction_count':1})
    inputs = {int(key):value for key,value in plan['inputs'].items()}
    eligible,classification = run_classification_stage(job_id,plan,plan['paper_ids'],inputs,source='historical_backfill',explicit=True,progress=progress)
    # A failed new direction never hides a failure behind a previously matched direction.
    prior_results = db.paper_direction_results(plan['paper_ids'])
    mixed_failure = any(any(r['model_decision']=='failed' for r in rows) and any(r['model_decision'] in {'matched','possible'} for r in rows)
                        for rows in prior_results.values())
    classification['has_partial_failure'] |= mixed_failure
    abstract,config_error = run_pipeline_abstract_stage(job_id,plan,eligible,progress=progress)
    classification.update({'abstract_new':abstract['success'],'abstract_reused':abstract['already_successful'],'abstract_failed':abstract['failed']})
    status = classification['status']
    if status == 'success' and (abstract['failed'] or mixed_failure):
        status = 'partial_success'
    if config_error and abstract['failed']:
        status = 'failed'
    result = {'status':status,'classification':classification,'abstract':abstract,'preview':plan['preview'],
              'duration_ms':round((perf_counter()-started)*1000)}
    db.append_job_event(job_id,f'backfill:{job_id}:complete','direction_backfill','direction_backfill.completed',metrics=result,
                        level='info' if status=='success' else 'warning')
    if progress:
        progress(1,1,'历史补分类已结束',{'phase':'finalize',**result})
    return result


def run_daily_pipeline(job_id: int, plan: dict[str, Any], progress: ProgressCallback | None = None) -> dict[str, Any]:
    started = perf_counter()
    job = db.get_job(job_id)
    previous = {}
    if job and job.get('retry_of_job_id'):
        previous = {(event['crawl_date'], event['category']): event for event in db.pipeline_retry_units(
            job['retry_of_job_id'])}
    for target_date in plan['dates']:
        pending = []
        for category in plan['categories']:
            prior = previous.get((target_date, category['category']))
            if plan.get('retry_mode') == 'abstract_only':
                metrics = dict(prior['metrics']) if prior else {'final_status': 'failed', 'persisted_count': 0}
                _append_crawl_terminal_event(job_id, target_date, category['category'], metrics['final_status'],
                    {**metrics, 'reused_from_job_id': job['retry_of_job_id']},
                    prior['error_code'] if prior else 'pipeline_interrupted')
            elif prior and prior['metrics'].get('final_status') in {'success', 'empty_success'}:
                _append_crawl_terminal_event(
                    job_id, target_date, category['category'], prior['metrics']['final_status'],
                    {**prior['metrics'], 'reused_from_job_id': job['retry_of_job_id']}, None,
                )
            else:
                pending.append(category)
        if pending:
            crawl_all_categories(crawl_date=target_date, progress=progress, pipeline_job_id=job_id, category_snapshot=pending)
    units = db.list_all_job_events(job_id, event_type='crawl.category_completed')
    expected_units = len(plan['dates']) * len(plan['categories'])
    if len(units) != expected_units:
        raise RuntimeError('pipeline_units_incomplete')
    candidates = [int(paper_id) for unit in units
                  if unit['metrics'].get('final_status') in {'success', 'warning'}
                  for paper_id in unit['metrics'].get('paper_ids', [])]
    unique_ids = list(dict.fromkeys(candidates))
    new_ids = {key for unit in units for key in unit['metrics'].get('new_paper_ids', [])}
    abstract_only = plan.get('retry_mode') == 'abstract_only'
    inputs = db.classification_inputs(unique_ids, dates=plan['dates'], categories=[c['category'] for c in plan['categories']]) if plan.get('directions') else {}
    unique_ids, classification = run_classification_stage(job_id,plan,unique_ids,inputs,new_ids=set() if abstract_only else new_ids,
        explicit=bool(job and job.get('retry_of_job_id')) and not abstract_only,progress=progress)
    summary, config_error = run_pipeline_abstract_stage(job_id,plan,unique_ids,
        candidate_count=len(candidates) if not plan.get('directions') else len(unique_ids),progress=progress,
        skip_daily_failures=not bool(job and job.get('retry_of_job_id')))
    classification.update({'abstract_new':summary['success'], 'abstract_reused':summary['already_successful'],
                           'abstract_failed':summary['failed']})
    crawl_summary = {status: sum(unit['metrics'].get('final_status') == status for unit in units)
                     for status in ('success', 'empty_success', 'warning', 'failed')}
    status = _crawl_overall_status([{'status': unit['metrics']['final_status']} for unit in units])
    if (summary['failed'] or classification.get('has_partial_failure') or classification.get('failed')) and status == 'success':
        status = 'partial_success'
    if (config_error and summary['failed']) or classification['status'] == 'failed':
        status = 'failed'
    crawl_metrics = {key: sum(int(unit['metrics'].get(key) or 0) for unit in units)
                     for key in ('parsed_count', 'persisted_count', 'new_count', 'updated_count', 'duplicate_count', 'failed_count')}
    result = {'status': status, 'crawl': crawl_summary, 'abstract': summary, 'crawl_metrics': crawl_metrics,
              'classification': classification, 'duration_ms': round((perf_counter()-started)*1000)}
    if progress:
        progress(1, 1, '今日情报已更新' if status == 'success' else '流水线已结束，请检查失败或警告', {'phase': 'finalize', **result})
    return result


def run_pipeline_abstract_stage(job_id, plan, unique_ids, *, candidate_count=None, progress=None, skip_daily_failures=False):
    candidate_count = len(unique_ids) if candidate_count is None else candidate_count
    db.append_job_event(job_id, f'abstract:{job_id}:plan', 'abstract_plan', 'abstract.plan_created',
                        metrics={'candidate_count': candidate_count, 'unique_count': len(unique_ids), 'paper_ids': unique_ids})
    summary = {key: 0 for key in ('success', 'failed', 'already_successful', 'evaluation_already_running', 'input_incomplete', 'retry_count', 'terminal_failed','previous_failure')}
    abstract_started = perf_counter()
    config = None
    config_error = False
    prior_failures = db.daily_abstract_attempted(unique_ids) if skip_daily_failures else set()
    if unique_ids:
        if progress:
            progress(0, len(unique_ids), f'准备摘要评估 {len(unique_ids)} 篇论文', {'phase': 'abstract_eval', 'summary': dict(summary)})
        try:
            config = _pipeline_evaluation_config(plan)
        except ValueError:
            config_error = True
        with (make_llm_client(config.profile) if config else nullcontext(None)) as client:
            with ThreadPoolExecutor(max_workers=min(len(unique_ids), plan['abstract_concurrency'])) as executor:
                futures = [executor.submit(
                    evaluate_abstract_candidate, paper_id, config=config, llm_client=client,
                    pipeline_job_id=job_id, job_id=job_id, max_retries=plan['abstract_retries'],
                    config_error=config_error,
                    skip_prior_failure=paper_id in prior_failures,
                ) for paper_id in unique_ids]
                for index, future in enumerate(as_completed(futures), 1):
                    outcome = future.result()
                    summary[outcome.get('skip_reason') or outcome['status']] += 1
                    summary['retry_count'] += outcome.get('retry_count', 0)
                    summary['terminal_failed'] += int(outcome.get('terminal_failure', False))
                    if progress:
                        progress(index, len(unique_ids), f"摘要评估 {index}/{len(unique_ids)}", {'phase': 'abstract_eval', 'summary': dict(summary)})
    # Terminal events and persisted results are the final source of truth.
    summary = {key: 0 for key in summary}
    terminals = [event for kind in ('succeeded', 'failed', 'skipped')
                 for event in db.list_all_job_events(job_id, f'abstract.paper_{kind}')]
    successful_evaluation_ids = {item['id'] for item in db.list_pipeline_evaluations(job_id) if item['status'] == 'success'}
    if len(terminals) != len(unique_ids):
        raise RuntimeError('abstract_terminals_incomplete')
    for terminal in terminals:
        metrics = terminal['metrics']
        if metrics['status'] == 'success' and metrics['evaluation_id'] not in successful_evaluation_ids:
            raise RuntimeError('abstract_result_not_persisted')
        summary[metrics.get('skip_reason') or metrics['status']] += 1
        summary['retry_count'] += metrics.get('retry_count', 0)
        summary['terminal_failed'] += int(metrics.get('terminal_failure', False))
    summary.update({'candidate_count': candidate_count, 'unique_count': len(unique_ids),
                    'duration_ms': round((perf_counter()-abstract_started)*1000)})
    if config:
        summary.update({'prompt_id': config.prompt_id, 'prompt_version': config.prompt_version,
                        'profile_id': config.profile_id, 'model': config.model})
    db.append_job_event(job_id, f'abstract:{job_id}:completed', 'abstract_eval', 'abstract.stage_completed', metrics=summary)
    return summary,config_error


def _abstract_retry_wait(attempt: int) -> None:
    time.sleep(min(30.0, (2 ** (attempt - 1)) + random.random()))


def evaluate_abstract_candidate(
    paper_id: int, *, config: EvaluationConfig | None = None, llm_client: httpx.Client | None = None,
    pipeline_job_id: int | None = None, job_id: int | None = None, skip_success: bool = True,
    max_retries: int = 2, retry_wait: Callable[[int], None] | None = None,
    config_error: bool = False,
    raise_errors: bool = False,
    skip_prior_failure: bool = False,
) -> dict[str, Any]:
    paper = db.get_paper(paper_id)
    def event(kind: str, *, terminal: bool = False, attempt: int = 1, metrics=None, code=None):
        if pipeline_job_id is not None:
            db.append_job_event(
                pipeline_job_id, f"abstract:{pipeline_job_id}:{paper_id}:" + ('terminal' if terminal else f'{attempt}:{kind}'),
                'abstract_eval', f'abstract.paper_{kind}', paper_id=paper_id,
                arxiv_id=paper.get('arxiv_id') if paper else None, attempt=attempt,
                level='error' if kind == 'failed' else ('warning' if kind == 'retrying' else 'info'),
                metrics=metrics or {}, error_code=code, message=f'摘要评估：{kind}',
            )
    if not paper or not all(str(paper.get(key) or '').strip() for key in ('arxiv_id', 'title', 'abstract')):
        outcome = {'status': 'skipped', 'skip_reason': 'input_incomplete'}
        event('skipped', terminal=True, metrics=outcome)
        return outcome
    token, reason = db.claim_abstract_evaluation(paper_id, skip_success=skip_success, job_id=job_id, pipeline_job_id=pipeline_job_id)
    if not token:
        outcome = {'status': 'skipped', 'skip_reason': reason}
        event('skipped', terminal=True, metrics=outcome, code=reason if reason == 'evaluation_already_running' else None)
        return outcome
    started = perf_counter()
    try:
        if skip_prior_failure:
            outcome = {'status':'skipped','skip_reason':'previous_failure'}
            event('skipped',terminal=True,metrics=outcome,code='previous_failure')
            return outcome
        if config_error:
            evaluation_id = db.create_evaluation(paper_id,'abstract_review',None,None,None,None,'failed',None,None,
                'evaluation_config_missing',error_code='evaluation_config_missing',pipeline_job_id=pipeline_job_id)
            outcome = {'status': 'failed', 'terminal_failure': True,'evaluation_id':evaluation_id}
            event('failed', terminal=True, metrics=outcome, code='evaluation_config_missing')
            return outcome
        config = config or resolve_evaluation_config('abstract_review')
        runner = EvaluationRunner(config=config, llm_client=llm_client)
        for attempt in range(1, max_retries + 2):
            event('started', attempt=attempt, metrics={'prompt_id': config.prompt_id, 'profile_id': config.profile_id, 'model': config.model})
            try:
                evaluated = runner.evaluate(EvaluationRequest(paper_id, 'abstract_review', pipeline_job_id=pipeline_job_id, claim_token=token))
            except sqlite3.DatabaseError:
                raise
            except Exception as exc:
                retryable = bool(getattr(exc, 'retryable', False))
                code = 'invalid_llm_result' if isinstance(exc, LLMResultError) else ('provider_retryable_error' if retryable else 'provider_terminal_error')
                if retryable and attempt <= max_retries:
                    event('retrying', attempt=attempt, code=code)
                    (retry_wait or _abstract_retry_wait)(attempt)
                    continue
                if raise_errors:
                    raise
                outcome = {'status': 'failed', 'retry_count': attempt - 1, 'terminal_failure': not retryable,
                           'duration_ms': round((perf_counter()-started)*1000)}
                event('failed', terminal=True, attempt=attempt, metrics=outcome, code=code)
                return outcome
            outcome = {'status': 'success', 'retry_count': attempt - 1, 'duration_ms': round((perf_counter()-started)*1000), **evaluated}
            event('succeeded', terminal=True, attempt=attempt,
                  metrics={key: value for key, value in outcome.items() if key != 'result'})
            return outcome
    finally:
        db.release_evaluation_claim(token)


def evaluate_missing_abstracts(
    limit: int = 200,
    progress: ProgressCallback | None = None,
    job_id: int | None = None,
) -> dict[str, Any]:
    paper_ids = db.list_papers_missing_evaluation("abstract_review", limit=limit, include_terminal_failures=True)
    success = 0
    failed = 0
    skipped = 0
    total = len(paper_ids)
    if progress:
        progress(0, total, f"准备摘要评估 {total} 篇论文")
    if total == 0:
        return {"success": 0, "failed": 0, "skipped": 0, "total": 0}

    concurrency = max(1, db.get_int_setting("llm.abstract_concurrency", 4))
    concurrency = min(concurrency, total)
    completed = 0
    lock = Lock()
    config = resolve_evaluation_config("abstract_review")

    def evaluate_one(target_paper_id: int) -> str:
        try:
            result = evaluate_abstract_candidate(target_paper_id, config=config, llm_client=llm_client, job_id=job_id)
            return result['status']
        except Exception as exc:
            logger.warning("Abstract evaluation failed for paper_id=%s error_type=%s", target_paper_id, type(exc).__name__)
            return 'failed'

    with make_llm_client(config.profile) as llm_client:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(evaluate_one, paper_id) for paper_id in paper_ids]
            for future in as_completed(futures):
                outcome = future.result()
                with lock:
                    completed += 1
                    if outcome == 'success':
                        success += 1
                    elif outcome == 'skipped':
                        skipped += 1
                    else:
                        failed += 1
                    current = completed
                    current_success = success
                    current_failed = failed
                if progress:
                    progress(
                        current,
                        total,
                        f"摘要评估 {current}/{total}，并发 {concurrency}，成功 {current_success}，失败 {current_failed}，跳过 {skipped}",
                    )
    return {"success": success, "failed": failed, "skipped": skipped, "total": len(paper_ids)}


class EvaluationRunner:
    def __init__(
        self,
        config: EvaluationConfig | None = None,
        llm_client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._llm_client = llm_client

    def evaluate(self, request: EvaluationRequest) -> dict[str, Any]:
        paper = self._load_paper(request.paper_id)
        config = self._resolve_config(request)
        response = None
        phase = "preparation"
        try:
            prompt_text = self._build_prompt_text(request, paper, config)
            phase = "provider"
            if request.claim_token:
                db.mark_evaluation_provider_started(request.claim_token)
            response = call_llm(config.profile, prompt_text, client=self._llm_client)
            phase = "validation"
            if response.result_json is None:
                raise LLMResultError("invalid_json", "LLM 输出不是合法 JSON object")
            result = validate_evaluation_result(response.result_json, request.evaluation_type)
        except Exception as exc:
            error_code, retryable = _evaluation_failure(exc, phase)
            self._record_failure(
                request,
                config,
                raw_output=response.raw_text if response else None,
                error_message=safe_evaluation_error(exc, phase),
                error_code=error_code,
                error_retryable=retryable,
            )
            raise

        eval_id = db.create_evaluation(
            request.paper_id,
            request.evaluation_type,
            config.prompt_id,
            config.prompt_version,
            config.profile_id,
            config.model,
            "success",
            result,
            response.raw_text,
            None,
            pipeline_job_id=request.pipeline_job_id,
        )
        return {"evaluation_id": eval_id, "result": result, "usage": getattr(response, "usage", None)}

    def _load_paper(self, paper_id: int) -> dict[str, Any]:
        paper = db.get_paper(paper_id)
        if not paper:
            raise ValueError(f"论文不存在: {paper_id}")
        return paper

    def _resolve_config(self, request: EvaluationRequest) -> EvaluationConfig:
        if self._config:
            if self._config.evaluation_type != request.evaluation_type:
                raise ValueError(
                    f"Runner configured for {self._config.evaluation_type}, not {request.evaluation_type}"
                )
            if request.prompt_id and request.prompt_id != self._config.prompt_id:
                raise ValueError(
                    f"Runner configured for prompt {self._config.prompt_id}, not {request.prompt_id}"
                )
            return self._config
        return resolve_evaluation_config(request.evaluation_type, request.prompt_id)

    def _build_prompt_text(
        self,
        request: EvaluationRequest,
        paper: dict[str, Any],
        config: EvaluationConfig,
    ) -> str:
        variables = paper_variables(paper)
        if request.evaluation_type == "fulltext_review":
            md_path, _created = ensure_markdown(paper, force=request.force_markdown)
            variables["markdown"] = md_path.read_text(encoding="utf-8")

        prompt_text = render_prompt(config.prompt["template"], variables)
        if request.evaluation_type == "fulltext_review":
            self._ensure_context_window(prompt_text, config.profile)
        return prompt_text

    def _ensure_context_window(self, prompt_text: str, profile: dict[str, Any]) -> None:
        estimated = estimate_tokens(prompt_text)
        context_window = int(profile.get("context_window_tokens") or 0)
        max_output = int(profile.get("max_output_tokens") or 0)
        if context_window and estimated + max_output > context_window:
            raise ValueError(
                f"全文约 {estimated} tokens，超过模型上下文可用范围 "
                f"({context_window} - {max_output})。请切换长上下文模型。"
            )

    def _record_failure(
        self,
        request: EvaluationRequest,
        config: EvaluationConfig,
        raw_output: str | None,
        error_message: str,
        error_code: str,
        error_retryable: bool,
    ) -> int:
        return db.create_evaluation(
            request.paper_id,
            request.evaluation_type,
            config.prompt_id,
            config.prompt_version,
            config.profile_id,
            config.model,
            "failed",
            None,
            raw_output,
            error_message,
            error_code,
            error_retryable,
            pipeline_job_id=request.pipeline_job_id,
        )

def evaluate_paper(
    paper_id: int,
    evaluation_type: str,
    prompt_id: int | None = None,
    force_markdown: bool = False,
    job_id: int | None = None,
) -> dict[str, Any]:
    if evaluation_type == "abstract_review":
        return evaluate_abstract_candidate(
            paper_id, config=resolve_evaluation_config(evaluation_type, prompt_id),
            skip_success=False, job_id=job_id, max_retries=0,
            raise_errors=True,
        )
    request = EvaluationRequest(
        paper_id=paper_id,
        evaluation_type=evaluation_type,
        prompt_id=prompt_id,
        force_markdown=force_markdown,
    )
    return EvaluationRunner().evaluate(request)


def safe_evaluation_error(exc: Exception, phase: str = "provider") -> str:
    """Never persist provider bodies, request URLs, headers or chained exceptions."""
    if isinstance(exc, LLMResultError):
        if exc.code == 'invalid_json':
            return 'invalid_json: LLM 输出不是合法 JSON object'
        return 'invalid_schema: LLM 输出结构不符合评估要求'
    if phase == 'preparation':
        return 'preparation_failed: 评估准备失败，请检查论文输入、Prompt 和模型上下文设置'
    status = getattr(exc, 'status_code', None)
    status_label = f'（HTTP {status}）' if isinstance(status, int) and 100 <= status <= 599 else ''
    retryable = bool(getattr(exc, 'retryable', False))
    return f"provider_failed: 模型调用失败{status_label}；" + ('允许重试' if retryable else '请检查模型配置或输出要求')


def _evaluation_failure(exc: Exception, phase: str) -> tuple[str, bool]:
    if phase == "validation" or isinstance(exc, LLMResultError):
        return "invalid_response", False
    if phase == "provider":
        return "provider_failed", bool(getattr(exc, "retryable", False))
    retryable = isinstance(exc, (OSError, RuntimeError, httpx.HTTPError))
    return "preparation_failed", retryable


def paper_variables(paper: dict[str, Any]) -> dict[str, Any]:
    categories = db.get_paper_categories(paper["id"])
    latest = categories[0] if categories else {}
    return {
        "title": paper.get("title"),
        "category": latest.get("category", ""),
        "rank": latest.get("rank", ""),
        "stars": latest.get("reading_stars", ""),
        "published_at": paper.get("published_at", ""),
        "subjects": paper.get("subjects_list", []),
        "abstract": paper.get("abstract", ""),
        "markdown": "",
    }
