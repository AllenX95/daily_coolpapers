import json
from email.utils import formatdate
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from unittest.mock import patch

import httpx

from daily_coolpapers import call_attempts, config, db, jobs, llm, llm_concurrency, services
from tests.test_crawl_observability import _paper
from tests.benchmark_llm_calls import isolated_database


class ProviderLimiterTests(unittest.TestCase):
    def test_default_and_generation_reductions(self):
        limiter = llm_concurrency.ProviderLimiter()
        self.assertEqual(limiter.limit, 10)
        self.assertTrue(limiter.throttled(0))
        self.assertEqual(limiter.limit, 4)
        for _ in range(9):
            self.assertTrue(limiter.throttled(0))
            self.assertEqual(limiter.limit, 4)
        for expected in (3, 2, 1):
            self.assertTrue(limiter.throttled(limiter.generation))
            self.assertEqual(limiter.limit, expected)
        self.assertFalse(limiter.throttled(limiter.generation))
        limiter.configure(10)
        self.assertEqual(limiter.limit, 1)  # New client/stage must not erase throttling.
        limiter.reset_if_idle()
        self.assertEqual(limiter.limit, 10)

    def test_reduction_blocks_new_requests_until_old_requests_drain(self):
        limiter = llm_concurrency.ProviderLimiter()
        entered, allow_finish, newcomer = threading.Barrier(11), threading.Event(), threading.Event()
        def running():
            with limiter.slot():
                entered.wait(timeout=5)
                allow_finish.wait(timeout=5)
        def new_request():
            with limiter.slot():
                newcomer.set()
        with ThreadPoolExecutor(max_workers=11) as executor:
            futures = [executor.submit(running) for _ in range(10)]
            entered.wait(timeout=5)
            limiter.throttled(0)
            future = executor.submit(new_request)
            self.assertFalse(newcomer.wait(.05))
            limiter.reset_if_idle()
            self.assertEqual(limiter.limit, 4)
            allow_finish.set()
            for old in futures:
                old.result(timeout=5)
            future.result(timeout=5)
        self.assertTrue(newcomer.is_set())
        self.assertEqual(limiter.active, 0)

    def test_explicit_concurrency_errors_and_unrelated_limits(self):
        for status, body, expected in ((429, 'too many concurrent requests', True),
                (429, 'concurrency_limit_exceeded', True), (429, '并发数超限', True),
                (503, 'maximum parallel requests exceeded', True),
                (429, 'tokens per minute exceeded', False),
                (429, 'requests per minute exceeded', False),
                (429, 'insufficient_quota', False), (503, 'overloaded', False),
                (200, 'concurrency report', False)):
            with self.subTest(body=body):
                response = httpx.Response(status, json={'error': {'message': body}})
                self.assertEqual(llm_concurrency.is_concurrency_limit(response), expected)

    def test_account_and_provider_isolation(self):
        with patch.object(llm_concurrency, '_registry', {}):
            a = llm_concurrency.provider_limiter('openai_compatible', 'https://a.invalid/v1/chat/completions', {'Authorization':'Bearer A'})
            same = llm_concurrency.provider_limiter('openai_compatible', 'https://a.invalid/v2/chat/completions', {'authorization':'Bearer A'})
            b = llm_concurrency.provider_limiter('openai_compatible', 'https://a.invalid/v1/chat/completions', {'Authorization':'Bearer B'})
            other = llm_concurrency.provider_limiter('anthropic', 'https://a.invalid/v1/messages', {'x-api-key':'A'})
            a.throttled(0)
            self.assertIs(a, same)
            self.assertEqual((a.limit, b.limit, other.limit), (4, 10, 10))
            self.assertNotIn('Bearer A', repr(llm_concurrency._registry))

    def test_retry_after_seconds_dates_and_invalid_values(self):
        now = 1900000000
        cases = [(None,1), ('invalid',1), ('nan',1), ('inf',1), ('-2',1),
                 ('12',12), ('120',30), (formatdate(now+10, usegmt=True),10),
                 (formatdate(now+120, usegmt=True),30), (formatdate(now-10, usegmt=True),1)]
        with patch.object(llm.time, 'time', return_value=now):
            for value, expected in cases:
                with self.subTest(value=value):
                    self.assertEqual(llm._concurrency_retry_delay(value), expected)


class ProviderAdmissionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.isolation = isolated_database()
        self.components = self.isolation.__enter__()
        self.addCleanup(self.isolation.__exit__, None, None, None)
        db.init_llm_profiles_db()
        self.registry_patch = patch.object(llm_concurrency, '_registry', {})
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.profile = {'provider':'openai_compatible', 'base_url':'https://limiter.invalid/v1',
            'model':'test', 'encrypted_api_key_ref':self.components['secret_store'].encrypt('temporary-only')}

    def response(self, request, status=200, message=None):
        payload = {'error': {'message': message}} if status != 200 else {
            'choices':[{'message':{'content':'{"score":80,"attention":"read"}'}}]}
        return httpx.Response(status, json=payload, request=request)

    def test_provider_threshold_four_recovers_and_records_every_physical_attempt(self):
        barrier, lock = threading.Barrier(10), threading.Lock()
        first_entries, active, retry_active = [], 0, []
        counts = {}
        def handler(request):
            nonlocal active
            prompt = json.loads(request.content)['messages'][-1]['content']
            with lock:
                counts[prompt] = counts.get(prompt, 0) + 1
                attempt = counts[prompt]
                active += 1
                seat = active
                if attempt == 1:
                    first_entries.append(seat)
                else:
                    retry_active.append(active)
            try:
                if attempt == 1:
                    barrier.wait(timeout=10)
                sleep(.02)
                return self.response(request, 429, 'Too many concurrent requests') if seat > 4 else self.response(request)
            finally:
                with lock:
                    active -= 1
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            def invoke(index):
                with call_attempts.operation_context('abstract_review'):
                    return llm.call_llm(self.profile, str(index), client=client)
            with patch.object(llm.time, 'sleep', return_value=None):
                with ThreadPoolExecutor(max_workers=10) as executor:
                    responses = list(executor.map(invoke, range(10)))
        self.assertEqual(len(responses), 10)
        self.assertEqual(max(first_entries), 10)
        self.assertLessEqual(max(retry_active), 4)
        limiter = next(iter(llm_concurrency._registry.values()))
        self.assertEqual((limiter.limit, limiter.active), (4, 0))
        with db.connect() as conn:
            rows = conn.execute('SELECT status,retry_reason FROM llm_call_attempts').fetchall()
        self.assertEqual(len(rows), 16)
        self.assertEqual(sum(row['status']=='failed' for row in rows), 6)
        self.assertEqual(sum(row['retry_reason']=='provider_concurrency_limit' for row in rows), 6)

    def test_reductions_reach_one_without_using_business_retries(self):
        limits = []
        def handler(request):
            limiter = next(iter(llm_concurrency._registry.values()))
            limits.append(limiter.limit)
            return self.response(request, 429, 'concurrency limit exceeded') if limiter.limit > 1 else self.response(request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with patch.object(llm.time, 'sleep', return_value=None):
                with call_attempts.operation_context('abstract_review'):
                    result = llm.call_llm(self.profile, 'test', client=client)
        self.assertEqual(limits, [10, 4, 3, 2, 1])
        self.assertEqual(result.result_json['score'], 80)
        with db.connect() as conn:
            rows = conn.execute('SELECT attempt_no,business_attempt_no,status FROM llm_call_attempts ORDER BY id').fetchall()
        self.assertEqual([row['attempt_no'] for row in rows], [1,2,3,4,5])
        self.assertEqual({row['business_attempt_no'] for row in rows}, {1})

    def test_rpm_limit_does_not_reduce_or_add_transport_retry(self):
        calls = []
        def handler(request):
            calls.append(request)
            return self.response(request, 429, 'requests per minute exceeded')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(llm.LLMHTTPError):
                llm.call_llm(self.profile, 'test', client=client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(iter(llm_concurrency._registry.values())).limit, 10)

    def test_database_and_transport_failures_release_admission(self):
        def handler(request):
            raise httpx.ConnectError('synthetic', request=request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(llm.LLMError):
                llm.call_llm(self.profile, 'test', client=client)
            limiter = next(iter(llm_concurrency._registry.values()))
            self.assertEqual(limiter.active, 0)
            with patch.object(call_attempts, 'begin_provider_request', side_effect=RuntimeError('DB unavailable')):
                with self.assertRaises(RuntimeError):
                    llm.call_llm(self.profile, 'test', client=client)
            self.assertEqual(limiter.active, 0)

    def test_defaults_are_ten_and_client_reads_setting_once(self):
        self.assertEqual(config.DEFAULT_SETTINGS['llm.abstract_concurrency'], 10)
        self.assertEqual(db.get_int_setting('llm.abstract_concurrency', 0), 10)
        db.set_setting('llm.abstract_concurrency', 6)
        with llm.make_llm_client(self.profile) as client:
            self.assertEqual(client._dcp_concurrency, 6)
        plan = services.build_daily_pipeline_plan('manual_latest')
        self.assertEqual(plan['abstract_concurrency'], 6)

    def test_classification_and_abstract_share_reduction_and_next_job_resets(self):
        db.save_llm_profile({**self.profile, 'name':'Fixture', 'enabled':True,
            'is_default_abstract':True, 'is_default_classification':True})
        direction = db.create_attention_direction('Frontier', 'Synthetic research scope.')
        seen = []
        def handler(request):
            limiter = next(iter(llm_concurrency._registry.values()))
            prompt = json.loads(request.content)['messages'][-1]['content']
            is_classification = 'direction_id' in prompt and 'directions' in prompt
            seen.append(('classification' if is_classification else 'abstract', limiter.limit))
            if len(seen) == 1:
                return self.response(request, 429, 'concurrent request limit exceeded')
            if is_classification:
                result = {'directions':[{'direction_id':direction, 'decision':'matched', 'reason':'Synthetic evidence'}]}
                return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]}, request=request)
            return self.response(request)
        plan = services.build_daily_pipeline_plan('manual_latest')
        job_id = db.create_job(db.DAILY_PIPELINE_JOB_TYPE, plan)
        def fetch(*_):
            return services.CategoryFetchResult([_paper('2609.00001')], 'success', (), {}, ())
        from contextlib import nullcontext
        with patch.object(services, '_fetch_category_from_config', side_effect=fetch), \
                patch.object(services, '_crawler_client_from_settings', side_effect=lambda:nullcontext(object())), \
                patch.object(services, 'make_llm_client', side_effect=lambda _:httpx.Client(transport=httpx.MockTransport(handler))), \
                patch.object(llm.time, 'sleep', return_value=None):
            jobs.JobRunner()._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'], 'success')
        self.assertEqual(seen, [('classification',10),('classification',4),('abstract',4)])
        runner = jobs.JobRunner()
        next_job = db.create_job('crawl', {})
        with patch.object(runner, '_dispatch', return_value={}):
            runner._run_job(next_job)
        self.assertEqual(next(iter(llm_concurrency._registry.values())).limit, 10)

    def test_anthropic_and_persistent_floor_are_bounded(self):
        profile = {**self.profile, 'provider':'anthropic'}
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={'error':{'message':'Too many concurrent requests'}}, request=request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with patch.object(llm.time, 'sleep', return_value=None):
                with self.assertRaises(llm.LLMHTTPError):
                    llm.call_llm(profile, 'test', client=client)
        self.assertEqual(len(calls), 5)
        self.assertEqual(next(iter(llm_concurrency._registry.values())).limit, 1)

    def test_db_finish_failure_preserves_throttle_and_releases_slot(self):
        with httpx.Client(transport=httpx.MockTransport(
                lambda request:self.response(request, 429, 'concurrency limit exceeded'))) as client:
            with patch.object(call_attempts, 'finish_provider_response', side_effect=RuntimeError('DB unavailable')):
                with self.assertRaises(RuntimeError):
                    llm.call_llm(self.profile, 'test', client=client)
        limiter = next(iter(llm_concurrency._registry.values()))
        self.assertEqual((limiter.limit, limiter.active), (4,0))

    def test_old_client_does_not_undo_new_configuration(self):
        headers = {'Authorization':'Bearer fixture'}
        with httpx.Client() as old_client, httpx.Client() as new_client:
            limiter = llm_concurrency.provider_limiter('test', 'https://a.invalid', headers, 10, client=old_client)
            limiter.throttled(0)
            newer = llm_concurrency.provider_limiter('test', 'https://a.invalid', headers, 6, client=new_client)
            newer.throttled(newer.generation)
            self.assertEqual(newer.limit, 4)
            again = llm_concurrency.provider_limiter('test', 'https://a.invalid', headers, 10, client=old_client)
            self.assertIs(again, newer)
            self.assertEqual(again.limit, 4)

    def test_persistent_floor_uses_existing_bounded_business_retries(self):
        db.save_llm_profile({**self.profile, 'name':'Fixture', 'enabled':True, 'is_default_abstract':True})
        paper = db.upsert_papers([_paper('2609.00001')], 'cs.AI', '2026-09-03')[0]
        job = db.create_job(db.DAILY_PIPELINE_JOB_TYPE, {})
        def handler(request):
            return self.response(request, 429, 'concurrency limit exceeded')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with patch.object(llm.time, 'sleep', return_value=None), patch.object(services, '_abstract_retry_wait'):
                result = services.evaluate_abstract_candidate(paper, llm_client=client,
                    job_id=job, pipeline_job_id=job, max_retries=2)
        self.assertEqual((result['status'], result['retry_count']), ('failed',2))
        with db.connect() as conn:
            count = conn.execute('SELECT COUNT(*) FROM llm_call_attempts').fetchone()[0]
        self.assertEqual(count, 7)  # First business attempt: 5 POSTs; each floor retry: 1.
        self.assertEqual(next(iter(llm_concurrency._registry.values())).active, 0)


if __name__ == '__main__':
    unittest.main()
