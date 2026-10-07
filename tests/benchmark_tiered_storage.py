"""Isolated tiered-storage benchmark; never opens the real app database/cache."""
import argparse
import hashlib
import sqlite3
import json
import logging
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily_coolpapers import cache_db, cache_manager, db


def run(count):
    with tempfile.TemporaryDirectory(prefix='dcp-tiered-') as temporary, ExitStack() as stack:
        root=Path(temporary)
        for directory in [root/'data',root/'cache'/'pdf',root/'cache'/'markdown']:
            directory.mkdir(parents=True)
        stack.enter_context(patch.object(db,'DB_PATH',root/'data'/'main.sqlite3'))
        stack.enter_context(patch.object(db,'ensure_directories',lambda:None))
        stack.enter_context(patch.object(cache_manager,'ensure_directories',lambda:None))
        stack.enter_context(patch.object(cache_manager,'PDF_CACHE_DIR',root/'cache'/'pdf'))
        stack.enter_context(patch.object(cache_manager,'MARKDOWN_CACHE_DIR',root/'cache'/'markdown'))
        db.init_db()
        base=db.DB_PATH.stat().st_size
        now=cache_db.utc_now();stamp=cache_db.timestamp(now)
        started=time.perf_counter()
        # 100% metadata, 20% abstract evaluation, 5% fulltext evaluation, 20% core.
        for offset in range(0,count,1000):
            papers=[{'arxiv_id':f'2609.{i:05}','title':f'Synthetic research paper {i}',
                'authors':['Ada','Ben'],'abstract':'Synthetic research evidence. '*55,
                'subjects':['cs.AI','cs.LG'],'published_at':'2026-09-03'} for i in range(offset,min(offset+1000,count))]
            result=db.upsert_papers_with_stats(papers,'cs.AI','2026-09-03')
            db.upsert_papers_with_stats(papers,'cs.LG','2026-09-03')
            with db.connect() as conn:
                for kind,stride in [('abstract_review',5),('fulltext_review',20)]:
                    conn.executemany("""INSERT INTO evaluations(paper_id,evaluation_type,model,status,result_json,raw_output,created_at)
                        VALUES(?,?,?,'success',?,?,?)""", [(paper_id,kind,'synthetic-no-LLM',json.dumps({'score':80,'summary':'Synthetic finding. '*70}),'Synthetic output. '*70,stamp)
                        for i,paper_id in enumerate(result.paper_ids,start=offset) if i%stride==0])
                conn.executemany("INSERT INTO paper_dispositions(paper_id,decision,created_at,updated_at) VALUES(?,'favorite',?,?)",
                    [(paper_id,stamp,stamp) for i,paper_id in enumerate(result.paper_ids,start=offset) if i%5==0])
                # Deliberately one cached file per paper for the scan stress scenario.
                cache_rows=[]
                for i,paper_id in enumerate(result.paper_ids,start=offset):
                    key=papers[i-offset]['arxiv_id'];path=root/'cache'/'markdown'/(key+'.md')
                    path.write_bytes(b'# Synthetic cache evidence\n'*10)
                    used=cache_db.timestamp(now-timedelta(days=200)) if i%100==1 else stamp
                    cache_rows.append((key,'markdown',paper_id,stamp,used,path.stat().st_size))
                conn.executemany("""INSERT INTO paper_cache_artifacts
                    (arxiv_id,artifact_kind,paper_id,generated_at,last_used_at,last_known_size_bytes)
                    VALUES(?,?,?,?,?,?)""",cache_rows)
        preparation=time.perf_counter()-started
        with db.connect() as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            counts={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['papers','paper_categories','evaluations','paper_cache_artifacts']}
        before=cache_manager.cache_usage()
        holds=[]
        original_lock=cache_manager.cache_lock
        @contextmanager
        def measured_lock(key):
            with original_lock(key):
                acquired=time.perf_counter()
                try: yield
                finally: holds.append((time.perf_counter()-acquired)*1000)
        started=time.perf_counter()
        with patch.object(cache_manager,'cache_lock',measured_lock):
            result=cache_manager.cleanup_caches()
        cleanup=time.perf_counter()-started
        holds.sort()
        lock_metrics={'count':len(holds),'median_ms':round(holds[len(holds)//2],3) if holds else 0,
            'p95_ms':round(holds[min(len(holds)-1,int(len(holds)*0.95))],3) if holds else 0,
            'max_ms':round(max(holds),3) if holds else 0}
        after=cache_manager.cache_usage()
        # Simulate restart: migration and inventory repeated, no use/grace changes.
        with db.connect() as conn:
            before_times=list(conn.execute('SELECT arxiv_id,last_used_at,migration_grace_at FROM paper_cache_artifacts ORDER BY arxiv_id'))
        db.init_db();cache_manager.register_legacy_caches()
        with db.connect() as conn:
            after_times=list(conn.execute('SELECT arxiv_id,last_used_at,migration_grace_at FROM paper_cache_artifacts ORDER BY arxiv_id'))
            recovery_ok=[tuple(row) for row in before_times]==[tuple(row) for row in after_times]
        return {'measured_at':stamp,'runtime':{'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'platform':sys.platform},
            'source_sha256':{name:hashlib.sha256((Path(__file__).resolve().parents[1]/'daily_coolpapers'/name).read_bytes()).hexdigest() for name in ['cache_db.py','cache_manager.py','db.py']},'paper_count':count,'counts_before':counts,'database_growth_bytes':before['database_bytes']-base,
            'database_bytes_per_paper':round((before['database_bytes']-base)/count,2),
            'preparation_seconds':round(preparation,3),'cleanup_seconds':round(cleanup,3),
            'cache_lock_hold':lock_metrics,'storage_before':before,'storage_after':after,'cleanup':result,'restart_preserves_times':recovery_ok,
            'note':'Synthetic metadata/evaluations; no classification or memo snapshots. One MD per paper is scan stress, not production fulltext rate.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sizes',nargs='+',type=int,default=[10000,100000]);parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'doc'/'TIERED_STORAGE_BENCHMARK.json')
    args=parser.parse_args();logging.basicConfig(level=logging.WARNING)
    results=[]
    for count in args.sizes:
        result=run(count);results.append(result);print(json.dumps(result),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
