"""Safe, read-only view models for task cards, events and diagnostic exports."""
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db

LOCAL_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
STAGES = {'plan': '计划', 'crawl_http': '抓取', 'crawl_parse': '完整性检查', 'persist': '入库',
          'direction_backfill': '历史补分类', 'classification': '关注方向分类',
          'abstract_plan': '候选选择', 'abstract_eval': '摘要评估', 'investment_memo':'备忘录生成', 'finalize': '完成'}
SOURCES = {'scheduled': '定时运行', 'manual_latest': '手动最新', 'manual_catch_up': '手动补抓'}
STATUS_LABELS = {'pending': '排队中', 'running': '运行中', 'success': '已完成',
                 'partial_success': '部分完成', 'failed': '失败', 'interrupted': '已中断',
                 'warning': '警告', 'empty_success': '合法空结果'}
EVENT_LABELS = {
    'investment_memo.version_created':'备忘录版本已创建','investment_memo.generation_started':'备忘录生成开始',
    'investment_memo.generation_succeeded':'备忘录生成成功','investment_memo.generation_failed':'备忘录生成失败／中断',
    'classification.plan_created':'分类计划已创建','classification.skipped_no_active_directions':'没有启用方向，合法跳过分类',
    'classification.paper_started':'论文分类开始','classification.paper_succeeded':'论文分类成功',
    'classification.paper_failed':'论文分类失败','classification.paper_skipped_existing':'论文分类跳过',
    'classification.paper_retrying':'论文分类重试','classification.stage_completed':'分类阶段完成',
    'direction_backfill.started':'历史补分类开始','direction_backfill.previewed':'补分类范围预览',
    'direction_backfill.completed':'历史补分类完成',
    'pipeline.plan_created': '计划已创建', 'pipeline.started': '流水线开始',
    'pipeline.completed': '流水线结束', 'pipeline.interrupted': '服务中断',
    'crawl.category_started': '类目开始', 'crawl.http_retrying': 'HTTP 重试',
    'crawl.http_succeeded': 'HTTP 成功', 'crawl.http_failed': 'HTTP 失败',
    'crawl.parse_completed': '解析完成', 'crawl.parse_anomaly': '完整性异常',
    'crawl.persist_completed': '入库完成', 'crawl.category_completed': '类目终态',
    'abstract.plan_created': '摘要候选已生成', 'abstract.paper_started': '论文评估开始',
    'abstract.paper_retrying': '论文评估重试', 'abstract.paper_succeeded': '论文评估成功',
    'abstract.paper_failed': '论文评估失败', 'abstract.paper_skipped': '论文评估跳过',
    'abstract.stage_completed': '摘要阶段完成',
}
ERROR_LABELS = {
    'memo_config_invalid':'备忘录配置不可用','memo_context_exceeded':'备忘录上下文超限','memo_auth_error':'模型认证失败',
    'memo_transport_error':'模型连接失败','memo_provider_error':'模型服务错误','memo_json_invalid':'备忘录 JSON 无效',
    'memo_schema_invalid':'备忘录结构不符合契约','memo_evidence_invalid':'论文引用编号不合法',
    'memo_database_error':'备忘录数据库保存失败','memo_generation_error':'备忘录生成失败','memo_interrupted':'备忘录生成中断',
    'invalid_classification_result':'分类输出不符合契约', 'input_incomplete':'论文输入不完整',
    'previous_failure':'此前摘要评估失败，请显式重试或手动评估',
    'network_timeout': '网络超时', 'network_http_error': 'HTTP 请求失败',
    'unexpected_redirect': '重定向异常', 'page_date_mismatch': '页面日期不符',
    'page_date_unknown': '无法确认页面日期', 'declared_total_unknown': '无法解析声明总数',
    'declared_total_mismatch': '声明总数不符', 'parse_zero_with_nonzero_total': '有论文但解析为零',
    'parse_zero_without_total': '总数未知且解析为零', 'parse_incomplete': '解析不完整',
    'missing_arxiv_id': '缺少 arXiv ID', 'missing_critical_fields': '缺少关键字段',
    'database_write_failed': '数据库写入失败', 'evaluation_config_missing': '评估配置缺失或连接已变更',
    'evaluation_already_running': '已有摘要评估排队或运行', 'provider_retryable_error': '模型调用失败（可重试）',
    'provider_terminal_error': '模型调用失败（需检查配置）', 'invalid_llm_result': '模型结果格式不合法',
    'pipeline_interrupted': '服务中断', 'external_outcome_unknown': '外部调用结果未知，重试可能产生额外费用',
    'pipeline_system_error': '流水线系统错误',
}
METRIC_LABELS = {
    'version_id':'备忘录版本 ID','paper_count':'确认论文数','estimated_input_tokens':'预计输入 token',
    'direction_count':'方向数','calls':'模型调用','call_success':'有效调用','call_failed':'失败调用',
    'matched':'明确匹配','possible':'可能匹配（待确认）','unmatched':'不匹配',
    'already_classified':'已有分类或历史论文','classification_already_running':'正在分类',
    'input_tokens':'输入 token','output_tokens':'输出 token','previous_failure':'历史失败待手动重试',
    'abstract_new':'新增摘要评估','abstract_reused':'复用摘要评估','abstract_failed':'后续摘要失败',
    'failed_papers':'失败分类论文数',
    'declared_total': '页面声明总数', 'top_n': '配置 Top N', 'expected_count': '预期条数',
    'parsed_count': '解析条数', 'valid_arxiv_count': '有效 ID', 'persisted_count': '入库条数',
    'new_count': '新增', 'updated_count': '更新', 'duplicate_count': '重复', 'failed_count': '写入失败',
    'missing_arxiv_id': '缺 ID', 'missing_title': '缺标题', 'missing_abstract': '缺摘要',
    'missing_authors': '缺作者', 'missing_published_at': '缺发布时间',
    'http_status': 'HTTP 状态', 'response_ms': '响应毫秒', 'response_bytes': '响应字节',
    'retry_count': '重试次数', 'total_ms': '单元毫秒', 'duration_ms': '耗时毫秒',
    'candidate_count': '候选条数', 'unique_count': '去重篇数', 'success': '成功', 'failed': '失败',
    'already_successful': '已有成功评估', 'evaluation_already_running': '正在评估',
    'input_incomplete': '输入不完整', 'terminal_failed': '终止性失败',
    'prompt_id': 'Prompt ID', 'prompt_version': 'Prompt 版本', 'profile_id': 'Profile ID',
    'evaluation_id': '评估 ID', 'reused_from_job_id': '复用自任务',
}


def redact_text(value: Any) -> str:
    """Defense in depth for legacy free-text displays; diagnostics use an allowlist."""
    text = str(value or '')
    text = re.sub(r'https?://[^\s<>"\']+', '[URL 已隐藏]', text, flags=re.I)
    text = re.sub(r'(?im)\b(authorization|cookie|set-cookie|custom_headers)\b[^\r\n]*', r'\1: [已隐藏]', text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+[^\s,;]+', '[认证已隐藏]', text)
    text = re.sub(r'(?i)(\b(?:api[_-]?key|token|secret|password|encrypted_api_key_ref)\b[\s"\']*[:=][\s"\']*)[^\s,;"\'}]+', r'\1[已隐藏]', text)
    return text


def local_time(value: Any) -> str:
    parsed = _time(value)
    return parsed.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S') if parsed else ''


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)
    except (ValueError, TypeError):
        return None


def _number(value: Any, default: int | None = 0) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return max(0, int(value))
        except (ValueError, OverflowError):
            pass
    return default


def _date(value: Any) -> str:
    return str(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value or '')) else ''


def _category(value: Any) -> str:
    return str(value) if re.fullmatch(r'[A-Za-z][A-Za-z.-]{0,30}', str(value or '')) else ''


def pipeline_cards(jobs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    pipelines = [job for job in jobs if job.get('type') in {db.DAILY_PIPELINE_JOB_TYPE,'direction_backfill'}]
    facts = db.job_observability_batch(job['id'] for job in pipelines)
    return {job['id']: pipeline_card(job, facts.get(job['id'], {})) for job in pipelines}


def pipeline_card(job: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    plan, retained = facts.get('plan', {}), facts.get('retained', {})
    groups = facts.get('groups', [])
    is_backfill = job.get('type') == 'direction_backfill'
    history_complete = any(group['event_type'] == ('direction_backfill.previewed' if is_backfill else 'pipeline.plan_created') for group in groups)
    crawl = {key: 0 for key in ('success', 'warning', 'failed', 'empty_success')}
    counts = {key: 0 for key in ('parsed_count', 'persisted_count', 'new_count', 'updated_count', 'duplicate_count', 'failed_count')}
    abstract = {key: 0 for key in ('candidate_count', 'unique_count', 'success', 'failed', 'already_successful', 'evaluation_already_running', 'input_incomplete', 'retry_count', 'terminal_failed','previous_failure')}
    for group in groups:
        kind, outcome = group['event_type'], group['outcome']
        if kind == 'crawl.category_completed':
            if outcome in crawl:
                crawl[outcome] += _number(group['count'])
            for key in counts:
                counts[key] += _number(group.get(key))
        elif kind == 'abstract.plan_created':
            abstract['candidate_count'] = _number(group['candidate_count'])
            abstract['unique_count'] = _number(group['unique_count'])
        elif kind in {'abstract.paper_succeeded', 'abstract.paper_failed', 'abstract.paper_skipped'}:
            key = {'abstract.paper_succeeded': 'success', 'abstract.paper_failed': 'failed'}.get(kind, outcome)
            if key in abstract:
                abstract[key] += _number(group['count'])
            abstract['retry_count'] += _number(group['retry_count'])
            abstract['terminal_failed'] += _number(group['terminal_failed'])
    active = job['status'] in db.JOB_ACTIVE_STATUSES
    if not active and retained.get('phase') == 'finalize':
        crawl.update({key: _number(retained.get('crawl', {}).get(key), crawl[key]) for key in crawl})
        abstract.update({key: _number(retained.get('abstract', {}).get(key), abstract[key]) for key in abstract})
        counts = {key: _number(retained.get('crawl_metrics', {}).get(key), counts[key] if history_complete else None) for key in counts}
    event_count = sum(_number(group['count']) for group in groups)
    if not active and not event_count and not retained.get('crawl_metrics'):
        counts = {key: None for key in counts}
    last = facts.get('last') or {}
    stage = last.get('stage') or 'plan'
    if not active:
        stage = 'finalize'
    categories = [_category(item.get('category')) for item in plan.get('categories', [])]
    dates = [_date(day) for day in plan.get('dates', [])]
    planned = len(dates) * len(categories)
    skipped = sum(abstract[key] for key in ('already_successful', 'evaluation_already_running', 'input_incomplete','previous_failure'))
    completed = abstract['success'] + abstract['failed'] + skipped
    start = _time(job.get('started_at') or job.get('created_at'))
    end = datetime.now(timezone.utc) if active else _time(job.get('finished_at'))
    duration = max(0, int((end-start).total_seconds())) if start and end else _number(retained.get('duration_ms'), 0)//1000
    messages = {
        'success': '今日情报已更新',
        'partial_success': f"已完成，但存在 {crawl['failed']} 个类目或 {abstract['failed']} 篇论文失败；{crawl['warning']} 个类目警告",
        'failed': '任务失败，请查看下方错误和配置提示', 'interrupted': '任务已中断；未自动续跑',
        'pending': '已创建计划，等待执行', 'running': f"正在{STAGES.get(stage, '处理')}",
    }
    classification_raw = retained.get('classification') or facts.get('classification') or {}
    classification = {key:_number(classification_raw.get(key)) for key in
        ('direction_count','candidate_count','success','failed','failed_papers','matched','possible','unmatched','input_incomplete',
         'calls','call_success','call_failed','retry_count','already_classified','input_tokens','output_tokens')}
    classification['has_partial_failure'] = bool(classification_raw.get('has_partial_failure'))
    classification['no_directions'] = not plan.get('directions')
    classification['available'] = bool(classification_raw) or 'directions' in plan
    if job['status'] == 'partial_success' and (classification['failed'] or classification['has_partial_failure']):
        messages['partial_success'] += '；关注方向存在分类失败'
    return {
        'classification':classification,
        'is_backfill':is_backfill,
        'direction_id':_number((plan.get('directions') or [{}])[0].get('id')),
        'start_date':_date(plan.get('start_date')), 'end_date':_date(plan.get('end_date')),
        'source': '手动历史补分类' if is_backfill else SOURCES.get(plan.get('trigger_source'), '历史任务'),
        'date_range': f"{_date(plan.get('start_date'))} → {_date(plan.get('end_date'))}",
        'stage': stage, 'stage_label': STAGES.get(stage, '处理'), 'message': messages.get(job['status'], '任务记录'),
        'duration': f'{duration//60} 分 {duration%60} 秒', 'crawl': crawl, 'counts': counts,
        'abstract': abstract, 'skipped': skipped, 'pending_abstracts': max(0, abstract['unique_count']-completed),
        'planned_units': planned, 'completed_units': sum(crawl.values()), 'event_count': event_count,
        'events_unavailable': not active and not event_count, 'categories': [value for value in categories if value],
        'retry_history_unavailable': not active and not history_complete,
        'dates': [value for value in dates if value], 'retry_of_job_id': job.get('retry_of_job_id'),
        'can_retry': not active and history_complete and bool(dates and categories),
        'summary_lines': [f"类目：成功 {crawl['success']} · 警告 {crawl['warning']} · 失败 {crawl['failed']} · 合法空 {crawl['empty_success']}",
                          f"解析 {counts['parsed_count'] if counts['parsed_count'] is not None else '—'} · 入库 {counts['persisted_count'] if counts['persisted_count'] is not None else '—'} 条",
                          f"摘要：待评估 {max(0, abstract['unique_count']-completed)} · 成功 {abstract['success']} · 失败 {abstract['failed']} · 跳过 {skipped}"],
    }


def event_view(event: dict[str, Any]) -> dict[str, Any]:
    metrics = event.get('metrics') or {}
    safe_metrics = {key: _number(metrics[key], None) for key in METRIC_LABELS if key in metrics}
    if isinstance(metrics.get('terminal_failure'), bool):
        safe_metrics['terminal_failed'] = int(metrics['terminal_failure'])
    for key in ('request_url', 'final_url'):
        if key in metrics:
            try:
                url = urlsplit(str(metrics[key]))
                if url.hostname and (url.hostname == 'papers.cool' or url.hostname.endswith('.papers.cool')):
                    query = [(name, value) for name, value in parse_qsl(url.query)
                             if (name in {'show', 'sort'} and value.isdigit()) or (name == 'date' and _date(value))]
                    path = url.path if re.fullmatch(r'/arxiv/[A-Za-z.-]+', url.path) else '/arxiv/'
                    safe_metrics[key] = urlunsplit(('https', 'papers.cool', path, urlencode(query), ''))
                else:
                    safe_metrics[key] = '非 papers.cool 重定向（URL 已隐藏）'
            except ValueError:
                safe_metrics[key] = 'URL 无效'
    for key in ('target_date', 'page_date', 'crawl_date'):
        if key in metrics:
            safe_metrics[key] = _date(metrics[key]) or '未知'
    if 'final_status' in metrics:
        safe_metrics['final_status'] = STATUS_LABELS.get(metrics['final_status'], '未知')
    if 'skip_reason' in metrics:
        safe_metrics['skip_reason'] = {'already_successful': '已有成功评估', 'evaluation_already_running': '正在评估', 'input_incomplete': '输入不完整'}.get(metrics['skip_reason'], '未知')
    if 'model' in metrics:
        safe_metrics['model'] = redact_text(metrics['model'])[:120]
    usage = metrics.get('usage')
    if isinstance(usage, dict):
        safe_metrics['usage'] = {key: _number(usage[key]) for key in ('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens') if key in usage}
    code = event.get('error_code')
    code = code if code in ERROR_LABELS else ('unknown_error' if code else '')
    arxiv_id = str(event.get('arxiv_id') or '')
    if not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})', arxiv_id):
        arxiv_id = ''
    return {
        'id': event['id'], 'stage': event['stage'] if event['stage'] in STAGES else 'unknown',
        'stage_label': STAGES.get(event['stage'], '其他'), 'type_label': EVENT_LABELS.get(event['event_type'], '历史事件'),
        'level': event['level'] if event['level'] in {'info', 'warning', 'error'} else 'info',
        'level_label': {'info': '正常', 'warning': '警告', 'error': '失败'}.get(event['level'], '正常'),
        'category': _category(event.get('category')), 'crawl_date': _date(event.get('crawl_date')),
        'paper_id': event.get('existing_paper_id'), 'arxiv_id': arxiv_id,
        'paper_title': redact_text(event.get('paper_title'))[:200], 'attempt': _number(event.get('attempt'), 1),
        'created_at': local_time(event.get('created_at')), 'error_code': code,
        'error_label': ERROR_LABELS.get(code, '未知错误' if code else ''), 'metrics': safe_metrics,
        'metric_rows': [{'label': METRIC_LABELS.get(key, {'request_url': '请求 URL', 'final_url': '最终 URL', 'target_date': '目标日期', 'page_date': '页面日期', 'crawl_date': '入库日期', 'final_status': '终态', 'skip_reason': '跳过原因', 'usage': 'Token 用量'}.get(key, key)),
                         'value': value if value is not None else '未知'} for key, value in safe_metrics.items()],
    }
