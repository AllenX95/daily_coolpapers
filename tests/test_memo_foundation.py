import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from werkzeug.datastructures import MultiDict

from daily_coolpapers import db, memo_db, memos
from daily_coolpapers.form_commands import FormValidationError
from tests import test_personal_library as library


class MemoFoundationTests(unittest.TestCase):
    paper = library.PersonalLibraryTests.paper
    evaluate = library.PersonalLibraryTests.evaluate

    def setUp(self):
        library.PersonalLibraryTests.setUp(self)
        self.profile_id = db.save_llm_profile({'name':'Memo fixture','provider':'openai_compatible','base_url':'https://example.invalid',
            'model':'memo-fixture','enabled':1,'is_default_memo':1,'context_window_tokens':128000,'max_output_tokens':4000})

    def favorite(self,number=1):
        key = self.paper(number)
        db.set_paper_decision(key,'favorite')
        return key

    def command(self,ids,**changes):
        values = {'title':'AI Research','source_mode':'manual','paper_ids':list(ids),
                  'idempotency_key':'foundation-key-0001',**changes}
        if values.get('series_id'):
            values.pop('title',None)
            values.pop('source_mode',None)
        return memos.MemoRequest.from_form(values,creating=True)

    def state(self):
        with db.connect() as conn:
            return {t:[tuple(row) for row in conn.execute(f'SELECT * FROM {t}')] for t in
                    ('investment_memo_series','investment_memo_versions','investment_memo_version_papers','jobs','job_events')}

    def test_creation_freezes_order_latest_success_evaluations_and_one_job(self):
        first,second = self.favorite(),self.favorite(2)
        self.evaluate(first,'failed')
        abstract = self.evaluate(first,'success',kind='abstract_review')
        result = memos.create_memo_version(self.command([second,first]))
        with db.connect() as conn:
            version = dict(conn.execute('SELECT * FROM investment_memo_versions WHERE id=?',(result['id'],)).fetchone())
            papers = [dict(r) for r in conn.execute('SELECT * FROM investment_memo_version_papers ORDER BY display_order')]
        self.assertEqual([p['paper_id'] for p in papers],[second,first])
        self.assertEqual(papers[1]['abstract_evaluation_id'],abstract)
        self.assertEqual(papers[1]['fulltext_evaluation_id'],db.get_latest_successful_evaluation(first,'fulltext_review')['id'])
        self.assertIsNone(papers[0]['abstract_evaluation_id'])
        self.assertEqual(db.get_job(result['job_id'])['payload_data'],{'version_id':result['id']})
        snapshot = json.loads(version['input_snapshot_json'])
        self.assertEqual([p['evidence_ref'] for p in snapshot['papers']],['P1','P2'])
        self.assertNotIn('encrypted_api_key_ref',version['profile_snapshot_json'])
        self.assertNotIn('base_url',version['profile_snapshot_json'])
        self.assertIn('is_default_memo',db.get_llm_profile(self.profile_id))
        self.llm.assert_not_called()

    def test_preview_no_write_and_qualification_change_rejected_at_confirmation(self):
        paper = self.favorite()
        command = self.command([paper])
        before = self.state()
        preview = memos.preview_memo(command)
        self.assertGreater(preview['estimated_input_tokens'],0)
        self.assertEqual(self.state(),before)
        db.set_paper_decision(paper,'clear')
        with self.assertRaises(memo_db.MemoConflictError):
            memos.create_memo_version(command)
        self.assertEqual(self.state(),before)
        self.llm.assert_not_called()

    def test_invalid_unknown_duplicate_and_missing_fulltext_zero_write(self):
        good = self.favorite()
        bad = self.paper(2,status=None)
        with db.connect() as conn:
            conn.execute("INSERT INTO paper_dispositions(paper_id,decision,created_at,updated_at) VALUES (?,'favorite','t','t')",(bad,))
        for ids,error in [([good,bad],memo_db.MemoConflictError),([good,999],memo_db.MemoNotFoundError)]:
            before = self.state()
            with self.assertRaises(error):
                memos.create_memo_version(self.command(ids))
            self.assertEqual(self.state(),before)
        for ids in ([],[good,good]):
            with self.assertRaises(FormValidationError):
                self.command(ids)

    def test_order_form_is_explicit_and_rejects_duplicates(self):
        values = MultiDict([('title','Order'),('source_mode','manual'),('paper_ids','1'),('paper_ids','2'),
                           ('order_1','2'),('order_2','1')])
        self.assertEqual(memos.MemoRequest.from_form(values).paper_ids,[2,1])
        values['order_2']='2'
        with self.assertRaises(FormValidationError):
            memos.MemoRequest.from_form(values)

    def test_source_modes_preselect_only_effective_favorites_and_manual_additions(self):
        first,second,third = self.favorite(),self.favorite(2),self.favorite(3)
        nonfavorite = self.paper(4)
        direction = db.create_attention_direction('Memory','Scope')
        for paper,state,manual in [(first,'matched',None),(second,'possible',None),(third,'matched','rejected'),(nonfavorite,'matched',None)]:
            with db.connect() as conn:
                conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,manual_decision,created_at,updated_at)
                    VALUES (?,?,?,?,'t','t')''',(paper,direction,state,manual))
        command = self.command([first,second],source_mode='attention_direction',source_id=direction)
        page = memos.candidate_page(command,{})
        self.assertEqual([p['id'] for p in page['candidates'] if p['preselected']],[first])
        self.assertEqual(page['counts']['preselected'],1)
        self.assertEqual(page['counts']['pending_possible'],1)
        self.assertEqual(page['counts']['rejected'],1)
        self.assertEqual(page['counts']['not_favorite'],1)
        version = memos.create_memo_version(command)
        with db.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT selection_origin FROM investment_memo_version_papers ORDER BY display_order')],['preselected','manual_added'])
        self.assertEqual(memo_db.get_series(version['series_id'])[0]['source_direction_id'],direction)

    def test_theme_preselection_and_all_required_manual_filters(self):
        first,second = self.favorite(),self.favorite(2)
        theme = db.create_investment_theme('Theme')
        db.set_paper_investment_themes(first,[theme])
        direction = db.create_attention_direction('D','Scope')
        db.set_direction_decision(first,direction,'confirmed')
        db.save_paper_team_tracking(first,{'author_mode':'new','author_name':'Ada Manual','organization_mode':'new','organization_name':'Research Lab','organization_type':'company'})
        command = self.command([first],source_mode='investment_theme',source_id=theme)
        page = memos.candidate_page(command,{})
        self.assertEqual([p['id'] for p in page['candidates'] if p['preselected']],[first])
        filters = memos.parse_candidate_filters({'query':'2609.00001','filter_direction_id':str(direction),'filter_theme_id':str(theme),
            'author':'Ada Manual','organization':'Research','favorite_from':'2000-01-01','favorite_to':'2099-12-31','min_score':'70','sort':'score_desc'})
        self.assertEqual([p['id'] for p in memos.candidate_page(command,filters)['candidates']],[first])
        self.assertNotEqual(first,second)

    def test_no_drift_after_changes_and_metadata_team_not_required(self):
        paper = self.favorite()
        theme = db.create_investment_theme('Before','Old scope')
        db.set_paper_investment_themes(paper,[theme])
        created = memos.create_memo_version(self.command([paper],source_mode='investment_theme',source_id=theme))
        before = self.state()
        db.update_investment_theme(theme,'update','After','New scope')
        db.set_paper_decision(paper,'clear')
        db.remove_paper_investment_theme(paper,theme)
        self.evaluate(paper,'success',score=99)
        with db.connect() as conn:
            conn.execute("UPDATE papers SET title='Changed title' WHERE id=?",(paper,))
        self.assertEqual(self.state(),before)
        with db.connect() as conn:
            data = json.loads(conn.execute('SELECT input_snapshot_json FROM investment_memo_versions WHERE id=?',(created['id'],)).fetchone()[0])
        self.assertEqual(data['source']['name'],'Before')
        self.assertIsNone(data['papers'][0]['team'])

    def test_idempotency_concurrent_submissions_one_version_even_after_unfavorite(self):
        paper = self.favorite()
        command = self.command([paper])
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _:memos.create_memo_version(command),range(4)))
        self.assertEqual(len({r['id'] for r in results}),1)
        self.assertEqual(sum(r['created'] for r in results),1)
        db.set_paper_decision(paper,'clear')
        self.assertFalse(memos.create_memo_version(command)['created'])
        self.assertEqual(len(self.state()['jobs']),1)

    def test_atomic_failure_rolls_back_series_version_papers_job(self):
        paper = self.favorite()
        before = self.state()
        with patch.object(db,'_insert_job_event',side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                memos.create_memo_version(self.command([paper]))
        self.assertEqual(self.state(),before)

    def test_series_identity_archive_and_monotonic_failed_versions(self):
        paper = self.favorite()
        first = memos.create_memo_version(self.command([paper]))
        with db.connect() as conn:
            conn.execute("UPDATE investment_memo_versions SET status='failed' WHERE id=?",(first['id'],))
        second = memos.create_memo_version(self.command([paper],series_id=first['series_id'],idempotency_key='foundation-key-0002'))
        series,versions = memo_db.get_series(first['series_id'])
        self.assertEqual([v['version_no'] for v in versions],[2,1])
        self.assertNotEqual(first['id'],second['id'])
        with self.assertRaises(FormValidationError):
            memos.MemoRequest.from_form({'series_id':series['id'],'title':'Tamper','paper_ids':[paper]})
        with db.connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE investment_memo_series SET title='Tamper' WHERE id=?",(series['id'],))
            conn.execute("UPDATE investment_memo_series SET status='archived' WHERE id=?",(series['id'],))
        with self.assertRaises(memo_db.MemoConflictError):
            memos.create_memo_version(self.command([paper],series_id=series['id'],idempotency_key='foundation-key-0003'))

    def test_archived_source_blocks_new_version_but_keeps_history(self):
        paper,direction = self.favorite(),db.create_attention_direction('Source','Scope')
        first = memos.create_memo_version(self.command([paper],source_mode='attention_direction',source_id=direction))
        db.archive_attention_direction(direction)
        with self.assertRaises(memo_db.MemoConflictError):
            memos.create_memo_version(self.command([paper],series_id=first['series_id'],idempotency_key='foundation-key-0002'))
        self.assertEqual(len(memo_db.get_series(first['series_id'])[1]),1)

    def test_context_missing_config_and_disabled_explicit_profile_block(self):
        paper = self.favorite()
        profile = db.get_llm_profile(self.profile_id)
        db.save_llm_profile({**profile,'context_window_tokens':100})
        with self.assertRaises(memo_db.MemoConflictError):
            memos.create_memo_version(self.command([paper]))
        self.assertEqual(len(self.state()['jobs']),0)
        db.save_llm_profile({**profile,'enabled':0})
        with self.assertRaises(memo_db.MemoConflictError):
            memos.resolve_memo_config(profile_id=self.profile_id)

    def test_query_count_fixed_for_many_selected_papers(self):
        ids = [self.favorite(i) for i in range(1,31)]
        statements = []
        with db.connect() as conn:
            conn.set_trace_callback(statements.append)
            memo_db.paper_snapshots(conn,ids)
        self.assertEqual(sum(sql.lstrip().upper().startswith('SELECT') for sql in statements),6)

    def test_schema_migration_idempotent_and_snapshots_survive_source_cleanup(self):
        paper = self.favorite()
        direction = db.create_attention_direction('Source','Scope')
        first = memos.create_memo_version(self.command([paper],source_mode='attention_direction',source_id=direction))
        before = self.state()
        db.init_db()
        self.assertEqual(self.state(),before)
        with db.connect() as conn:
            conn.execute('DELETE FROM evaluations WHERE paper_id=?',(paper,))
            conn.execute('DELETE FROM papers WHERE id=?',(paper,))
            conn.execute('DELETE FROM attention_directions WHERE id=?',(direction,))
            saved = conn.execute('SELECT * FROM investment_memo_version_papers').fetchone()
            self.assertIsNone(saved['paper_id'])
            self.assertIsNone(saved['fulltext_evaluation_id'])
            self.assertIn('Library Paper 1',saved['paper_snapshot_json'])
        self.assertIsNone(memo_db.get_series(first['series_id'])[0]['source_direction_id'])

    def test_editor_and_preview_routes_render_and_do_not_create_tasks(self):
        paper = self.favorite()
        for path in ('/investment-memos','/investment-memos/new','/investment-memos/new?query=Library&sort=score_desc'):
            self.assertEqual(self.client.get(path).status_code,200)
        response = self.client.post('/investment-memos/preview',data={'csrf_token':'test-library','title':'Preview',
            'source_mode':'manual','paper_ids':str(paper)})
        self.assertEqual(response.status_code,200)
        self.assertIn('确认生成新版本',response.get_data(as_text=True))
        self.assertIn('全文评估 #',response.get_data(as_text=True))
        self.assertEqual(self.state()['jobs'],[])
        self.llm.assert_not_called()

    def test_create_route_csrf_idempotency_unknown_and_series_render(self):
        paper = self.favorite()
        values = {'title':'Route version','source_mode':'manual','paper_ids':str(paper),'idempotency_key':'route-memo-key-01'}
        self.assertEqual(self.client.post('/investment-memos',data=values).status_code,403)
        values['csrf_token']='test-library'
        self.assertEqual(self.client.post('/investment-memos',data=values,headers={'Origin':'https://evil.example'}).status_code,403)
        response = self.client.post('/investment-memos',data=values)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.client.post('/investment-memos',data=values).location,response.location)
        self.assertEqual(self.runner.queue.qsize(),1)
        self.assertEqual(self.client.get(response.location).status_code,200)
        self.assertEqual(self.client.get('/investment-memos/1').status_code,200)
        self.assertEqual(self.client.get('/investment-memos/999').status_code,404)
        self.assertEqual(self.client.get('/investment-memos/1/versions/999').status_code,404)
        self.llm.assert_not_called()

    def test_default_memo_profile_migration_and_no_fallback(self):
        with db.connect_llm_profiles() as conn:
            conn.execute('DROP INDEX idx_profiles_memo')
            conn.execute('ALTER TABLE llm_profiles DROP COLUMN is_default_memo')
        db.init_llm_profiles_db()
        self.assertEqual(db.get_llm_profile(self.profile_id)['is_default_memo'],0)
        self.assertIsNone(db.get_default_llm_profile('investment_memo'))
        with self.assertRaises(memo_db.MemoConflictError):
            memos.resolve_memo_config()


if __name__ == '__main__':
    unittest.main()
