"""Offline S0 baseline. Run from the repository root with python -B -m tests.benchmark_architecture."""
from __future__ import annotations
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from unittest.mock import patch

from daily_coolpapers import config, db, memo_db

STAMP = '2026-09-05T08:00:00+08:00'

@contextmanager
def isolated_database():
    # Patch config before importing app and every module-bound runtime path too.
    with tempfile.TemporaryDirectory(prefix='coolpapers-s0-') as temp, ExitStack() as stack:
        root = Path(temp)
        paths = {'BASE_DIR': root, 'INSTANCE_DIR': root/'instance', 'DATA_DIR': root/'data',
                 'CACHE_DIR': root/'cache', 'PDF_CACHE_DIR': root/'cache/pdf',
                 'MARKDOWN_CACHE_DIR': root/'cache/markdown', 'LOG_DIR': root/'logs',
                 'CURRENT_LOG': root/'logs/current.log', 'DB_PATH': root/'data/main.sqlite3',
                 'LLM_PROFILES_DB_PATH': root/'instance/profiles.sqlite3'}
        for name, value in paths.items():
            stack.enter_context(patch.object(config, name, value))
        from daily_coolpapers import app, cache_manager, logging_setup, llm, security
        for module in (db, app, cache_manager, logging_setup):
            for name, value in paths.items():
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, value))
        secret = security.SecretStore(root/'instance/fernet.key')
        for module in (app, llm, security):
            stack.enter_context(patch.object(module, 'secret_store', secret))
        # Any accidental network access or runtime startup is a hard error.
        stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        stack.enter_context(patch.object(app, 'start_runtime', side_effect=AssertionError('runtime forbidden')))
        db.init_db()
        application = app.create_app(secret_key='synthetic-benchmark', store=secret)
        application.config['TESTING'] = True
        yield application, root


def populate(count, seed=20260905):
    rng = random.Random(seed)
    with db.connect() as conn:
        for theme in range(1, 5):
            conn.execute('INSERT INTO investment_themes(id,name,normalized_name,description,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (theme, f'Theme {theme}', f'theme {theme}', 'Synthetic source', STAMP, STAMP))
        conn.execute("INSERT INTO attention_directions(id,name,normalized_name,scope_text,created_at,status_updated_at) VALUES(1,'AI','ai','Synthetic scope',?,?)", (STAMP,STAMP))
        for start in range(1, count+1, 1000):
            papers, evaluations, categories, favorites, themes, directions = [], [], [], [], [], []
            for i in range(start, min(start+1000, count+1)):
                title = f'Synthetic {i:06d} ' + ('Rare Straße 中文 %_' if i%100==0 else 'AI systems')
                papers.append((i,f'2609.{i:06d}',title,'["Author A", "Author B"]','Synthetic abstract '*20,'["cs.AI", "cs.LG"]',STAMP,STAMP,STAMP))
                score = [None,True,'bad',7,7,8][i%6]
                result = json.dumps({'score':score,'attention':'high','summary':'Evidence '*128,'analysis':'Synthetic reasoning '*100},ensure_ascii=False)
                for j, kind in enumerate(('abstract_review','fulltext_review','fulltext_review')):
                    failed = j==2 and i%7==0
                    evaluations.append((i,kind,'failed' if failed else 'success',result,'Synthetic raw '*128,f'2026-09-05T08:00:0{j}+08:00'))
                for cat in ('cs.AI','cs.LG'):
                    categories.append((i,cat,'2026-09-05',rng.randrange(1,31),rng.randrange(20),STAMP))
                if i%5==0:
                    favorites.append((i,'favorite',STAMP,STAMP))
                for theme in (1,2+i%3):
                    themes.append((i,theme,STAMP))
                directions.append((i,1,('matched','possible','unmatched')[i%3], 'rejected' if i%11==0 else None,STAMP,STAMP))
            conn.executemany('INSERT INTO papers(id,arxiv_id,title,authors,abstract,subjects,published_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',papers)
            conn.executemany('INSERT INTO evaluations(paper_id,evaluation_type,status,result_json,raw_output,created_at) VALUES(?,?,?,?,?,?)',evaluations)
            conn.executemany('INSERT INTO paper_categories(paper_id,category,crawl_date,rank,reading_stars,created_at) VALUES(?,?,?,?,?,?)',categories)
            conn.executemany('INSERT INTO paper_dispositions VALUES(?,?,?,?)',favorites)
            conn.executemany('INSERT INTO paper_investment_themes VALUES(?,?,?)',themes)
            conn.executemany('INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,manual_decision,created_at,updated_at) VALUES(?,?,?,?,?,?)',directions)
        conn.commit()


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)]


def measure(name, call, warmup, samples):
    for _ in range(warmup):
        call()
    timings = []
    size = 0
    for sample_index in range(samples):
        begin = time.perf_counter()
        payload = call()
        timings.append((time.perf_counter()-begin)*1000)
        size = len(payload)
        if (sample_index+1)%5==0:
            print(f'  {name}: {sample_index+1}/{samples} timed samples, last {timings[-1]:.0f}ms',flush=True)
    # SQL tracing and allocation tracing are separate from latency measurements.
    statements = []
    original = db.connect
    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith(('SELECT','WITH')) else None)
        return conn
    with patch.object(db, 'connect', traced):
        call()
    plans = []
    seen = set()
    with original() as conn:
        for sql in statements:
            # SQL trace expands IN ids; retain only one plan per query shape.
            import re
            shape = re.sub(r'\b\d+\b', '?', sql)
            shape = re.sub(r'\?(?:,\?)+', '?...', shape)
            if shape in seen:
                continue
            seen.add(shape)
            try:
                plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+sql)]
            except sqlite3.Error as exc:
                plan = ['UNAVAILABLE: '+str(exc)]
            plans.append({'query':shape[:1200], 'plan':plan})
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'case':name,'samples':samples,'warmup':warmup,'p50_ms':round(statistics.median(timings),2),
            'p95_ms':round(percentile(timings,.95),2),'payload_bytes':size,'python_peak_mib':round(peak/1024**2,2),
            'select_count':len(statements),'plans':plans}


def cases(app, count):
    from daily_coolpapers import services
    client = app.test_client()
    def route(url):
        def run():
            response = client.get(url)
            assert response.status_code==200, (url,response.status_code)
            return response.data
        return run
    def model(function):
        return lambda: json.dumps(function(),ensure_ascii=False,default=str).encode('utf-8')
    def candidates(filters):
        def run():
            with db.connect() as conn:
                return memo_db.candidate_data(conn,{'mode':'investment_theme','id':1},filters)
        return model(run)
    visible_count = db.list_paper_page(crawl_date='2026-09-05', page_size=1)['total']
    yield 'home/first/30 HTML', route('/?date=2026-09-05&page_size=30')
    yield 'home/middle/30 HTML', route(f'/?date=2026-09-05&page_size=30&page={max(1,math.ceil(visible_count/60))}')
    yield 'home/last/100 HTML', route(f'/?date=2026-09-05&page_size=100&page={math.ceil(visible_count/100)}')
    yield 'favorites model JSON', model(services.favorite_papers_page_model)
    yield 'reviewed model JSON', model(services.reviewed_papers_page_model)
    yield 'theme model JSON', model(lambda: services.investment_theme_papers_model(1))
    yield 'memo/all model JSON', candidates({})
    yield 'memo/rare model JSON', candidates({'query':'Rare'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes',nargs='+',type=int,default=[100,10000,100000])
    parser.add_argument('--warmup',type=int,default=5)
    parser.add_argument('--samples',type=int,default=30)
    parser.add_argument('--case', action='append', help='Only cases containing this substring; repeatable')
    parser.add_argument('--report',type=Path,default=Path('ARCHITECTURE_BASELINE.md'))
    args = parser.parse_args()
    if args.samples<1 or args.warmup<0 or any(n<1 for n in args.sizes):
        parser.error('sizes/samples must be positive, warmup nonnegative')
    environment = {'time':time.strftime('%Y-%m-%d %H:%M:%S %z'),'python':platform.python_version(),
                   'executable':os.sys.executable,'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),
                   'processor':platform.processor(),'logical_cpus':os.cpu_count(),'seed':20260905,'argv':os.sys.argv,'parameters':vars(args)|{'report':str(args.report)},
                   'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('daily_coolpapers/db.py'),Path('daily_coolpapers/services.py'),Path('daily_coolpapers/memo_db.py'),Path(__file__),Path('daily_coolpapers/app.py'),Path('daily_coolpapers/templates/index.html')]}}
    with args.report.open('a',encoding='utf-8') as report:
        report.write('\n## Measurement run\n\n```json\n'+json.dumps(environment,ensure_ascii=False,indent=2)+'\n```\n')
        report.flush()
        for size in args.sizes:
            print(f'Generating {size} papers',flush=True)
            with isolated_database() as (app, root):
                begin=time.perf_counter()
                populate(size)
                report.write(f'\n### {size:,} papers / {3*size:,} evaluations / {size//5:,} favorites\n\nSeed generation: {time.perf_counter()-begin:.2f}s.\n\n')
                report.write('| Case | p50 ms | p95 ms | Bytes | Python peak MiB | SELECTs |\n|---|---:|---:|---:|---:|---:|\n')
                report.flush()
                results=[]
                for name,call in cases(app,size):
                    if args.case and not any(value in name for value in args.case):
                        continue
                    print(f'{size}: {name}',flush=True)
                    result=measure(name,call,args.warmup,args.samples)
                    results.append(result)
                    report.write(f"| {name} | {result['p50_ms']} | {result['p95_ms']} | {result['payload_bytes']} | {result['python_peak_mib']} | {result['select_count']} |\n")
                    report.flush()
                report.write('\n<details><summary>Exact sample settings and EXPLAIN QUERY PLAN</summary>\n\n```json\n'+json.dumps(results,ensure_ascii=False,indent=2)+'\n```\n</details>\n')
                report.flush()

if __name__=='__main__':
    main()
