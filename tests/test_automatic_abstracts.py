import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from daily_coolpapers import db, jobs, llm, services
from daily_coolpapers.crawler import CategoryFetchResult
from daily_coolpapers.llm import LLMError, LLMResponse, LLMResultError
from tests.test_crawl_observability import _paper
from daily_coolpapers.services import _abstract_retry_wait


class AutomaticAbstractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(db, 'DB_PATH', Path(self.tmp.name)/'main.sqlite3'))
        self.stack.enter_context(patch.object(db, 'LLM_PROFILES_DB_PATH', Path(self.tmp.name)/'profiles.sqlite3'))
        self.stack.enter_context(patch.object(db, 'ensure_directories'))
        db.init_db()
        db.init_llm_profiles_db()
        self.profile_id = db.save_llm_profile({
            'name': 'test', 'provider': 'openai_compatible', 'base_url': 'https://example.invalid/v1',
            'model': 'test-model', 'enabled': True, 'is_default_abstract': True,
        })
        self.categories = db.list_categories(True)[:2]
        self.ids = [item['id'] for item in self.categories]
        self.stack.enter_context(patch.object(services, 'latest_available_arxiv_date', return_value='2026-09-03'))
        self.stack.enter_context(patch.object(services, '_crawler_client_from_settings', side_effect=lambda: nullcontext(object())))
        self.stack.enter_context(patch.object(services, 'make_llm_client', side_effect=lambda _profile: nullcontext(object())))
        self.stack.enter_context(patch.object(services, '_abstract_retry_wait'))
        self.runner = jobs.JobRunner()
        self.response = LLMResponse('{}', {'score': 80, 'attention': 'read'}, {'input_tokens': 12, 'output_tokens': 8})

    def fetch(self, category, target_date, _callback, _client):
        return CategoryFetchResult([_paper('2609.00001')], 'success', (),
                                   {'page_date': target_date, 'parsed_count': 1, 'valid_arxiv_count': 1}, ())

    def run_pipeline(self, source='manual_latest', **kwargs):
        job_id, created = self.runner.enqueue_pipeline(source, self.ids, **kwargs)
        self.assertTrue(created)
        self.runner._run_job(job_id)
        return db.get_job(job_id)

    def test_pipeline_dedup_links_evaluation_and_counts(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response) as call:
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'success')
        call.assert_called_once()
        evaluations = db.list_pipeline_evaluations(job['id'])
        self.assertEqual(len(evaluations), 1)
        stage = db.list_job_events(job['id'], event_type='abstract.stage_completed')[0]['metrics']
        self.assertEqual((stage['candidate_count'], stage['unique_count'], stage['success']), (2, 1, 1))
        terminal = db.list_job_events(job['id'], event_type='abstract.paper_succeeded')
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['metrics']['usage']['input_tokens'], 12)
        self.assertNotIn('result', terminal[0]['metrics'])
        self.assertEqual(db.count_job_events(job['id'], event_type='pipeline.completed'), 1)

    def test_partial_crawl_failure_still_evaluates_and_warning_is_partial(self):
        def partial(category, target, callback, client):
            if category['id'] == self.ids[1]:
                return CategoryFetchResult([], 'failed', ('parse_zero_without_total',), {'page_date': target}, ())
            return self.fetch(category, target, callback, client)
        with patch.object(services, '_fetch_category_from_config', side_effect=partial), patch.object(services, 'call_llm', return_value=self.response) as call:
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'partial_success')
        call.assert_called_once()
        def warning(*_args):
            return CategoryFetchResult([_paper('2609.00002')], 'warning', ('declared_total_unknown',), {}, ())
        with patch.object(services, '_fetch_category_from_config', side_effect=warning), patch.object(services, 'call_llm', return_value=self.response):
            self.assertEqual(self.run_pipeline()['status'], 'partial_success')

    def test_legal_empty_and_all_failed(self):
        for status, expected in [('empty_success', 'success'), ('failed', 'failed')]:
            with patch.object(services, '_fetch_category_from_config', return_value=CategoryFetchResult([], status, (), {}, ())), patch.object(services, 'call_llm') as call:
                job = self.run_pipeline()
            self.assertEqual(job['status'], expected)
            call.assert_not_called()

    def test_existing_success_is_skipped_even_when_metadata_changes(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response) as call:
            self.run_pipeline()
            second = self.run_pipeline()
        self.assertEqual(call.call_count, 1)
        event = db.list_job_events(second['id'], event_type='abstract.paper_skipped')[0]
        self.assertEqual(event['metrics']['skip_reason'], 'already_successful')

    def test_queued_abstract_and_incomplete_input_are_skipped(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        db.create_job('abstract_eval', {'paper_id': paper_id})
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm') as call:
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'success')
        call.assert_not_called()
        self.assertEqual(db.list_job_events(job['id'], event_type='abstract.paper_skipped')[0]['error_code'], 'evaluation_already_running')
        incomplete = {**_paper('2609.00002'), 'abstract': '  '}
        other = db.upsert_papers([incomplete], 'cs.AI', '2026-09-03')[0]
        self.assertEqual(services.evaluate_abstract_candidate(other)['skip_reason'], 'input_incomplete')

    def test_retryable_errors_retry_but_terminal_and_invalid_results_do_not(self):
        for i, (error, expected_calls, code) in enumerate([
            (LLMError('SECRET', retryable=True), 3, 'provider_retryable_error'),
            (LLMError('SECRET', retryable=False), 1, 'provider_terminal_error'),
            (LLMResultError('invalid_schema', 'SECRET'), 1, 'invalid_llm_result'),
        ]):
            paper_id = db.upsert_papers([_paper(f'2609.{i+100:05}')], 'cs.AI', '2026-09-03')[0]
            job_id = db.create_job('daily_pipeline', {})
            with patch.object(services, 'call_llm', side_effect=error) as call:
                outcome = services.evaluate_abstract_candidate(paper_id, pipeline_job_id=job_id, job_id=job_id)
            self.assertEqual(call.call_count, expected_calls)
            self.assertEqual(outcome['status'], 'failed')
            events = db.list_job_events(job_id)
            self.assertEqual(events[-1]['error_code'], code)
            self.assertNotIn('SECRET', json.dumps(events))
            self.assertNotIn('SECRET', json.dumps(db.list_pipeline_evaluations(job_id)))

    def test_retry_then_success_has_one_terminal_and_retains_attempt_records(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', side_effect=[LLMError('retry', retryable=True), self.response]):
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'success')
        self.assertEqual(len(db.list_pipeline_evaluations(job['id'])), 2)
        self.assertEqual(db.count_job_events(job['id'], event_type='abstract.paper_succeeded'), 1)
        self.assertEqual(db.count_job_events(job['id'], event_type='abstract.paper_failed'), 0)

    def test_single_paper_failure_does_not_block_other_papers(self):
        def two(*_args):
            return CategoryFetchResult([_paper('2609.00001', 'FAIL'), _paper('2609.00002', 'PASS')], 'success', (), {}, ())
        def respond(_profile, prompt, **_kwargs):
            if 'FAIL' in prompt:
                raise LLMError('authentication', retryable=False)
            return self.response
        with patch.object(services, '_fetch_category_from_config', side_effect=two), patch.object(services, 'call_llm', side_effect=respond):
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'partial_success')
        summary = db.list_job_events(job['id'], event_type='abstract.stage_completed')[0]['metrics']
        self.assertEqual((summary['success'], summary['failed']), (1, 1))

    def test_admission_lock_is_atomic_and_released_after_failure(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        barrier = Barrier(2)
        def claim():
            barrier.wait(3)
            return db.claim_abstract_evaluation(paper_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(token is not None for token, _ in outcomes), 1)
        token = next(token for token, _ in outcomes if token)
        db.release_evaluation_claim(token)
        with patch.object(services, 'call_llm', side_effect=LLMError('fail')):
            services.evaluate_abstract_candidate(paper_id)
        token, reason = db.claim_abstract_evaluation(paper_id)
        self.assertIsNone(reason)
        db.release_evaluation_claim(token)

    def test_plan_snapshot_survives_prompt_model_and_category_changes(self):
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        plan = db.get_job(job_id)['payload_data']
        prompt_id = plan['abstract_config']['prompt']['id']
        with db.connect() as conn:
            conn.execute("UPDATE prompts SET template='CHANGED', version=999 WHERE id=?", (prompt_id,))
            conn.execute('UPDATE categories SET enabled=0, top_n=999')
        with db.connect_llm_profiles() as conn:
            conn.execute("UPDATE llm_profiles SET model='CHANGED'")
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch) as fetch, patch.object(services, 'call_llm', return_value=self.response) as call:
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'], 'success')
        self.assertEqual(call.call_args.args[0]['model'], 'test-model')
        self.assertNotEqual(call.call_args.args[1], 'CHANGED')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0]['top_n'], 30)
        self.assertNotIn('encrypted_api_key_ref', json.dumps(plan))
        self.assertNotIn('base_url', json.dumps(plan))

    def test_config_missing_is_explicit_and_skipped_successes_need_no_config(self):
        with db.connect_llm_profiles() as conn:
            conn.execute('DELETE FROM llm_profiles')
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm') as call:
            job = self.run_pipeline()
        self.assertEqual(job['status'], 'failed')
        call.assert_not_called()
        self.assertEqual(db.list_job_events(job['id'], event_type='abstract.paper_failed')[0]['error_code'], 'evaluation_config_missing')

    def test_duplicate_pipeline_triggers_are_not_enqueued_twice(self):
        first = self.runner.enqueue_pipeline('manual_latest', self.ids)
        second = self.runner.enqueue_pipeline('manual_catch_up', self.ids)
        self.assertEqual(second, (first[0], False))
        self.assertEqual(self.runner.queue.qsize(), 1)

    def test_manual_catch_up_dates_are_frozen_and_auto_evaluated(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch) as fetch, patch.object(services, 'call_llm', return_value=self.response) as call:
            job = self.run_pipeline('manual_catch_up', start_date='2026-08-28', end_date='2026-09-03')
        self.assertEqual(job['payload_data']['dates'], ['2026-08-28', '2026-08-31', '2026-09-01', '2026-09-02', '2026-09-03'])
        self.assertEqual(fetch.call_count, 10)
        call.assert_called_once()
        self.assertEqual(job['status'], 'success')

    def test_scheduler_enqueues_pipeline_and_restart_does_not_repeat_slot(self):
        now = datetime(2026, 9, 3, 10, 30, tzinfo=services.SHANGHAI_TZ)
        with patch.object(jobs, 'datetime') as clock, patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response) as call:
            clock.now.return_value = now
            self.runner._maybe_schedule_daily_work()
            job_id = self.runner.queue.get_nowait()
            self.runner._run_job(job_id)
            restarted = jobs.JobRunner()
            restarted._maybe_schedule_daily_work()
        job = db.get_job(job_id)
        self.assertEqual(job['type'], 'daily_pipeline')
        self.assertEqual(job['payload_data']['trigger_source'], 'scheduled')
        self.assertEqual(job['status'], 'success')
        call.assert_called_once()
        self.assertNotIn('daily_pipeline', [db.get_job(i)['type'] for i in list(restarted.queue.queue)])

    def test_retry_skips_successful_units_but_evaluates_their_missing_abstracts(self):
        original_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        plan = db.get_job(original_id)['payload_data']
        db.update_job(original_id, 'running')
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch):
            services.crawl_all_categories(crawl_date='2026-09-03', pipeline_job_id=original_id, category_snapshot=plan['categories'][:1])
        db.mark_unfinished_jobs_interrupted()
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch) as fetch, patch.object(services, 'call_llm', return_value=self.response) as call:
            job = self.run_pipeline(retry_of_job_id=original_id)
        self.assertEqual(fetch.call_count, 1)
        call.assert_called_once()
        self.assertEqual(job['status'], 'success')
        self.assertEqual(job['retry_of_job_id'], original_id)

    def test_interruption_marks_unknown_outcome_and_clears_claim(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.update_job(job_id, 'running')
        token, _ = db.claim_abstract_evaluation(paper_id, pipeline_job_id=job_id, job_id=job_id)
        db.mark_evaluation_provider_started(token)
        db.mark_unfinished_jobs_interrupted()
        self.assertEqual(db.get_job(job_id)['status'], 'interrupted')
        self.assertEqual(db.list_pipeline_evaluations(job_id)[0]['error_code'], 'external_outcome_unknown')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM evaluation_claims').fetchone()[0], 0)
        self.assertEqual(db.count_job_events(job_id, event_type='abstract.paper_failed'), 1)
        self.assertEqual(db.mark_unfinished_jobs_interrupted(), 0)

    def test_usage_is_optional_and_drops_non_numeric_provider_fields(self):
        with patch.object(llm, '_call_openai_compatible', return_value=llm._ProviderText('{}', {'input_tokens': 5, 'secret': 'SECRET', 'details': {'cached_tokens': 2}})):
            response = llm.call_llm({'provider': 'openai_compatible'}, 'prompt')
        self.assertEqual(response.usage, {'input_tokens': 5, 'details': {'cached_tokens': 2}})
        with patch.object(llm, '_call_openai_compatible', return_value='{}'):
            self.assertIsNone(llm.call_llm({'provider': 'openai_compatible'}, 'prompt').usage)

    def test_manual_routes_create_and_execute_the_same_pipeline(self):
        from flask import Flask
        from daily_coolpapers import app as app_module
        app = Flask(__name__)
        app.secret_key = 'test-only'
        app.extensions['daily_coolpapers.job_runner'] = self.runner
        app.extensions['daily_coolpapers.secret_store'] = object()
        # No runtime initialization; routes and CSRF are exercised against the temp DB only.
        with patch.object(app_module, 'job_runner', self.runner):
            app_module.register_request_security(app)
            app_module.register_routes(app)
        client = app.test_client()
        with client.session_transaction() as session:
            session['_csrf_token'] = 'test-token'
        for path, source in [('/api/crawl/run', 'manual_latest'), ('/api/crawl/catch-up', 'manual_catch_up')]:
            form = {'csrf_token': 'test-token', 'category_ids': str(self.ids[0])}
            if source == 'manual_catch_up':
                form.update(start_date='2026-09-02', end_date='2026-09-03')
            with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response):
                response = client.post(path, data=form)
                self.assertEqual(response.status_code, 302)
                job_id = self.runner.queue.get_nowait()
                self.runner._run_job(job_id)
            self.assertEqual(db.get_job(job_id)['payload_data']['trigger_source'], source)
            self.assertEqual(db.get_job(job_id)['status'], 'success')

    def test_interruption_after_result_commit_recovers_success_without_unknown(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.update_job(job_id, 'running')
        token, _ = db.claim_abstract_evaluation(paper_id, pipeline_job_id=job_id, job_id=job_id)
        db.mark_evaluation_provider_started(token)
        evaluation_id = db.create_evaluation(paper_id, 'abstract_review', None, None, None, 'test', 'success', {'score': 80}, '{}', None, pipeline_job_id=job_id)
        db.mark_unfinished_jobs_interrupted()
        self.assertEqual(len(db.list_pipeline_evaluations(job_id)), 1)
        event = db.list_job_events(job_id, event_type='abstract.paper_succeeded')[0]
        self.assertEqual(event['metrics']['evaluation_id'], evaluation_id)
        self.assertIsNone(event['error_code'])

    def test_interruption_closes_not_yet_started_candidates(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.update_job(job_id, 'running')
        db.append_job_event(job_id, 'abstract-plan', 'abstract_plan', 'abstract.plan_created', metrics={'paper_ids': [paper_id]})
        db.mark_unfinished_jobs_interrupted()
        self.assertEqual(db.list_job_events(job_id, event_type='abstract.paper_failed')[0]['error_code'], 'pipeline_interrupted')
        self.assertEqual(db.list_pipeline_evaluations(job_id), [])

    def test_profile_transport_change_is_explicit_configuration_failure(self):
        job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        with db.connect_llm_profiles() as conn:
            conn.execute("UPDATE llm_profiles SET base_url='https://changed.invalid/?token=SECRET'")
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm') as call:
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'], 'failed')
        call.assert_not_called()
        self.assertNotIn('SECRET', json.dumps(db.list_job_events(job_id)))
        self.assertEqual(db.list_job_events(job_id, event_type='abstract.paper_failed')[0]['error_code'], 'evaluation_config_missing')

    def test_no_work_catch_up_succeeds_without_network(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response):
            self.run_pipeline()
        with patch.object(services, '_fetch_category_from_config') as fetch, patch.object(services, 'call_llm') as call:
            job = self.run_pipeline('manual_catch_up')
        self.assertEqual(job['status'], 'success')
        self.assertEqual(job['payload_data']['dates'], [])
        fetch.assert_not_called()
        call.assert_not_called()

    def test_manual_missing_abstract_repair_retries_terminal_failures(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        db.create_evaluation(paper_id, 'abstract_review', None, None, None, 'test', 'failed', None, None, 'old failure', 'provider_failed', False)
        with patch.object(services, 'call_llm', return_value=self.response) as call:
            result = services.evaluate_missing_abstracts()
            second = services.evaluate_missing_abstracts()
        self.assertEqual(result['success'], 1)
        self.assertEqual(second['total'], 0)
        call.assert_called_once()

    def test_plan_rejects_bad_date_range_and_no_enabled_categories(self):
        with self.assertRaises(ValueError):
            services.build_daily_pipeline_plan('manual_catch_up', start_date='2026-09-03')
        with self.assertRaises(ValueError):
            services.build_daily_pipeline_plan('manual_catch_up', start_date='2026-09-04', end_date='2026-09-03')
        with db.connect() as conn:
            conn.execute('UPDATE categories SET enabled=0')
        with self.assertRaises(ValueError):
            services.build_daily_pipeline_plan('manual_latest')

    def test_retry_backoff_is_injectable_and_bounded(self):
        with patch.object(services.time, 'sleep') as sleep, patch.object(services.random, 'random', return_value=0.5):
            _abstract_retry_wait(1)
            _abstract_retry_wait(12)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.5, 30.0])

    def test_plan_uses_shanghai_time_and_skips_known_non_update_dates(self):
        with patch.object(services, 'latest_available_arxiv_date', return_value='2026-09-09') as latest:
            plan = services.build_daily_pipeline_plan('manual_catch_up', self.ids,
                start_date='2026-09-04', end_date='2026-09-09', now=datetime(2026, 9, 9, 2, tzinfo=timezone.utc))
        self.assertEqual(plan['dates'], ['2026-09-04', '2026-09-07', '2026-09-09'])
        self.assertEqual(latest.call_args.args[0].hour, 10)

    def test_disabled_scheduler_does_not_enqueue(self):
        with patch.object(jobs.db, 'get_bool_setting', return_value=False):
            self.runner._maybe_schedule_daily_work()
        self.assertEqual(self.runner.queue.qsize(), 0)

    def test_subset_success_does_not_hide_missing_categories_in_catch_up(self):
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response):
            job_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids[:1])
            self.runner._run_job(job_id)
        plan = services.build_daily_pipeline_plan('manual_catch_up', self.ids)
        self.assertEqual(plan['dates'], ['2026-09-03'])

    def test_retry_preserves_categories_even_if_all_current_categories_are_disabled(self):
        original_id, _ = self.runner.enqueue_pipeline('manual_latest', self.ids)
        db.mark_unfinished_jobs_interrupted()
        with db.connect() as conn:
            conn.execute('UPDATE categories SET enabled=0')
        with patch.object(services, '_fetch_category_from_config', side_effect=self.fetch), patch.object(services, 'call_llm', return_value=self.response):
            job = self.run_pipeline(retry_of_job_id=original_id)
        self.assertEqual(job['status'], 'success')
        self.assertEqual([item['id'] for item in job['payload_data']['categories']], self.ids)

    def test_manual_failure_does_not_persist_provider_url_in_job_or_evaluation(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        job_id = self.runner.enqueue('abstract_eval', {'paper_id': paper_id})
        with patch.object(services, 'call_llm', side_effect=LLMError('https://provider.invalid/?api_key=SECRET')):
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'], 'failed')
        self.assertNotIn('SECRET', json.dumps(db.get_job(job_id)))
        self.assertNotIn('SECRET', json.dumps(db.list_evaluations(paper_id)))

    def test_manual_missing_repair_counts_running_skip_separately_from_success(self):
        paper_id = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        token, _ = db.claim_abstract_evaluation(paper_id)
        try:
            with patch.object(services, 'call_llm') as call:
                result = services.evaluate_missing_abstracts()
            self.assertEqual((result['success'], result['failed'], result['skipped']), (0, 0, 1))
            call.assert_not_called()
        finally:
            db.release_evaluation_claim(token)


if __name__ == '__main__':
    unittest.main()
