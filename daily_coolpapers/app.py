import hmac
import json
import logging
import os
import secrets
import re
import threading
import time
from dataclasses import dataclass
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
from . import job_views
from . import memos, memo_db
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
    parse_theme_ids,
)
from .jobs import JobRunner, job_runner
from .llm import test_profile
from .logging_setup import setup_logging
from .security import SecretStore, secret_store
from .services import (
    build_paper_digest_csv,
    build_paper_evaluation_export,
    evaluation_prompt_options,
    evaluation_result_view,
    favorite_papers_page_model,
    reviewed_papers_page_model,
    paper_decision_model,
    paper_themes_model,
    investment_theme_papers_model,
    team_form_model,
    research_entities_model,
    safe_arxiv_abstract_url,
    paper_evaluation_result_model,
    resolve_evaluation_config,
    direction_backfill_preview,
)

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
        if request.endpoint and request.endpoint.startswith('memo_'):
            return _memo_error_response({'errors':error.errors},400)
        if request.endpoint in DIRECTION_ENDPOINTS:
            return _direction_error_response({'errors': error.errors}, 400)
        if request.endpoint in TEAM_WRITE_ENDPOINTS:
            return _team_error_response({'errors': error.errors}, 400)
        return _theme_form_error_response({'errors': error.errors}, 400)

    @app.errorhandler(db.InvestmentThemeNotFoundError)
    @app.errorhandler(db.DirectionNotFoundError)
    @app.errorhandler(db.ResearchEntityNotFoundError)
    @app.errorhandler(db.PaperNotFoundError)
    def handle_missing_organization_entity(error):
        if request.endpoint in DIRECTION_ENDPOINTS:
            return _direction_error_response({'error': str(error)}, 404)
        if request.endpoint in TEAM_WRITE_ENDPOINTS:
            return _team_error_response({'error': str(error)}, 404)
        return _theme_form_error_response({'error': str(error)}, 404)

    @app.errorhandler(db.ArchivedThemeError)
    @app.errorhandler(db.DirectionConflictError)
    @app.errorhandler(db.FulltextRequiredError)
    def handle_organization_conflict(error):
        if request.endpoint in DIRECTION_ENDPOINTS:
            return _direction_error_response({'error': str(error)}, 409)
        if request.endpoint in TEAM_WRITE_ENDPOINTS:
            return _team_error_response({'error': str(error)}, 409)
        return _theme_form_error_response({'error': str(error)}, 409)

    @app.errorhandler(db.ResearchEntityConflictError)
    def handle_research_conflict(error):
        return _team_error_response({'error': str(error), 'conflicts': error.conflicts}, 409)

    @app.errorhandler(memo_db.MemoNotFoundError)
    def missing_memo(error):
        return _memo_error_response({'error':str(error)},404)

    @app.errorhandler(memo_db.MemoConflictError)
    def conflict_memo(error):
        return _memo_error_response({'error':str(error)},409)

    return app


TEAM_WRITE_ENDPOINTS = {'save_team_tracking', 'archive_team_tracking', 'update_research_author', 'update_research_organization'}
DIRECTION_ENDPOINTS = {'create_attention_direction', 'archive_attention_direction', 'direction_backfill', 'save_direction_decision'}


def _direction_error_response(payload, status):
    if request.accept_mimetypes.best_match(['application/json', 'text/html']) != 'text/html':
        return payload, status
    return render_template('theme_form_error.html', errors=payload.get('errors', {}),
                           message=payload.get('error'), return_url=url_for('attention_directions'),
                           return_label='返回关注方向设置'), status


def _memo_error_response(payload,status):
    if request.accept_mimetypes.best_match(['application/json','text/html']) != 'text/html':
        return payload,status
    return render_template('theme_form_error.html',errors=payload.get('errors',{}),message=payload.get('error'),
                           return_url=url_for('memo_new'),return_label='返回研究备忘录，重新确认论文与配置'),status


def _team_error_response(payload: dict, status: int):
    if request.accept_mimetypes.best_match(['application/json', 'text/html']) != 'text/html':
        return payload, status
    paper_id = (request.view_args or {}).get('paper_id')
    paper = db.get_paper(paper_id) if paper_id and paper_id <= 2**63-1 else None
    form = team_form_model(paper, submitted=request.form) if paper and request.endpoint == 'save_team_tracking' and db.has_successful_fulltext(paper_id) else None
    return render_template('team_form_page.html', paper=paper, team_form=form, errors=payload.get('errors', {}),
                           message=payload.get('error'), conflicts=payload.get('conflicts', [])), status


def _theme_form_error_response(payload: dict, status: int):
    """Keep machine-readable errors while providing safe browser recovery links."""
    theme_forms = {'create_investment_theme', 'update_investment_theme',
                   'save_paper_investment_themes', 'remove_paper_investment_theme'}
    if (request.endpoint not in theme_forms or
            request.accept_mimetypes.best_match(['application/json', 'text/html']) != 'text/html'):
        return payload, status
    values = request.view_args or {}
    if 'paper_id' in values:
        return_url = url_for('paper_detail', paper_id=values['paper_id'], _anchor='fulltext-result')
        return_label = '返回论文并刷新主题选项'
    else:
        anchor = f"theme-{values['theme_id']}" if 'theme_id' in values else None
        return_url = url_for('investment_themes', _anchor=anchor)
        return_label = '返回投资主题设置'
    return render_template('theme_form_error.html', errors=payload.get('errors', {}),
                           message=payload.get('error'), return_url=return_url,
                           return_label=return_label), status


def _flask_secret() -> str:
    path = INSTANCE_DIR / "flask_secret.key"
    if not path.exists():
        path.write_text(os.urandom(32).hex(), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def register_template_helpers(app: Flask) -> None:
    app.add_template_filter(safe_arxiv_abstract_url, 'safe_arxiv_url')

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
        digest_query = _paper_digest_query_from_request()
        direction_filters = _direction_filters()
        page_number = _query_int(request.args.get("page"), 1, 1)
        page_size = _query_int(request.args.get("page_size"), 50, 1, 100)
        paper_page = db.list_paper_page(
            crawl_date=digest_query.selected_date or None,
            date_from=digest_query.date_from or None,
            date_to=digest_query.date_to or None,
            category=digest_query.category or None,
            attention=digest_query.attention or None,
            sort=digest_query.sort,
            page=page_number,
            page_size=page_size,
            **direction_filters,
        )
        jobs = _job_status_payloads(db.list_job_summaries(12))
        active_jobs = [job for job in jobs if job["status"] in {"pending", "running"}]
        progress_jobs = (active_jobs + [job for job in jobs if job not in active_jobs])[:4]
        return render_template(
            "index.html",
            papers=paper_page["items"],
            directions=db.list_attention_directions(),
            direction_filters=direction_filters,
            paper_page=paper_page,
            categories=db.list_categories(),
            digest_query=digest_query,
            selected_date=digest_query.selected_date,
            date_from=digest_query.date_from,
            date_to=digest_query.date_to,
            use_date_range=digest_query.use_date_range,
            selected_category=digest_query.category,
            attention=digest_query.attention,
            sort=digest_query.sort,
            export_csv_url=url_for("export_csv", **digest_query.url_args(), **direction_filters),
            rank_sort_url=url_for(
                "index", **digest_query.url_args(sort="rank_desc"), page_size=page_size, **direction_filters
            ),
            stars_sort_url=url_for(
                "index", **digest_query.url_args(sort="stars_desc"), page_size=page_size, **direction_filters
            ),
            jobs=jobs[:8],
            has_active_jobs=bool(active_jobs),
            progress_jobs=progress_jobs,
        )

    @app.get("/favorites")
    def favorites():
        page_model = favorite_papers_page_model(request.args.get("sort"))
        return render_template(
            "favorites.html",
            **page_model,
        )

    @app.get('/reviewed-papers')
    def reviewed_papers():
        decision = parse_choice(request.args.get('decision', 'all'), 'decision', db.PAPER_DECISION_FILTERS)
        return render_template('favorites.html', **reviewed_papers_page_model(request.args.get('sort'), decision))

    def theme_return_paper(value):
        if not value:
            return None
        paper_id = parse_int(value, 'paper_id', minimum=1, maximum=2**63-1)
        paper = db.get_paper(paper_id)
        if not paper:
            abort(404, description='返回的论文不存在')
        return paper

    @app.get('/investment-themes')
    def investment_themes():
        return_paper = theme_return_paper(request.args.get('paper_id'))
        return render_template('investment_themes.html', themes=db.list_investment_themes(), return_paper=return_paper)

    @app.post('/investment-themes')
    def create_investment_theme():
        return_paper = theme_return_paper(request.form.get('paper_id'))
        theme_id = db.create_investment_theme(request.form.get('name'), request.form.get('description'))
        flash('投资主题已创建；请回到论文手动选择加入' if return_paper else '投资主题已创建')
        return redirect(url_for('investment_themes', paper_id=return_paper['id'] if return_paper else None, _anchor=f'theme-{theme_id}'))

    @app.post('/investment-themes/<int:theme_id>')
    def update_investment_theme(theme_id: int):
        return_paper = theme_return_paper(request.form.get('paper_id'))
        action = parse_choice(request.form.get('action'), 'action', {'update','archive','restore'})
        db.update_investment_theme(theme_id, action, request.form.get('name'), request.form.get('description'))
        flash({'update': '投资主题已更新，论文关系保持不变', 'archive': '主题已归档，历史论文关系保留', 'restore': '主题已恢复，可重新加入论文'}[action])
        return redirect(url_for('investment_themes', paper_id=return_paper['id'] if return_paper else None, _anchor=f'theme-{theme_id}'))

    @app.get('/investment-themes/<int:theme_id>/papers')
    def investment_theme_papers(theme_id: int):
        return render_template('favorites.html', **investment_theme_papers_model(theme_id, request.args.get('sort')))

    @app.post('/api/papers/<int:paper_id>/investment-themes')
    def save_paper_investment_themes(paper_id: int):
        ids = parse_theme_ids(request.form.getlist('theme_ids'))
        db.set_paper_investment_themes(paper_id, ids)
        flash('投资主题已保存；已有归档主题关系保持不变', 'themes')
        return redirect(url_for('paper_detail', paper_id=paper_id, _anchor='fulltext-result'))

    @app.post('/api/papers/<int:paper_id>/investment-themes/<int:theme_id>/remove')
    def remove_paper_investment_theme(paper_id: int, theme_id: int):
        db.remove_paper_investment_theme(paper_id, theme_id)
        flash('已从论文移除该主题，其他关系和个人决策不变', 'themes')
        return redirect(url_for('paper_detail', paper_id=paper_id, _anchor='fulltext-result'))

    @app.post('/api/papers/<int:paper_id>/decision')
    def save_paper_decision(paper_id: int):
        decision = parse_choice(request.form.get('decision'), 'decision', db.PAPER_DECISIONS)
        try:
            db.set_paper_decision(paper_id, decision)
        except db.PaperNotFoundError as exc:
            abort(404, description=str(exc))
        except db.FulltextRequiredError as exc:
            abort(409, description=str(exc))
        flash({'favorite': '已收藏此论文', 'skipped': '已跳过此论文；论文和评估记录均保留',
               'clear': '已恢复未处理'}[decision], 'decision')
        return redirect(url_for('paper_detail', paper_id=paper_id, _anchor='fulltext-result'))

    @app.post('/api/papers/<int:paper_id>/team-tracking')
    def save_team_tracking(paper_id: int):
        db.save_paper_team_tracking(paper_id, request.form)
        flash('团队跟踪已保存，作者与机构均按你的选择记录', 'team')
        return redirect(url_for('paper_detail', paper_id=paper_id, _anchor='fulltext-result'))

    @app.get('/research-entities')
    def research_entities():
        return render_template('research_entities.html', **research_entities_model(request.args))

    @app.post('/api/papers/<int:paper_id>/team-tracking/archive')
    def archive_team_tracking(paper_id: int):
        db.archive_paper_team_tracking(paper_id)
        flash('已停止跟踪；作者、机构和历史关系保留', 'team')
        return redirect(url_for('paper_detail', paper_id=paper_id, _anchor='fulltext-result'))

    def save_research_entity(kind, entity_id):
        action = parse_choice(request.form.get('action'), 'action', {'update', 'archive', 'restore'})
        return_paper = None
        if request.form.get('return_paper_id'):
            return_id = parse_int(request.form.get('return_paper_id'), 'return_paper_id', minimum=1, maximum=2**63-1)
            return_paper = db.get_paper(return_id)
            if return_paper is None:
                raise db.PaperNotFoundError('返回的论文不存在')
        if return_paper and action != 'restore':
            raise FormValidationError({'action': '论文冲突处理仅支持显式恢复实体'})
        db.update_research_entity(kind, entity_id, action, request.form)
        if return_paper:
            flash('实体已恢复；尚未保存团队关系，请核对作者、机构及备注后重新提交', 'team')
            return redirect(url_for('paper_detail', paper_id=return_paper['id'],
                **{'team_'+kind+'_id': entity_id}, _anchor='fulltext-result'))
        flash({'update': '研究对象已更新', 'archive': '实体已归档，论文跟踪状态不变', 'restore': '实体已恢复，论文跟踪状态不变'}[action])
        return redirect(url_for('research_entities', view='authors' if kind == 'author' else 'organizations', status='all'))

    @app.post('/research-authors/<int:author_id>')
    def update_research_author(author_id: int):
        return save_research_entity('author', author_id)

    @app.post('/research-organizations/<int:organization_id>')
    def update_research_organization(organization_id: int):
        return save_research_entity('organization', organization_id)

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
        try:
            job_id, created = runtime_runner.enqueue_pipeline('manual_latest', category_ids or None)
        except ValueError as exc:
            flash(f'无法创建流水线：{exc}')
            return redirect(url_for('index'))
        flash(f"已创建抓取并摘要评估任务 #{job_id}" if created else f"已有抓取任务 #{job_id}，本次未重复创建")
        return redirect(url_for('job_detail', job_id=job_id))

    @app.post("/api/crawl/catch-up")
    def run_crawl_catch_up():
        category_ids = [parse_int(item, "category_ids", minimum=1) for item in request.form.getlist("category_ids") if item]
        try:
            job_id, created = runtime_runner.enqueue_pipeline(
                'manual_catch_up', category_ids or None,
                start_date=request.form.get('start_date') or None, end_date=request.form.get('end_date') or None,
            )
        except ValueError as exc:
            flash(f'无法创建流水线：{exc}')
            return redirect(url_for('index'))
        flash(f"已创建补抓并摘要评估任务 #{job_id}" if created else f"已有抓取任务 #{job_id}，本次未重复创建")
        return redirect(url_for('job_detail', job_id=job_id))

    @app.post("/api/abstract-evaluations/run")
    def run_abstract_evaluations():
        job_id = runtime_runner.enqueue("abstract_eval", {})
        flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(url_for("index"))

    @app.post("/api/papers/<int:paper_id>/evaluate-abstract")
    def evaluate_abstract(paper_id: int):
        job_id = _enqueue_paper_evaluation(
            runtime_runner, paper_id, "abstract_review", "abstract_eval"
        )
        if job_id:
            flash(f"已创建摘要评估任务 #{job_id}")
        return redirect(_back_to_detail_or_index(paper_id))

    @app.post("/api/papers/<int:paper_id>/evaluate-fulltext")
    def evaluate_fulltext(paper_id: int):
        force_markdown = parse_bool(request.form.get("force_markdown"), "force_markdown")
        job_id = _enqueue_paper_evaluation(
            runtime_runner,
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
        personal_decision = paper_decision_model(paper_id)
        return render_template(
            "paper_detail.html",
            paper=paper,
            direction_results=db.paper_direction_results([paper_id]).get(paper_id,[]),
            attention_directions=db.list_attention_directions(active_only=True),
            categories=db.get_paper_categories(paper_id),
            evaluation_results=paper_evaluation_result_model(paper_id),
            personal_decision=personal_decision,
            paper_themes=paper_themes_model(paper_id) if personal_decision['eligible'] else None,
            team_form=team_form_model(paper, selections=request.args) if personal_decision['eligible'] else None,
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
        body = build_paper_evaluation_export(paper)
        return Response(
            body,
            mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={paper['arxiv_id']}.md"},
        )

    @app.get("/export.csv")
    def export_csv():
        digest_query = _paper_digest_query_from_request()
        body = build_paper_digest_csv(db.filter_papers_by_directions(db.list_paper_rows(digest_query), **_direction_filters()))
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
            "category": parse_required_text(request.form.get("category"), "category"),
            "name": parse_required_text(request.form.get("name"), "name"),
            "enabled": parse_bool(request.form.get("enabled"), "enabled"),
            "top_n": parse_int(request.form.get("top_n"), "top_n", default=30, minimum=1, maximum=200),
            "sort_param": request.form.get("sort_param") or "sort=1",
        }
        db.save_category(data)
        flash("类目已保存")
        return redirect(url_for("categories"))

    @app.get('/investment-memos')
    def memo_list():
        return render_template('memo_list.html',series_list=memo_db.list_series(request.args.get('archived')=='1'))

    @app.get('/investment-memos/new')
    def memo_new():
        mode = parse_choice(request.args.get('source_mode','manual'),'source_mode',{'manual','attention_direction','investment_theme'})
        source_id = request.args.get('source_id') or request.args.get('source_direction_id' if mode=='attention_direction' else 'source_theme_id')
        entity_id = memos.optional_id(source_id,'source_id') if mode!='manual' else None
        command = memos.MemoRequest('',mode,entity_id,[],None,None)
        filters = memos.parse_candidate_filters(request.args)
        model = memos.candidate_page(command,filters)
        return render_template('memo_new.html',**model,command=command,filters=filters,
            selected_ids=[p['id'] for p in model['candidates'] if p['preselected']],
            directions=db.list_attention_directions(active_only=True),themes=[t for t in db.list_investment_themes() if t['status']=='active'],
            prompts=db.list_prompts('investment_memo',enabled_only=True),profiles=db.list_llm_profiles(enabled_only=True))

    @app.post('/investment-memos/preview')
    def memo_preview():
        command = memos.MemoRequest.from_form(request.form)
        return render_template('memo_preview.html',preview=memos.preview_memo(command),command=command,disclaimer=memos.DISCLAIMER)

    @app.post('/investment-memos')
    def memo_create():
        command = memos.MemoRequest.from_form(request.form,creating=True)
        created = runtime_runner.enqueue_memo(command)
        flash('已创建备忘录版本，后台将仅调用一次模型。' if created['created'] else '此提交已处理，返回原版本，未重复调用。')
        return redirect(url_for('memo_version',series_id=created['series_id'],version_id=created['id']))

    @app.get('/investment-memos/<int:series_id>')
    def memo_series(series_id):
        series,versions = memo_db.get_series(series_id)
        return render_template('memo_series.html',series=series,versions=versions)

    @app.get('/investment-memos/<int:series_id>/versions/<int:version_id>')
    def memo_version(series_id,version_id):
        series,version,papers = memo_db.get_version(series_id,version_id)
        return render_template('memo_version.html',series=series,version=version,papers=papers,
                               disclaimer=memos.DISCLAIMER,sections=memos.SECTIONS,claim_labels=memos.CLAIM_LABELS)

    @app.post('/investment-memos/<int:series_id>/versions/<int:version_id>/personal-judgment')
    def memo_personal_judgment(series_id,version_id):
        if set(request.form)-{'csrf_token','personal_judgment_markdown'} or 'personal_judgment_markdown' not in request.form:
            raise FormValidationError({'personal_judgment_markdown':'此入口只允许保存个人判断，不能修改 AI 或版本字段'})
        memo_db.save_personal_judgment(series_id,version_id,request.form['personal_judgment_markdown'])
        logger.info('Memo personal judgment saved series_id=%s version_id=%s',series_id,version_id)
        flash('个人判断已保存；AI 草稿和生成输入未改变，没有调用模型。')
        return redirect(url_for('memo_version',series_id=series_id,version_id=version_id,_anchor='personal-judgment'))

    @app.get('/investment-memos/<int:series_id>/versions/<int:version_id>/new-version')
    def memo_new_version(series_id,version_id):
        filters = memos.parse_candidate_filters(request.args)
        model = memos.new_version_editor(series_id,version_id,filters)
        prompts = db.list_prompts('investment_memo',enabled_only=True)
        profiles = db.list_llm_profiles(enabled_only=True)
        command = model['command']
        for entries,key in ((prompts,command.prompt_id),(profiles,command.profile_id)):
            if key and not any(item['id']==key for item in entries):
                entries.append({'id':key,'name':'原配置已不可用，请重新选择','model':'不可用','version':'不可用'})
        return render_template('memo_new.html',**model,filters=filters,prompts=prompts,profiles=profiles,
            directions=db.list_attention_directions(active_only=True),themes=[t for t in db.list_investment_themes() if t['status']=='active'])

    @app.post('/investment-memos/<int:series_id>/archive')
    def memo_archive(series_id):
        if set(request.form)-{'csrf_token'}:
            raise FormValidationError({'series':'此入口仅允许归档，不接受恢复或修改'})
        memo_db.archive_series(series_id)
        flash('系列已归档；历史版本和个人判断保留，不再接受新版本。')
        return redirect(url_for('memo_series',series_id=series_id))

    @app.get('/investment-memos/<int:series_id>/versions/<int:version_id>/export.md')
    def memo_export(series_id,version_id):
        markdown = memos.export_memo(series_id,version_id)
        logger.info('Memo exported series_id=%s version_id=%s',series_id,version_id)
        return Response(markdown,content_type='text/markdown; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="investment-memo-{series_id}-version-{version_id}.md"',
            'X-Content-Type-Options':'nosniff'})

    @app.get('/attention-directions')
    def attention_directions():
        return render_template('attention_directions.html', directions=db.list_attention_directions(),
                               show_archived=request.args.get('archived') == '1')

    @app.post('/attention-directions')
    def create_attention_direction():
        direction_id = db.create_attention_direction(request.form.get('name'), request.form.get('scope_text'))
        flash('关注方向已创建；历史论文不会自动重跑。名称和范围保存后不可修改。')
        return redirect(url_for('attention_directions', _anchor=f'direction-{direction_id}'))

    @app.post('/attention-directions/<int:direction_id>/archive')
    def archive_attention_direction(direction_id):
        if any(key in request.form for key in ('name', 'scope_text', 'action', 'status')):
            raise FormValidationError({'direction': '此入口只允许归档，不接受内容修改或恢复'})
        db.archive_attention_direction(direction_id)
        flash('关注方向已归档；历史结果保留，此方向不可恢复。')
        return redirect(url_for('attention_directions', archived='1', _anchor=f'direction-{direction_id}'))

    @app.post('/attention-directions/<int:direction_id>/backfill')
    def direction_backfill(direction_id):
        confirmed = parse_bool(request.form.get('confirmed'),'confirmed')
        date_from,date_to = request.form.get('date_from'),request.form.get('date_to')
        if not confirmed:
            preview = direction_backfill_preview(direction_id,date_from,date_to)
            return render_template('direction_backfill_preview.html',preview=preview)
        job_id = runtime_runner.enqueue_direction_backfill(direction_id,date_from,date_to)
        flash(f'已创建历史补分类任务 #{job_id}，匹配／可能匹配后自动继续缺失摘要评估。')
        return redirect(url_for('job_detail',job_id=job_id))

    @app.post('/api/papers/<int:paper_id>/direction-decisions')
    def save_direction_decision(paper_id):
        direction_id = parse_int(request.form.get('direction_id'),'direction_id',minimum=1,maximum=2**63-1)
        db.set_direction_decision(paper_id,direction_id,request.form.get('decision'))
        flash('人工分类已保存；模型原始判断未被覆盖，不触发模型评估。')
        return redirect(url_for('paper_detail',paper_id=paper_id,_anchor='paper-directions'))

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
            {"abstract_review", "fulltext_review", "direction_classification", "investment_memo"},
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
            try:
                profile["api_key_masked"] = runtime_secret_store.masked(
                    profile.get("encrypted_api_key_ref")
                )
            except ValueError:
                logger.warning(
                    "Unable to decrypt API key for LLM profile id=%s",
                    profile.get("id"),
                )
                profile["api_key_masked"] = "无法解密，请重新输入"
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
            "is_default_classification": parse_bool(request.form.get('is_default_classification'), 'is_default_classification'),
            "is_default_memo": parse_bool(request.form.get('is_default_memo'),'is_default_memo'),
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
            jobs=_job_status_payloads(db.list_job_summaries(30)),
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
                jobs=_job_status_payloads(db.list_job_summaries(30)),
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
        log_text = _current_log_text()
        return render_template("logs.html", log_text=log_text, jobs=_job_status_payloads(db.list_job_summaries(80)))

    @app.get("/api/logs/current")
    def current_log():
        return Response(_current_log_text(), mimetype="text/plain; charset=utf-8", headers={'Cache-Control': 'no-store'})

    @app.get("/api/jobs/progress")
    def jobs_progress():
        jobs = _job_status_payloads(db.list_active_job_progress(12))
        return {
            "jobs": jobs
        }

    @app.get('/jobs/<int:job_id>')
    def job_detail(job_id: int):
        job, card, filters, event_page = job_detail_data(job_id)
        return render_template('job_detail.html', job=job, pipeline=card, filters=filters,
                               event_page=event_page, stages=job_views.STAGES)

    def job_detail_data(job_id: int):
        raw = db.get_job(job_id)
        if not raw:
            abort(404)
        severity = request.args.get('severity', 'issues')
        view = request.args.get('view', 'grouped')
        stage = request.args.get('stage', '')
        if severity not in {'issues', 'all', 'warning', 'error'} or view not in {'grouped', 'timeline'} or (stage and stage not in job_views.STAGES):
            abort(400)
        filters = {'severity': severity, 'view': view, 'stage': stage,
                   'category': request.args.get('category', '')[:64], 'crawl_date': request.args.get('crawl_date', '')[:16]}
        if filters['category'] and not re.fullmatch(r'[A-Za-z][A-Za-z.-]{0,30}', filters['category']):
            abort(400)
        if filters['crawl_date'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', filters['crawl_date']):
            abort(400)
        job = _job_status_payloads([raw])[0]
        card = job.get('pipeline')
        events = db.list_job_event_page(job_id, **filters, page=_query_int(request.args.get('page'), 1, 1))
        events['items'] = [job_views.event_view(event) for event in events['items']]
        return job, card, filters, events

    @app.get('/api/jobs/<int:job_id>/diagnostic')
    def job_diagnostic(job_id: int):
        job, card, filters, events = job_detail_data(job_id)
        # Only safe display models leave this endpoint, never raw payloads or messages.
        result = {'job_id': job_id, 'status': job['status'], 'timezone': 'Asia/Shanghai',
                  'summary': card, 'filters': filters, 'events': events,
                  'scope': '仅包含当前筛选和当前页；完整时间线请逐页查看'}
        return Response(json.dumps(result, ensure_ascii=False, indent=2), mimetype='application/json',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/api/jobs/<int:job_id>/retry')
    def retry_pipeline(job_id: int):
        original = db.get_job(job_id)
        if not original:
            abort(404)
        if original['type'] != db.DAILY_PIPELINE_JOB_TYPE or original['status'] not in db.JOB_TERMINAL_STATUSES:
            abort(409)
        mode = request.form.get('mode', 'all')
        if mode not in {'all', 'abstract_only'}:
            abort(400)
        try:
            target_id, created = runtime_runner.enqueue_pipeline('manual_latest', retry_of_job_id=job_id, retry_mode=mode)
        except (ValueError, KeyError):
            flash('无法重试：计划或事件不可用。事件已清理时，请使用新的抓取或“评估缺失摘要”入口。')
            return redirect(url_for('job_detail', job_id=job_id))
        flash(f'已创建重试任务 #{target_id}' if created else f'已有抓取任务 #{target_id}，未重复创建')
        return redirect(url_for('job_detail', job_id=target_id))


def _current_log_text() -> str:
    if not CURRENT_LOG.exists():
        return ''
    with CURRENT_LOG.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 256 * 1024))
        value = stream.read().decode('utf-8', errors='replace')
    return job_views.redact_text(value)


def _optional_int(value: str | None) -> int | None:
    return parse_optional_int(value, "id")


def _direction_filters():
    direction_id = parse_int(request.args.get('direction_id'),'direction_id',minimum=1,maximum=2**63-1) if request.args.get('direction_id') else None
    filters = {'direction_id':direction_id,'model_state':request.args.get('model_state',''),
               'manual_state':request.args.get('manual_state',''),'direction_view':request.args.get('direction_view','focused')}
    db.direction_filter_sql('p.id',**filters)
    return filters


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
        "abstract_retries": _safe_setting_int(values, 'llm.abstract_retries', 2, minimum=0, maximum=5),
        "event_retention_days": _safe_setting_int(values, 'job_events.retention_days', 30, minimum=1, maximum=3650),
        "missing_field_warning_rate": values.get('crawler.missing_field_warning_rate', 0.0),
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


def _paper_evaluation_actions(paper_id: int) -> list[dict[str, Any]]:
    return [
        {
            "key": "abstract_review",
            "title": "摘要 Prompt",
            "action_url": url_for("evaluate_abstract", paper_id=paper_id),
            "button_label": "手动摘要评估（已有结果时重新评估）",
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
    runner: JobRunner,
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
    return runner.enqueue(job_type, payload)


def _job_type_label(job_type: str | None) -> str:
    return {
        "daily_pipeline": "每日情报流水线",
        "direction_backfill": "关注方向历史补分类",
        "investment_memo_generation": "研究备忘录生成",
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
        "partial_success": "部分完成",
        "failed": "失败",
        "interrupted": "已中断",
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
        "is_failed": status in {"failed", "interrupted"},
    }


def _job_message(job: dict[str, Any]) -> str:
    if job.get("status") in {"failed", "interrupted"} and job.get("error_message"):
        return str(job.get("error_message") or "")
    return str(job.get("progress_message") or _job_progress_label(job))


def _job_status_payloads(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards = job_views.pipeline_cards(jobs)
    result = []
    for raw in jobs:
        job = _job_progress_payload(raw)
        job['detail_url'] = url_for('job_detail', job_id=job['id'])
        for key in ('created_at', 'started_at', 'finished_at'):
            job[key] = job_views.local_time(job[key])
        if job['id'] in cards:
            card = cards[job['id']]
            job.update(pipeline=card, message=card['message'], progress_message=card['message'],
                       error_message='', progress_details={},
                       detail={'summary_lines': card['summary_lines'], 'item_rows': []})
        else:
            job['message'] = job_views.redact_text(job['message'])
            job['error_message'] = job_views.redact_text(job['error_message'])
            job['progress_message'] = job_views.redact_text(job['progress_message'])
            # Legacy progress needs only a small, sanitized presentation model.
            job['progress_details'] = {}
            if job['detail']:
                for item in job['detail']['item_rows']:
                    item['line'] = job_views.redact_text(item['line'])
                    item['title'] = job_views.redact_text(item['title'])
                job['detail']['summary_lines'] = [job_views.redact_text(line) for line in job['detail']['summary_lines']]
        result.append(job)
    return result


def _job_detail_payload(details: dict[str, Any]) -> dict[str, Any] | None:
    phase = details.get("phase")
    if phase == 'investment_memo':
        return {'summary_lines':[f"备忘录版本 #{details.get('version_id')} · {details.get('paper_count',0)} 篇论文",
                                 f"输入 token {details.get('input_tokens')} / 输出 {details.get('output_tokens')}"], 'item_rows':[]}
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
