"""Small HTML workflow using the existing local request security and job runner."""
from datetime import datetime,timedelta
from flask import current_app,flash,redirect,render_template,request,url_for
from . import db,exploration,exploration_db as store
from .form_commands import FormValidationError,parse_int,parse_bool


def register_exploration_routes(app):
    @app.get('/direction-exploration')
    def exploration_list():
        page=parse_int(request.args.get('page','1'),'page',minimum=1,maximum=1000000)
        reports,total=store.history(page)
        return render_template('exploration.html',plan=exploration.build_plan(),reports=reports,total=total,page=page,
            profiles=db.list_llm_profiles(enabled_only=True),profile_id=db.get_int_setting('exploration.profile_id',0))

    @app.post('/direction-exploration/profile')
    def exploration_profile():
        profile_id=parse_int(request.form.get('profile_id'),'profile',minimum=1,maximum=2**63-1)
        profile=db.get_llm_profile(profile_id)
        if not profile or not profile['enabled']:
            raise FormValidationError({'profile':'请选择启用的探索模型'})
        db.save_settings({'exploration.profile_id':profile_id})
        flash('探索模型已保存；不会启动模型调用。单独绑定的探索 Prompt 优先使用其绑定模型。')
        return redirect(url_for('exploration_list'))

    @app.post('/direction-exploration')
    def exploration_generate():
        run_id,job_id,created=current_app.extensions['daily_coolpapers.job_runner'].enqueue_exploration()
        flash(f"{'已创建' if created else '已存在'}探索任务 #{job_id}，只处理论文摘要。")
        preview = request.form.get('fingerprint')
        if preview and preview != store.get(run_id)['plan'].get('fingerprint'):
            flash('入队计划与此前预览不同，请以下方固化计划为准。')
        return redirect(url_for('exploration_detail',run_id=run_id))

    def render_detail(run, focus=None):
        papers={p['id']:p for p in run['plan']['papers']}
        for prior in run['plan']['prior']:
            for paper in prior.get('evidence_snapshot',[]):papers.setdefault(paper['id'],paper)
        retired = run['execution'].get('candidate_history',[])
        for candidate in [*run['candidates'],*retired]:
            candidate_papers = {**papers,**{p['id']:p for p in candidate.get('evidence_snapshot',[])}}
            candidate['papers'] = candidate_papers
            evidence=[candidate_papers[i] for i in candidate['paper_ids'] if i in candidate_papers]
            overlaps=sorted({a for p in evidence for a in p.get('authors',[]) if sum(a in q.get('authors',[]) for q in evidence)>1})
            candidate['author_overlap']=overlaps
            versions = run['execution'].get('candidate_versions',{}).get(str(candidate['id']),[])
            candidate['versions'] = [{**version,'papers':{p['id']:p for p in version.get('evidence_snapshot',[])}}
                                     for version in versions]
        extracted,_ = exploration._successful_extraction(run)
        return render_template('exploration_detail.html',run=run,job=db.get_job(run['job_id']),retired=retired,focus=focus,
            extracted_count=len(extracted),retry_error=exploration.original_retry_error(run),
            profiles=db.list_llm_profiles(enabled_only=True),
            backfill_from=(datetime.fromisoformat(run['date_to'])-timedelta(days=29)).date().isoformat(),
            backfill_to=run['date_to'],directions={d['id']:d for d in db.list_attention_directions()})

    @app.get('/direction-exploration/<int:run_id>')
    def exploration_detail(run_id):
        return render_detail(store.get(run_id))

    @app.get('/direction-exploration/<int:run_id>/candidates/<int:candidate_id>')
    def exploration_candidate(run_id,candidate_id):
        run,candidate = store.candidate(run_id,candidate_id)
        return render_detail(run,focus=candidate['id'])

    @app.post('/direction-exploration/<int:run_id>/retry')
    def exploration_retry(run_id):
        synthesis_only = parse_bool(request.form.get('synthesis_only'),'synthesis_only')
        profile = parse_int(request.form.get('profile_id'),'profile',minimum=1,maximum=2**63-1) if synthesis_only else None
        target,job_id,_=current_app.extensions['daily_coolpapers.job_runner'].enqueue_exploration(
            run_id=run_id,synthesis_only=synthesis_only,profile_id=profile,
            acknowledged=parse_bool(request.form.get('acknowledged'),'acknowledged'))
        flash(f'关联恢复任务 #{job_id} 已就绪；成功批次不会重发。')
        return redirect(url_for('exploration_detail',run_id=target))

    @app.post('/direction-exploration/<int:run_id>/candidates/<int:candidate_id>')
    def exploration_decide(run_id,candidate_id):
        store.decide(run_id,candidate_id,request.form.get('status'),request.form.get('reason',''),
            parse_int(request.form.get('version'),'version',minimum=0,maximum=2**63-1))
        flash('候选处理已保存，不触发模型调用。')
        return redirect(url_for('exploration_detail',run_id=run_id,_anchor=f'candidate-{candidate_id}'))

    @app.post('/direction-exploration/<int:run_id>/review')
    def exploration_review(run_id):
        store.record_review(run_id,parse_int(request.form.get('minutes'),'minutes',minimum=0,maximum=240),
            parse_int(request.form.get('version'),'version',minimum=0,maximum=2**63-1))
        flash('本周审阅时间已记录。')
        return redirect(url_for('exploration_detail',run_id=run_id))
