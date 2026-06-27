import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    prompt = db.get_default_prompt("abstract_review")
    if not prompt:
        raise ValueError("No available prompt: abstract_review")
    profile = db.get_llm_profile(prompt.get("llm_profile_id")) or db.get_default_llm_profile("abstract_review")
    if not profile:
        raise ValueError("No available LLM profile")

    def evaluate_one(target_paper_id: int) -> bool:
        try:
            evaluate_paper(
                target_paper_id,
                "abstract_review",
                prompt=prompt,
                profile=profile,
                llm_client=llm_client,
            )
            return True
        except Exception as exc:
            logger.warning("Abstract evaluation failed for paper_id=%s: %s", target_paper_id, exc)
            return False

    with make_llm_client(profile) as llm_client:
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


def evaluate_paper(
    paper_id: int,
    evaluation_type: str,
    prompt_id: int | None = None,
    force_markdown: bool = False,
    prompt: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    llm_client: httpx.Client | None = None,
) -> dict[str, Any]:
    paper = db.get_paper(paper_id)
    if not paper:
        raise ValueError(f"论文不存在: {paper_id}")

    prompt = prompt or (db.get_prompt(prompt_id) if prompt_id else db.get_default_prompt(evaluation_type))
    if not prompt:
        raise ValueError(f"没有可用 Prompt: {evaluation_type}")

    profile = profile or db.get_llm_profile(prompt.get("llm_profile_id")) or db.get_default_llm_profile(evaluation_type)
    if not profile:
        raise ValueError("没有可用 LLM Profile，请先在 LLM 配置页新增模型")

    variables = paper_variables(paper)
    prompt_text = ""
    if evaluation_type == "fulltext_review":
        md_path, _created = ensure_markdown(paper, force=force_markdown)
        markdown = md_path.read_text(encoding="utf-8")
        variables["markdown"] = markdown
        prompt_text = render_prompt(prompt["template"], variables)
        estimated = estimate_tokens(prompt_text)
        context_window = int(profile.get("context_window_tokens") or 0)
        max_output = int(profile.get("max_output_tokens") or 0)
        if context_window and estimated + max_output > context_window:
            raise ValueError(
                f"全文约 {estimated} tokens，超过模型上下文可用范围 "
                f"({context_window} - {max_output})。请切换长上下文模型。"
            )

    if not prompt_text:
        prompt_text = render_prompt(prompt["template"], variables)
    try:
        response = call_llm(profile, prompt_text, client=llm_client)
        if response.result_json is None:
            db.create_evaluation(
                paper_id,
                evaluation_type,
                prompt["id"],
                prompt["version"],
                profile["id"],
                profile["model"],
                "failed",
                None,
                response.raw_text,
                "LLM 输出不是合法 JSON",
            )
            raise LLMError("LLM 输出不是合法 JSON")
        eval_id = db.create_evaluation(
            paper_id,
            evaluation_type,
            prompt["id"],
            prompt["version"],
            profile["id"],
            profile["model"],
            "success",
            response.result_json,
            response.raw_text,
            None,
        )
        return {"evaluation_id": eval_id, "result": response.result_json}
    except Exception as exc:
        if not isinstance(exc, LLMError) or str(exc) != "LLM 输出不是合法 JSON":
            db.create_evaluation(
                paper_id,
                evaluation_type,
                prompt["id"],
                prompt["version"],
                profile["id"],
                profile["model"],
                "failed",
                None,
                None,
                str(exc),
            )
        raise


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
