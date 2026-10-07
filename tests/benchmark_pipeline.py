"""Isolated full-collection pipeline benchmark, with optional read-only source timing.

Run: python -B -m tests.benchmark_pipeline --output doc/PIPELINE_BENCHMARK.json --live
No production database, credentials, cache, runtime, or paid LLM calls are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import platform
import re
import sqlite3
import threading
import time
from unittest.mock import patch

import httpx

from daily_coolpapers import crawler, db, jobs, services
from tests.benchmark_llm_calls import isolated_database
from tests.test_crawl_observability import _html, _paper_block

DATE = '2026-10-06'
# Previously observed category appearances, not unique-paper counts.
COUNTS = dict(zip(('cs.AI', 'cs.CL', 'cs.CV', 'cs.HC', 'cs.IR', 'cs.LG',
                   'cs.RO', 'cs.SE', 'eess.IV', 'stat.ML'),
                  (554, 243, 285, 46, 33, 600, 174, 58, 12, 114)))


def distribution(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(values), 'sum_ms': round(sum(values), 3),
            'p50_ms': round(ordered[math.ceil(len(values) * .5) - 1], 3),
            'p95_ms': round(ordered[math.ceil(len(values) * .95) - 1], 3),
            'max_ms': round(ordered[-1], 3)}


class Timings:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def add(self, name, seconds):
        with self.lock:
            self.values.setdefault(name, []).append(seconds * 1000)

    def wrap(self, name, function):
        def measured(*args, **kwargs):
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.add(name, time.perf_counter() - start)
        return measured

    def report(self):
        return {key: distribution(value) for key, value in self.values.items()}


def fixture_pages(unique_count, counts):
    pages, offset = {}, 0
    for category, count in counts.items():
        blocks = []
        for rank in range(1, count + 1):
            index = (offset + rank - 1) % unique_count + 1
            block = _paper_block(f'2609.{index:05}', rank,
                                 abstract='Synthetic frontier research evidence. ' * 35)
            blocks.append(block.replace(f'Paper {rank}', f'BENCH_PAPER_{index:05}'))
        pages[category] = {1: _html(blocks[0], page_date=DATE, total=count),
                           count: _html(*blocks, page_date=DATE, total=count)}
        offset += count
    return pages


class MockProvider:
    def __init__(self, direction_ids, classification_delay, abstract_delay, retry_every=0):
        self.direction_ids = direction_ids
        self.delays = {'classification': classification_delay, 'abstract': abstract_delay}
        self.retry_every = retry_every
        self.attempts = {}
        self.active = 0
        self.max_active = 0
        self.failures = 0
        self.lock = threading.Lock()
        self.timings = Timings()

    def handler(self, request):
        assert request.url.host == 'benchmark.invalid' and request.method == 'POST'
        payload = json.loads(request.content)
        prompt = payload['messages'][-1]['content']
        match = re.search(r'BENCH_PAPER_(\d+)', prompt)
        assert match, 'fixture paper marker missing from rendered production prompt'
        index = int(match.group(1))
        kind = 'classification' if 'directions' in prompt and 'direction_id' in prompt else 'abstract'
        key = (kind, index)
        with self.lock:
            attempt = self.attempts[key] = self.attempts.get(key, 0) + 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        started = time.perf_counter()
        try:
            time.sleep(self.delays[kind])
            if self.retry_every and index % self.retry_every == 0 and attempt == 1:
                with self.lock:
                    self.failures += 1
                return httpx.Response(429, json={'error': {'message': 'Synthetic rate limit'}}, request=request)
            if kind == 'classification':
                result = {'directions': [{'direction_id': key,
                    'decision': 'matched' if index % 5 == 0 else 'unmatched',
                    'reason': 'Deterministic synthetic evidence.'} for key in self.direction_ids]}
            else:
                result = {'score': 80, 'attention': 'read', 'summary': 'Synthetic finding. ' * 40}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}],
                'usage': {'prompt_tokens': 500, 'completion_tokens': 150, 'total_tokens': 650}}, request=request)
        finally:
            self.timings.add(kind, time.perf_counter() - started)
            with self.lock:
                self.active -= 1


def run_offline(name, concurrency, classification_delay, abstract_delay, unique_count=1500,
                retry_every=0, repeat=False):
    # HTML fixture construction and schema/profile setup are outside the timed job.
    counts_by_category = COUNTS if unique_count == 1500 else {key: min(value, unique_count // 2) for key, value in COUNTS.items()}
    appearances = sum(counts_by_category.values())
    pages = fixture_pages(unique_count, counts_by_category)
    timings = Timings()
    crawler_requests = []
    with isolated_database() as components, ExitStack() as stack:
        db.init_llm_profiles_db()
        db.save_llm_profile({'name': 'Synthetic benchmark', 'provider': 'openai_compatible',
            'base_url': 'https://benchmark.invalid/v1', 'model': 'synthetic-no-paid-model',
            'encrypted_api_key_ref': components['secret_store'].encrypt('ephemeral-benchmark-only'),
            'enabled': True, 'is_default_abstract': True, 'is_default_classification': True})
        direction_ids = [db.create_attention_direction('Benchmark frontier', 'Synthetic frontier scope.')]
        db.set_setting('llm.abstract_concurrency', str(concurrency))
        db.set_setting('crawler.concurrency', '6')
        provider = MockProvider(direction_ids, classification_delay, abstract_delay, retry_every)

        def source_handler(request):
            assert request.url.host == 'papers.cool' and request.method == 'GET'
            category = request.url.path.rsplit('/', 1)[-1]
            show = int(request.url.params['show'])
            with timings.lock:
                crawler_requests.append((category, show))
            return httpx.Response(200, text=pages[category][show], request=request)

        stack.enter_context(patch.object(services, 'latest_available_arxiv_date', return_value=DATE))
        stack.enter_context(patch.object(services, '_crawler_client_from_settings',
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(source_handler))))
        def mock_llm_client(_):
            client = httpx.Client(transport=httpx.MockTransport(provider.handler))
            client._dcp_concurrency = concurrency
            return client
        stack.enter_context(patch.object(services, 'make_llm_client', side_effect=mock_llm_client))
        for module, attribute, metric in ((crawler, '_analyze_category_response', 'response_parse'),
                (db, 'upsert_papers_with_stats', 'category_upsert'),
                (services, 'crawl_all_categories', 'crawl_stage')):
            stack.enter_context(patch.object(module, attribute, timings.wrap(metric, getattr(module, attribute))))
        # Production retry code/backoff is retained; deterministic jitter makes it repeatable.
        stack.enter_context(patch.object(services.random, 'random', return_value=.5))
        categories = [{**category, 'collection_mode': 'full'} for category in db.list_categories(True)]
        plan = services.build_daily_pipeline_plan('manual_latest', category_snapshot=categories)
        runner = jobs.JobRunner()  # No runtime threads or scheduler are started.
        job_id = db.create_job(db.DAILY_PIPELINE_JOB_TYPE, plan)
        started = time.perf_counter()
        runner._run_job(job_id)
        elapsed = time.perf_counter() - started
        job = db.get_job(job_id)
        final = db.list_job_events(job_id, event_type='pipeline.completed')[0]['metrics']
        with db.connect() as conn:
            counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ('papers', 'paper_categories', 'evaluations', 'job_events', 'llm_call_attempts')}
            failed_evaluations = conn.execute("SELECT COUNT(*) FROM evaluations WHERE status='failed'").fetchone()[0]
            busy_errors = conn.execute("SELECT COUNT(*) FROM job_events WHERE error_code='pipeline_system_error'").fetchone()[0]
        expected_abstract = unique_count // 5
        assert job['status'] == 'success', final
        assert counts['papers'] == unique_count and counts['paper_categories'] == appearances, counts
        assert final['classification']['success'] == unique_count, final
        assert final['abstract']['success'] == expected_abstract, final
        assert len(crawler_requests) == 20
        assert provider.max_active <= concurrency
        assert sum(provider.attempts.values()) == counts['llm_call_attempts'], counts
        result = {'name': name, 'concurrency': concurrency, 'unique_papers': unique_count,
            'category_appearances': appearances, 'selection_rate': .2,
            'mock_delay_seconds': provider.delays, 'retry_every': retry_every,
            'job_status': job['status'], 'wall_seconds': round(elapsed, 3),
            'timings': timings.report(), 'mock_request_timings': provider.timings.report(),
            'max_simultaneous_mock_requests': provider.max_active,
            'mock_429_count': provider.failures, 'crawler_requests': len(crawler_requests),
            'counts': counts, 'failed_evaluation_rows_including_recovered_attempts': failed_evaluations,
            'pipeline_system_errors': busy_errors, 'classification': final['classification'],
            'abstract': final['abstract'], 'checks_passed': True}
        if repeat:
            before = sum(provider.attempts.values())
            second = db.create_job(db.DAILY_PIPELINE_JOB_TYPE, plan)
            started = time.perf_counter()
            runner._run_job(second)
            second_job = db.get_job(second)
            repeat_calls = sum(provider.attempts.values()) - before
            assert second_job['status'] == 'success' and repeat_calls == 0
            result['same_day_repeat'] = {'wall_seconds': round(time.perf_counter() - started, 3),
                'new_llm_requests': repeat_calls, 'status': second_job['status']}
        return result


def run_live():
    """Exactly ten source units, shared HTTP client, explicit date, no LLM or production DB."""
    timings = Timings()
    reports = {}
    started = time.perf_counter()
    with patch.object(crawler, '_analyze_category_response',
                      timings.wrap('response_parse', crawler._analyze_category_response)):
        with httpx.Client(timeout=20, follow_redirects=True, trust_env=False) as client:
            def fetch(category):
                start = time.perf_counter()
                report = crawler.fetch_category_report(category, collection_mode='full', crawl_date=DATE,
                    sort_param='sort=1', retries=2, client=client)
                return report, time.perf_counter() - start
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {executor.submit(fetch, category): category for category in COUNTS}
                for future in as_completed(futures):
                    category = futures[future]
                    try:
                        report, seconds = future.result()
                        reports[category] = (report, seconds)
                    except Exception as error:
                        reports[category] = (None, type(error).__name__)
    fetch_seconds = time.perf_counter() - started
    units = []
    # Networking is finished before entering the offline guard and temporary database.
    with isolated_database():
        started = time.perf_counter()
        for category, (report, seconds) in reports.items():
            if report is None:
                units.append({'category': category, 'status': 'error', 'error_type': seconds})
                continue
            start = time.perf_counter()
            stored = db.upsert_papers_with_stats(report.papers, category, DATE)
            upsert_seconds = time.perf_counter() - start
            units.append({'category': category, 'status': report.status, 'fetch_parse_seconds': round(seconds, 3),
                'upsert_seconds': round(upsert_seconds, 3), 'paper_count': len(report.papers),
                'declared_total': report.metrics.get('declared_total'),
                'http_response_ms': report.metrics.get('total_response_ms', report.metrics.get('response_ms')),
                'completeness_verified': report.metrics.get('completeness_verified', False),
                'persisted_count': len(stored.paper_ids), 'error_codes': report.error_codes})
        upsert_wall = time.perf_counter() - started
        with db.connect() as conn:
            unique = conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
    return {'mode': 'real_read_only_source_temporary_db_no_llm', 'date': DATE, 'concurrency': 6,
        'fetch_parse_wall_seconds': round(fetch_seconds, 3), 'serial_upsert_wall_seconds': round(upsert_wall, 3),
        'fetch_then_upsert_wall_seconds': round(fetch_seconds + upsert_wall, 3),
        'unique_papers': unique, 'category_appearances': sum(unit.get('paper_count', 0) for unit in units),
        'parse_timings': timings.report(), 'units': sorted(units, key=lambda unit: unit['category']),
        'all_complete': all(unit.get('completeness_verified') for unit in units)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('doc/PIPELINE_BENCHMARK.json'))
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--live-only', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {'measured_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
        'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
        'scope': 'Real application pipeline + isolated SQLite + synthetic HTTP/LLM. No paid model calls.',
        'limits': ['Single run per case, not repeated statistical trials.',
                   'Synthetic matching/output/token counts; no model-quality or provider-quota measurement.',
                   'Mock delays are unscaled wall-clock sleeps, not real provider latency.',
                   'No historical database, active runtime, interactive job, or catch-up workload.'],
        'source_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
            [Path('tests/benchmark_pipeline.py'), *[Path('daily_coolpapers') / (name + '.py')
              for name in ('services', 'crawler', 'db', 'jobs', 'llm', 'llm_concurrency', 'call_attempts')]]}, 'offline': []}
    cases = [('local_overhead', 4, 0, 0, 0, True),
             ('delayed_c4', 4, .2, 1, 0, False), ('delayed_c8', 8, .2, 1, 0, False),
             ('delayed_c12', 12, .2, 1, 0, False), ('retry_c4', 4, .2, 1, 50, False)]
    if args.smoke:
        cases = [('smoke', 10, 0, 0, 0, True)]
    if args.live_only:
        cases = []
    for name, concurrency, class_delay, abstract_delay, retry_every, repeat in cases:
        print(f'Starting {name}', flush=True)
        result = run_offline(name, concurrency, class_delay, abstract_delay,
            unique_count=100 if args.smoke else 1500, retry_every=retry_every, repeat=repeat)
        report['offline'].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Completed {name}: {result['wall_seconds']}s, status={result['job_status']}", flush=True)
    if args.live or args.live_only:
        print('Starting read-only source timing', flush=True)
        report['live_source'] = run_live()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report saved: {args.output.resolve()}', flush=True)


if __name__ == '__main__':
    main()
