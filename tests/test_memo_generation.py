import copy
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import patch

from daily_coolpapers import db, memo_db, memos, services
from daily_coolpapers import memo_contract as contract
from daily_coolpapers.llm import LLMResponse, LLMError, LLMHTTPError
from tests import test_memo_foundation as foundation


def valid_result(refs=None):
    refs = refs if refs is not None else ['P1']
    return {'schema_version':'investment_memo.v1','language':'zh-CN',
        'sections':{key:{'status':'supported','summary':'基于论文的研究假设。',
            'claims':[{'claim_text':'需要继续验证的技术观察。','claim_type':'cross_paper_inference',
                       'evidence_refs':list(refs),'reasoning':'论文提供技术线索，不构成商业核验。'}]} for key in contract.SECTIONS},
        'diligence_questions':[{'question_text':'技术能否独立复现？','reason':'当前未做复现实验。','evidence_refs':list(refs)}]}


class MemoGenerationTests(unittest.TestCase):
    setUp = foundation.MemoFoundationTests.setUp
    paper = foundation.MemoFoundationTests.paper
    evaluate = foundation.MemoFoundationTests.evaluate
    favorite = foundation.MemoFoundationTests.favorite
    command = foundation.MemoFoundationTests.command
    state = foundation.MemoFoundationTests.state

    def create(self,**changes):
        return memos.create_memo_version(self.command([self.favorite()],**changes))

    def run_version(self,created,result=None,error=None):
        response = LLMResponse(json.dumps(result or valid_result(),ensure_ascii=False),result if result is not None else valid_result(),
                               {'prompt_tokens':123,'completion_tokens':45})
        with patch.object(services,'make_llm_client',return_value=nullcontext(None)), \
             patch.object(services,'call_llm',return_value=response,side_effect=error) as call:
            outcome = memos.generate_memo(created['job_id'],created['id'])
        return outcome,call

    def get(self,created):
        return memo_db.get_version(created['series_id'],created['id'])[1]

    def test_success_atomic_job_version_markdown_index_and_usage(self):
        created = self.create()
        outcome,call = self.run_version(created)
        version = self.get(created)
        self.assertEqual(outcome['status'],'success')
        self.assertEqual(version['status'],db.get_job(created['job_id'])['status'])
        self.assertEqual(version['input_tokens'],123)
        self.assertEqual(version['output_tokens'],45)
        self.assertEqual(version['result']['disclaimer'],contract.DISCLAIMER)
        self.assertEqual(version['result']['evidence_index'][0]['title'],'Library Paper 1')
        self.assertIn('## 13. 论文证据索引',version['rendered_markdown'])
        self.assertEqual(call.call_count,1)
        self.assertFalse(call.call_args.args[0]['allow_response_format_fallback'])
        self.assertIn('JSON Schema',call.call_args.args[0]['system_prompt'])
        self.assertEqual(self.client.get(f"/investment-memos/{created['series_id']}/versions/{created['id']}").status_code,200)
        self.assertEqual(self.client.get(f"/jobs/{created['job_id']}").status_code,200)

    def test_duplicate_and_concurrent_dispatch_only_one_call(self):
        created = self.create()
        with patch.object(services,'make_llm_client',return_value=nullcontext(None)), \
             patch.object(services,'call_llm',return_value=LLMResponse('{}',valid_result())) as call:
            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(lambda _:memos.generate_memo(created['job_id'],created['id']),range(4)))
        self.assertEqual(call.call_count,1)
        self.assertEqual(sum(o['status']=='success' for o in outcomes),1)
        self.assertEqual(memos.generate_memo(created['job_id'],created['id'])['status'],'skipped')

    def test_snapshot_used_even_after_profile_prompt_metadata_and_membership_changes(self):
        created = self.create()
        before = self.get(created)
        profile = db.get_llm_profile(self.profile_id)
        db.save_llm_profile({**profile,'model':'changed-model','temperature':0.9,'max_output_tokens':1})
        with db.connect() as conn:
            conn.execute("UPDATE prompts SET template='changed prompt' WHERE id=?",(before['prompt_id'],))
            conn.execute("UPDATE papers SET title='new title'")
        db.set_paper_decision(1,'clear')
        _,call = self.run_version(created)
        self.assertEqual(call.call_args.args[1],before['prompt_snapshot'])
        self.assertEqual(call.call_args.args[0]['model'],'memo-fixture')
        self.assertEqual(call.call_args.args[0]['max_output_tokens'],4000)
        self.assertEqual(self.get(created)['status'],'success')

    def test_disabled_or_changed_connection_never_calls_model(self):
        created = self.create()
        profile = db.get_llm_profile(self.profile_id)
        db.save_llm_profile({**profile,'enabled':0})
        _,call = self.run_version(created)
        call.assert_not_called()
        version = self.get(created)
        self.assertEqual(version['error_code'],'memo_config_invalid')
        self.assertEqual(version['provider_started'],0)

    def test_invalid_schema_no_partial_draft_one_call_raw_preserved(self):
        created = self.create()
        _,call = self.run_version(created,{'unexpected':'raw-only'})
        version = self.get(created)
        self.assertEqual(call.call_count,1)
        self.assertEqual(version['status'],'failed')
        self.assertEqual(version['error_code'],'memo_schema_invalid')
        self.assertIsNone(version['rendered_markdown'])
        self.assertIsNone(version['result_json'])
        self.assertIn('raw-only',version['raw_output'])
        self.assertEqual(db.get_job(created['job_id'])['status'],'failed')

    def test_non_json_and_unknown_evidence_fail_differently(self):
        for i,result,code in [(1,None,'memo_json_invalid'),(2,valid_result(['P2']),'memo_evidence_invalid')]:
            created = memos.create_memo_version(self.command([self.favorite(i)],idempotency_key=f'json-invalid-key-{i:02}'))
            with patch.object(services,'make_llm_client',return_value=nullcontext(None)), \
                 patch.object(services,'call_llm',return_value=LLMResponse('raw',result)) as call:
                memos.generate_memo(created['job_id'],created['id'])
            self.assertEqual(call.call_count,1)
            self.assertEqual(self.get(created)['error_code'],code)

    def test_transport_auth_provider_failures_redacted_without_retries(self):
        cases = [(LLMError('secret://hidden',code='transport_error',retryable=True),'memo_transport_error'),
                 (LLMHTTPError('openai',401,'private-key',retryable=False),'memo_auth_error'),
                 (LLMHTTPError('openai',500,'private-key',retryable=True),'memo_provider_error')]
        for i,(error,code) in enumerate(cases,1):
            created = memos.create_memo_version(self.command([self.favorite(i)],idempotency_key=f'error-test-key-{i:02}'))
            _,call = self.run_version(created,error=error)
            self.assertEqual(call.call_count,1)
            version = self.get(created)
            self.assertEqual(version['error_code'],code)
            self.assertNotIn('private-key',version['error_message'])
            self.assertNotIn('secret://',version['error_message'])

    def test_terminal_write_failure_rolls_back_success_and_marks_failed_without_recall(self):
        created = self.create()
        original = db._insert_job_event
        def inject(conn,event):
            if event['event_type']=='investment_memo.generation_succeeded':
                raise sqlite3.OperationalError('synthetic')
            return original(conn,event)
        with patch.object(db,'_insert_job_event',side_effect=inject):
            _,call = self.run_version(created)
        self.assertEqual(call.call_count,1)
        version = self.get(created)
        self.assertEqual(version['status'],'failed')
        self.assertEqual(version['error_code'],'memo_database_error')
        self.assertIsNone(version['result_json'])
        self.assertEqual(db.get_job(created['job_id'])['status'],'failed')

    def test_unwritable_terminal_then_recovery_unknown_outcome_no_recall(self):
        created = self.create()
        with patch.object(memo_db,'finish_generation',side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_version(created)
        self.assertEqual(self.get(created)['status'],'running')
        self.assertEqual(db.mark_unfinished_jobs_interrupted(),1)
        version = self.get(created)
        self.assertEqual(version['status'],'interrupted')
        self.assertEqual(version['error_code'],'external_outcome_unknown')
        self.assertEqual(db.get_job(created['job_id'])['status'],'interrupted')
        self.assertIn('可能已产生一次费用',version['error_message'])
        self.assertEqual(db.mark_unfinished_jobs_interrupted(),0)
        self.assertEqual(memos.generate_memo(created['job_id'],created['id'])['status'],'skipped')

    def test_pending_and_running_recovery_atomic_and_idempotent(self):
        created = self.create()
        self.assertEqual(db.mark_unfinished_jobs_interrupted(),1)
        self.assertEqual(self.get(created)['error_code'],'memo_interrupted')
        self.assertEqual(self.get(created)['status'],db.get_job(created['job_id'])['status'])
        before = self.state()
        self.assertEqual(db.mark_unfinished_jobs_interrupted(),0)
        self.assertEqual(self.state(),before)

    def test_success_ai_content_cannot_be_overwritten(self):
        created = self.create()
        self.run_version(created)
        for field,value in [('result_json','{}'),('rendered_markdown','tamper'),('status','pending'),('prompt_snapshot','tamper'),('input_snapshot_json','{}')]:
            with db.connect() as conn, self.assertRaises(sqlite3.IntegrityError):
                conn.execute(f'UPDATE investment_memo_versions SET {field}=? WHERE id=?',(value,created['id']))
        with self.assertRaises(memo_db.MemoConflictError):
            memo_db.finish_generation(created['id'],error_code='tamper')

    def test_recovery_repairs_lagging_job_to_terminal_version(self):
        created = self.create()
        self.run_version(created)
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?",(created['job_id'],))
        db.mark_unfinished_jobs_interrupted()
        self.assertEqual(db.get_job(created['job_id'])['status'],'success')
        self.assertEqual(self.get(created)['status'],'success')

    def test_worker_dispatch_keeps_atomic_terminal(self):
        created = self.create()
        with patch.object(services,'make_llm_client',return_value=nullcontext(None)), \
             patch.object(services,'call_llm',return_value=LLMResponse('{}',valid_result())) as call:
            self.runner._run_job(created['job_id'])
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.get(created)['status'],'success')
        self.assertEqual(db.get_job(created['job_id'])['status'],'success')


class MemoContractTests(unittest.TestCase):
    def papers(self):
        return [{'paper':{'id':7,'title':'<script>alert(1)</script>','arxiv_id':'2609.00001'},
                 'abstract_evaluation':None,'fulltext_evaluation':{'id':9}}]

    def test_fixed_schema_and_generated_index(self):
        result = contract.validate_memo_result(valid_result(),self.papers())
        self.assertEqual(len(result['sections']),11)
        self.assertEqual(result['evidence_index'][0]['local_url'],'/papers/7')
        self.assertNotIn('<script>',contract.render_evidence_markdown(result['evidence_index']))

    def test_schema_failure_matrix(self):
        invalid = []
        def change(fn):
            result = valid_result()
            fn(result)
            invalid.append(result)
        change(lambda r:r.pop('language'))
        change(lambda r:r.update(extra='forbidden'))
        change(lambda r:r.update(evidence_index=[]))
        change(lambda r:r.update(disclaimer='tamper'))
        change(lambda r:r['sections'].pop('risks'))
        change(lambda r:r['sections']['risks'].update(extra='forbidden'))
        change(lambda r:r['sections']['risks'].update(summary=' \n\t'))
        change(lambda r:r['sections']['risks'].update(status=None))
        change(lambda r:r['sections']['risks'].update(claims=[]))
        change(lambda r:r['sections']['risks']['claims'][0].update(claim_text=''))
        change(lambda r:r['sections']['risks']['claims'][0].update(reasoning=None))
        change(lambda r:r['sections']['risks']['claims'][0].update(evidence_refs=[]))
        change(lambda r:r['sections']['risks']['claims'][0].update(evidence_refs=['P1','P1']))
        change(lambda r:r['sections']['risks']['claims'][0].update(evidence_refs=['P0']))
        change(lambda r:r['sections']['risks']['claims'][0].update(evidence_refs=[1]))
        change(lambda r:r['sections']['china_market_hypotheses']['claims'][0].update(claim_type='paper_fact'))
        change(lambda r:r.update(diligence_questions=[]))
        change(lambda r:r['diligence_questions'][0].pop('reason'))
        change(lambda r:r['diligence_questions'][0].update(evidence_refs=['P1','P1']))
        for i,result in enumerate(invalid):
            with self.subTest(case=i), self.assertRaises(contract.MemoOutputError) as caught:
                contract.validate_memo_result(result,self.papers())
            self.assertEqual(caught.exception.code,'memo_schema_invalid')

    def test_insufficient_evidence_permits_explicit_empty_claims_and_refs(self):
        result = valid_result()
        result['sections']['risks'] = {'status':'insufficient_evidence','summary':'缺少证据','claims':[]}
        result['sections']['technical_scope']['claims'][0].update(claim_type='insufficient_evidence',evidence_refs=[])
        result['diligence_questions'][0]['evidence_refs']=[]
        self.assertTrue(contract.validate_memo_result(result,self.papers()))

    def test_out_of_range_reference_in_diligence_rejected(self):
        result = valid_result()
        result['diligence_questions'][0]['evidence_refs']=['P2']
        with self.assertRaises(contract.MemoOutputError) as caught:
            contract.validate_memo_result(result,self.papers())
        self.assertEqual(caught.exception.code,'memo_evidence_invalid')


if __name__=='__main__':
    unittest.main()
