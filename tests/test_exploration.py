import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

import httpx
from daily_coolpapers import db,exploration as explore,exploration_db as store,llm
from daily_coolpapers.form_commands import FormValidationError
from tests import test_personal_library as library

NOW=datetime(2026,10,8,12,tzinfo=explore.SHANGHAI_TZ)


class ExplorationTests(unittest.TestCase):
    setUp=library.PersonalLibraryTests.setUp

    def papers(self,count=40,category='cs.AI',offset=0):
        rows=[{'arxiv_id':f'2610.{i+offset:05}','title':f'Paper {i+offset}',
            'abstract':'A new capability with controlled experiments.','authors':['Ada' if i%2 else 'Bob'],
            'subjects':[category],'reading_stars':i,'pdf_clicks':i,'kimi_clicks':0,'rank':i}
            for i in range(1,count+1)]
        ids=db.upsert_papers(rows,category,'2026-10-07')
        with db.connect() as conn:
            conn.execute("UPDATE papers SET created_at='2026-10-07T04:00:00+00:00'")
        return ids

    def configure(self):
        key=db.save_llm_profile({'name':'Explore','provider':'openai_compatible','base_url':'https://fixture.invalid/v1',
            'model':'fixture','enabled':True,'context_window_tokens':65536,'max_output_tokens':9999})
        db.save_settings({'exploration.profile_id':key})
        return key

    def create(self):
        plan=explore.build_plan(NOW)
        return store.create(plan)

    def extract(self,papers):
        return {'papers':[{'paper_id':p['id'],'problem':'new problem','method':'method','capability':'capability','limitations':'unverified'} for p in papers],'groups':[]}

    def synthesis(self,ids,**overrides):
        c={'name':'New research','scope_text':'A specific new capability and boundary.','signal_type':'multi_paper',
           'paper_ids':ids[:2],'why_now':'Two research approaches','difference_from_existing':'new boundary',
           'evidence_gaps':'Requires replication','prior_candidate_ref':None,'relation_to_prior':'new','new_evidence':''}
        c.update(overrides)
        return {'candidates':[c],'summary':'A direction worth watching.'}

    def execute(self,run_id,job_id,responder=None):
        plan=store.get(run_id)['plan']
        calls=[]
        def response(request):
            body=json.loads(request.content);calls.append(body)
            if responder:return responder(body,len(calls),request)
            if body['max_tokens']==1000:
                raw=body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0]
                result=self.extract(json.loads(raw))
            else:
                result=self.synthesis([p['id'] for p in plan['papers']])
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}],
                'usage':{'prompt_tokens':30,'completion_tokens':15,'total_tokens':45}},request=request)
        with patch.object(llm,'_api_key',return_value='fixture'),patch.object(llm,'make_llm_client',
            side_effect=lambda _:httpx.Client(transport=httpx.MockTransport(response))),patch.object(llm.time,'sleep'):
            self.runner._run_job(job_id)
        return calls

    def test_migration_idempotent_and_prompt_registration(self):
        db.init_db()
        self.assertEqual(len([p for p in db.list_prompts() if p['type'] in explore.DEFAULT_PROMPTS]),2)
        self.assertIn('新方向探索',self.client.get('/direction-exploration').get_data(as_text=True))
        self.assertEqual(self.client.get('/direction-exploration?page=0').status_code,400)

    def test_seven_full_days_and_deterministic_no_direction_split(self):
        self.papers(60);self.configure()
        first=explore.build_plan(NOW);second=explore.build_plan(NOW)
        self.assertEqual((first['date_from'],first['date_to']),('2026-10-01','2026-10-07'))
        self.assertEqual(first,second)
        self.assertEqual(first['sample_counts'],{'heat':30,'random':10})
        self.assertEqual(len({p['arxiv_id'] for p in first['papers']}),40)
        self.assertEqual(first['logical_calls'],5)

    def test_timezone_boundaries_missing_and_failed_classification(self):
        ids=self.papers(5);self.configure()
        with db.connect() as conn:
            conn.execute("UPDATE papers SET created_at='2026-09-30T16:00:00+00:00' WHERE id=?",(ids[0],))
            conn.execute("UPDATE papers SET created_at='2026-09-30T15:59:59+00:00' WHERE id=?",(ids[1],))
            conn.execute("UPDATE papers SET created_at='2026-10-07T16:00:00+00:00' WHERE id=?",(ids[2],))
            conn.execute("UPDATE papers SET abstract='' WHERE id=?",(ids[3],))
        plan=explore.build_plan(NOW)
        self.assertEqual({p['id'] for p in plan['papers']},{ids[0],ids[4]})
        db.create_attention_direction('Known','existing')
        plan=explore.build_plan(NOW)
        self.assertEqual(plan['papers'],[])
        self.assertEqual(plan['counts']['classification_pending'],2)

    def test_20_10_10_with_manual_overrides_and_cross_category_dedup(self):
        ids=self.papers(60);self.configure()
        direction=db.create_attention_direction('Known','existing')
        with db.connect() as conn:
            for i,pid in enumerate(ids):
                conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,created_at,updated_at)
                    VALUES (?,?,?,?,?)''',(pid,direction,'unmatched' if i<45 else 'possible',db.now_iso(),db.now_iso()))
        db.set_direction_decision(ids[-1],direction,'rejected')
        plan=explore.build_plan(NOW)
        self.assertEqual(plan['sample_counts'],{'heat':20,'random':10,'edge':10})
        self.assertTrue(all(p['state']=='possible' for p in plan['papers'] if p['sample_group']=='edge'))

    def test_empty_pool_zero_requests_even_without_config(self):
        run_id,job_id,_=self.create()
        with patch.object(llm,'call_llm',side_effect=AssertionError('forbidden')):
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        self.assertEqual(store.get(run_id)['execution']['coverage'],0)

    def test_preview_config_failure_blocks_enqueue_zero_jobs(self):
        self.papers(1)
        with patch.object(explore,'datetime') as clock:
            clock.now.return_value=NOW;clock.combine=datetime.combine;clock.min=datetime.min
            with self.assertRaises(FormValidationError):self.runner.enqueue_exploration()
        self.assertEqual(db.list_jobs(),[])

    def test_five_calls_checkpoint_usage_and_no_business_side_effects(self):
        self.papers();self.configure();run_id,job_id,_=self.create()
        calls=self.execute(run_id,job_id)
        run=store.get(run_id)
        self.assertEqual(len(calls),5)
        self.assertEqual(run['execution']['requests'],5)
        self.assertEqual(run['execution']['usage']['input_tokens'],150)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        self.assertEqual(len(run['candidates']),1)
        self.assertTrue(all('reading_stars' not in c['messages'][1]['content'] for c in calls))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_direction_results').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_cache_artifacts').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM llm_call_attempts WHERE job_id=?',(job_id,)).fetchone()[0],5)
        again=store.create(run['plan']);self.assertFalse(again[2])
        self.assertEqual(explore.build_plan(NOW)['papers'],[])
        html=self.client.get(f'/direction-exploration/{run_id}').get_data(as_text=True)
        self.assertIn('New research',html)
        self.assertIn('作者重叠',html) if len({p['authors'][0] for p in run['plan']['papers'][:2]})==1 else None
        self.assertNotIn('encrypted_api_key_ref',json.dumps(run))

    def test_invalid_extract_atomic_and_empty_synthesis_allowed(self):
        papers=[{'id':1},{'id':2}]
        for result in ({},self.extract(papers[:1]),self.extract([papers[0],papers[0]]),self.extract([{'id':3},papers[1]])):
            with self.subTest(result=result),self.assertRaises(llm.LLMError):explore.validate_extract(result,papers)
        self.assertEqual(explore.validate_synthesis({'candidates':[],'summary':'No new evidence.'},papers,[])['candidates'],[])
        invalid=self.synthesis([1,2]);invalid['candidates'][0]['paper_ids']=[1,999]
        with self.assertRaises(llm.LLMError):explore.validate_synthesis(invalid,papers,[])
        invalid=self.synthesis([1,2],signal_type='single_paper')
        with self.assertRaises(llm.LLMError):explore.validate_synthesis(invalid,papers,[])

    def test_physical_budget_covers_provider_concurrency_retries(self):
        self.papers(10);self.configure();run_id,job_id,_=self.create()
        def limited(body,n,request):return httpx.Response(429,json={'error':{'message':'concurrency limit'}},request=request)
        calls=self.execute(run_id,job_id,limited)
        self.assertEqual(len(calls),2)
        self.assertEqual(store.get(run_id)['execution']['requests'],2)
        self.assertEqual(db.get_job(job_id)['status'],'failed')
        self.assertNotIn('consumed',store.get(run_id)['execution'])

    def test_bad_json_retries_only_twice_and_unmatched_not_consumed(self):
        self.papers(10);self.configure();run_id,job_id,_=self.create()
        def invalid(body,n,request):return httpx.Response(200,json={'choices':[{'message':{'content':'bad json'}}]},request=request)
        self.assertEqual(len(self.execute(run_id,job_id,invalid)),2)
        self.assertEqual(db.get_job(job_id)['status'],'failed')
        self.assertEqual(len(explore.build_plan(NOW)['papers']),10)

    def test_partial_batches_and_recover_synthesis_without_extract_calls(self):
        self.papers(20);self.configure();run_id,job_id,_=self.create();plan=store.get(run_id)['plan']
        def bad_synthesis(body,n,request):
            if body['max_tokens']==1000:
                raw=body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0]
                result=self.extract(json.loads(raw))
                return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]},request=request)
            return httpx.Response(500,json={'error':'fail'},request=request)
        self.assertEqual(len(self.execute(run_id,job_id,bad_synthesis)),4)
        child,child_job,_=store.retry(run_id,synthesis_only=True)
        calls=self.execute(child,child_job)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['max_tokens'],2000)
        self.assertEqual(store.get(child)['retry_of'],run_id)
        self.assertEqual(store.get(child)['execution']['requests'],1)
        self.assertEqual(store.retry(run_id,synthesis_only=True),(child,child_job,False))

    def test_human_states_prefill_atomic_add_and_stale_update(self):
        self.papers(10);self.configure();run_id,job_id,_=self.create();self.execute(run_id,job_id)
        candidate=store.get(run_id)['candidates'][0];cid=candidate['id']
        with self.assertRaises(FormValidationError):store.decide(run_id,cid,'ignored','',0)
        store.decide(run_id,cid,'observing','watch',1)
        with self.assertRaises(FormValidationError):store.decide(run_id,cid,'ignored','not relevant',1)
        run=store.get(run_id)
        store.decide(run_id,cid,'ignored','not relevant',run['version'])
        self.assertEqual(store.prior_candidates()[0]['status'],'ignored')
        prefill=self.client.get(f'/attention-directions?exploration_run={run_id}&candidate_id={cid}')
        self.assertEqual(prefill.status_code,200)
        self.assertEqual(db.list_attention_directions(),[])
        version=store.get(run_id)['version']
        data={'csrf_token':'test-library','exploration_run':run_id,'candidate_id':cid,'version':version,'name':'New focus','scope_text':'edited scope'}
        self.assertEqual(self.client.post('/attention-directions',data=data).status_code,302)
        self.assertEqual(self.client.post('/attention-directions',data=data).status_code,302)
        self.assertEqual(len(db.list_attention_directions()),1)
        self.assertEqual(db.list_attention_directions()[0]['scope_text'],'edited scope')
        self.assertTrue(self.runner.queue.empty())

    def test_prior_suppression_and_new_evidence_reference(self):
        prior=[{'ref':'1:1','id':1,'status':'ignored','paper_ids':[1,2],'evidence_snapshot':[]}]
        result=self.synthesis([1,2],prior_candidate_ref='1:1',relation_to_prior='ignored_repeat')
        self.assertEqual(explore.validate_synthesis(result,[{'id':3}],prior)['suppressed'],1)
        result=self.synthesis([1,3],prior_candidate_ref='1:1',relation_to_prior='ignored_repeat',new_evidence='new measurement')
        validated=explore.validate_synthesis(result,[{'id':3}],prior)
        self.assertEqual(validated['candidates'][0]['new_paper_ids'],[3])
        invalid=self.synthesis([1,3],prior_candidate_ref='99:1',relation_to_prior='ignored_repeat',new_evidence='new')
        with self.assertRaises(llm.LLMError):explore.validate_synthesis(invalid,[{'id':3}],prior)

    def test_context_limits_skip_long_abstract_without_truncation(self):
        ids=self.papers(1);profile_id=self.configure()
        with db.connect() as conn:conn.execute('UPDATE papers SET abstract=? WHERE id=?',('X'*300000,ids[0]))
        plan=explore.build_plan(NOW)
        self.assertEqual(plan['papers'],[])
        self.assertEqual(plan['counts']['context_excluded'],1)
        self.assertEqual(len(db.get_paper(ids[0])['abstract']),300000)

    def test_routes_csrf_profile_and_escaping(self):
        self.assertEqual(self.client.post('/direction-exploration').status_code,403)
        self.assertEqual(self.client.get('/direction-exploration/999').status_code,404)
        profile_id=self.configure()
        self.assertEqual(self.client.post('/direction-exploration/profile',data={'csrf_token':'test-library','profile_id':profile_id}).status_code,302)
        self.assertTrue(self.runner.queue.empty())
        self.assertEqual(self.client.post('/direction-exploration/profile',data={'csrf_token':'test-library','profile_id':999}).status_code,400)
        self.papers(2);run_id,job_id,_=self.create();self.execute(run_id,job_id)
        run=store.get(run_id);run['candidates'][0]['name']='<script>bad</script>'
        with db.connect() as conn:conn.execute('UPDATE direction_exploration_runs SET candidates_json=? WHERE id=?',(json.dumps(run['candidates']),run_id))
        html=self.client.get(f'/direction-exploration/{run_id}').get_data(as_text=True)
        self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>bad',html)

    def test_reservation_survives_failed_stage_and_restart(self):
        self.papers(1);self.configure();run_id,job_id,_=self.create()
        store.save_execution(run_id,{'job_ids':[job_id]})
        store.reserve_request(run_id,'batch_0');store.reserve_request(run_id,'batch_0')
        with self.assertRaises(llm.LLMError):store.reserve_request(run_id,'batch_0')
        db.update_job(job_id,'interrupted')
        _,retry_job,_=store.retry(run_id)
        self.assertEqual(len(self.execute(run_id,retry_job)),0)
        self.assertEqual(store.get(run_id)['execution']['requests'],2)

    def test_all_stages_retry_once_total_ten_requests(self):
        self.papers(40);self.configure();run_id,job_id,_=self.create()
        plan=store.get(run_id)['plan']
        def respond(body,n,request):
            if n%2:return httpx.Response(200,json={'choices':[{'message':{'content':'broken'}}]},request=request)
            if body['max_tokens']==1000:
                papers=json.loads(body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0])
                result=self.extract(papers)
            else:result=self.synthesis([p['id'] for p in plan['papers']])
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]},request=request)
        self.assertEqual(len(self.execute(run_id,job_id,respond)),10)
        self.assertEqual(store.get(run_id)['execution']['requests'],10)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        with self.assertRaises(llm.LLMError):store.reserve_request(run_id,'additional')

    def test_partial_batch_failure_reports_real_coverage(self):
        self.papers(20);self.configure();run_id,job_id,_=self.create();plan=store.get(run_id)['plan']
        def respond(body,n,request):
            if body['max_tokens']==1000:
                papers=json.loads(body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0])
                if papers[0]['id']==plan['papers'][0]['id']:
                    return httpx.Response(500,json={'error':'fixture'},request=request)
                result=self.extract(papers)
            else:result=self.synthesis([p['id'] for p in plan['papers'][10:]])
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]},request=request)
        calls=self.execute(run_id,job_id,respond)
        self.assertEqual(len(calls),4)
        self.assertEqual(db.get_job(job_id)['status'],'partial_success')
        run=store.get(run_id)
        self.assertEqual(run['execution']['coverage'],10)
        self.assertEqual(len(run['execution']['consumed']),10)

    def test_unknown_request_requires_explicit_acknowledgment(self):
        self.papers(10);self.configure();run_id,job_id,_=self.create();self.execute(run_id,job_id)
        with db.connect() as conn:
            conn.execute("UPDATE direction_exploration_runs SET status='failed' WHERE id=?",(run_id,))
            conn.execute("UPDATE jobs SET status='interrupted' WHERE id=?",(job_id,))
            conn.execute("UPDATE llm_call_attempts SET status='external_outcome_unknown' WHERE job_id=?",(job_id,))
        with self.assertRaises(FormValidationError):store.retry(run_id)
        self.assertTrue(store.retry(run_id,acknowledged=True)[2])

    def test_observer_update_tracks_identity_and_blocks_old_actions(self):
        self.papers(10);self.configure();run_id,job_id,_=self.create();self.execute(run_id,job_id)
        run=store.get(run_id);cid=run['candidates'][0]['id']
        store.decide(run_id,cid,'observing','track this',run['version'])
        prior=store.prior_candidates()[0]
        plan={**run['plan'],'date_from':'2026-10-08','date_to':'2026-10-14'}
        next_run,next_job,_=store.create(plan)
        c={**run['candidates'][0],'prior_candidate_ref':prior['ref'],'relation_to_prior':'observing_update','new_evidence':'new test','status':'pending'}
        store.save_execution(next_run,{},'success',[c])
        self.assertEqual(store.get(next_run)['candidates'][0]['status'],'observing')
        self.assertEqual(len(store.prior_candidates()),1)
        self.assertEqual(store.prior_candidates()[0]['ref'],f'{next_run}:{cid}')
        with self.assertRaises(FormValidationError):store.decide(run_id,cid,'ignored','old',store.get(run_id)['version'])

    def test_review_time_and_large_unknown_id(self):
        self.papers(2);self.configure();run_id,job_id,_=self.create();self.execute(run_id,job_id)
        run=store.get(run_id)
        self.assertEqual(self.client.post(f'/direction-exploration/{run_id}/review',data={
            'csrf_token':'test-library','version':run['version'],'minutes':9}).status_code,302)
        self.assertEqual(store.get(run_id)['execution']['review_minutes'],9)
        self.assertEqual(self.client.get(f'/direction-exploration/{2**100}').status_code,404)


if __name__=='__main__':unittest.main()
