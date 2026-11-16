import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

import httpx
from daily_coolpapers import crawler,db,exploration as explore,exploration_db as store,llm,services
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
        with patch.object(db,'utc_now_iso',return_value='2026-10-07T04:00:00+00:00'):
            ids=db.upsert_papers(rows,category,'2026-10-07')
        with db.connect() as conn:
            conn.execute("UPDATE papers SET created_at='2026-10-07T04:00:00+00:00'")
        return ids

    def configure(self, **overrides):
        profile={'name':'Explore','provider':'openai_compatible','base_url':'https://fixture.invalid/v1',
                 'model':'fixture','enabled':True,'context_window_tokens':65536,'max_output_tokens':9999}
        key=db.save_llm_profile({**profile,**overrides})
        db.save_settings({'exploration.profile_id':key})
        return key

    def synthesis_profile(self, context):
        key=db.save_llm_profile({'name':f'Synthesis {context}','provider':'openai_compatible',
            'base_url':'https://fixture.invalid/v1','model':f'synthesis-{context}','enabled':True,
            'context_window_tokens':context})
        with db.connect() as conn:
            conn.execute('UPDATE prompts SET llm_profile_id=? WHERE type=?',(key,explore.SYNTHESIS))
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
            if '论文数据：' in body['messages'][1]['content']:
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

    def test_legacy_heat_and_candidate_upgrade_is_repeatable(self):
        ids=self.papers(2);profile=self.configure()
        run_id,_,_=self.create()
        legacy_candidates=[{**self.synthesis(ids,name='Original current')['candidates'][0],
                            'id':1,'status':'observing','reason':'legacy observation','decisions':[],
                            'updated_at':'2026-10-07 12:00:00','new_paper_ids':ids}]
        legacy_execution={'previous_candidates':[[{**legacy_candidates[0],'name':'Earlier possible mismatch'}]]}
        with db.connect() as conn:
            conn.executescript("""ALTER TABLE paper_categories RENAME TO paper_categories_current;
                CREATE TABLE paper_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    category TEXT NOT NULL, crawl_date TEXT NOT NULL, rank INTEGER,
                    reading_stars INTEGER NOT NULL DEFAULT 0, pdf_clicks INTEGER NOT NULL DEFAULT 0,
                    kimi_clicks INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    UNIQUE(paper_id,category,crawl_date));
                INSERT INTO paper_categories(id,paper_id,category,crawl_date,rank,reading_stars,pdf_clicks,kimi_clicks,created_at)
                SELECT id,paper_id,category,crawl_date,rank,
                    CASE rank WHEN 1 THEN 12 ELSE 0 END, CASE rank WHEN 1 THEN 12 ELSE 0 END, 0,created_at
                FROM paper_categories_current;
                DROP TABLE paper_categories_current;
                DELETE FROM schema_migrations WHERE name='paper_heat_observations_v1';""")
            conn.execute('UPDATE direction_exploration_runs SET candidates_json=?,execution_json=? WHERE id=?',
                         (json.dumps(legacy_candidates),json.dumps(legacy_execution),run_id))
        original=store.get(run_id)
        db.init_db()
        db.init_db()
        self.assertEqual(store.get(run_id),original)
        self.assertEqual(db.get_int_setting('exploration.profile_id',0),profile)
        self.assertEqual(db.get_llm_profile(profile)['model'],'fixture')
        with db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='paper_heat_observations_v1'").fetchone()[0],1)
            for row in conn.execute('SELECT * FROM paper_categories'):
                for field in ('reading_stars','pdf_clicks','kimi_clicks','heat_captured_at'):
                    self.assertIsNone(row[field])
                legacy_count=12 if row['rank']==1 else 0
                self.assertEqual(json.loads(row['legacy_heat_json']),{
                    'reading_stars':legacy_count,'pdf_clicks':legacy_count,'kimi_clicks':0})
        self.assertTrue(all(p['sample_group']=='random' for p in explore.build_plan(NOW)['papers']))
        legacy_page=self.client.get(f'/direction-exploration/{run_id}/candidates/1').get_data(as_text=True)
        self.assertIn('Original current',legacy_page)
        self.assertIn('历史身份对应关系无法确认',legacy_page)

    def test_legacy_local_timestamps_match_beijing_window(self):
        ids=self.papers(4)
        with db.connect() as conn:
            conn.executemany('UPDATE papers SET created_at=? WHERE id=?',zip(
                ['2026-09-30 20:00:00','2026-10-07 20:00:00','2026-10-08 00:00:00','invalid'],ids))
        plan=explore.build_plan(NOW)
        self.assertEqual([p['id'] for p in plan['papers']],[ids[1]])
        self.assertEqual(plan['counts']['time_invalid'],1)

    def test_synthesis_small_context_blocks_before_queue(self):
        self.papers(20);self.configure()
        self.synthesis_profile(2600)
        with db.connect() as conn:
            conn.execute('UPDATE papers SET abstract=?',('X'*500,))
        with patch.object(explore,'datetime') as clock:
            clock.now.return_value=NOW;clock.combine=datetime.combine;clock.min=datetime.min
            preview=self.client.get('/direction-exploration').get_data(as_text=True)
            self.assertIn('容量不足',preview)
            self.assertEqual(self.client.post('/direction-exploration',data={'csrf_token':'test-library'}).status_code,400)
        self.assertEqual(db.list_jobs(),[])

    def test_official_deepseek_limits_and_synthesis_budget_are_frozen(self):
        self.papers(2)
        profile_id=self.configure(base_url='https://api.deepseek.com',model='deepseek-v4-flash',
                                  context_window_tokens=1_024_000_000,max_output_tokens=393_216)
        run_id,job_id,_=self.create()
        plan=store.get(run_id)['plan']
        synthesis=plan['configs'][explore.SYNTHESIS]
        self.assertEqual(synthesis['profile']['max_output_tokens'],393_216)
        self.assertEqual(synthesis['request_output_tokens'],8_192)
        self.assertEqual(plan['capacity'][explore.SYNTHESIS]['output'],8_192)
        self.assertEqual(plan['capacity'][explore.SYNTHESIS]['limit'],1_048_576)
        with db.connect_llm_profiles() as conn:
            conn.execute('UPDATE llm_profiles SET max_output_tokens=? WHERE id=?',(3_072,profile_id))
        self.assertEqual(explore.build_plan(NOW)['configs'][explore.SYNTHESIS]['request_output_tokens'],3_072)
        calls=self.execute(run_id,job_id)
        self.assertEqual([body['max_tokens'] for body in calls],[1_000,8_192])
        self.assertEqual(store.get(run_id)['status'],'success')
        self.assertIn('本次输出预算 8192',self.client.get(f'/direction-exploration/{run_id}').get_data(as_text=True))

    def test_official_mimo_and_proxy_use_their_own_output_parameter(self):
        self.papers(2)
        self.configure(base_url='https://api.xiaomimimo.com/v1',model='mimo-v2.6-pro',
            context_window_tokens=2_000_000,max_output_tokens=131_072)
        run_id,job_id,_=self.create()
        plan=store.get(run_id)['plan']
        self.assertEqual(plan['capacity'][explore.SYNTHESIS]['limit'],1_048_576)
        self.assertEqual(llm.official_model_token_limits({'base_url':'https://api.xiaomimimo.com/v1',
            'model':'mimo-v2.6-flash'}),(1_048_576,131_072))
        calls=self.execute(run_id,job_id)
        self.assertEqual([body['max_completion_tokens'] for body in calls],[1_000,8_192])
        self.assertTrue(all('max_tokens' not in body for body in calls))
        self.assertEqual(store.get(run_id)['status'],'success')
        self.configure(base_url='https://fixture.invalid/v1',model='mimo-v2.6-pro',
            context_window_tokens=50_000,max_output_tokens=512)
        proxy=explore.configs()
        self.assertEqual(proxy[explore.SYNTHESIS]['context_limit_tokens'],50_000)
        self.assertEqual(proxy[explore.SYNTHESIS]['request_output_tokens'],512)
        self.assertEqual(proxy[explore.EXTRACT]['request_output_tokens'],512)
        self.assertIsNone(llm.official_model_token_limits({'base_url':'https://fixture.invalid/v1','model':'mimo-v2.6-pro'}))
        def respond(request):
            wire = json.loads(request.content)
            self.assertEqual(wire['max_tokens'],512)
            self.assertNotIn('max_completion_tokens',wire)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]},request=request)
        with patch.object(llm,'_api_key',return_value='fixture'),httpx.Client(transport=httpx.MockTransport(respond)) as client:
            llm.call_llm(explore._runtime_profile(proxy[explore.SYNTHESIS]),'fixture',client=client)

    def test_legacy_frozen_plan_preserves_2000_output(self):
        self.papers(2);self.configure()
        plan=explore.build_plan(NOW)
        for kind, config in plan['configs'].items():
            del config['request_output_tokens']
            del config['context_limit_tokens']
            config['profile']['max_output_tokens']=1000 if kind==explore.EXTRACT else 2000
        run_id,job_id,_=store.create(plan)
        calls=self.execute(run_id,job_id)
        self.assertEqual([body['max_tokens'] for body in calls],[1_000,2_000])
        self.assertEqual(store.get(run_id)['status'],'success')

    def test_capacity_trim_and_frozen_post_replanning(self):
        self.papers(20);self.configure();self.synthesis_profile(4800)
        with db.connect() as conn:
            conn.execute('UPDATE papers SET abstract=?',('X'*500,))
        previous = explore.build_plan(NOW)
        self.assertLess(len(previous['papers']),20)
        self.assertGreater(len(previous['papers']),0)
        self.assertEqual(previous['counts']['context_excluded'],20-len(previous['papers']))
        self.assertEqual(previous['capacity'][explore.SYNTHESIS]['extraction_reserve'],1000)
        self.assertTrue(all(len(p['abstract'])==500 for p in previous['papers']))
        self.synthesis_profile(6600)
        with patch.object(explore,'datetime') as clock:
            clock.now.return_value=NOW;clock.combine=datetime.combine;clock.min=datetime.min
            response=self.client.post('/direction-exploration',data={
                'csrf_token':'test-library','fingerprint':previous['fingerprint']},follow_redirects=True)
        self.assertIn('入队计划与此前预览不同',response.get_data(as_text=True))
        run=store.get(1)
        self.assertGreater(len(run['plan']['papers']),len(previous['papers']))
        self.assertEqual(run['plan']['configs'][explore.SYNTHESIS]['profile']['model'],'synthesis-6600')

    def test_actual_synthesis_oversize_checkpoint_and_configured_retry(self):
        self.papers(10)
        larger = self.configure()
        smaller = self.synthesis_profile(4100)
        run_id,job_id,_=self.create()
        def oversized(body,n,request):
            self.assertEqual(body['max_tokens'],1000)
            raw=body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0]
            result=self.extract(json.loads(raw))
            for row in result['papers']:
                row['limitations']='证'*600
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]},request=request)
        calls=self.execute(run_id,job_id,oversized)
        self.assertEqual(len(calls),1)
        original=store.get(run_id)
        self.assertEqual(original['status'],'failed')
        self.assertEqual(original['execution']['synthesis']['error'],'exploration_context_exceeded')
        self.assertEqual(len(original['execution']['consumed']),10)
        self.assertIn('已提取 10 / 10 篇',self.client.get('/direction-exploration/1').get_data(as_text=True))
        url=f'/direction-exploration/{run_id}/retry'
        self.assertEqual(self.client.post(url,data={'csrf_token':'test-library'}).status_code,400)
        form={'csrf_token':'test-library','synthesis_only':'1','profile_id':smaller}
        self.assertEqual(self.client.post(url,data=form).status_code,400)
        form['profile_id']=larger
        response=self.client.post(url,data=form)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.client.post(url,data=form).location,response.location)
        child_id=int(response.location.rsplit('/',1)[-1])
        child=store.get(child_id)
        self.assertEqual(child['retry_of'],run_id)
        self.assertEqual(child['plan']['configs'][explore.SYNTHESIS]['profile']['id'],larger)
        self.assertEqual(child['plan']['request_limit'],2)
        # Reserve one request before the interrupted external outcome is reconciled.
        reserved_job=child['job_id']
        store.reserve_request(child_id,'synthesis')
        db.mark_unfinished_jobs_interrupted()
        retry=self.client.post(f'/direction-exploration/{child_id}/retry',data={'csrf_token':'test-library'})
        self.assertEqual(retry.status_code,302)
        child=store.get(child_id)
        self.assertNotEqual(child['job_id'],reserved_job)
        self.assertEqual([body['max_tokens'] for body in self.execute(child_id,child['job_id'])],[8192])
        self.assertEqual(store.get(child_id)['execution']['requests'],2)
        self.assertEqual(store.get(run_id),original)

    def test_worker_rechecks_legacy_frozen_capacity_before_extracting(self):
        self.papers(20);self.configure()
        run_id,job_id,_=self.create()
        plan = store.get(run_id)['plan']
        config=plan['configs'][explore.SYNTHESIS]
        del config['request_output_tokens']
        del config['context_limit_tokens']
        config['profile'].update(context_window_tokens=2400,max_output_tokens=2000)
        with db.connect() as conn:
            conn.execute('UPDATE direction_exploration_runs SET plan_json=? WHERE id=?',(json.dumps(plan),run_id))
        self.assertEqual(self.execute(run_id,job_id),[])
        detail = store.get(run_id)
        self.assertEqual(detail['status'],'failed')
        self.assertIn('容量不足',detail['execution']['preflight']['reason'])
        self.assertNotIn('consumed',detail['execution'])

    def test_unknown_heat_is_not_zero_and_observation_updates(self):
        ids=self.papers(2)
        paper=db.get_paper(ids[0])
        paper.update(pdf_clicks=None,kimi_clicks=0)
        with patch.object(db,'utc_now_iso',return_value='2026-10-08T01:00:00+00:00'):
            db.upsert_paper(paper,'cs.AI','2026-10-07')
        plan=explore.build_plan(NOW)
        rows={p['id']:p for p in plan['papers']}
        self.assertIsNone(rows[ids[0]]['reading_stars'])
        self.assertEqual(rows[ids[0]]['sample_group'],'random')
        self.assertEqual(rows[ids[0]]['comparison_size'],1)
        self.assertEqual(rows[ids[0]]['heat_captured_at'],'2026-10-08T01:00:00+00:00')

    def test_known_match_survives_missing_direction(self):
        pid=self.papers(1)[0]
        direction=db.create_attention_direction('Known','existing')
        db.create_attention_direction('No result','existing')
        db.set_direction_decision(pid,direction,'confirmed')
        plan=explore.build_plan(NOW)
        self.assertEqual([(p['id'],p['sample_group']) for p in plan['papers']],[(pid,'edge')])

    def test_first_seen_offsets_across_year_and_repeated_ingestion(self):
        points = ['2026-12-26T15:59:59+00:00','2026-12-26T16:00:00+00:00',
                  '2026-12-27T00:00:00+08:00','2026-12-27 00:00:00',
                  '2027-01-02T15:59:59+00:00','2027-01-02T16:00:00+00:00']
        ids = []
        for index, point in enumerate(points):
            paper = {'arxiv_id':f'2701.{index:05d}','title':'Paper','abstract':'Research evidence'}
            with patch.object(db,'utc_now_iso',return_value=point):
                ids.append(db.upsert_paper(paper,'cs.AI','2027-01-02'))
            db.upsert_paper(paper,'cs.AI','2027-01-02')
        now = datetime.fromisoformat('2027-01-02T17:00:00+00:00')
        plan = explore.build_plan(now)
        self.assertEqual((plan['date_from'],plan['date_to']),('2026-12-27','2027-01-02'))
        self.assertEqual({p['id'] for p in plan['papers']},set(ids[1:-1]))
        self.assertEqual([db.get_paper(pid)['created_at'] for pid in ids],points)

    def test_direction_aggregation_matrix(self):
        # Includes an archived direction carrying a stale match for every row.
        first=db.create_attention_direction('First','scope')
        second=db.create_attention_direction('Second','scope')
        ignored=db.create_attention_direction('Archived','scope')
        examples = [
            ('matched',None,None,'matched',False),
            ('matched','failed',None,'matched',False),
            (None,None,'confirmed','matched',False),
            ('matched',None,'rejected','pending',False),
            ('unmatched','unmatched',None,'unmatched',True),
            ('possible','unmatched',None,'possible',True),
            ('possible',None,None,'pending',False),
            (None,None,None,'pending',False),
        ]
        ids=self.papers(len(examples))
        with db.connect() as conn:
            conn.execute("UPDATE attention_directions SET status='archived' WHERE id=?",(ignored,))
            for pid,(a,b,manual,_,_) in zip(ids,examples):
                for direction, value in [(first,a),(second,b),(ignored,'matched')]:
                    if value:
                        conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,created_at,updated_at)
                            VALUES (?,?,?,?,?)''',(pid,direction,value,db.now_iso(),db.now_iso()))
        for pid,(_,_,manual,_,_) in zip(ids,examples):
            if manual:
                db.set_direction_decision(pid,first,manual)
        plan=explore.build_plan(NOW)
        self.assertEqual(plan['counts']['classification_pending'],3)
        self.assertEqual(plan['counts']['classification_incomplete'],6)
        sampled={p['id']:p for p in plan['papers']}
        for pid,(_,_,_,state,complete) in zip(ids,examples):
            if state == 'pending':
                self.assertNotIn(pid,sampled)
            else:
                self.assertEqual((sampled[pid]['state'],sampled[pid]['classification_complete']),(state,complete))
                self.assertEqual(sampled[pid]['sample_group'],'heat' if state=='unmatched' else 'edge')

    def test_exploration_respects_refinement_and_manual_removal(self):
        first=db.create_attention_direction('First','scope')
        second=db.create_attention_direction('Second','scope')
        refined, excluded, low_confidence, removed, fallback = self.papers(5)
        now = db.now_iso()
        with db.connect() as conn:
            conn.executemany('''INSERT INTO paper_direction_results(
                paper_id,direction_id,model_decision,pro_decision,pro_confidence,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)''', [
                (refined,first,'possible','matched','high',now,now),
                (excluded,first,'matched','unmatched','high',now,now),
                (excluded,second,'unmatched',None,None,now,now),
                (low_confidence,first,'matched','matched','low',now,now),
                (low_confidence,second,'unmatched',None,None,now,now),
                (removed,first,'matched',None,None,now,now),
                (fallback,first,'matched','failed',None,now,now),
            ])
        db.remove_classification_targets([(removed,'attention_direction',first)])

        plan = explore.build_plan(NOW)
        self.assertEqual(plan['counts']['classification_pending'],1)
        self.assertEqual(plan['counts']['classification_incomplete'],3)
        self.assertEqual({p['id']:(p['state'],p['sample_group'],p['classification_complete']) for p in plan['papers']},{
            refined:('matched','edge',False), excluded:('unmatched','heat',True),
            low_confidence:('possible','edge',True), fallback:('matched','edge',False),
        })

    def test_heat_counts_zero_failed_and_recapture(self):
        for value, expected in [('PDF 0',0),('NoPDF 0',None),('PDF ',None),('PDF -1',None),
                                ('PDF loading',None),('PDF 1.2',None),('PDF 10',10)]:
            self.assertEqual(crawler._extract_label_count(value,'PDF'),expected)
        ids = self.papers(2)
        first, second = db.get_paper(ids[0]), db.get_paper(ids[1])
        first.update(pdf_clicks=0,kimi_clicks=0)
        second.update(pdf_clicks=0,kimi_clicks='missing')
        for day, pdf in [(8,0),(9,10),(10,10),(11,None)]:
            timestamp=f'2026-10-{day:02}T09:00:00+00:00'
            first['pdf_clicks'] = pdf
            with patch.object(db,'utc_now_iso',return_value=timestamp):
                db.upsert_papers([first,second],'cs.AI','2026-10-07')
            rows={p['id']:p for p in explore.build_plan(NOW)['papers']}
            row=rows[ids[0]]
            self.assertEqual(row['reading_stars'],pdf)
            self.assertEqual(row['created_at'],'2026-10-07T04:00:00+00:00')
            self.assertEqual(row['heat_captured_at'],timestamp)
            self.assertEqual(row['heat_percentile'],0.5 if pdf is not None else None)
            self.assertEqual(rows[ids[1]]['comparison_size'],int(pdf is not None))
            self.assertIsNone(rows[ids[1]]['kimi_clicks'])
        self.assertIn('未知',self.client.get(f'/papers/{ids[0]}').get_data(as_text=True))
        listed=db.list_paper_page(crawl_date='2026-10-07',sort='stars')['items']
        self.assertIsNone(listed[0]['reading_stars'])
        self.assertEqual(services.build_paper_digest_csv(listed).splitlines()[1].split(',')[3],'')

    def test_candidate_identity_survives_reordering(self):
        self.papers(2);self.configure()
        run_id,_,_=self.create()
        plan=store.get(run_id)['plan']
        ids=[p['id'] for p in plan['papers']]
        alpha=self.synthesis(ids,name='Alpha')['candidates'][0]
        beta=self.synthesis(ids,name='Beta')['candidates'][0]
        def save(rows):
            result=explore.validate_synthesis({'candidates':rows,'summary':'fixture'},plan['papers'],[])
            store.save_execution(run_id,{},'partial_success',result['candidates'])
        save([alpha,beta])
        first=store.get(run_id)
        identity=first['candidates'][0]['id']
        store.decide(run_id,identity,'observing','watch',first['version'])
        save([beta,alpha])
        _,candidate=store.candidate(run_id,identity)
        self.assertEqual((candidate['name'],candidate['status']),('Alpha','observing'))

    def test_queue_partial_then_interrupted_recovery_keeps_old_links(self):
        self.papers(20);self.configure()
        with patch.object(explore,'datetime') as clock:
            clock.now.return_value=NOW;clock.combine=datetime.combine;clock.min=datetime.min
            response=self.client.post('/direction-exploration',data={'csrf_token':'test-library'})
        self.assertEqual(response.status_code,302)
        run_id=int(response.location.rsplit('/',1)[-1])
        job_id=self.runner.queue.get_nowait()
        plan=store.get(run_id)['plan']
        first_indices=[p['id'] for p in plan['papers'][:10]]
        second_indices=[p['id'] for p in plan['papers'][10:]]
        alpha=self.synthesis(first_indices,name='Alpha')['candidates'][0]
        beta=self.synthesis(first_indices[2:],name='Beta',scope_text='Original Beta scope')['candidates'][0]
        gamma=self.synthesis(second_indices,name='Beta',scope_text='Different Beta scope')['candidates'][0]
        original_event=db.append_job_event
        def fail_after_publish(*args,**kwargs):
            if args[3]=='exploration.completed':
                raise KeyboardInterrupt('simulated stop after transaction, before job finalization')
            return original_event(*args,**kwargs)
        def respond(body,n,request):
            if body['max_tokens']==1000:
                raw=body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0]
                papers=json.loads(raw)
                if papers[0]['id']==second_indices[0] and db.get_job(job_id)['status']=='running':
                    return httpx.Response(400,json={'error':{'message':'fixture error'}},request=request)
                result=self.extract(papers)
            elif db.get_job(job_id)['status']=='running':
                result={'candidates':[alpha,beta],'summary':'partial'}
            else:
                self.assertIn('本周报已发布的候选',body['messages'][1]['content'])
                self.assertNotIn('reading_stars',body['messages'][1]['content'])
                state=store.get(run_id)
                result={'candidates':[gamma,alpha],'summary':'restored'}
                form={'csrf_token':'test-library','status':'observing','reason':'edited during recovery','version':state['version']}
                self.assertEqual(self.client.post(f'/direction-exploration/{run_id}/candidates/{alpha_id}',data=form).status_code,302)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]},request=request)
        with patch.object(db,'append_job_event',side_effect=fail_after_publish),self.assertRaises(KeyboardInterrupt):
            self.execute(run_id,job_id,respond)
        db.mark_unfinished_jobs_interrupted()
        partial=store.get(run_id)
        self.assertEqual(partial['status'],'partial_success')
        self.assertEqual(partial['execution']['requests'],3)
        alpha_id,beta_id=(c['id'] for c in partial['candidates'])
        old_form={'csrf_token':'test-library','status':'ignored','reason':'stale','version':partial['version']}
        retry=self.client.post(f'/direction-exploration/{run_id}/retry',data={'csrf_token':'test-library'})
        self.assertEqual(retry.status_code,302)
        retry_job=self.runner.queue.get_nowait()
        calls=self.execute(run_id,retry_job,respond)
        self.assertEqual([body['max_tokens'] for body in calls],[1000,8192])
        success=store.get(run_id)
        self.assertEqual(db.get_job(retry_job)['status'],'success')
        self.assertEqual(success['execution']['requests'],5)
        self.assertEqual(success['candidates'][1]['id'],alpha_id)
        self.assertEqual(success['candidates'][1]['reason'],'edited during recovery')
        self.assertGreater(success['candidates'][0]['id'],beta_id)
        self.assertEqual(success['candidates'][0]['status'],'pending')
        old_link=f'/direction-exploration/{run_id}/candidates/{beta_id}'
        old_html=self.client.get(old_link).get_data(as_text=True)
        self.assertIn('Original Beta scope',old_html)
        self.assertNotIn('Different Beta scope',old_html)
        self.assertIn('已被替代',old_html)
        self.assertEqual(self.client.post(old_link,data=old_form).status_code,409)

    def test_continuation_versions_and_invalid_refs_are_atomic(self):
        self.papers(2);self.configure();run_id,_,_=self.create()
        plan=store.get(run_id)['plan']
        papers=plan['papers']
        original=self.synthesis([p['id'] for p in papers],name='Alpha',scope_text='Original scope')
        validated=explore.validate_synthesis(original,papers,[])
        store.save_execution(run_id,{},'partial_success',validated['candidates'])
        run=store.get(run_id)
        cid=run['candidates'][0]['id']
        ref=f'{run_id}:{cid}'
        store.decide(run_id,cid,'observing','Keep watching',run['version'])
        old=store.get(run_id)
        published=store.published_candidates(old)
        rename=self.synthesis([p['id'] for p in papers],name='Renamed',continuation_ref=ref)
        result=explore.validate_synthesis(rename,papers,[],published)
        for illegal in ([{**result['candidates'][0],'continuation_ref':'999:1'}],result['candidates']*2):
            with self.assertRaises(llm.LLMResultError):
                store.save_execution(run_id,{'invalid':True},'success',illegal)
        self.assertEqual(store.get(run_id),old)
        store.save_execution(run_id,{},'success',result['candidates'])
        fresh=store.get(run_id)
        self.assertEqual((fresh['candidates'][0]['id'],fresh['candidates'][0]['status']),(cid,'observing'))
        self.assertEqual(fresh['execution']['candidate_versions'][str(cid)][0]['name'],'Alpha')
        store.save_execution(run_id,{},'success',result['candidates'])
        self.assertEqual(store.get(run_id)['version'],fresh['version'])
        self.assertIn('Original scope',self.client.get(f'/direction-exploration/{run_id}/candidates/{cid}').get_data(as_text=True))

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
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['max_tokens'],8192)
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
