"""SQLite persistence for immutable research memo versions and their evidence."""
import json

from . import db
from .form_commands import FormValidationError


class MemoNotFoundError(LookupError):
    pass


class MemoConflictError(ValueError):
    pass


def init_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS investment_memo_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 120),
            source_mode TEXT NOT NULL CHECK(source_mode IN ('manual','attention_direction','investment_theme')),
            source_direction_id INTEGER REFERENCES attention_directions(id) ON DELETE SET NULL,
            source_theme_id INTEGER REFERENCES investment_themes(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK((source_mode='manual' AND source_direction_id IS NULL AND source_theme_id IS NULL)
               OR (source_mode='attention_direction' AND source_theme_id IS NULL)
               OR (source_mode='investment_theme' AND source_direction_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS investment_memo_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES investment_memo_series(id) ON DELETE RESTRICT,
            version_no INTEGER NOT NULL CHECK(version_no>0),
            previous_version_id INTEGER REFERENCES investment_memo_versions(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            source_mode_snapshot TEXT NOT NULL,
            source_entity_id_snapshot INTEGER,
            source_name_snapshot TEXT NOT NULL DEFAULT '', source_scope_snapshot TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending','running','success','failed','interrupted')),
            job_id INTEGER REFERENCES jobs(id),
            prompt_id INTEGER REFERENCES prompts(id) ON DELETE SET NULL, profile_id INTEGER,
            prompt_snapshot TEXT NOT NULL, profile_snapshot_json TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, input_snapshot_json TEXT NOT NULL,
            estimated_input_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            result_json TEXT, rendered_markdown TEXT, raw_output TEXT,
            personal_judgment_markdown TEXT NOT NULL DEFAULT '', personal_judgment_updated_at TEXT,
            error_code TEXT, error_message TEXT, provider_started INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            UNIQUE(series_id,version_no),
            CHECK(status!='success' OR (result_json IS NOT NULL AND rendered_markdown IS NOT NULL))
        );
        CREATE TABLE IF NOT EXISTS investment_memo_version_papers (
            memo_version_id INTEGER NOT NULL REFERENCES investment_memo_versions(id) ON DELETE CASCADE,
            paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
            paper_arxiv_id_snapshot TEXT NOT NULL,
            display_order INTEGER NOT NULL CHECK(display_order>0),
            selection_origin TEXT NOT NULL CHECK(selection_origin IN ('preselected','manual_selected','manual_added')),
            abstract_evaluation_id INTEGER REFERENCES evaluations(id) ON DELETE SET NULL,
            fulltext_evaluation_id INTEGER REFERENCES evaluations(id) ON DELETE SET NULL,
            paper_snapshot_json TEXT NOT NULL,
            PRIMARY KEY(memo_version_id,display_order),
            UNIQUE(memo_version_id,paper_arxiv_id_snapshot)
        );
        CREATE INDEX IF NOT EXISTS idx_memo_series_status ON investment_memo_series(status,updated_at);
        CREATE INDEX IF NOT EXISTS idx_memo_version_status ON investment_memo_versions(status,created_at);
        CREATE INDEX IF NOT EXISTS idx_memo_version_order ON investment_memo_versions(series_id,version_no);
        CREATE INDEX IF NOT EXISTS idx_memo_paper_order ON investment_memo_version_papers(memo_version_id,display_order);
        CREATE TRIGGER IF NOT EXISTS memo_series_identity_immutable BEFORE UPDATE ON investment_memo_series
        WHEN OLD.title IS NOT NEW.title OR OLD.source_mode IS NOT NEW.source_mode
          OR (NEW.source_direction_id IS NOT NULL AND OLD.source_direction_id IS NOT NEW.source_direction_id)
          OR (NEW.source_theme_id IS NOT NULL AND OLD.source_theme_id IS NOT NEW.source_theme_id)
          OR (OLD.status='archived' AND NEW.status!='archived')
        BEGIN SELECT RAISE(ABORT,'memo_series_identity_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS memo_input_immutable BEFORE UPDATE ON investment_memo_versions
        WHEN OLD.series_id IS NOT NEW.series_id OR OLD.version_no IS NOT NEW.version_no
          OR OLD.previous_version_id IS NOT NEW.previous_version_id OR OLD.idempotency_key IS NOT NEW.idempotency_key
          OR OLD.source_mode_snapshot IS NOT NEW.source_mode_snapshot
          OR OLD.source_entity_id_snapshot IS NOT NEW.source_entity_id_snapshot
          OR OLD.source_name_snapshot IS NOT NEW.source_name_snapshot OR OLD.source_scope_snapshot IS NOT NEW.source_scope_snapshot
          OR OLD.prompt_snapshot IS NOT NEW.prompt_snapshot OR OLD.profile_snapshot_json IS NOT NEW.profile_snapshot_json
          OR OLD.profile_id IS NOT NEW.profile_id OR OLD.provider IS NOT NEW.provider OR OLD.model IS NOT NEW.model
          OR OLD.input_snapshot_json IS NOT NEW.input_snapshot_json OR OLD.estimated_input_tokens IS NOT NEW.estimated_input_tokens
          OR OLD.created_at IS NOT NEW.created_at OR (OLD.job_id IS NOT NULL AND OLD.job_id IS NOT NEW.job_id)
        BEGIN SELECT RAISE(ABORT,'memo_input_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS memo_success_immutable BEFORE UPDATE ON investment_memo_versions
        WHEN OLD.status='success' AND (OLD.status IS NOT NEW.status OR OLD.result_json IS NOT NEW.result_json
          OR OLD.rendered_markdown IS NOT NEW.rendered_markdown OR OLD.raw_output IS NOT NEW.raw_output
          OR OLD.input_tokens IS NOT NEW.input_tokens OR OLD.output_tokens IS NOT NEW.output_tokens
          OR OLD.started_at IS NOT NEW.started_at OR OLD.finished_at IS NOT NEW.finished_at
          OR OLD.error_code IS NOT NEW.error_code OR OLD.error_message IS NOT NEW.error_message)
        BEGIN SELECT RAISE(ABORT,'memo_success_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS memo_evidence_immutable BEFORE UPDATE ON investment_memo_version_papers
        WHEN OLD.memo_version_id IS NOT NEW.memo_version_id OR OLD.display_order IS NOT NEW.display_order
          OR OLD.paper_arxiv_id_snapshot IS NOT NEW.paper_arxiv_id_snapshot
          OR OLD.selection_origin IS NOT NEW.selection_origin OR OLD.paper_snapshot_json IS NOT NEW.paper_snapshot_json
        BEGIN SELECT RAISE(ABORT,'memo_evidence_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS memo_reference_immutable BEFORE UPDATE ON investment_memo_version_papers
        WHEN (NEW.paper_id IS NOT NULL AND OLD.paper_id IS NOT NEW.paper_id)
          OR (NEW.abstract_evaluation_id IS NOT NULL AND OLD.abstract_evaluation_id IS NOT NEW.abstract_evaluation_id)
          OR (NEW.fulltext_evaluation_id IS NOT NULL AND OLD.fulltext_evaluation_id IS NOT NEW.fulltext_evaluation_id)
        BEGIN SELECT RAISE(ABORT,'memo_reference_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS memo_config_reference_immutable BEFORE UPDATE ON investment_memo_versions
        WHEN (NEW.prompt_id IS NOT NULL AND OLD.prompt_id IS NOT NEW.prompt_id)
          OR (OLD.status='success' AND OLD.provider_started IS NOT NEW.provider_started)
        BEGIN SELECT RAISE(ABORT,'memo_config_reference_immutable'); END;
    ''')


def series_row(conn,series_id,*,active=False):
    row = conn.execute('SELECT * FROM investment_memo_series WHERE id=?',(series_id,)).fetchone() if 0 < series_id <= 2**63-1 else None
    if not row:
        raise MemoNotFoundError('备忘录系列不存在')
    if active and row['status'] != 'active':
        raise MemoConflictError('系列已归档，只能查看历史版本')
    return dict(row)


def version_row(conn,series_id,version_id):
    series_row(conn,series_id)
    row = conn.execute('SELECT * FROM investment_memo_versions WHERE id=? AND series_id=?',(version_id,series_id)).fetchone() if 0 < version_id <= 2**63-1 else None
    if not row:
        raise MemoNotFoundError('此系列中不存在该备忘录版本')
    return dict(row)


def source_snapshot(conn,mode,entity_id):
    if mode == 'manual':
        if entity_id is not None:
            raise FormValidationError({'source':'手工模式不接受来源实体'})
        return {'mode':'manual','id':None,'name':'','scope':''}
    tables = {'attention_direction':('attention_directions','scope_text'), 'investment_theme':('investment_themes','description')}
    if mode not in tables:
        raise FormValidationError({'source_mode':'无效的来源模式'})
    if entity_id is None:
        raise MemoConflictError('来源记录已不可用，请另建手工系列')
    table,scope = tables[mode]
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?',(entity_id,)).fetchone() if 0 < entity_id <= 2**63-1 else None
    if not row:
        raise MemoNotFoundError('所选来源不存在')
    if row['status'] != 'active':
        raise MemoConflictError('所选来源已归档，不能创建新的备忘录版本')
    return {'mode':mode,'id':entity_id,'name':row['name'],'scope':row[scope]}


def paper_snapshots(conn,paper_ids,*,require_eligible=True):
    """Six bounded queries per batch, all on the caller's transaction snapshot."""
    snapshots = {}
    for chunk in db._chunks(paper_ids):
        marks = ','.join('?' for _ in chunk)
        for row in conn.execute(f'''SELECT p.*,d.decision,d.created_at AS favorited_at FROM papers p
            LEFT JOIN paper_dispositions d ON d.paper_id=p.id WHERE p.id IN ({marks})''',chunk):
            paper = dict(row)
            paper['authors'] = db.loads_json(paper['authors'],[])
            paper['subjects'] = db.loads_json(paper['subjects'],[])
            snapshots[paper['id']] = {'paper':paper,'categories':[],'directions':[],'themes':[],'team':None,
                                      'abstract_evaluation':None,'fulltext_evaluation':None}
        for row in conn.execute(f'''SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER (
            PARTITION BY paper_id,evaluation_type ORDER BY created_at DESC,id DESC) AS rn FROM evaluations e
            WHERE paper_id IN ({marks}) AND status='success' AND evaluation_type IN ('abstract_review','fulltext_review')) WHERE rn=1''',chunk):
            evaluation = {key:row[key] for key in ('id','evaluation_type','prompt_id','prompt_version','llm_profile_id','model','created_at')}
            evaluation['result'] = db.loads_json_object(row['result_json'])
            snapshots[row['paper_id']]['abstract_evaluation' if row['evaluation_type']=='abstract_review' else 'fulltext_evaluation'] = evaluation
        for row in conn.execute(f'SELECT * FROM paper_categories WHERE paper_id IN ({marks}) ORDER BY crawl_date,category',chunk):
            snapshots[row['paper_id']]['categories'].append(dict(row))
        for row in conn.execute(f'''SELECT r.*,d.name,d.scope_text,d.status AS direction_status FROM paper_direction_results r
            JOIN attention_directions d ON d.id=r.direction_id WHERE r.paper_id IN ({marks}) ORDER BY r.direction_id''',chunk):
            item = dict(row)
            item['effective'] = item['manual_decision']=='confirmed' or (item['manual_decision'] is None and item['model_decision']=='matched')
            snapshots[row['paper_id']]['directions'].append(item)
        for row in conn.execute(f'''SELECT r.paper_id,t.* FROM paper_investment_themes r
            JOIN investment_themes t ON t.id=r.theme_id WHERE r.paper_id IN ({marks}) ORDER BY t.id''',chunk):
            snapshots[row['paper_id']]['themes'].append(dict(row))
        for row in conn.execute(f'''SELECT t.*,a.name AS author_name,a.author_category,a.notes AS author_notes,
            a.status AS author_status,o.name AS organization_name,o.organization_type,o.region,
            o.notes AS organization_notes,o.status AS organization_status FROM paper_team_tracking t
            JOIN research_authors a ON a.id=t.lead_author_id JOIN research_organizations o ON o.id=t.organization_id
            WHERE t.paper_id IN ({marks})''',chunk):
            snapshots[row['paper_id']]['team'] = dict(row)
    missing = [key for key in paper_ids if key not in snapshots]
    if missing:
        raise MemoNotFoundError('论文不存在：' + ', '.join(map(str,missing)))
    invalid = [key for key in paper_ids if snapshots[key]['paper']['decision']!='favorite' or not snapshots[key]['fulltext_evaluation']]
    if invalid and require_eligible:
        raise MemoConflictError('以下论文已不满足收藏／成功全文评估资格，请重新确认：' + ', '.join(map(str,invalid)))
    return snapshots


def source_matches(snapshot,source):
    if source['mode']=='manual':
        return False
    if source['mode']=='investment_theme':
        return any(t['id']==source['id'] for t in snapshot['themes'])
    return any(d['direction_id']==source['id'] and d['effective'] for d in snapshot['directions'])


def candidate_data(conn,source,filters):
    ids = [r[0] for r in conn.execute("SELECT p.id FROM papers p JOIN paper_dispositions d ON d.paper_id=p.id WHERE d.decision='favorite' ORDER BY p.id")]
    snapshots = paper_snapshots(conn,ids,require_eligible=False)
    counts = {key:0 for key in ('source_total','source_favorites','preselected','not_favorite','pending_possible','rejected','missing_fulltext','not_effective')}
    if source['mode'] != 'manual':
        if source['mode']=='investment_theme':
            source_ids = [r[0] for r in conn.execute('SELECT paper_id FROM paper_investment_themes WHERE theme_id=?',(source['id'],))]
        else:
            source_ids = [r[0] for r in conn.execute('SELECT paper_id FROM paper_direction_results WHERE direction_id=?',(source['id'],))]
        source_papers = paper_snapshots(conn,source_ids,require_eligible=False)
        for item in source_papers.values():
            counts['source_total'] += 1
            favorite = item['paper']['decision']=='favorite'
            counts['source_favorites'] += int(favorite)
            counts['not_favorite'] += int(not favorite)
            counts['missing_fulltext'] += int(not item['fulltext_evaluation'])
            if source['mode']=='attention_direction':
                relation = next(d for d in item['directions'] if d['direction_id']==source['id'])
                counts['pending_possible'] += int(relation['model_decision']=='possible' and relation['manual_decision'] is None)
                counts['rejected'] += int(relation['manual_decision']=='rejected')
                counts['not_effective'] += int(not relation['effective'])
            counts['preselected'] += int(favorite and bool(item['fulltext_evaluation']) and source_matches(item,source))
    candidates = []
    for item in snapshots.values():
        if not item['fulltext_evaluation']:
            continue
        paper,team = item['paper'], item['team'] or {}
        if filters.get('query') and filters['query'].casefold() not in (paper['title']+' '+paper['arxiv_id']).casefold():
            continue
        if filters.get('direction_id') and not any(d['direction_id']==filters['direction_id'] and d['effective'] for d in item['directions']):
            continue
        if filters.get('theme_id') and not any(t['id']==filters['theme_id'] for t in item['themes']):
            continue
        if filters.get('author') and filters['author'].casefold() not in team.get('author_name','').casefold():
            continue
        if filters.get('organization') and filters['organization'].casefold() not in team.get('organization_name','').casefold():
            continue
        if filters.get('favorite_from') and (paper['favorited_at'] or '')[:10] < filters['favorite_from']:
            continue
        if filters.get('favorite_to') and (paper['favorited_at'] or '')[:10] > filters['favorite_to']:
            continue
        score = item['fulltext_evaluation']['result'].get('score')
        score = float(score) if isinstance(score,(int,float)) and not isinstance(score,bool) else -1
        if filters.get('min_score') is not None and score < filters['min_score']:
            continue
        candidates.append({'id':paper['id'],'title':paper['title'],'arxiv_id':paper['arxiv_id'],'score':score,
                           'favorited_at':paper['favorited_at'],'directions':item['directions'],'themes':item['themes'],
                           'team':team,'preselected':source_matches(item,source)})
    sort = filters.get('sort','favorite_desc')
    if sort=='title':
        candidates.sort(key=lambda p:(p['title'].casefold(),p['id']))
    elif sort=='score_desc':
        candidates.sort(key=lambda p:(-p['score'],p['id']))
    else:
        candidates.sort(key=lambda p:(p['favorited_at'] or '',p['id']),reverse=sort!='favorite_asc')
    return candidates,counts


def list_series(include_archived=False):
    with db.connect() as conn:
        return [dict(row) for row in conn.execute('''SELECT s.*,v.id AS latest_success_id,v.version_no AS latest_success_no,
            identity.source_name_snapshot AS source_name,
            (SELECT COUNT(*) FROM investment_memo_version_papers p WHERE p.memo_version_id=v.id) AS paper_count
            FROM investment_memo_series s LEFT JOIN investment_memo_versions v ON v.id=(
                SELECT id FROM investment_memo_versions WHERE series_id=s.id AND status='success' ORDER BY version_no DESC LIMIT 1)
            LEFT JOIN investment_memo_versions identity ON identity.id=(
                SELECT id FROM investment_memo_versions WHERE series_id=s.id ORDER BY version_no ASC LIMIT 1)
            ''' + (" WHERE s.status='active'" if not include_archived else '') + ' ORDER BY s.updated_at DESC,s.id DESC')]


def get_series(series_id):
    with db.connect() as conn:
        series = series_row(conn,series_id)
        versions = [dict(row) for row in conn.execute('''SELECT id,version_no,status,model,created_at,finished_at,job_id,error_code,
            (SELECT COUNT(*) FROM investment_memo_version_papers p WHERE p.memo_version_id=v.id) AS paper_count
            FROM investment_memo_versions v WHERE series_id=? ORDER BY version_no DESC''',(series_id,))]
    return series,versions


def get_version(series_id,version_id):
    with db.connect() as conn:
        conn.execute('BEGIN')
        series = series_row(conn,series_id)
        version = version_row(conn,series_id,version_id)
        papers = [dict(r) for r in conn.execute('SELECT * FROM investment_memo_version_papers WHERE memo_version_id=? ORDER BY display_order',(version_id,))]
    version['input_snapshot'] = json.loads(version['input_snapshot_json'])
    version['profile_snapshot'] = json.loads(version['profile_snapshot_json'])
    version['result'] = db.loads_json_object(version['result_json'])
    for paper in papers:
        paper['snapshot'] = json.loads(paper['paper_snapshot_json'])
    return series,version,papers


def save_personal_judgment(series_id,version_id,text):
    if not isinstance(text,str) or '\x00' in text:
        raise FormValidationError({'personal_judgment_markdown':'请填写有效 Markdown 文本'})
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        version_row(conn,series_id,version_id)
        now = db.now_iso()
        conn.execute('''UPDATE investment_memo_versions SET personal_judgment_markdown=?,personal_judgment_updated_at=? WHERE id=?''',
                     (text,now,version_id))
        conn.execute('UPDATE investment_memo_series SET updated_at=? WHERE id=?',(now,series_id))


def archive_series(series_id):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        series = series_row(conn,series_id)
        if series['status']=='active':
            conn.execute("UPDATE investment_memo_series SET status='archived',updated_at=? WHERE id=?",(db.now_iso(),series_id))


def start_generation(version_id,job_id):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT v.* FROM investment_memo_versions v JOIN jobs j ON j.id=v.job_id
            WHERE v.id=? AND v.job_id=? AND v.status='pending' AND j.status='pending' ''',(version_id,job_id)).fetchone()
        if not row:
            return None
        now = db.now_iso()
        conn.execute("UPDATE investment_memo_versions SET status='running',started_at=? WHERE id=?",(now,version_id))
        conn.execute("UPDATE jobs SET status='running',started_at=?,progress_current=0,progress_total=1,progress_message='备忘录生成中；仅一次模型调用' WHERE id=?",(now,job_id))
        db._insert_job_event(conn,db._normalize_job_event(job_id,f'memo:{version_id}:started','investment_memo',
            'investment_memo.generation_started',metrics={'version_id':version_id,'prompt_id':row['prompt_id'],'profile_id':row['profile_id'],'model':row['model']}))
        return dict(row)


def mark_provider_started(version_id):
    with db.connect() as conn:
        changed = conn.execute("UPDATE investment_memo_versions SET provider_started=1 WHERE id=? AND status='running' AND provider_started=0",(version_id,)).rowcount
        if changed!=1:
            raise MemoConflictError('版本不处于可调用状态；不会重复请求模型')


def finish_generation(version_id,*,result=None,markdown=None,raw_output=None,error_code=None,error_message=None,usage=None,duration_ms=0):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        version = conn.execute('SELECT * FROM investment_memo_versions WHERE id=?',(version_id,)).fetchone()
        if not version or version['status']!='running':
            raise MemoConflictError('版本已结束或不存在，不能覆盖 AI 结果')
        status = 'success' if result is not None else 'failed'
        now = db.now_iso()
        usage = usage if isinstance(usage,dict) else {}
        tokens = {}
        for key,alias in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens')]:
            value = usage.get(key,usage.get(alias))
            tokens[key] = max(0,int(value)) if isinstance(value,(int,float)) and not isinstance(value,bool) else None
        conn.execute('''UPDATE investment_memo_versions SET status=?,result_json=?,rendered_markdown=?,raw_output=?,
            input_tokens=?,output_tokens=?,error_code=?,error_message=?,finished_at=? WHERE id=?''',
            (status,json.dumps(result,ensure_ascii=False) if result is not None else None,markdown if result is not None else None,
             raw_output,tokens['input_tokens'],tokens['output_tokens'],error_code,error_message,now,version_id))
        snapshot = json.loads(version['input_snapshot_json'])
        metrics = {'version_id':version_id,'series_id':version['series_id'],'source_mode':version['source_mode_snapshot'],
                   'paper_count':len(snapshot['papers']),'prompt_id':version['prompt_id'],'profile_id':version['profile_id'],
                   'model':version['model'],'status':status,'duration_ms':duration_ms,**tokens}
        conn.execute('''UPDATE jobs SET status=?,finished_at=?,error_message=?,progress_current=1,progress_total=1,
            progress_message=?,progress_details_json=? WHERE id=?''',
            (status,now,error_message,'备忘录已生成' if status=='success' else '备忘录生成失败，未自动重试',
             json.dumps({'phase':'investment_memo',**metrics},ensure_ascii=False),version['job_id']))
        conn.execute('UPDATE investment_memo_series SET updated_at=? WHERE id=?',(now,version['series_id']))
        db._insert_job_event(conn,db._normalize_job_event(version['job_id'],f'memo:{version_id}:finished','investment_memo',
            'investment_memo.generation_succeeded' if status=='success' else 'investment_memo.generation_failed',
            level='info' if status=='success' else 'error',metrics=metrics,error_code=error_code,message=error_message or '备忘录生成完成'))
    return metrics


def recover_versions(conn,job_ids=None):
    sql = '''SELECT v.*,j.status AS job_status FROM investment_memo_versions v LEFT JOIN jobs j ON j.id=v.job_id
             WHERE (v.status IN ('pending','running') OR j.status IN ('pending','running'))'''
    params = []
    interrupted = 0
    if job_ids is not None:
        if not job_ids:
            return 0
        sql += f" AND v.job_id IN ({','.join('?' for _ in job_ids)})"
        params = list(job_ids)
    for version in conn.execute(sql,params).fetchall():
        now = db.now_iso()
        if version['status'] in {'success','failed','interrupted'}:
            # The version is authoritative if a legacy/inconsistent job lagged behind.
            conn.execute('UPDATE jobs SET status=?,finished_at=COALESCE(?,?),error_message=? WHERE id=?',
                         (version['status'],version['finished_at'],now,version['error_message'],version['job_id']))
            continue
        code = 'external_outcome_unknown' if version['provider_started'] else 'memo_interrupted'
        interrupted += int(version['job_status'] in {'pending','running'})
        message = '调用结果未知，可能已产生一次费用；请重新确认后创建新版本。' if version['provider_started'] else '服务中断，未自动续跑；请重新确认并创建新版本。'
        conn.execute("UPDATE investment_memo_versions SET status='interrupted',error_code=?,error_message=?,finished_at=? WHERE id=?",(code,message,now,version['id']))
        conn.execute("UPDATE jobs SET status='interrupted',error_message=?,finished_at=? WHERE id=?",(message,now,version['job_id']))
        if version['job_id']:
            db._insert_job_event(conn,db._normalize_job_event(version['job_id'],f"memo:{version['id']}:interrupted",'investment_memo',
                'investment_memo.generation_failed',level='warning',error_code=code,message=message,
                metrics={'version_id':version['id'],'status':'interrupted'}))
    return interrupted
