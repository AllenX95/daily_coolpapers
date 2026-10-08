"""Small, checkpointed exploration reports; human decisions are updated separately."""
import json
from . import db
from .form_commands import FormValidationError, research_text


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


def create(plan):
    key = f"{plan['date_from']}:{plan['date_to']}"
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT id,job_id FROM direction_exploration_runs WHERE window_key=?', (key,)).fetchone()
        if existing:
            return existing['id'], existing['job_id'], False
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


def retry(run_id, *, synthesis_only=False, acknowledged=False):
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
            if not execution.get('batches') or not any(b.get('status') == 'success' for b in execution['batches'].values()):
                raise FormValidationError({'run':'没有可复用的成功提取结果'})
            if execution.get('synthesis',{}).get('status') == 'success':
                raise FormValidationError({'run':'汇总已成功，请使用失败批次恢复入口'})
            key = f"synthesis:{run_id}"
            child = conn.execute('SELECT id,job_id FROM direction_exploration_runs WHERE window_key=?',(key,)).fetchone()
            if child:
                return child['id'],child['job_id'],False
            plan = {**run['plan'], 'request_limit':2, 'synthesis_only':True}
            reused = {'batches':execution['batches'], 'consumed':execution.get('consumed',[])}
            return _insert(conn,plan,key,run_id,reused)
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
        fields = ['execution_json=?','updated_at=?']
        args = [json.dumps(execution,ensure_ascii=False),db.now_iso()]
        if status:
            fields.append('status=?');args.append(status)
        if candidates is not None:
            current = decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(run_id,)).fetchone())
            old_by_name = {db.normalized_research_name(c['name']):c for c in current['candidates']}
            # Preserve human decisions for the same candidate after recovering a partial report.
            for c in candidates:
                old = old_by_name.get(db.normalized_research_name(c['name']))
                if old:
                    for key in ('status','reason','decisions','direction_id'):
                        if key in old:c[key]=old[key]
                ref = c.get('prior_candidate_ref')
                if ref and c.get('relation_to_prior')=='observing_update':
                    previous_id,previous_cid=map(int,ref.split(':'))
                    previous=decode(conn.execute('SELECT * FROM direction_exploration_runs WHERE id=?',(previous_id,)).fetchone())
                    previous_c=next((item for item in previous['candidates'] if item['id']==previous_cid),None)
                    if previous_c and previous_c['status']=='observing' and not previous_c.get('superseded_by'):
                        c.update(status='observing',reason=previous_c.get('reason',''),decisions=previous_c.get('decisions',[]))
                        previous_c['superseded_by']=f"{run_id}:{c['id']}"
                        conn.execute('UPDATE direction_exploration_runs SET candidates_json=?,version=version+1 WHERE id=?',
                            (json.dumps(previous['candidates'],ensure_ascii=False),previous_id))
            if current['candidates'] and current['candidates']!=candidates:
                execution.setdefault('previous_candidates',[]).append(current['candidates'])
                args[0]=json.dumps(execution,ensure_ascii=False)
            fields.extend(['candidates_json=?','version=version+1'])
            args.append(json.dumps(candidates,ensure_ascii=False))
        conn.execute(f"UPDATE direction_exploration_runs SET {','.join(fields)} WHERE id=?",[*args,run_id])


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
    item = next((c for c in run['candidates'] if c['id']==candidate_id),None)
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
        c = next((c for c in run['candidates'] if c['id']==candidate_id),None)
        if c is None:
            raise db.DirectionNotFoundError('候选方向不存在')
        if c.get('superseded_by'):
            raise FormValidationError({'candidate':'此观察候选已更新，请打开后续周报处理'})
        if c['status']=='added':
            return c.get('direction_id')  # Duplicate submit cannot create another direction.
        if run['version'] != version:
            raise FormValidationError({'version':'此周报已更新，请刷新后再处理'})
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
