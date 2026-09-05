import json
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from daily_coolpapers import app as app_module, db, job_views, services
from daily_coolpapers.crawler import CategoryFetchResult
from tests import test_automatic_abstracts as automatic
from tests.test_crawl_observability import _paper


class JobCenterTests(unittest.TestCase):
    fetch = automatic.AutomaticAbstractTests.fetch
    run_pipeline = automatic.AutomaticAbstractTests.run_pipeline

    def setUp(self):
        automatic.AutomaticAbstractTests.setUp(self)
        self.stack.enter_context(patch.object(app_module, 'has_pdf', return_value=False))
        self.stack.enter_context(patch.object(app_module, 'has_markdown', return_value=False))
        self.app = app_module.create_app(runner=self.runner, secret_key='test')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'test'

    def completed(self, partial=False):
        def fetch(category, target, callback, client):
            if partial and category['id'] == self.ids[1]:
                return CategoryFetchResult([], 'failed', ('network_timeout',), {}, ())
            return self.fetch(category, target, callback, client)
        with patch.object(services, '_fetch_category_from_config', side_effect=fetch), patch.object(services, 'call_llm', return_value=self.response):
            return self.run_pipeline()

    def test_detail_and_index_keep_completed_partial_results(self):
        job = self.completed(partial=True)
        for url in ('/', f"/jobs/{job['id']}"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('部分完成', html)
            self.assertIn('存在 1 个类目或 0 篇论文失败', html)
            self.assertIn('手动最新', html)
            self.assertIn('2026-09-03', html)
        html = self.client.get(f"/jobs/{job['id']}").get_data(as_text=True)
        self.assertIn('只补评缺失摘要', html)
        self.assertIn('network_timeout', html)
        self.assertNotIn('今日情报已更新', html)

    def test_default_issues_filter_and_full_timeline(self):
        job = self.completed()
        default = self.client.get(f"/jobs/{job['id']}").get_data(as_text=True)
        self.assertIn('当前筛选没有事件', default)
        all_events = self.client.get(f"/jobs/{job['id']}?severity=all&view=timeline").get_data(as_text=True)
        self.assertIn('摘要候选已生成', all_events)
        self.assertIn('论文评估成功', all_events)
        self.assertIn('/papers/1', all_events)
        self.assertIn('test-model', all_events)

    def test_filters_and_pagination_are_applied_before_limit(self):
        job = self.completed()
        for index in range(56):
            db.append_job_event(job['id'], f'filter:{index}', 'crawl_parse', 'crawl.parse_anomaly',
                level='warning', category='cs.AI', crawl_date='2026-09-03', error_code='parse_incomplete')
        url = f"/api/jobs/{job['id']}/diagnostic?severity=warning&category=cs.AI&stage=crawl_parse&crawl_date=2026-09-03&view=timeline"
        first, second = self.client.get(url).get_json(), self.client.get(url+'&page=2').get_json()
        self.assertEqual((first['events']['total'], first['events']['pages']), (56, 2))
        self.assertEqual((len(first['events']['items']), len(second['events']['items'])), (50, 6))
        self.assertTrue(all(item['category'] == 'cs.AI' for item in second['events']['items']))
        self.assertLess(first['events']['items'][-1]['id'], second['events']['items'][0]['id'])

    def test_bad_filters_and_missing_jobs(self):
        job = self.completed()
        for query in ('severity=critical', 'stage=unknown', 'view=bad', 'category=https://example/?token=SECRET', 'crawl_date=SECRET'):
            self.assertEqual(self.client.get(f"/jobs/{job['id']}?{query}").status_code, 400)
        self.assertEqual(self.client.get('/jobs/99999').status_code, 404)
        self.assertEqual(self.client.get('/api/jobs/99999/diagnostic').status_code, 404)

    def test_diagnostic_does_not_expose_raw_payload_output_or_unknown_metrics(self):
        job = self.completed()
        paper_id = db.list_pipeline_evaluations(job['id'])[0]['paper_id']
        with db.connect() as conn:
            payload = db.get_job(job['id'])['payload_data']
            payload['api_key'] = 'SECRET'
            payload['abstract_config']['prompt']['template'] = 'PRIVATE_PROMPT'
            conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
        db.append_job_event(job['id'], 'sensitive', 'abstract_eval', 'abstract.paper_failed', level='error',
            paper_id=paper_id, arxiv_id='2609.00001', message='Authorization: Bearer SECRET',
            error_code='provider_terminal_error', metrics={
                'raw_output': 'PRIVATE_OUTPUT', 'custom_headers': {'Authorization': 'SECRET'},
                'request_url': 'https://user:SECRET@papers.cool/arxiv/cs.AI?token=SECRET&sort=1',
                'final_url': 'https://other.invalid/SECRET', 'response_bytes': 42,
            })
        for path in (f"/jobs/{job['id']}", f"/api/jobs/{job['id']}/diagnostic", '/api/jobs/progress', '/'):
            text = self.client.get(path).get_data(as_text=True)
            for secret in ('SECRET', 'PRIVATE_PROMPT', 'PRIVATE_OUTPUT'):
                self.assertNotIn(secret, text)
        diagnostic = self.client.get(f"/api/jobs/{job['id']}/diagnostic").get_json()
        self.assertEqual(diagnostic['events']['items'][0]['metrics']['response_bytes'], 42)

    def test_retry_post_links_new_job_and_only_runs_unresolved_units(self):
        job = self.completed(partial=True)
        response = self.client.post(f"/api/jobs/{job['id']}/retry", data={'csrf_token': 'test', 'mode': 'all'})
        new_id = int(response.location.rsplit('/', 1)[1])
        self.assertNotEqual(new_id, job['id'])
        self.assertEqual(db.get_job(new_id)['retry_of_job_id'], job['id'])
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch) as fetch, patch.object(services, 'call_llm', return_value=self.response) as call:
            self.runner._run_job(new_id)
        self.assertEqual(fetch.call_count, 1)
        call.assert_not_called()
        self.assertEqual(db.get_job(new_id)['status'], 'success')
        self.assertEqual(db.get_job(job['id'])['status'], 'partial_success')

    def test_abstract_only_retry_never_fetches_and_preserves_crawl_warning(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', side_effect=services.LLMError('failed')):
            job = self.run_pipeline()
        response = self.client.post(f"/api/jobs/{job['id']}/retry", data={'csrf_token': 'test', 'mode': 'abstract_only'})
        new_id = int(response.location.rsplit('/', 1)[1])
        with patch.object(services, '_fetch_category_from_config') as fetch, patch.object(services, 'call_llm', return_value=self.response) as call:
            self.runner._run_job(new_id)
        fetch.assert_not_called()
        call.assert_called_once()
        self.assertEqual(db.get_job(new_id)['status'], 'success')
        partial = self.completed(partial=True)
        response = self.client.post(f"/api/jobs/{partial['id']}/retry", data={'csrf_token': 'test', 'mode': 'abstract_only'})
        new_id = int(response.location.rsplit('/', 1)[1])
        with patch.object(services, '_fetch_category_from_config') as fetch:
            self.runner._run_job(new_id)
        fetch.assert_not_called()
        self.assertEqual(db.get_job(new_id)['status'], 'partial_success')

    def test_retry_authorization_validation_and_duplicate_click(self):
        job = self.completed(partial=True)
        url = f"/api/jobs/{job['id']}/retry"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, data={'mode': 'all'}).status_code, 403)
        self.assertEqual(self.client.post(url, data={'csrf_token': 'test'}, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(self.client.post(url, data={'csrf_token': 'test', 'mode': 'wrong'}).status_code, 400)
        first = self.client.post(url, data={'csrf_token': 'test'})
        second = self.client.post(url, data={'csrf_token': 'test'})
        self.assertEqual(first.location, second.location)
        active_id = int(first.location.rsplit('/', 1)[1])
        self.assertEqual(self.client.post(f'/api/jobs/{active_id}/retry', data={'csrf_token': 'test'}).status_code, 409)

    def test_polling_and_detail_reads_do_not_write(self):
        job = self.completed()
        statements = []
        original = db.connect
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, 'connect', side_effect=connect), patch.object(self.runner, 'reconcile_orphaned_pending_jobs') as reconcile:
            for url in ('/api/jobs/progress', f"/jobs/{job['id']}", f"/api/jobs/{job['id']}/diagnostic"):
                self.assertEqual(self.client.get(url).status_code, 200)
        reconcile.assert_not_called()
        self.assertFalse([sql for sql in statements if sql.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'BEGIN IMMEDIATE'))])

    def test_card_queries_do_not_grow_per_paper_or_per_job(self):
        first = self.completed()
        for _ in range(8):
            self.completed()
        statements = []
        original = db.connect
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, 'connect', side_effect=connect):
            cards = job_views.pipeline_cards(db.list_jobs(20))
        self.assertEqual(len(cards), 9)
        self.assertLessEqual(sum(sql.lstrip().upper().startswith('SELECT ') for sql in statements), 4)
        self.assertEqual(cards[first['id']]['abstract']['success'], 1)

    def test_retained_summary_survives_event_cleanup_and_retry_is_disabled(self):
        job = self.completed()
        db.delete_expired_job_events(0, current_time=datetime.now(timezone.utc)+timedelta(days=1))
        response = self.client.get(f"/jobs/{job['id']}")
        text = response.get_data(as_text=True)
        self.assertIn('明细事件已清理', text)
        self.assertNotIn('name="mode"', text)
        card = job_views.pipeline_cards([db.get_job(job['id'])])[job['id']]
        self.assertEqual(card['counts']['persisted_count'], 2)
        self.assertEqual(card['abstract']['success'], 1)
        count = len(db.list_jobs())
        self.client.post(f"/api/jobs/{job['id']}/retry", data={'csrf_token': 'test'})
        self.assertEqual(len(db.list_jobs()), count)

    def test_utc_events_display_in_shanghai_and_unknown_fields_are_safe(self):
        event = {'id': 1, 'stage': 'plan', 'event_type': 'pipeline.started', 'level': 'info',
                 'created_at': '2026-09-03T02:30:00+00:00', 'metrics': {'raw_output': 'SECRET'}}
        view = job_views.event_view(event)
        self.assertEqual(view['created_at'], '2026-09-03 10:30:00')
        self.assertNotIn('SECRET', json.dumps(view))

    def test_partially_expired_events_keep_summary_but_cannot_retry(self):
        job = self.completed()
        with db.connect() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id=? AND event_type IN ('pipeline.plan_created','crawl.category_completed')", (job['id'],))
        card = job_views.pipeline_cards([db.get_job(job['id'])])[job['id']]
        self.assertEqual(card['counts']['persisted_count'], 2)
        self.assertTrue(card['retry_history_unavailable'])
        self.assertFalse(card['can_retry'])
        self.assertGreater(card['event_count'], 0)
        html = self.client.get(f"/jobs/{job['id']}").get_data(as_text=True)
        self.assertIn('部分明细事件已清理', html)
        self.assertNotIn('name="mode"', html)
        with self.assertRaises(ValueError):
            self.runner.enqueue_pipeline('manual_latest', retry_of_job_id=job['id'])

    def test_expiry_after_enqueue_stops_retry_before_external_calls(self):
        job = self.completed()
        retry_id, _ = self.runner.enqueue_pipeline('manual_latest', retry_of_job_id=job['id'])
        with db.connect() as conn:
            conn.execute('DELETE FROM job_events WHERE job_id=?', (job['id'],))
        with patch.object(services, '_fetch_category_from_config') as fetch, patch.object(services, 'call_llm') as call:
            self.runner._run_job(retry_id)
        fetch.assert_not_called()
        call.assert_not_called()
        self.assertEqual(db.get_job(retry_id)['status'], 'failed')

    def test_settings_validate_then_atomically_save_new_fields(self):
        before = db.get_setting('job_events.retention_days')
        bad = self.client.post('/api/settings', data={'csrf_token': 'test', 'event_retention_days': '0', 'abstract_retries': '3'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(db.get_setting('job_events.retention_days'), before)
        good = self.client.post('/api/settings', data={'csrf_token': 'test', 'event_retention_days': '14', 'abstract_retries': '3', 'missing_field_warning_rate': '0.25'})
        self.assertEqual(good.status_code, 302)
        self.assertEqual(db.get_setting('job_events.retention_days'), 14)
        self.assertEqual(db.get_setting('llm.abstract_retries'), 3)
        self.assertEqual(db.get_setting('crawler.missing_field_warning_rate'), 0.25)

    def test_legacy_job_detail_and_progress_remain_usable(self):
        job_id = db.create_job('abstract_eval', {})
        db.update_job_progress(job_id, 0, 1, 'Authorization: Bearer SECRET')
        html = self.client.get(f'/jobs/{job_id}').get_data(as_text=True)
        self.assertIn('摘要评估', html)
        self.assertNotIn('SECRET', html)
        self.assertNotIn('name="mode"', html)

    def test_progress_counts_match_persisted_database_during_running_job(self):
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.update_job(job_id, 'running')
        plan = db.get_job(job_id)['payload_data']
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch):
            services.crawl_all_categories(crawl_date='2026-09-03', pipeline_job_id=job_id, category_snapshot=plan['categories'])
        data = self.client.get('/api/jobs/progress').get_json()['jobs'][0]
        self.assertEqual(data['pipeline']['completed_units'], 2)
        self.assertEqual(data['pipeline']['counts']['persisted_count'], 2)
        self.assertEqual(data['pipeline']['counts']['new_count'], 1)
        self.assertEqual(data['pipeline']['counts']['duplicate_count'], 1)
        self.assertEqual(data['pipeline']['stage_label'], '入库')

    def test_legacy_log_display_is_bounded_and_redacted_without_modifying_file(self):
        raw = b'old line\n' * 40000 + b'Authorization: Bearer SECRET\nhttps://provider.invalid/?token=SECRET\napi_key=SECRET\n'
        with patch.object(app_module, 'CURRENT_LOG') as log_path:
            log_path.exists.return_value = True
            log_path.open.side_effect = lambda *_: io.BytesIO(raw)
            response = self.client.get('/api/logs/current')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertLess(len(response.data), 270000)
        log_path.write_text.assert_not_called()

    def test_running_terminal_failure_counts_match_paper_events(self):
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.update_job(job_id, 'running')
        db.append_job_event(job_id, 'terminal-failed', 'abstract_eval', 'abstract.paper_failed',
                            level='error', error_code='provider_terminal_error',
                            metrics={'status': 'failed', 'terminal_failure': True})
        db.append_job_event(job_id, 'retry-exhausted', 'abstract_eval', 'abstract.paper_failed',
                            level='error', error_code='provider_retryable_error',
                            metrics={'status': 'failed', 'terminal_failure': False, 'retry_count': 2})
        data = self.client.get('/api/jobs/progress').get_json()['jobs'][0]['pipeline']['abstract']
        self.assertEqual((data['failed'], data['terminal_failed'], data['retry_count']), (2, 1, 2))
        events = self.client.get(f'/api/jobs/{job_id}/diagnostic').get_json()['events']['items']
        self.assertEqual([event['metrics']['terminal_failed'] for event in events], [1, 0])

    def test_grouped_order_is_deterministic_by_date_category_stage(self):
        job = self.completed()
        for i, day, category in [(1, '2026-09-03', 'cs.CL'), (2, '2026-09-02', 'cs.AI'), (3, '2026-09-03', 'cs.AI')]:
            db.append_job_event(job['id'], f'ordered:{i}', 'crawl_parse', 'crawl.parse_anomaly',
                                level='warning', category=category, crawl_date=day)
        page = db.list_job_event_page(job['id'], view='grouped')
        self.assertEqual([(event['crawl_date'], event['category']) for event in page['items']],
                         [('2026-09-02', 'cs.AI'), ('2026-09-03', 'cs.AI'), ('2026-09-03', 'cs.CL')])
        for index, stage in enumerate(reversed(job_views.STAGES)):
            db.append_job_event(job['id'], f'stage:{index}', stage, 'crawl.parse_anomaly',
                                level='warning', category='cs.CV', crawl_date='2026-09-03')
        page = db.list_job_event_page(job['id'], category='cs.CV', view='grouped')
        self.assertEqual([event['stage'] for event in page['items']], list(job_views.STAGES))


if __name__ == '__main__':
    unittest.main()
