import os
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from daily_coolpapers import app as app_module, cache_db, cache_manager, db, fulltext, services
from daily_coolpapers.form_commands import SettingsCommand, FormValidationError
from daily_coolpapers.jobs import JobRunner
from tests.test_cache import VALID_PDF
from tests import test_personal_library as library


class TieredCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.tmp.name)
        self.pdf = root/'cache'/'pdf'
        self.md = root/'cache'/'markdown'
        for directory in [root/'data', root/'instance', self.pdf, self.md]:
            directory.mkdir(parents=True)
        for target, key, value in [(db,'DB_PATH',root/'data'/'main.sqlite3'),
            (db,'LLM_PROFILES_DB_PATH',root/'instance'/'profiles.sqlite3'), (db,'ensure_directories',lambda: None), (cache_manager,'ensure_directories',lambda: None),
            (cache_manager,'PDF_CACHE_DIR',self.pdf),(cache_manager,'MARKDOWN_CACHE_DIR',self.md)]:
            self.stack.enter_context(patch.object(target,key,value))
        self.stack.enter_context(patch.object(services,'call_llm',side_effect=AssertionError('LLM forbidden')))
        db.init_db()
        db.init_llm_profiles_db()
        self.paper_id = db.upsert_paper({'arxiv_id':'2609.00001','title':'Cache fixture',
            'abstract':'Original evidence','authors':['Ada'],'subjects':['cs.AI'],
            'pdf_url':'https://arxiv.org/pdf/2609.00001'},'cs.AI','2026-09-03')
        self.evaluate(self.paper_id,'success')
        self.now = cache_db.utc_now()

    evaluate = library.PersonalLibraryTests.evaluate

    def file(self,kind='pdf',age=10):
        path = (self.pdf if kind=='pdf' else self.md)/('2609.00001.pdf' if kind=='pdf' else '2609.00001.md')
        path.write_bytes(VALID_PDF if kind=='pdf' else b'# Evidence')
        old = self.now-timedelta(days=age)
        os.utime(path,(old.timestamp(),old.timestamp()))
        with db.connect() as conn:
            cache_db.register_artifact(conn,'2609.00001',kind,path.stat().st_size,
                generated_at=cache_db.timestamp(old),used=True,now=old)
        return path

    def row(self,kind='pdf'):
        with db.connect_readonly() as conn:
            row = conn.execute('SELECT * FROM paper_cache_artifacts WHERE artifact_kind=?',(kind,)).fetchone()
            return dict(row) if row else None

    def test_defaults_migration_idempotence_preserves_settings_and_legacy_keys(self):
        db.save_settings({'cache.core_pdf_retention_days':45,'cache.pdf_retention_days':2})
        with db.connect() as conn:
            cache_db.init_schema(conn)
            cache_db.init_schema(conn)
            self.assertEqual(cache_db.retention_settings(conn)['cache.core_pdf_retention_days'],45)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE name=?',(cache_db.MIGRATION,)).fetchone()[0],1)
        self.assertEqual(db.get_setting('cache.pdf_retention_days'),2)

    def test_legacy_grace_one_time_unknown_and_damaged_files_preserved(self):
        path = self.pdf/'2609.00001.pdf'
        path.write_bytes(VALID_PDF)
        os.utime(path,(1,1))
        orphan = self.pdf/'2609.99999.pdf'; orphan.write_bytes(VALID_PDF)
        damaged = self.md/'unknown.md'; damaged.write_text('orphan')
        self.assertEqual(cache_manager.register_legacy_caches(),1)
        grace = self.row()['migration_grace_at']
        self.assertEqual(cache_manager.register_legacy_caches(),0)
        self.assertEqual(self.row()['migration_grace_at'],grace)
        result=cache_manager.cleanup_caches()
        self.assertEqual(result['pdf_deleted'],0)
        self.assertEqual(result['orphan_skipped'],2)
        self.assertTrue(path.exists() and orphan.exists() and damaged.exists())

    def test_ordinary_and_core_independent_expiry_and_evidence_preservation(self):
        pdf=self.file(); md=self.file('markdown',age=10)
        result=cache_manager.cleanup_caches()
        self.assertEqual(result['pdf_deleted'],1)
        self.assertFalse(pdf.exists()); self.assertTrue(md.exists())
        self.assertEqual(db.get_paper(self.paper_id)['abstract'],'Original evidence')
        self.assertIsNotNone(db.get_latest_successful_evaluation(self.paper_id,'fulltext_review'))
        self.assertEqual(result['deleted_bytes'],len(VALID_PDF))
        self.file(age=10)
        db.set_paper_decision(self.paper_id,'favorite')
        self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],0)
        db.set_paper_decision(self.paper_id,'clear')
        self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],1)

    def test_actual_markdown_use_only_updates_markdown_readonly_status_never_writes(self):
        self.file(age=2);self.file('markdown',age=2)
        before=self.row();before_md=self.row('markdown')
        cache_manager.has_pdf('2609.00001');cache_manager.has_markdown('2609.00001')
        cache_manager.cache_status('2609.00001');cache_manager.cache_usage()
        self.assertEqual(before,self.row());self.assertEqual(before_md,self.row('markdown'))
        self.assertEqual(cache_manager.read_cached_markdown('2609.00001'),'# Evidence')
        self.assertEqual(before,self.row())
        self.assertGreater(self.row('markdown')['last_used_at'],before_md['last_used_at'])

    def test_generation_resets_time_even_within_throttle_window(self):
        path=self.file(age=2)
        cache_manager.record_cache_use('2609.00001','pdf',path)
        old=self.row()['generated_at']
        cache_manager.record_cache_use('2609.00001','pdf',path,generated=True)
        self.assertGreater(self.row()['generated_at'],old)

    def test_promotion_window_not_read_and_additional_reason_does_not_extend(self):
        self.file(age=40)
        original=self.row()['last_used_at']
        db.set_paper_decision(self.paper_id,'favorite')
        first=self.row()['tier_promoted_at']
        self.assertIsNotNone(first);self.assertEqual(self.row()['last_used_at'],original)
        theme=db.create_investment_theme('Robotics')
        db.set_paper_investment_themes(self.paper_id,[theme])
        db.update_investment_theme(theme,'archive')
        db.set_paper_decision(self.paper_id,'clear')
        self.assertEqual(self.row()['tier_promoted_at'],first)
        self.assertEqual(cache_manager.cache_status('2609.00001')['reasons'],['theme_relation'])
        self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],0)
        db.remove_paper_investment_theme(self.paper_id,theme)
        self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],1)

    def test_missing_or_malformed_configuration_and_missing_schema_fail_closed(self):
        path=self.file()
        with db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key='cache.core_pdf_retention_days'")
        self.assertEqual(cache_manager.cleanup_caches()['errors'],1);self.assertTrue(path.exists())
        db.save_settings({'cache.core_pdf_retention_days':'bad'})
        self.assertEqual(cache_manager.cleanup_caches()['errors'],1);self.assertTrue(path.exists())
        with db.connect() as conn:
            conn.execute('DELETE FROM schema_migrations WHERE name=?',(cache_db.MIGRATION,))
        self.assertEqual(cache_manager.cleanup_caches()['errors'],1);self.assertTrue(path.exists())

    def test_unlink_error_preserves_registration(self):
        path=self.file();original=self.row()
        with patch.object(Path,'unlink',side_effect=PermissionError('occupied')):
            result=cache_manager.cleanup_caches()
        self.assertEqual(result['errors'],1);self.assertTrue(path.exists());self.assertEqual(self.row(),original)

    def test_final_recheck_sees_successful_read_and_tier_promotion(self):
        path=self.file(age=40)
        original=cache_manager._expired_candidates
        def candidates(*args):
            for item in original(*args):
                db.set_paper_decision(self.paper_id,'favorite')
                yield item
        with patch.object(cache_manager,'_expired_candidates',side_effect=candidates):
            self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],0)
        self.assertTrue(path.exists())
        db.set_paper_decision(self.paper_id,'clear')
        def used(*args):
            for item in original(*args):
                cache_manager.record_cache_use('2609.00001','pdf',path)
                yield item
        with patch.object(cache_manager,'_expired_candidates',side_effect=used):
            self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],0)

    def test_cache_lock_serializes_reader_and_cleanup(self):
        path=self.file()
        started=Event();done=Event();errors=[]
        def cleaner():
            started.set()
            try: cache_manager.cleanup_caches()
            except BaseException as exc: errors.append(exc)
            finally: done.set()
        with cache_manager.cache_lock('2609.00001'):
            worker=Thread(target=cleaner);worker.start();self.assertTrue(started.wait(2))
            cache_manager.record_cache_use('2609.00001','pdf',path)
        worker.join(5)
        self.assertTrue(done.is_set());self.assertFalse(errors);self.assertTrue(path.exists())

    def test_replaced_file_identity_preserved(self):
        path=self.file()
        original=cache_manager._expired_candidates
        def replaced(*args):
            for item in original(*args):
                path.write_bytes(VALID_PDF+b'new content')
                yield item
        with patch.object(cache_manager,'_expired_candidates',side_effect=replaced):
            result=cache_manager.cleanup_caches()
        self.assertEqual(result['pdf_deleted'],0);self.assertTrue(path.exists())

    def test_temporary_files_only_known_names_and_not_during_writer(self):
        known=self.pdf/'.2609.00001.pdf.unit.tmp';known.write_bytes(b'x');os.utime(known,(1,1))
        unknown=self.pdf/'unknown.tmp';unknown.write_bytes(b'x');os.utime(unknown,(1,1))
        result=cache_manager.cleanup_caches()
        self.assertEqual(result['pdf_tmp_deleted'],1);self.assertTrue(unknown.exists())

    def test_team_tracking_archival_semantics_and_transaction_rollback(self):
        self.file(age=40)
        db.save_paper_team_tracking(self.paper_id,{'author_mode':'new','author_name':'Ada',
            'organization_mode':'new','organization_name':'Example Lab','organization_type':'company'})
        relation=db.get_paper_team_tracking(self.paper_id)
        db.update_research_entity('author',relation['lead_author_id'],'archive')
        db.update_research_entity('organization',relation['organization_id'],'archive')
        self.assertEqual(cache_manager.cache_status('2609.00001')['reasons'],['team_tracking'])
        self.assertIsNotNone(self.row()['tier_promoted_at'])
        db.archive_paper_team_tracking(self.paper_id)
        self.assertEqual(cache_manager.cache_status('2609.00001')['tier'],'ordinary')
        original=self.row()
        with patch.object(cache_db,'core_reasons',side_effect=[{'2609.00001':[]},RuntimeError('rollback')]):
            with self.assertRaises(RuntimeError):db.set_paper_decision(self.paper_id,'favorite')
        self.assertEqual(db.get_paper_decision_state(self.paper_id)['decision'],'undecided')
        self.assertEqual(self.row(),original)

    def test_mode_persistence_snapshot_and_idempotent_full_insert(self):
        self.assertTrue(all(row['collection_mode']=='top_n' for row in db.list_categories()))
        category_id=db.save_category({'category':'test.X','name':'Test','enabled':True,'collection_mode':'full'})
        category=db.get_category(category_id) if hasattr(db,'get_category') else next(row for row in db.list_categories() if row['id']==category_id)
        with patch.object(services,'fetch_category_report') as fetch:
            services._fetch_category_from_config(category,'2026-09-03',client='shared')
            self.assertEqual(fetch.call_args.kwargs['collection_mode'],'full')
        plan=services.build_daily_pipeline_plan('manual_latest',category_ids=[category_id])
        self.assertEqual(plan['categories'][0]['collection_mode'],'full')
        paper={'arxiv_id':'2609.00001','title':'Cache fixture','abstract':'Original evidence','authors':['Ada'],'subjects':['cs.AI']}
        for category in ['cs.AI','cs.LG','cs.AI']:
            db.upsert_paper(paper,category,'2026-09-03')
        with db.connect_readonly() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_categories').fetchone()[0],2)

    def test_full_collection_event_ledger_keeps_both_requests(self):
        from contextlib import nullcontext
        from daily_coolpapers.crawler import fetch_category_report
        from tests.test_crawl_observability import FakeClient,FakeResponse,_html,_paper_block
        client=FakeClient(FakeResponse(_html(_paper_block('2609.00001',1),total=3)),
            FakeResponse(_html(*[_paper_block(f'2609.{i:05}',i) for i in range(1,4)],total=3)))
        job_id=db.create_job(db.DAILY_PIPELINE_JOB_TYPE,{})
        category=db.list_categories()[0]
        category.update(category='cs.AI',collection_mode='full')
        with patch.object(services,'fetch_category_report',wraps=fetch_category_report),patch.object(services,'_crawler_client_from_settings',return_value=nullcontext(client)):
            result=services.crawl_all_categories(crawl_date='2026-09-03',pipeline_job_id=job_id,category_snapshot=[category])
        with db.connect_readonly() as conn:
            events=list(conn.execute("SELECT event_key FROM job_events WHERE job_id=? AND event_type='crawl.http_succeeded'",(job_id,)))
            self.assertEqual(len(events),2)
            self.assertTrue(any('request:1:' in row[0] for row in events));self.assertTrue(any('request:2:' in row[0] for row in events))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0],3)

    def test_missing_file_ledger_repaired_after_failed_commit(self):
        path=self.file()
        path.unlink()  # Simulate unlink success followed by DB rollback.
        self.assertIsNotNone(self.row())
        result=cache_manager.cleanup_caches()
        self.assertEqual(result['missing_records_repaired'],1);self.assertIsNone(self.row())
        self.assertIsNotNone(db.get_latest_successful_evaluation(self.paper_id,'fulltext_review'))
        self.assertEqual(cache_manager.cleanup_caches()['missing_records_repaired'],0)

    def test_failed_conversion_of_existing_pdf_does_not_refresh_use(self):
        self.file(age=40);before=self.row()
        with patch.object(fulltext,'convert_pdf_to_markdown',side_effect=ValueError('invalid content')):
            with self.assertRaises(ValueError):fulltext.ensure_markdown(db.get_paper(self.paper_id))
        self.assertEqual(self.row(),before)

    def test_registration_failure_does_not_retry_successful_download(self):
        from tests.test_cache import FakeClient,FakeResponse
        client=FakeClient([FakeResponse(200,'https://arxiv.org/pdf/2609.00001',chunks=[VALID_PDF])])
        with patch.object(cache_manager.httpx,'Client',return_value=client),patch.object(cache_manager,'record_cache_use',side_effect=RuntimeError('registration failed')):
            with self.assertRaisesRegex(RuntimeError,'registration failed'):
                cache_manager.download_pdf('2609.00001','https://arxiv.org/pdf/2609.00001',retries=2)
        self.assertEqual(len(client.calls),1);self.assertTrue((self.pdf/'2609.00001.pdf').exists())

    def test_legacy_upgrade_restart_and_backup_rollback_without_old_cleanup(self):
        import sqlite3
        path=self.file()
        original_bytes=path.read_bytes()
        with db.connect() as conn:
            conn.execute('DROP TABLE paper_cache_artifacts')
            conn.execute('DELETE FROM schema_migrations WHERE name=?',(cache_db.MIGRATION,))
            conn.execute("DELETE FROM settings WHERE key LIKE 'cache.ordinary_%' OR key LIKE 'cache.core_%'")
            conn.execute("ALTER TABLE categories DROP COLUMN collection_mode")
        db.save_settings({'cache.pdf_retention_days':2,'cache.markdown_retention_days':4})
        backup=db.DB_PATH.parent/'pre-tiered.sqlite3'
        with closing(sqlite3.connect(backup)) as target,db.connect_readonly() as source:
            source.backup(target)
        db.init_db();cache_manager.register_legacy_caches()
        grace=self.row()['migration_grace_at']
        self.assertEqual(self.row()['source_version'],'unknown')
        db.init_db();cache_manager.register_legacy_caches()
        self.assertEqual(self.row()['migration_grace_at'],grace)
        self.assertEqual(path.read_bytes(),original_bytes)
        with closing(sqlite3.connect(backup)) as source,db.connect() as target:
            source.backup(target)
        # A rollback package must disable the historical shorter cleanup rules.
        db.save_settings({'cache.cleanup_on_start':False,'cache.cleanup_daily':False})
        self.assertFalse(db.get_bool_setting('cache.cleanup_on_start'))
        self.assertFalse(db.get_bool_setting('cache.cleanup_daily'))
        self.assertEqual(db.get_setting('cache.pdf_retention_days'),2)
        self.assertIsNotNone(db.get_latest_successful_evaluation(self.paper_id,'fulltext_review'))
        self.assertEqual(path.read_bytes(),original_bytes)

    def test_exact_expiry_boundary_and_core_180_day_markdown(self):
        path=self.file(age=7)
        with patch.object(cache_db,'utc_now',return_value=self.now):
            self.assertEqual(cache_manager.cleanup_caches()['pdf_deleted'],1)
        self.assertFalse(path.exists())
        path=self.file('markdown',age=180)
        db.set_paper_decision(self.paper_id,'favorite')
        with db.connect() as conn:
            conn.execute('UPDATE paper_cache_artifacts SET tier_promoted_at=last_used_at')
        with patch.object(cache_db,'utc_now',return_value=self.now-timedelta(microseconds=1)):
            self.assertEqual(cache_manager.cleanup_caches()['markdown_deleted'],0)
        with patch.object(cache_db,'utc_now',return_value=self.now):
            self.assertEqual(cache_manager.cleanup_caches()['core_deleted'],1)
        self.assertFalse(path.exists())

    def test_successful_reads_are_throttled_only_while_ttl_is_fresh(self):
        path=self.file('markdown',age=40)
        with patch.object(cache_db,'utc_now',return_value=self.now):
            cache_manager.read_cached_markdown('2609.00001')
        first=self.row('markdown')['last_used_at']
        with patch.object(cache_db,'utc_now',return_value=self.now+timedelta(seconds=30)):
            cache_manager.read_cached_markdown('2609.00001')
        self.assertEqual(self.row('markdown')['last_used_at'],first)
        with patch.object(cache_db,'utc_now',return_value=self.now+timedelta(seconds=61)):
            cache_manager.read_cached_markdown('2609.00001')
        self.assertNotEqual(self.row('markdown')['last_used_at'],first)

    def test_batched_core_plan_searches_keys_instead_of_scanning_favorites(self):
        statements=[]
        with db.connect_readonly() as conn:
            conn.set_trace_callback(statements.append)
            cache_db.core_reasons(conn,[f'2609.{i:05}' for i in range(100)])
            conn.set_trace_callback(None)
            query=next(sql for sql in statements if sql.startswith('SELECT p.arxiv_id'))
            plan=[row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+query)]
        self.assertFalse(any('idx_paper_dispositions_decision_updated' in detail for detail in plan))
        self.assertTrue(any('SEARCH d USING INTEGER PRIMARY KEY' in detail for detail in plan))
        self.assertTrue(any('idx_memo_cached_paper' in detail for detail in plan))

    def test_form_range_and_tier_constraints(self):
        self.assertEqual(SettingsCommand.from_form({}).values['cache.core_pdf_retention_days'],30)
        for form in [{'core_pdf_retention_days':'0'},{'ordinary_pdf_retention_days':'31'},{'core_markdown_retention_days':'bad'}]:
            with self.assertRaises(FormValidationError):SettingsCommand.from_form(form)

    def test_cache_only_job_and_routes_do_not_evaluate(self):
        app=app_module.create_app(runner=JobRunner(),secret_key='cache-unit')
        app.config['TESTING']=True
        client=app.test_client()
        with client.session_transaction() as session:session['_csrf_token']='cache-unit'
        self.file(age=2);self.file('markdown',age=2)
        self.assertEqual(client.get(f'/papers/{self.paper_id}').status_code,200)
        self.assertEqual(client.get('/settings').status_code,200)
        pdf=client.get(f'/papers/{self.paper_id}/pdf');self.assertEqual(pdf.status_code,200);pdf.close()
        partial=client.get(f'/papers/{self.paper_id}/pdf',headers={'Range':'bytes=0-4'})
        self.assertEqual(partial.status_code,206);self.assertEqual(partial.data,b'%PDF-');partial.close()
        self.assertEqual(client.get(f'/papers/{self.paper_id}/markdown').status_code,200)
        with patch.object(fulltext,'ensure_markdown',return_value=(self.md/'2609.00001.md',False)),patch.object(cache_manager,'download_pdf'):
            response=client.post(f'/papers/{self.paper_id}/cache',data={'csrf_token':'cache-unit'})
            self.assertEqual(response.status_code,302)
            job=db.list_jobs(1)[0]
            self.assertEqual(job['type'],'prepare_cache')
            app.extensions['daily_coolpapers.job_runner']._run_job(job['id'])
            self.assertEqual(db.get_job(job['id'])['status'],'success')


if __name__ == '__main__':unittest.main()
