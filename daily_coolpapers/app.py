import json
import logging
import os
import threading
import time
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import db
from .cache_manager import cleanup_caches, has_markdown, has_pdf, markdown_path, pdf_path
from .config import CURRENT_LOG, INSTANCE_DIR, ensure_directories
from .jobs import job_runner
from .llm import test_profile
from .logging_setup import setup_logging
from .security import secret_store
from .services import (
    build_paper_digest_csv,
    build_paper_evaluation_export,
    evaluation_prompt_options,
    evaluation_result_view,
    paper_evaluation_result_model,
    resolve_evaluation_config,
)

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    ensure_directories()
    setup_logging(clear_on_start=True)
    db.init_db()
    db.init_llm_profiles_db()
    db.migrate_llm_profiles_from_main_db()
    db.mark_unfinished_jobs_interrupted()
    app = Flask(__name__)
    app.secret_key = _flask_secret()

    if db.get_bool_setting("cache.cleanup_on_start", True):
        cleanup_caches()
    if os.environ.get("DAILY_COOLPAPERS_DISABLE_WORKER") != "1":
        job_runner.start()

    register_template_helpers(app)
    register_routes(app)
    return app


def _flask_secret() -> str:
    path = INSTANCE_DIR / "flask_secret.key"
    if not path.exists():
        path.write_text(os.urandom(32).hex(), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def register_template_helpers(app: Flask) -> None:
    @app.template_filter("json_pretty")
    def json_pretty(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)

    @app.template_filter("truncate_text")
    def truncate_text(value: str | None, length: int = 220) -> str:
        if not value:
            return ""
        return value if len(value) <= length else value[:length].rstrip() + "..."

    @app.template_global()
    def latest_score(evaluation: dict | None) -> str:
        view = evaluation_result_view(evaluation)
        return view["score_text"] if view else "-"

    @app.template_global()
    def latest_attention(evaluation: dict | None) -> str:
        view = evaluation_result_view(evaluation)
        return view["attention"] if view else "pending"

    @app.template_global()
    def vc_impact(evaluation: dict | None) -> str:
        view = evaluation_result_view(evaluation)
        return view["vc_impact"] if view and view["is_success"] else ""

    @app.template_global()
    def markdown_cached(arxiv_id: str) -> bool:
        return has_markdown(arxiv_id)


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        job_runner.reconcile_orphaned_pending_jobs(min_interval_seconds=30)
        digest_query = _paper_digest_query_from_request()
        papers = db.list_paper_rows(digest_query)
        jobs = _job_status_payloads(db.list_job_summaries(12))
        active_jobs = [job for job in jobs if job["status"] in {"pending", "running"}]
        progress_jobs = active_jobs
        if not progress_jobs and jobs and jobs[0]["status"] == "failed":
            progress_jobs = [jobs[0]]
        return render_template(
            "index.html",
            papers=papers,
            categories=db.list_categories(),
            digest_query=digest_query,
            selected_date=digest_query.selected_date,
            date_from=digest_query.date_from,
            date_to=digest_query.date_to,
            use_date_range=digest_query.use_date_range,
            selected_category=digest_query.category,
            attention=digest_query.attention,
            sort=digest_query.sort,
            export_csv_url=url_for("export_csv", **digest_query.url_args()),
            rank_sort_url=url_for("index", **digest_query.url_args(sort="rank_desc")),
            stars_sort_url=url_for("index", **digest_query.url_args(sort="stars_desc")),
            jobs=jobs[:8],
            has_active_jobs=bool(active_jobs),
            progress_jobs=progress_jobs,
        )

    @app.get("/favorites")
    def favorites():
        sort = request.args.get("sort") or "evaluated_desc"
        if sort not in {"evaluated_desc", "score_desc", "rank", "title"}:
            sort = "evaluated_desc"
        return render_template(
            "favorites.html",
            papers=db.list_fulltext_reviewed_papers(sort=sort),
            sort=sort,
        )

    @app.post("/api/shutdown")
    def shutdown():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)
        shutdown_func = request.environ.get("werkzeug.server.shutdown")
        logger.info("Shutdown requested from %s", request.remote_addr)
        if os.environ.get("DAILY_COOLPAPERS_DISABLE_SHUTDOWN") != "1":
            threading.Thread(
                target=_delayed_shutdown,
                args=(shutdown_func,),
                name="shutdown-worker",
                daemon=True,
            ).start()
        return render_template("shutdown.html")

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "service": "daily-coolpapers",
            "pid": os.getpid(),
        }

    @app.post("/api/crawl/run")
    def run_crawl():
        category_ids = [int(item) for item in request.form.getlist("category_ids") if item]
        payload: dict[str, Any] = {}
        if category_ids:
            payload["category_ids"] = category_ids
        job_id = job_runner.enqueue("crawl", payload)
        flash(f"已创建 metadata 抓取任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/crawl/catch-up")
    def run_crawl_catch_up():
        category_ids = [int(item) for item in request.form.getlist("category_ids") if item]
        payload: dict[str, Any] = {}
        if category_ids:
            payload["category_ids"] = category_ids
        job_id = job_runner.enqueue("crawl_catch_up", payload)
        flash(f"已创建 metadata 补抓到最新任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/abstract-evaluations/run")
    def run_abstract_evaluations():
        job_id = job_runner.enqueue("abstract_eval", {})
        flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/papers/<int:paper_id>/evaluate-abstract")
    def evaluate_abstract(paper_id: int):
        job_id = _enqueue_paper_evaluation(paper_id, "abstract_review", "abstract_eval")
        if job_id:
            flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(_back_to_detail_or_index(paper_id))

    @app.post("/api/papers/<int:paper_id>/evaluate-fulltext")
    def evaluate_fulltext(paper_id: int):
        force_markdown = bool(request.form.get("force_markdown"))
        job_id = _enqueue_paper_evaluation(
            paper_id,
            "fulltext_review",
            "fulltext_eval",
            force_markdown=force_markdown,
        )
        if job_id:
            flash(f"已创建全文阅读任务 #{job_id}")
        return redirect(_back_to_detail_or_index(paper_id))

    @app.get("/papers/<int:paper_id>")
    def paper_detail(paper_id: int):
        paper = db.get_paper(paper_id)
        if not paper:
            flash("论文不存在")
            return redirect(url_for("index"))
        return render_template(
            "paper_detail.html",
            paper=paper,
            categories=db.get_paper_categories(paper_id),
            evaluation_results=paper_evaluation_result_model(paper_id),
            evaluation_actions=_paper_evaluation_actions(paper_id),
            has_pdf=has_pdf(paper["arxiv_id"]),
            has_markdown=has_markdown(paper["arxiv_id"]),
            pdf_path=pdf_path(paper["arxiv_id"]),
            markdown_path=markdown_path(paper["arxiv_id"]),
        )

    @app.get("/papers/<int:paper_id>/markdown")
    def view_markdown(paper_id: int):
        paper = db.get_paper(paper_id)
        if not paper:
            flash("论文不存在")
            return redirect(url_for("index"))
        path = markdown_path(paper["arxiv_id"])
        if not path.exists():
            flash("Markdown 缓存不存在，请先触发全文阅读")
            return redirect(url_for("paper_detail", paper_id=paper_id))
        return render_template("markdown_view.html", paper=paper, markdown=path.read_text(encoding="utf-8"))

    @app.get("/papers/<int:paper_id>/pdf")
    def open_pdf(paper_id: int):
        paper = db.get_paper(paper_id)
        if not paper:
            flash("论文不存在")
            return redirect(url_for("index"))
        path = pdf_path(paper["arxiv_id"])
        if path.exists():
            return send_file(path, mimetype="application/pdf", as_attachment=False)
        if paper.get("pdf_url"):
            return redirect(paper["pdf_url"])
        flash("PDF 不存在")
        return redirect(url_for("paper_detail", paper_id=paper_id))

    @app.get("/papers/<int:paper_id>/export.md")
    def export_paper_markdown(paper_id: int):
        paper = db.get_paper(paper_id)
        if not paper:
            flash("论文不存在")
            return redirect(url_for("index"))
        body = build_paper_evaluation_export(paper)
        return Response(
            body,
            mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={paper['arxiv_id']}.md"},
        )

    @app.get("/export.csv")
    def export_csv():
        digest_query = _paper_digest_query_from_request()
        body = build_paper_digest_csv(db.list_paper_rows(digest_query))
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=papers.csv"},
        )

    @app.get("/categories")
    def categories():
        return render_template("categories.html", categories=db.list_categories())

    @app.post("/api/categories")
    def save_category():
        data = {
            "id": _optional_int(request.form.get("id")),
            "category": request.form["category"].strip(),
            "name": request.form["name"].strip(),
            "enabled": bool(request.form.get("enabled")),
            "top_n": request.form.get("top_n") or 30,
            "sort_param": request.form.get("sort_param") or "sort=1",
        }
        db.save_category(data)
        flash("类目已保存")
        return redirect(url_for("categories"))

    @app.get("/prompts")
    def prompts():
        return render_template(
            "prompts.html",
            prompts=db.list_prompts(),
            profiles=db.list_llm_profiles(enabled_only=True),
        )

    @app.post("/api/prompts")
    def save_prompt():
        data = {
            "id": _optional_int(request.form.get("id")),
            "name": request.form["name"].strip(),
            "type": request.form["type"],
            "template": request.form["template"],
            "llm_profile_id": _optional_int(request.form.get("llm_profile_id")),
            "is_default": bool(request.form.get("is_default")),
            "enabled": bool(request.form.get("enabled")),
        }
        db.save_prompt(data)
        flash("Prompt 已保存")
        return redirect(url_for("prompts"))

    @app.post("/api/prompts/<int:prompt_id>/copy")
    def copy_prompt(prompt_id: int):
        prompt = db.get_prompt(prompt_id)
        if not prompt:
            flash("Prompt 不存在")
            return redirect(url_for("prompts"))
        prompt["id"] = None
        prompt["name"] = f"{prompt['name']} Copy"
        prompt["is_default"] = 0
        db.save_prompt(prompt)
        flash("Prompt 已复制")
        return redirect(url_for("prompts"))

    @app.get("/llm-profiles")
    def llm_profiles():
        profiles = db.list_llm_profiles()
        for profile in profiles:
            profile["api_key_masked"] = secret_store.masked(profile.get("encrypted_api_key_ref"))
        return render_template(
            "llm_profiles.html",
            profiles=profiles,
            prompts=db.list_prompts(),
        )

    @app.post("/api/llm-profiles")
    def save_llm_profile():
        encrypted = None
        api_key = request.form.get("api_key", "").strip()
        if api_key:
            encrypted = secret_store.encrypt(api_key)
        data = {
            "id": _optional_int(request.form.get("id")),
            "name": request.form["name"].strip(),
            "provider": request.form["provider"],
            "base_url": request.form["base_url"].strip().rstrip("/"),
            "model": request.form["model"].strip(),
            "encrypted_api_key_ref": encrypted,
            "custom_headers": _valid_json_or_empty(request.form.get("custom_headers")),
            "temperature": request.form.get("temperature") or 0.2,
            "max_output_tokens": request.form.get("max_output_tokens") or 2000,
            "context_window_tokens": request.form.get("context_window_tokens") or 128000,
            "timeout_seconds": request.form.get("timeout_seconds") or 120,
            "enabled": bool(request.form.get("enabled")),
            "is_default_abstract": bool(request.form.get("is_default_abstract")),
            "is_default_fulltext": bool(request.form.get("is_default_fulltext")),
        }
        db.save_llm_profile(data)
        flash("LLM Profile 已保存")
        return redirect(url_for("llm_profiles"))

    @app.post("/api/prompt-model-bindings")
    def save_prompt_model_bindings():
        prompts = db.list_prompts()
        for prompt in prompts:
            field = f"prompt_{prompt['id']}_llm_profile_id"
            db.update_prompt_llm_profile(prompt["id"], _optional_int(request.form.get(field)))
        flash("Prompt 模型绑定已保存")
        return redirect(url_for("llm_profiles"))

    @app.post("/api/llm-profiles/<int:profile_id>/test")
    def test_llm_profile(profile_id: int):
        profile = db.get_llm_profile(profile_id)
        if not profile:
            flash("LLM Profile 不存在")
            return redirect(url_for("llm_profiles"))
        try:
            raw = test_profile(profile)
            flash(f"连接测试成功：{raw[:200]}")
        except Exception as exc:
            flash(f"连接测试失败：{exc}")
        return redirect(url_for("llm_profiles"))

    @app.get("/settings")
    def settings():
        return render_template(
            "settings.html",
            settings={
                "pdf_retention_days": db.get_int_setting("cache.pdf_retention_days", 5),
                "markdown_retention_days": db.get_int_setting("cache.markdown_retention_days", 7),
                "cleanup_on_start": db.get_bool_setting("cache.cleanup_on_start", True),
                "cleanup_daily": db.get_bool_setting("cache.cleanup_daily", True),
                "abstract_concurrency": db.get_int_setting("llm.abstract_concurrency", 4),
                "crawler_trust_env_proxy": db.get_bool_setting("crawler.trust_env_proxy", False),
                "crawler_proxy_url": db.get_setting("crawler.proxy_url", ""),
                "llm_trust_env_proxy": db.get_bool_setting("llm.trust_env_proxy", False),
                "pdf_download_timeout_seconds": db.get_int_setting("llm.pdf_download_timeout_seconds", 300),
                "pdf_download_retries": db.get_int_setting("llm.pdf_download_retries", 2),
                "scheduler_enabled": db.get_bool_setting("scheduler.enabled", True),
                "scheduler_daily_times": db.get_setting("scheduler.daily_times", "10:30,12:00"),
            },
            jobs=_job_status_payloads(db.list_job_summaries(30)),
        )

    @app.post("/api/settings")
    def save_settings():
        db.set_setting("cache.pdf_retention_days", int(request.form.get("pdf_retention_days") or 5))
        db.set_setting("cache.markdown_retention_days", int(request.form.get("markdown_retention_days") or 7))
        db.set_setting("cache.cleanup_on_start", bool(request.form.get("cleanup_on_start")))
        db.set_setting("cache.cleanup_daily", bool(request.form.get("cleanup_daily")))
        db.set_setting("llm.abstract_concurrency", int(request.form.get("abstract_concurrency") or 4))
        db.set_setting("crawler.trust_env_proxy", bool(request.form.get("crawler_trust_env_proxy")))
        db.set_setting("crawler.proxy_url", (request.form.get("crawler_proxy_url") or "").strip())
        db.set_setting("llm.trust_env_proxy", bool(request.form.get("llm_trust_env_proxy")))
        db.set_setting("llm.pdf_download_timeout_seconds", int(request.form.get("pdf_download_timeout_seconds") or 300))
        db.set_setting("llm.pdf_download_retries", int(request.form.get("pdf_download_retries") or 2))
        db.set_setting("scheduler.enabled", bool(request.form.get("scheduler_enabled")))
        db.set_setting("scheduler.daily_times", request.form.get("scheduler_daily_times") or "10:30,12:00")
        flash("设置已保存")
        return redirect(url_for("settings"))

    @app.post("/api/cache/cleanup")
    def run_cleanup():
        job_id = job_runner.enqueue("cleanup", {})
        flash(f"已创建缓存清理任务 #{job_id}")
        return redirect(url_for("settings"))

    @app.get("/logs")
    def logs():
        log_text = CURRENT_LOG.read_text(encoding="utf-8") if CURRENT_LOG.exists() else ""
        return render_template("logs.html", log_text=log_text, jobs=_job_status_payloads(db.list_job_summaries(80)))

    @app.get("/api/logs/current")
    def current_log():
        log_text = CURRENT_LOG.read_text(encoding="utf-8") if CURRENT_LOG.exists() else ""
        return Response(log_text, mimetype="text/plain; charset=utf-8")

    @app.get("/api/jobs/progress")
    def jobs_progress():
        jobs = _job_status_payloads(db.list_active_job_progress(12))
        return {
            "jobs": jobs
        }


def _optional_int(value: str | None) -> int | None:
    if value in {None, "", "None"}:
        return None
    return int(value)


def _paper_digest_query_from_request() -> db.PaperDigestQuery:
    return db.PaperDigestQuery.from_raw(
        date_value=request.args.get("date"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        category=request.args.get("category"),
        attention=request.args.get("attention"),
        sort=request.args.get("sort"),
        latest_crawl_date=db.get_latest_crawl_date(),
    )


def _valid_json_or_empty(value: str | None) -> str:
    if not value or not value.strip():
        return "{}"
    json.loads(value)
    return value


def _delayed_shutdown(shutdown_func: Any) -> None:
    time.sleep(1.2)
    try:
        if shutdown_func:
            shutdown_func()
            time.sleep(0.5)
    finally:
        os._exit(0)


def _back_to_detail_or_index(paper_id: int) -> str:
    if request.form.get("from_detail"):
        return url_for("paper_detail", paper_id=paper_id)
    return request.referrer or url_for("index")


def _paper_evaluation_actions(paper_id: int) -> list[dict[str, Any]]:
    return [
        {
            "key": "abstract_review",
            "title": "摘要 Prompt",
            "action_url": url_for("evaluate_abstract", paper_id=paper_id),
            "button_label": "重新摘要评估",
            "prompt_options": evaluation_prompt_options("abstract_review"),
            "force_markdown": False,
        },
        {
            "key": "fulltext_review",
            "title": "全文 Prompt",
            "action_url": url_for("evaluate_fulltext", paper_id=paper_id),
            "button_label": "全文阅读",
            "prompt_options": evaluation_prompt_options("fulltext_review"),
            "force_markdown": True,
        },
    ]


def _enqueue_paper_evaluation(
    paper_id: int,
    evaluation_type: str,
    job_type: str,
    force_markdown: bool = False,
) -> int | None:
    prompt_id = _optional_int(request.form.get("prompt_id"))
    try:
        config = resolve_evaluation_config(evaluation_type, prompt_id)
    except ValueError as exc:
        flash(f"无法创建评估任务：{exc}")
        return None

    payload: dict[str, Any] = {
        "paper_id": paper_id,
        "prompt_id": config.prompt_id,
    }
    if force_markdown:
        payload["force_markdown"] = True
    return job_runner.enqueue(job_type, payload)


def _job_type_label(job_type: str | None) -> str:
    return {
        "crawl": "抓取 Metadata",
        "crawl_catch_up": "补抓到最新",
        "abstract_eval": "摘要评估",
        "fulltext_eval": "全文阅读",
        "cleanup": "缓存清理",
    }.get(str(job_type or ""), str(job_type or "任务"))


def _job_status_label(status: str | None) -> str:
    return {
        "pending": "排队中",
        "running": "运行中",
        "success": "已完成",
        "failed": "失败",
    }.get(str(status or ""), str(status or "未知"))


def _job_progress_label(job: dict[str, Any]) -> str:
    status = job.get("status")
    total = int(job.get("progress_total") or 0)
    current = int(job.get("progress_current") or 0)
    if status == "pending":
        return "等待执行"
    if total <= 0:
        return "准备中"
    return f"{current}/{total} · {job.get('progress_percent') or 0}%"


def _job_progress_payload(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "")
    return {
        "id": job["id"],
        "type": job["type"],
        "type_label": _job_type_label(job.get("type")),
        "status": status,
        "status_label": _job_status_label(job.get("status")),
        "progress_current": job["progress_current"],
        "progress_total": job["progress_total"],
        "progress_percent": job["progress_percent"],
        "progress_label": _job_progress_label(job),
        "progress_message": job["progress_message"],
        "message": _job_message(job),
        "progress_details": job.get("progress_details") or {},
        "detail": _job_detail_payload(job.get("progress_details") or {}),
        "error_message": job.get("error_message") or "",
        "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "",
        "finished_at": job.get("finished_at") or "",
        "is_pending": status == "pending",
        "is_failed": status == "failed",
    }


def _job_message(job: dict[str, Any]) -> str:
    if job.get("status") == "failed" and job.get("error_message"):
        return str(job.get("error_message") or "")
    return str(job.get("progress_message") or _job_progress_label(job))


def _job_status_payloads(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_job_progress_payload(job) for job in jobs]


def _job_detail_payload(details: dict[str, Any]) -> dict[str, Any] | None:
    phase = details.get("phase")
    if phase == "crawl":
        summary = details.get("summary") or {}
        return {
            "summary_lines": [
                f"成功 {summary.get('success') or 0}/{summary.get('total') or 0}",
                f"失败 {summary.get('failed') or 0}",
                f"已保存 {summary.get('saved') or 0} 篇",
                f"运行中 {summary.get('running') or 0}",
                f"待处理 {summary.get('pending') or 0}",
            ],
            "item_rows": [_crawl_detail_item_payload(item) for item in details.get("categories") or []],
        }
    if phase == "catch_up":
        summary = details.get("summary") or {}
        summary_lines = [
            f"目标内最新 {summary.get('latest_reference_date') or '无'}",
            f"数据库最大 {summary.get('latest_db_date') or '无'}",
            f"目标最新 {summary.get('latest_target_date') or ''}",
            f"日期 {summary.get('completed_dates') or 0}/{summary.get('total_dates') or 0}",
            f"失败 {summary.get('failed_dates') or 0}",
        ]
        if summary.get("current_date"):
            summary_lines.append(f"当前 {summary.get('current_date')}")
        return {
            "summary_lines": summary_lines,
            "item_rows": [_catch_up_detail_item_payload(item) for item in details.get("dates") or []],
        }
    return None


def _crawl_detail_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "pending")
    line = "等待中"
    if status == "success":
        line = f"{item.get('papers') or 0} 篇 · {item.get('crawl_date') or ''}"
    elif status == "running":
        line = f"第 {item.get('attempt') or 0}/{item.get('max_attempts') or 0} 次尝试"
    elif status == "failed":
        line = str(item.get("error") or "抓取失败")
    return {
        "title": item.get("category") or "",
        "status": status,
        "status_label": _job_status_label(status),
        "line": line,
    }


def _catch_up_detail_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "pending")
    line = "等待中"
    if status == "success":
        line = f"已保存 {item.get('saved') or 0} 条类目记录"
    elif status == "running":
        line = "抓取中"
    elif status == "failed":
        line = str(item.get("error") or "metadata 抓取失败")
    return {
        "title": item.get("date") or "",
        "status": status,
        "status_label": _job_status_label(status),
        "line": line,
    }
