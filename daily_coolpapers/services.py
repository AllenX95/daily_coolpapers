import csv
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any, Callable

import httpx

from . import db
from .crawler import available_arxiv_dates_after, crawl_date_from_papers, fetch_category, latest_available_arxiv_date
from .fulltext import ensure_markdown
from .llm import LLMError, call_llm, make_llm_client
from .network import httpx_proxy_kwargs
from .prompt_engine import estimate_tokens, render_prompt

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str, dict[str, Any] | None], None]

EVALUATION_TYPE_LABELS = {
    "abstract_review": "摘要评估",
    "fulltext_review": "全文评估",
}
EVALUATION_STATUS_LABELS = {
    "pending": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
}


@dataclass(frozen=True)
class EvaluationRequest:
    paper_id: int
    evaluation_type: str
    prompt_id: int | None = None
    force_markdown: bool = False


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
) -> dict[str, Any]:
    categories = db.list_categories(enabled_only=True)
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
    failed = 0
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
            state["url"] = event.get("url") or state["url"]
            if event.get("error"):
                state["error"] = event["error"]
            attempt = state["attempt"]
            max_attempts = state["max_attempts"]
        emit(f"正在抓取 {category_key}（第 {attempt}/{max_attempts} 次尝试）")

    if progress:
        date_label = f" {crawl_date}" if crawl_date else ""
        progress(0, total, f"准备抓取{date_label} {total} 个类目", snapshot())
    concurrency = max(1, db.get_int_setting("crawler.concurrency", 6))
    with _crawler_client_from_settings() as client:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
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
                    papers = future.result()
                except Exception as exc:
                    error = str(exc)
                    failures.append({"category": category_key, "error": error})
                    with lock:
                        completed += 1
                        failed += 1
                        category_state[category_key]["status"] = "failed"
                        category_state[category_key]["error"] = error
                    emit(f"抓取 {category_key} 失败：{_short_error(error)}")
                    continue

                saved_crawl_date = crawl_date or crawl_date_from_papers(papers)
                saved_for_category = len(db.upsert_papers(papers, category_key, saved_crawl_date))
                category_results.append(
                    {
                        "category": category_key,
                        "papers": len(papers),
                        "crawl_date": saved_crawl_date,
                    }
                )
                with lock:
                    saved += saved_for_category
                    completed += 1
                    succeeded += 1
                    category_state[category_key]["status"] = "success"
                    category_state[category_key]["papers"] = len(papers)
                    category_state[category_key]["crawl_date"] = saved_crawl_date
                    category_state[category_key]["error"] = ""
                emit(f"已抓取 {category_key}：{len(papers)} 篇；成功 {succeeded}/{total}，失败 {failed}")
    if failures:
        failure_text = "；".join(f"{item['category']}: {_short_error(item['error'], 160)}" for item in failures)
        emit(f"抓取完成：成功 {succeeded}/{total}，失败 {failed}。{failure_text}")
        raise RuntimeError(f"抓取失败：{failure_text}")
    return {"saved": saved, "categories": category_results}


def crawl_to_latest(
    category_ids: list[int] | None = None,
    progress: ProgressCallback | None = None,
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
            result = crawl_all_categories(category_ids, crawl_date=target_date, progress=date_progress)
            saved_for_date = int(result.get("saved") or 0)
            saved_total += saved_for_date
            date_results.append(
                {
                    "date": target_date,
                    "saved": saved_for_date,
                    "categories": result.get("categories") or [],
                }
            )
            date_state[target_date]["status"] = "success"
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
) -> list[dict]:
    return fetch_category(
        category["category"],
        top_n=int(category.get("top_n") or 30),
        sort_param=category.get("sort_param") or "sort=1",
        timeout_seconds=db.get_int_setting("crawler.timeout_seconds", 20),
        retries=db.get_int_setting("crawler.retries", 2),
        user_agent=str(db.get_setting("crawler.user_agent", "DailyCoolPapers/0.1")),
        trust_env_proxy=db.get_bool_setting("crawler.trust_env_proxy", False),
        proxy_url=str(db.get_setting("crawler.proxy_url", "") or ""),
        crawl_date=crawl_date,
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


def evaluate_missing_abstracts(
    limit: int = 200,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    paper_ids = db.list_papers_missing_evaluation("abstract_review", limit=limit)
    success = 0
    failed = 0
    total = len(paper_ids)
    if progress:
        progress(0, total, f"准备摘要评估 {total} 篇论文")
    if total == 0:
        return {"success": 0, "failed": 0, "total": 0}

    concurrency = max(1, db.get_int_setting("llm.abstract_concurrency", 4))
    concurrency = min(concurrency, total)
    completed = 0
    lock = Lock()
    config = resolve_evaluation_config("abstract_review")

    def evaluate_one(target_paper_id: int) -> bool:
        try:
            runner.evaluate(EvaluationRequest(target_paper_id, "abstract_review"))
            return True
        except Exception as exc:
            logger.warning("Abstract evaluation failed for paper_id=%s: %s", target_paper_id, exc)
            return False

    with make_llm_client(config.profile) as llm_client:
        runner = EvaluationRunner(config=config, llm_client=llm_client)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(evaluate_one, paper_id) for paper_id in paper_ids]
            for future in as_completed(futures):
                ok = future.result()
                with lock:
                    completed += 1
                    if ok:
                        success += 1
                    else:
                        failed += 1
                    current = completed
                    current_success = success
                    current_failed = failed
                if progress:
                    progress(
                        current,
                        total,
                        f"摘要评估 {current}/{total}，并发 {concurrency}，成功 {current_success}，失败 {current_failed}",
                    )
    return {"success": success, "failed": failed, "total": len(paper_ids)}


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
        prompt_text = self._build_prompt_text(request, paper, config)

        try:
            response = call_llm(config.profile, prompt_text, client=self._llm_client)
        except Exception as exc:
            self._record_failure(request, config, raw_output=None, error_message=str(exc))
            raise

        if response.result_json is None:
            message = "LLM 输出不是合法 JSON"
            self._record_failure(request, config, raw_output=response.raw_text, error_message=message)
            raise LLMError(message)

        eval_id = db.create_evaluation(
            request.paper_id,
            request.evaluation_type,
            config.prompt_id,
            config.prompt_version,
            config.profile_id,
            config.model,
            "success",
            response.result_json,
            response.raw_text,
            None,
        )
        return {"evaluation_id": eval_id, "result": response.result_json}

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
        )

def evaluate_paper(
    paper_id: int,
    evaluation_type: str,
    prompt_id: int | None = None,
    force_markdown: bool = False,
) -> dict[str, Any]:
    request = EvaluationRequest(
        paper_id=paper_id,
        evaluation_type=evaluation_type,
        prompt_id=prompt_id,
        force_markdown=force_markdown,
    )
    return EvaluationRunner().evaluate(request)


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
