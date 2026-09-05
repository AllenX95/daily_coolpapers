import json
import sqlite3
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from daily_coolpapers import db, memo_db, memos, services
from daily_coolpapers.llm import LLMResponse
from tests.test_memo_foundation import MemoFoundationTests
from tests.test_memo_generation import valid_result


class MemoVersionWorkflowTests(unittest.TestCase):
    setUp = MemoFoundationTests.setUp
    paper = MemoFoundationTests.paper
    evaluate = MemoFoundationTests.evaluate
    favorite = MemoFoundationTests.favorite
    command = MemoFoundationTests.command

    def generated(self,**changes):
        created = memos.create_memo_version(self.command([self.favorite()],**changes))
        with patch.object(services,'make_llm_client',return_value=nullcontext(None)), \
             patch.object(services,'call_llm',return_value=LLMResponse('{}',valid_result())):
            memos.generate_memo(created['job_id'],created['id'])
        return created

    def test_personal_judgment_only_field_changes_no_job_or_llm(self):
        created = self.generated()
        before = memo_db.get_version(created['series_id'],created['id'])[1]
        jobs_before = db.get_job(created['job_id'])
        with patch.object(services,'call_llm') as call:
            memo_db.save_personal_judgment(created['series_id'],created['id'],'## 判断\n\n- 待验证')
        after = memo_db.get_version(created['series_id'],created['id'])[1]
        call.assert_not_called()
        for key in ('result_json','rendered_markdown','input_snapshot_json','prompt_snapshot','finished_at'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['personal_judgment_markdown'],'## 判断\n\n- 待验证')
        self.assertEqual(db.get_job(created['job_id'])['status'],jobs_before['status'])

    def test_personal_judgment_route_csrf_allowlist_and_version_ownership(self):
        first,second = self.generated(),self.generated(idempotency_key='version-workflow-02')
        url = f"/investment-memos/{first['series_id']}/versions/{first['id']}/personal-judgment"
        self.assertEqual(self.client.post(url,data={'personal_judgment_markdown':'x'}).status_code,403)
        self.assertEqual(self.client.post(url,data={'csrf_token':'test-library','personal_judgment_markdown':'x','result_json':'tamper'}).status_code,400)
        self.assertEqual(self.client.post(url,data={'csrf_token':'test-library','personal_judgment_markdown':'hello'}).status_code,302)
        mismatch=f"/investment-memos/{second['series_id']}/versions/{first['id']}/personal-judgment"
        self.assertEqual(self.client.post(mismatch,data={'csrf_token':'test-library','personal_judgment_markdown':'x'}).status_code,404)

    def test_new_version_prefills_old_order_config_and_revalidates(self):
        first,second = self.favorite(),self.favorite(2)
        created = memos.create_memo_version(self.command([first,second]))
        path=f"/investment-memos/{created['series_id']}/versions/{created['id']}/new-version"
        response=self.client.get(path)
        html=response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertIn(f'value="{first}" checked',html)
        self.assertIn(f'value="{second}" checked',html)
        self.assertEqual(len(db.list_jobs()),1)
        db.set_paper_decision(second,'clear')
        html=self.client.get(path).get_data(as_text=True)
        self.assertIn('当前资格或筛选条件未被预选',html)
        self.assertNotIn(f'value="{second}" checked',html)

    def test_copy_judgment_to_new_version_not_model_input_and_old_unchanged(self):
        first=self.generated()
        memo_db.save_personal_judgment(first['series_id'],first['id'],'PRIVATE JUDGMENT')
        paper=memo_db.get_version(first['series_id'],first['id'])[2][0]['paper_id']
        command=self.command([paper],series_id=first['series_id'],previous_version_id=first['id'],copy_judgment=True,
                             idempotency_key='copy-judgment-key-2')
        second=memos.create_memo_version(command)
        _,version,_=memo_db.get_version(second['series_id'],second['id'])
        self.assertEqual(version['personal_judgment_markdown'],'PRIVATE JUDGMENT')
        self.assertNotIn('PRIVATE JUDGMENT',version['input_snapshot_json'])
        self.assertNotIn('PRIVATE JUDGMENT',version['prompt_snapshot'])
        memo_db.save_personal_judgment(second['series_id'],second['id'],'CHANGED')
        self.assertEqual(memo_db.get_version(first['series_id'],first['id'])[1]['personal_judgment_markdown'],'PRIVATE JUDGMENT')

    def test_archive_idempotent_blocks_new_versions_but_history_and_export_work(self):
        created=self.generated()
        path=f"/investment-memos/{created['series_id']}/archive"
        self.assertEqual(self.client.post(path,data={'csrf_token':'test-library'}).status_code,302)
        self.assertEqual(self.client.post(path,data={'csrf_token':'test-library'}).status_code,302)
        self.assertEqual(memo_db.get_series(created['series_id'])[0]['status'],'archived')
        self.assertEqual(self.client.get(f"/investment-memos/{created['series_id']}").status_code,200)
        self.assertEqual(self.client.get(f"/investment-memos/{created['series_id']}/versions/{created['id']}/export.md").status_code,200)
        self.assertEqual(self.client.get(f"/investment-memos/{created['series_id']}/versions/{created['id']}/new-version").status_code,409)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as conn:
                conn.execute("UPDATE investment_memo_series SET status='active' WHERE id=?",(created['series_id'],))

    def test_export_exact_order_stored_ai_personal_evidence_config_and_no_llm(self):
        created=self.generated()
        judgment='## 自己的判断\n\n<script>alert(1)</script>\n[bad](javascript:alert(1))'
        memo_db.save_personal_judgment(created['series_id'],created['id'],judgment)
        with patch.object(services,'call_llm') as call:
            first=memos.export_memo(created['series_id'],created['id'])
            second=memos.export_memo(created['series_id'],created['id'])
        call.assert_not_called()
        self.assertEqual(first,second)
        positions=[first.index(token) for token in (memos.DISCLAIMER,'## 1. 核心结论','## 我的投资判断','## 13. 论文证据索引','## 生成配置摘要')]
        self.assertEqual(positions,sorted(positions))
        self.assertNotIn('<script>',first)
        self.assertNotIn('](javascript:',first)
        self.assertIn('/papers/1',first)
        response=self.client.get(f"/investment-memos/{created['series_id']}/versions/{created['id']}/export.md")
        self.assertEqual(response.mimetype,'text/markdown')
        self.assertIn('attachment;',response.headers['Content-Disposition'])
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')

    def test_failed_export_409_unknown_paths_404_and_archive_allowlist(self):
        created=memos.create_memo_version(self.command([self.favorite()]))
        self.assertEqual(self.client.get(f"/investment-memos/{created['series_id']}/versions/{created['id']}/export.md").status_code,409)
        self.assertEqual(self.client.get('/investment-memos/999/versions/1/export.md').status_code,404)
        self.assertEqual(self.client.post(f"/investment-memos/{created['series_id']}/archive",data={'csrf_token':'test-library','status':'active'}).status_code,400)

    def test_direction_rejection_is_not_frozen_or_sent_as_positive_evidence(self):
        paper=self.favorite()
        direction=db.create_attention_direction('Rejected evidence','scope')
        with db.connect() as conn:
            conn.execute("INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,manual_decision,created_at,updated_at) VALUES (?,?,'matched','rejected','t','t')",(paper,direction))
        created=memos.create_memo_version(self.command([paper]))
        version=memo_db.get_version(created['series_id'],created['id'])[1]
        self.assertEqual(version['input_snapshot']['papers'][0]['directions'],[])
        self.assertNotIn('Rejected evidence',version['prompt_snapshot'])

    def test_series_list_shows_frozen_source_name_after_source_change_or_delete(self):
        paper=self.favorite()
        theme=db.create_investment_theme('Frozen source','scope')
        db.set_paper_investment_themes(paper,[theme])
        created=memos.create_memo_version(self.command([paper],source_mode='investment_theme',source_id=theme))
        db.update_investment_theme(theme,'update','New source','new')
        self.assertEqual(memo_db.list_series()[0]['source_name'],'Frozen source')
        with db.connect() as conn:
            conn.execute('DELETE FROM paper_investment_themes WHERE theme_id=?',(theme,))
            conn.execute('DELETE FROM investment_themes WHERE id=?',(theme,))
        self.assertIn('Frozen source',self.client.get('/investment-memos?archived=1').get_data(as_text=True))

    def test_reference_foreign_keys_cannot_be_repointed_but_set_null_cleanup_works(self):
        first=self.generated()
        second_paper=self.favorite(2)
        second=memos.create_memo_version(self.command([second_paper],idempotency_key='fk-guard-version-02'))
        _,v2,p2=memo_db.get_version(second['series_id'],second['id'])
        other_prompt=db.save_prompt({'name':'Other memo','type':'investment_memo','template':'Other {title} {source_name}','enabled':1,'set_default':0})
        with db.connect() as conn:
            for sql,args in [
                ('UPDATE investment_memo_versions SET prompt_id=? WHERE id=?',(other_prompt,first['id'])),
                ('UPDATE investment_memo_version_papers SET paper_id=? WHERE memo_version_id=?',(p2[0]['paper_id'],first['id'])),
                ('UPDATE investment_memo_version_papers SET fulltext_evaluation_id=? WHERE memo_version_id=?',(p2[0]['fulltext_evaluation_id'],first['id']))]:
                with self.assertRaises(sqlite3.IntegrityError): conn.execute(sql,args)
            conn.execute('DELETE FROM papers WHERE id=?',(p2[0]['paper_id'],))
        self.assertIsNone(memo_db.get_version(second['series_id'],second['id'])[2][0]['paper_id'])


if __name__=='__main__':
    unittest.main()
