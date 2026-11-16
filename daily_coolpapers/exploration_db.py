"""Small, checkpointed exploration reports; human decisions are updated separately."""
import json
from . import db
from .form_commands import FormValidationError, research_text
from .llm import LLMResultError


MODEL_FIELDS = (
    'name','scope_text','signal_type','paper_ids','why_now','difference_from_existing','evidence_gaps',
    'prior_candidate_ref','relation_to_prior','new_evidence','new_paper_ids','evidence_snapshot',
)


def model_candidate(candidate):
    return {key:candidate[key] for key in MODEL_FIELDS if key in candidate}


def _fingerprint(candidate):
    content = model_candidate(candidate)
    for field in ('paper_ids','new_paper_ids'):
        if field in content:
            content[field] = sorted(content[field])
    if 'evidence_snapshot' in content:
        content['evidence_snapshot'] = sorted(content['evidence_snapshot'],key=lambda paper:paper['id'])
    return json.dumps(content,ensure_ascii=False,sort_keys=True)


def published_candidates(run):
    return [{**candidate,'ref':f"{run['id']}:{candidate['id']}"} for candidate in run['candidates']]


def init_schema(conn):
    conn.execute('SAVEPOINT direction_exploration_v1')
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS direction_exploration_runs (
            id INTEGER PRIMARY KEY, window_key TEXT NOT NULL UNIQUE,
            job_id INTEGER REFERENCES jobs(id), retry_of INTEGER REFERENCES direction_exploration_runs(id),
            date_from TEXT NOT NULL, date_to TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', plan_json TEXT NOT NULL,
            execution_json TEXT NOT NULL DEFAULT '{}', candidates_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_exploration_date ON direction_exploration_runs(date_to DESC,id DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_exploration_job ON direction_exploration_runs(job_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_exploration_status ON direction_exploration_runs(status,updated_at)')
        conn.execute('INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES (?,?)',
                     ('direction_exploration_v1', db.now_iso()))
        from .exploration import DEFAULT_PROMPTS
        for kind, template in DEFAULT_PROMPTS.items():
            if not conn.execute('SELECT 1 FROM prompts WHERE type=?', (kind,)).fetchone():
                conn.execute('''INSERT INTO prompts(name,type,template,version,is_default,enabled,created_at,updated_at)
                    VALUES (?,?,?,1,1,1,?,?)''', (kind, kind, template, db.now_iso(), db.now_iso()))
        conn.execute('RELEASE SAVEPOINT direction_exploration_v1')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT direction_exploration_v1')
        conn.execute('RELEASE SAVEPOINT direction_exploration_v1')
        raise


def decode(row):
    if row is None:
        raise db.DirectionNotFoundError('探索周报不存在')
    result = dict(row)
    for field in ('plan', 'execution', 'candidates'):
        result[field] = json.loads(result.pop(field + '_json'))
    return result


def get(run_id):
    if not isinstance(run_id,int) or not 0<run_id<=2**63-1:
        raise db.DirectionNotFoundError('探索周报不存在')
    with db.connect() as conn:
        return decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?', (run_id,)).fetchone())


def history(page=1):
    with db.connect() as conn:
        total = conn.execute('SELECT COUNT(*) FROM direction_exploration_runs').fetchone()[0]
        rows = conn.execute('''SELECT r.id,r.date_from,r.date_to,r.status,r.created_at,r.job_id,
            j.status AS job_status,json_array_length(r.candidates_json) AS candidate_count
            FROM direction_exploration_runs r LEFT JOIN jobs j ON j.id=r.job_id
            ORDER BY r.id DESC LIMIT 10 OFFSET ?''', ((page-1)*10,))
        return [dict(r) for r in rows], total


def prior_candidates():
    with db.connect() as conn:
        rows = conn.execute('''SELECT r.id AS run_id,c.value AS candidate FROM direction_exploration_runs r,
            json_each(r.candidates_json) c
            WHERE json_extract(c.value,'$.status') IN ('observing','ignored')
            AND json_extract(c.value,'$.superseded_by') IS NULL
            ORDER BY CASE json_extract(c.value,'$.status') WHEN 'observing' THEN 0 ELSE 1 END,
            json_extract(c.value,'$.updated_at') DESC,r.id DESC LIMIT 20''').fetchall()
    return [{**json.loads(r['candidate']), 'ref': f"{r['run_id']}:{json.loads(r['candidate'])['id']}"} for r in rows]


def create(plan, *, require_sample=False):
    key = f"{plan['date_from']}:{plan['date_to']}"
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT id,job_id FROM direction_exploration_runs WHERE window_key=?', (key,)).fetchone()
        if existing:
            return existing['id'], existing['job_id'], False
        error = plan.get('capacity_error') or (plan.get('config_error') if plan['papers'] else None)
        if error or (require_sample and not plan['papers']):
            raise FormValidationError({'plan':error or '此时间窗没有符合条件且未探索的论文'})
        return _insert(conn, plan, key)


def _insert(conn, plan, key, retry_of=None, execution=None):
    now = db.now_iso()
    run_id = conn.execute('''INSERT INTO direction_exploration_runs
        (window_key,date_from,date_to,plan_json,retry_of,execution_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)''', (key, plan['date_from'], plan['date_to'], json.dumps(plan,ensure_ascii=False),
        retry_of, json.dumps(execution or {}), now, now)).lastrowid
    job_id = conn.execute("INSERT INTO jobs(type,status,payload,created_at) VALUES ('direction_exploration','pending',?,?)",
                          (json.dumps({'run_id': run_id}), now)).lastrowid
    conn.execute('UPDATE direction_exploration_runs SET job_id=? WHERE id=?', (job_id,run_id))
    db._insert_job_event(conn, db._normalize_job_event(job_id,f'exploration:{job_id}:plan','exploration',
        'exploration.plan_created',metrics={'candidate_count':len(plan['papers']),'request_limit':plan['request_limit']},
        message='探索计划已固化；只处理元数据与摘要'))
    return run_id, job_id, True


def retry(run_id, *, synthesis_only=False, acknowledged=False, profile_id=None):
    from .exploration import original_retry_error, synthesis_retry_plan
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run = decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
        job = conn.execute('SELECT * FROM jobs WHERE id=?',(run['job_id'],)).fetchone()
        if job['status'] in ('pending','running'):
            return run_id, job['id'], False
        if run['status'] == 'success':
            raise FormValidationError({'run':'此周报已完成，无需重试'})
        execution = run['execution']
        jobs = execution.get('job_ids', [job['id']])
        unknown = conn.execute(f"SELECT 1 FROM llm_call_attempts WHERE job_id IN ({','.join('?' for _ in jobs)}) AND status IN ('external_outcome_unknown','provider_started') LIMIT 1",jobs).fetchone()
        if unknown and not acknowledged:
            raise FormValidationError({'run':'存在结果未知的外部请求；请确认可能产生额外费用后重试'})
        if synthesis_only:
            if run['plan'].get('synthesis_only'):
                raise FormValidationError({'run':'请在本次汇总重试中恢复剩余请求预算'})
            if execution.get('synthesis',{}).get('status') == 'success':
                raise FormValidationError({'run':'汇总已成功，请使用失败批次恢复入口'})
            key = f"synthesis:{run_id}"
            child = conn.execute('SELECT id,job_id FROM direction_exploration_runs WHERE window_key=?',(key,)).fetchone()
            if child:
                return child['id'],child['job_id'],False
            try:
                plan = synthesis_retry_plan(run,profile_id)
            except ValueError as exc:
                raise FormValidationError({'profile':str(exc)}) from None
            reused = {'batches':execution['batches'], 'consumed':execution.get('consumed',[])}
            return _insert(conn,plan,key,run_id,reused)
        error = original_retry_error(run)
        if error:
            raise FormValidationError({'profile':error})
        if execution.get('requests',0) >= run['plan']['request_limit']:
            raise FormValidationError({'run':'本次探索预算已耗尽，可显式重试汇总（最多新增两次请求）'})
        # A stage's consumed physical budget never resets on process or job retry.
        job_id = conn.execute("INSERT INTO jobs(type,status,payload,retry_of_job_id,created_at) VALUES ('direction_exploration','pending',?,?,?)",
            (json.dumps({'run_id':run_id}),job['id'],db.now_iso())).lastrowid
        conn.execute("UPDATE direction_exploration_runs SET job_id=?,status='pending',updated_at=? WHERE id=?",
                     (job_id,db.now_iso(),run_id))
        return run_id,job_id,True


def save_execution(run_id, execution, status=None, candidates=None):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
        execution = dict(execution)
        for key in ('candidate_history','candidate_versions','previous_candidates','review_minutes'):
            if key in current['execution']:
                execution[key] = current['execution'][key]
        fields = ['execution_json=?','updated_at=?']
        now = db.now_iso()
        args = [None,now]
        if status:
            fields.append('status=?');args.append(status)
        if candidates is not None:
            candidates = _save_candidates(conn,current,execution,candidates,now)
            if candidates != current['candidates']:
                fields.extend(['candidates_json=?','version=version+1'])
                args.append(json.dumps(candidates,ensure_ascii=False))
        args[0] = json.dumps(execution,ensure_ascii=False)
        conn.execute(f"UPDATE direction_exploration_runs SET {','.join(fields)} WHERE id=?",[*args,run_id])


def _save_candidates(conn, run, execution, candidates, now):
    if len(candidates) > 3:
        raise LLMResultError('exploration_schema_invalid','当前候选最多三个')
    old = {f"{run['id']}:{c['id']}":c for c in run['candidates']}
    explicit = [c.get('continuation_ref') for c in candidates if c.get('continuation_ref') is not None]
    if any(not isinstance(ref,str) or ref not in old for ref in explicit) or len(set(explicit)) != len(explicit):
        raise LLMResultError('exploration_evidence_invalid','周报延续候选引用无效或重复')
    remaining = dict(old)
    archived = execution.get('candidate_history',[])
    legacy_ids = [c['id'] for batch in execution.get('previous_candidates',[]) for c in batch]
    next_id = max([0,*legacy_ids,*(c['id'] for c in [*archived,*old.values()])])+1
    output = []
    seen_contents = set()
    for candidate in candidates:
        value = model_candidate(candidate)
        ref = candidate.get('continuation_ref')
        fingerprint = _fingerprint(value)
        if ref is None and fingerprint in seen_contents:
            continue
        seen_contents.add(fingerprint)
        if ref is None:
            matches = [key for key,item in remaining.items()
                       if key not in explicit and _fingerprint(item) == fingerprint]
            ref = matches[0] if len(matches) == 1 else None
        if ref is not None:
            previous = remaining.pop(ref)
            if _fingerprint(previous) == fingerprint:
                output.append(previous)
                continue
            execution.setdefault('candidate_versions',{}).setdefault(str(previous['id']),[]).append({
                'saved_at':previous.get('content_saved_at',run['created_at']), **model_candidate(previous)})
            output.append({**previous,**value,'content_saved_at':now})
            continue
        value.update(id=next_id,status='pending',reason='',decisions=[],updated_at=now,content_saved_at=now)
        next_id += 1
        _advance_prior_observation(conn,run['id'],value)
        output.append(value)
    if remaining:
        execution['candidate_history'] = [*archived,*({**c,'retired_at':now} for c in remaining.values())]
    return output


def _advance_prior_observation(conn, run_id, candidate):
    ref = candidate.get('prior_candidate_ref')
    if not ref or candidate.get('relation_to_prior') != 'observing_update':
        return
    try:
        previous_id, cid = map(int,ref.split(':'))
    except (AttributeError, ValueError):
        raise LLMResultError('exploration_evidence_invalid','历史观察引用无效') from None
    if not 0 < previous_id <= 2**63-1 or previous_id == run_id:
        raise LLMResultError('exploration_evidence_invalid','历史观察引用无效')
    row = conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(previous_id,)).fetchone()
    if not row:
        raise LLMResultError('exploration_evidence_invalid','历史观察引用无效')
    previous = decode(row)
    item = next((c for c in previous['candidates'] if c['id']==cid),None)
    if item and item['status']=='observing' and not item.get('superseded_by'):
        candidate.update(status='observing',reason=item.get('reason',''),decisions=item.get('decisions',[]))
        item['superseded_by'] = f"{run_id}:{candidate['id']}"
        conn.execute('UPDATE direction_exploration_runs SET candidates_json=?,version=version+1 WHERE id=?',
                     (json.dumps(previous['candidates'],ensure_ascii=False),previous_id))


def reserve_request(run_id, stage):
    from .llm import LLMError
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run = decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
        e = run['execution']
        attempts = e.setdefault('attempts',{})
        if e.get('requests',0) >= run['plan']['request_limit'] or attempts.get(stage,0) >= 2:
            raise LLMError('探索请求预算已耗尽',code='exploration_budget_exhausted')
        attempts[stage] = attempts.get(stage,0)+1
        e['requests'] = e.get('requests',0)+1
        conn.execute('UPDATE direction_exploration_runs SET execution_json=?,updated_at=? WHERE id=?',
                     (json.dumps(e,ensure_ascii=False),db.now_iso(),run_id))


def candidate(run_id, candidate_id):
    run = get(run_id)
    item = next((c for c in [*run['candidates'],*run['execution'].get('candidate_history',[])] if c['id']==candidate_id),None)
    if item is None:
        raise db.DirectionNotFoundError('候选方向不存在')
    return run, item


def decide(run_id, candidate_id, status, reason, version, *, name=None, scope_text=None):
    if status not in ('observing','ignored','added'):
        raise FormValidationError({'status':'无效候选处理状态'})
    reason = research_text(reason,'reason',required=status=='ignored')
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run = decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
        if run['version'] != version:
            c = next((c for c in run['candidates'] if c['id']==candidate_id),None)
            if status=='added' and c and c['status']=='added':
                return c.get('direction_id')
            raise FormValidationError({'version':'此周报已更新，请刷新后再处理'})
        c = next((c for c in run['candidates'] if c['id']==candidate_id),None)
        if c is None:
            if any(c['id']==candidate_id for c in run['execution'].get('candidate_history',[])):
                raise FormValidationError({'candidate':'此候选已转为历史，请刷新周报'})
            raise db.DirectionNotFoundError('候选方向不存在')
        if c.get('superseded_by'):
            raise FormValidationError({'candidate':'此观察候选已更新，请打开后续周报处理'})
        if c['status']=='added':
            return c.get('direction_id')  # Duplicate submit cannot create another direction.
        direction_id = None
        if status=='added':
            name = research_text(name,'name',required=True)
            scope_text = research_text(scope_text,'scope_text',required=True)
            normalized = db.normalized_research_name(name)
            if conn.execute("SELECT 1 FROM attention_directions WHERE normalized_name=? AND status='active'",(normalized,)).fetchone():
                raise db.DirectionConflictError('未归档方向中已存在同名定义，请编辑名称或使用已有方向')
            direction_id = conn.execute('''INSERT INTO attention_directions
                (name,normalized_name,scope_text,created_at,status_updated_at) VALUES (?,?,?,?,?)''',
                (name,normalized,scope_text,db.now_iso(),db.now_iso())).lastrowid
        c.setdefault('decisions',[]).append({'status':status,'reason':reason,'at':db.now_iso()})
        c.update(status=status,reason=reason,updated_at=db.now_iso())
        if direction_id:
            c['direction_id']=direction_id
        conn.execute('UPDATE direction_exploration_runs SET candidates_json=?,version=version+1,updated_at=? WHERE id=?',
                     (json.dumps(run['candidates'],ensure_ascii=False),db.now_iso(),run_id))
        return direction_id


def record_review(run_id, minutes, version):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run=decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
        job=conn.execute('SELECT status FROM jobs WHERE id=?',(run['job_id'],)).fetchone()
        if run['version']!=version or job['status'] in ('pending','running'):
            raise FormValidationError({'review':'请等待任务完成并刷新后记录审阅时间'})
        run['execution']['review_minutes']=minutes
        conn.execute('UPDATE direction_exploration_runs SET execution_json=?,version=version+1,updated_at=? WHERE id=?',
                     (json.dumps(run['execution'],ensure_ascii=False),db.now_iso(),run_id))
