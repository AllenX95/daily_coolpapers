import csv
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import db
from .cache_manager import cleanup_caches, has_markdown, has_pdf, markdown_path, pdf_path
from .config import CURRENT_LOG, INSTANCE_DIR, ensure_directories
from .form_commands import (
    SETTINGS_DEFAULTS,
    FormValidationError,
    SettingsCommand,
    parse_bool,
    parse_choice,
    parse_float,
    parse_int,
    parse_json_object,
    parse_optional_int,
    parse_required_text,
)
from .jobs import JobRunner, job_runner
from .llm import test_profile
from .logging_setup import setup_logging
from .security import SecretStore, secret_store

logger = logging.getLogger(__name__)


@dataclass
class RuntimeHandle:
    runner: JobRunner
    worker_started: bool

    def stop(self) -> None:
        if self.worker_started:
            self.runner.stop()
            self.worker_started = False


def start_runtime(
    runner: JobRunner | None = None,
    start_worker: bool | None = None,
) -> RuntimeHandle:
    runtime_runner = runner or job_runner
    ensure_directories()
    db.init_db()
    db.init_llm_profiles_db()
    db.migrate_llm_profiles_from_main_db()
    db.mark_unfinished_jobs_interrupted()
    setup_logging(clear_on_start=db.get_bool_setting("logs.clear_on_start", True))
    if db.get_bool_setting("cache.cleanup_on_start", True):
        cleanup_caches()
    if start_worker is None:
        start_worker = os.environ.get("DAILY_COOLPAPERS_DISABLE_WORKER") != "1"
    if start_worker:
        runtime_runner.start()
    return RuntimeHandle(runtime_runner, bool(start_worker))


def create_app(
    runner: JobRunner | None = None,
    store: SecretStore | None = None,
    secret_key: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.extensions["daily_coolpapers.job_runner"] = runner or job_runner
    app.extensions["daily_coolpapers.secret_store"] = store or secret_store

    register_template_helpers(app)
    register_request_security(app)
    register_routes(app)

    @app.errorhandler(FormValidationError)
    def handle_form_validation(error: FormValidationError):
        return {"errors": error.errors}, 400

    return app


def _flask_secret() -> str:
    path = INSTANCE_DIR / "flask_secret.key"
    if not path.exists():
        path.write_text(os.urandom(32).hex(), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def register_template_helpers(app: Flask) -> None:
    @app.template_global()
    def csrf_token() -> str:
        return _csrf_token()

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
        if not evaluation or evaluation.get("status") != "success":
            return "-"
        score = (evaluation.get("result") or {}).get("score")
        return "-" if score is None else str(score)

    @app.template_global()
    def latest_attention(evaluation: dict | None) -> str:
        if not evaluation or evaluation.get("status") != "success":
            return "pending"
        return (evaluation.get("result") or {}).get("attention") or "unknown"

    @app.template_global()
    def vc_impact(evaluation: dict | None) -> str:
        if not evaluation or evaluation.get("status") != "success":
            return ""
        vc = (evaluation.get("result") or {}).get("vc_perspective") or {}
        return vc.get("impact", "") if isinstance(vc, dict) else ""

    @app.template_global()
    def markdown_cached(arxiv_id: str) -> bool:
        return has_markdown(arxiv_id)

    @app.template_global()
    def job_type_label(job_type: str | None) -> str:
        return _job_type_label(job_type)

    @app.template_global()
    def job_status_label(status: str | None) -> str:
        return _job_status_label(status)

    @app.template_global()
    def job_progress_label(job: dict[str, Any]) -> str:
        return _job_progress_label(job)


def register_request_security(app: Flask) -> None:
    @app.before_request
    def protect_state_changes():
        if request.method != "POST":
            return None
        expected = session.get("_csrf_token")
        provided = request.form.get("csrf_token")
        if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
            abort(403)
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and not _same_origin(source):
            abort(403)
        return None


def register_routes(app: Flask) -> None:
    runtime_runner: JobRunner = app.extensions["daily_coolpapers.job_runner"]
    runtime_secret_store: SecretStore = app.extensions["daily_coolpapers.secret_store"]

    @app.get("/")
    def index():
        runtime_runner.reconcile_orphaned_pending_jobs()
        date_from, date_to = _normalized_date_range(
            request.args.get("date_from"),
            request.args.get("date_to"),
        )
        use_date_range = bool(date_from or date_to)
        selected_date = "" if use_date_range else (_valid_date_string(request.args.get("date")) or db.get_latest_crawl_date())
        selected_category = request.args.get("category") or ""
        attention = request.args.get("attention") or ""
        sort = request.args.get("sort") or "rank"
        page_number = _query_int(request.args.get("page"), 1, 1)
        page_size = _query_int(request.args.get("page_size"), 50, 1, 100)
        paper_page = db.list_paper_page(
            crawl_date=selected_date,
            date_from=date_from or None,
            date_to=date_to or None,
            category=selected_category or None,
            attention=attention or None,
            sort=sort,
            page=page_number,
            page_size=page_size,
        )
        jobs = db.list_jobs(12)
        active_jobs = [job for job in jobs if job["status"] in {"pending", "running"}]
        progress_jobs = active_jobs
        if not progress_jobs and jobs and jobs[0]["status"] == "failed":
            progress_jobs = [jobs[0]]
        return render_template(
            "index.html",
            papers=paper_page["items"],
            paper_page=paper_page,
            categories=db.list_categories(),
            selected_date=selected_date,
            date_from=date_from,
            date_to=date_to,
            use_date_range=use_date_range,
            selected_category=selected_category,
            attention=attention,
            sort=sort,
            jobs=jobs[:8],
            active_jobs=active_jobs,
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
        category_ids = [parse_int(item, "category_ids", minimum=1) for item in request.form.getlist("category_ids") if item]
        payload: dict[str, Any] = {}
        if category_ids:
            payload["category_ids"] = category_ids
        job_id = runtime_runner.enqueue("crawl", payload)
        flash(f"已创建 metadata 抓取任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/crawl/catch-up")
    def run_crawl_catch_up():
        category_ids = [parse_int(item, "category_ids", minimum=1) for item in request.form.getlist("category_ids") if item]
        payload: dict[str, Any] = {}
        if category_ids:
            payload["category_ids"] = category_ids
        job_id = runtime_runner.enqueue("crawl_catch_up", payload)
        flash(f"已创建 metadata 补抓到最新任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/abstract-evaluations/run")
    def run_abstract_evaluations():
        job_id = runtime_runner.enqueue("abstract_eval", {})
        flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/papers/<int:paper_id>/evaluate-abstract")
    def evaluate_abstract(paper_id: int):
        prompt_id = _optional_int(request.form.get("prompt_id"))
        job_id = runtime_runner.enqueue("abstract_eval", {"paper_id": paper_id, "prompt_id": prompt_id})
        flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(_back_to_detail_or_index(paper_id))

    @app.post("/api/papers/<int:paper_id>/evaluate-fulltext")
    def evaluate_fulltext(paper_id: int):
        prompt_id = _optional_int(request.form.get("prompt_id"))
        force_markdown = parse_bool(request.form.get("force_markdown"), "force_markdown")
        job_id = runtime_runner.enqueue(
            "fulltext_eval",
            {"paper_id": paper_id, "prompt_id": prompt_id, "force_markdown": force_markdown},
        )
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
            evaluations=db.list_evaluations(paper_id),
            latest_abstract_eval=db.get_latest_evaluation(paper_id, "abstract_review"),
            latest_fulltext_eval=db.get_latest_evaluation(paper_id, "fulltext_review"),
            latest_successful_fulltext_eval=db.get_latest_successful_evaluation(
                paper_id, "fulltext_review"
            ),
            abstract_prompts=db.list_prompts("abstract_review", enabled_only=True),
            fulltext_prompts=db.list_prompts("fulltext_review", enabled_only=True),
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
        if not has_markdown(paper["arxiv_id"]):
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
        if has_pdf(paper["arxiv_id"]):
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
        body = build_paper_export(paper, db.list_evaluations(paper_id))
        return Response(
            body,
            mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={paper['arxiv_id']}.md"},
        )

    @app.get("/export.csv")
    def export_csv():
        date_from, date_to = _normalized_date_range(
            request.args.get("date_from"),
            request.args.get("date_to"),
        )
        use_date_range = bool(date_from or date_to)
        papers = db.list_paper_rows(
            crawl_date=None if use_date_range else (_valid_date_string(request.args.get("date")) or db.get_latest_crawl_date()),
            date_from=date_from or None,
            date_to=date_to or None,
            category=request.args.get("category") or None,
            attention=request.args.get("attention") or None,
            sort=request.args.get("sort") or "rank",
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "category", "rank", "stars", "score", "attention", "title", "arxiv_id", "pdf_url"])
        for paper in papers:
            evaluation = paper.get("latest_abstract_eval") or {}
            result = evaluation.get("result") or {}
            writer.writerow(
                [
                    paper.get("crawl_date"),
                    paper.get("category"),
                    paper.get("rank"),
                    paper.get("reading_stars"),
                    result.get("score"),
                    result.get("attention"),
                    paper.get("title"),
                    paper.get("arxiv_id"),
                    paper.get("pdf_url"),
                ]
            )
        return Response(
            output.getvalue(),
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
            "category": parse_required_text(request.form.get("category"), "category"),
            "name": parse_required_text(request.form.get("name"), "name"),
            "enabled": parse_bool(request.form.get("enabled"), "enabled"),
            "top_n": parse_int(request.form.get("top_n"), "top_n", default=30, minimum=1, maximum=200),
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
        prompt_type = parse_choice(
            request.form.get("type"),
            "type",
            {"abstract_review", "fulltext_review"},
        )
        data = {
            "id": _optional_int(request.form.get("id")),
            "name": parse_required_text(request.form.get("name"), "name"),
            "type": prompt_type,
            "template": parse_required_text(request.form.get("template"), "template"),
            "llm_profile_id": _optional_int(request.form.get("llm_profile_id")),
            "is_default": parse_bool(request.form.get("is_default"), "is_default"),
            "enabled": parse_bool(request.form.get("enabled"), "enabled"),
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
            profile["api_key_masked"] = runtime_secret_store.masked(profile.get("encrypted_api_key_ref"))
        return render_template(
            "llm_profiles.html",
            profiles=profiles,
            prompts=db.list_prompts(),
        )

    @app.post("/api/llm-profiles")
    def save_llm_profile():
        provider = parse_choice(
            request.form.get("provider"),
            "provider",
            {"openai_compatible", "anthropic"},
        )
        encrypted = None
        api_key = request.form.get("api_key", "").strip()
        if api_key:
            encrypted = runtime_secret_store.encrypt(api_key)
        data = {
            "id": _optional_int(request.form.get("id")),
            "name": parse_required_text(request.form.get("name"), "name"),
            "provider": provider,
            "base_url": parse_required_text(request.form.get("base_url"), "base_url").rstrip("/"),
            "model": parse_required_text(request.form.get("model"), "model"),
            "encrypted_api_key_ref": encrypted,
            "custom_headers": _valid_json_or_empty(request.form.get("custom_headers")),
            "temperature": parse_float(request.form.get("temperature"), "temperature", default=0.2, minimum=0),
            "max_output_tokens": parse_int(request.form.get("max_output_tokens"), "max_output_tokens", default=2000, minimum=1),
            "context_window_tokens": parse_int(request.form.get("context_window_tokens"), "context_window_tokens", default=128000, minimum=0),
            "timeout_seconds": parse_int(request.form.get("timeout_seconds"), "timeout_seconds", default=120, minimum=1),
            "enabled": parse_bool(request.form.get("enabled"), "enabled"),
            "is_default_abstract": parse_bool(request.form.get("is_default_abstract"), "is_default_abstract"),
            "is_default_fulltext": parse_bool(request.form.get("is_default_fulltext"), "is_default_fulltext"),
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
            settings=_settings_form_values(),
            jobs=db.list_jobs(30),
        )

    @app.post("/api/settings")
    def save_settings():
        try:
            command = SettingsCommand.from_form(request.form)
        except FormValidationError as exc:
            flash(f"设置未保存：{exc}")
            return render_template(
                "settings.html",
                settings=_settings_form_values(),
                jobs=db.list_jobs(30),
            ), 400
        db.save_settings(command.values)
        flash("设置已保存")
        return redirect(url_for("settings"))

    @app.post("/api/cache/cleanup")
    def run_cleanup():
        job_id = runtime_runner.enqueue("cleanup", {})
        flash(f"已创建缓存清理任务 #{job_id}")
        return redirect(url_for("settings"))

    @app.get("/logs")
    def logs():
        log_text = CURRENT_LOG.read_text(encoding="utf-8") if CURRENT_LOG.exists() else ""
        return render_template("logs.html", log_text=log_text, jobs=db.list_jobs(80))

    @app.get("/api/logs/current")
    def current_log():
        log_text = CURRENT_LOG.read_text(encoding="utf-8") if CURRENT_LOG.exists() else ""
        return Response(log_text, mimetype="text/plain; charset=utf-8")

    @app.get("/api/jobs/progress")
    def jobs_progress():
        runtime_runner.reconcile_orphaned_pending_jobs(min_interval_seconds=30)
        jobs = db.list_jobs(12)
        return {
            "jobs": [_job_progress_payload(job) for job in jobs]
        }


def _optional_int(value: str | None) -> int | None:
    return parse_optional_int(value, "id")


def _normalized_date_range(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    start = _valid_date_string(date_from)
    end = _valid_date_string(date_to)
    if start and end and start > end:
        start, end = end, start
    return start, end


def _valid_date_string(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if cleaned.isdigit() and len(cleaned) == 8:
        cleaned = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    else:
        cleaned = cleaned.replace("/", "-").replace(".", "-")
    try:
        date.fromisoformat(cleaned)
    except ValueError:
        return ""
    return cleaned


def _query_int(
    value: str | None,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value) if value not in {None, ""} else default
    except (TypeError, ValueError):
        return default
    parsed = max(minimum, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def _valid_json_or_empty(value: str | None) -> str:
    return parse_json_object(value, "custom_headers")


def _settings_form_values() -> dict[str, Any]:
    values = db.get_settings(SETTINGS_DEFAULTS)
    return {
        "pdf_retention_days": _safe_setting_int(values, "cache.pdf_retention_days", 5, minimum=0),
        "markdown_retention_days": _safe_setting_int(values, "cache.markdown_retention_days", 7, minimum=0),
        "cleanup_on_start": _safe_setting_bool(values, "cache.cleanup_on_start", True),
        "cleanup_daily": _safe_setting_bool(values, "cache.cleanup_daily", True),
        "abstract_concurrency": _safe_setting_int(
            values,
            "llm.abstract_concurrency",
            4,
            minimum=1,
            maximum=20,
        ),
        "crawler_trust_env_proxy": _safe_setting_bool(values, "crawler.trust_env_proxy", False),
        "crawler_proxy_url": str(values.get("crawler.proxy_url") or ""),
        "llm_trust_env_proxy": _safe_setting_bool(values, "llm.trust_env_proxy", False),
        "pdf_download_timeout_seconds": _safe_setting_int(
            values,
            "llm.pdf_download_timeout_seconds",
            300,
            minimum=30,
        ),
        "pdf_download_retries": _safe_setting_int(
            values,
            "llm.pdf_download_retries",
            2,
            minimum=0,
            maximum=5,
        ),
        "scheduler_enabled": _safe_setting_bool(values, "scheduler.enabled", True),
        "scheduler_daily_times": str(values.get("scheduler.daily_times") or "10:30,12:00"),
    }


def _safe_setting_int(
    values: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        return parse_int(
            values.get(key),
            key,
            default=default,
            minimum=minimum,
            maximum=maximum,
        )
    except FormValidationError:
        return default


def _safe_setting_bool(values: dict[str, Any], key: str, default: bool) -> bool:
    try:
        return parse_bool(values.get(key), key)
    except FormValidationError:
        return default


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
    return _safe_local_redirect(request.referrer) or url_for("index")


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return str(token)


def _origin_tuple(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not scheme or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, hostname, port


def _same_origin(url: str) -> bool:
    return _origin_tuple(url) == _origin_tuple(request.host_url)


def _safe_local_redirect(candidate: str | None) -> str | None:
    if not candidate or "\\" in candidate or any(ord(char) < 32 for char in candidate):
        return None
    if candidate.startswith("//"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        if not _same_origin(candidate):
            return None
        return urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    if not parsed.path.startswith("/"):
        return None
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


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
    return {
        "id": job["id"],
        "type": job["type"],
        "type_label": _job_type_label(job.get("type")),
        "status": job["status"],
        "status_label": _job_status_label(job.get("status")),
        "progress_current": job["progress_current"],
        "progress_total": job["progress_total"],
        "progress_percent": job["progress_percent"],
        "progress_label": _job_progress_label(job),
        "progress_message": job["progress_message"],
        "progress_details": job.get("progress_details") or {},
        "error_message": job.get("error_message") or "",
        "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "",
        "finished_at": job.get("finished_at") or "",
    }


def build_paper_export(paper: dict[str, Any], evaluations: list[dict[str, Any]]) -> str:
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
    for evaluation in evaluations:
        lines.extend(
            [
                f"## {evaluation['evaluation_type']} - {evaluation['status']} - {evaluation['created_at']}",
                "",
                "```json",
                json.dumps(evaluation.get("result") or {}, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
