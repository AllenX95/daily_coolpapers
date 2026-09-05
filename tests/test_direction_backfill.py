import json
import unittest
from unittest.mock import patch

from daily_coolpapers import app as app_module, db, services
from daily_coolpapers.llm import LLMError
from tests import test_automatic_abstracts as automatic
from tests import test_direction_pipeline as pipeline
from tests.test_crawl_observability import _paper


class DirectionBackfillTests(unittest.TestCase):
    direction = pipeline.DirectionPipelineTests.direction
    classified = pipeline.DirectionPipelineTests.classified

    def setUp(self):
        automatic.AutomaticAbstractTests.setUp(self)
        self.app = app_module.create_app(runner=self.runner,secret_key='backfill-test')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'backfill-test'

    def paper(self,number=1,category='cs.AI',day='2026-09-03',**changes):
        return db.upsert_papers([{**_paper(f'2609.{number:05}',f'Paper {number}'),**changes}],category,day)[0]

    def post(self,key,**changes):
        return self.client.post(f'/attention-directions/{key}/backfill',data={
            'csrf_token':'backfill-test','date_from':'2026-09-01','date_to':'2026-09-03',**changes})

    def run_backfill(self,key,**kwargs):
        job_id = self.runner.enqueue_direction_backfill(key,'2026-09-01','2026-09-03')
        self.runner._run_job(job_id)
        return db.get_job(job_id)

    def test_preview_read_only_closed_dates_dedup_categories_and_counts(self):
        key = self.direction()
        first = self.paper(day='2026-09-01')
        self.paper(category='cs.CV',day='2026-09-03')
        self.paper(category='cs.LG',day='2026-08-31')
        second = self.paper(2,abstract='')
        self.paper(3,day='2026-09-04')
        preview = services.direction_backfill_preview(key,'2026-09-01','2026-09-03')
        self.assertEqual(preview['paper_ids'],[first,second])
        self.assertEqual(preview['inputs'][first]['categories'],['cs.AI','cs.CV'])
        self.assertEqual(preview['counts'],{'total':2,'executable':1,'input_incomplete':1,'already_classified':0,'already_abstract':0,'max_additional_abstract':1})
        with patch.object(services,'call_llm') as call:
            response = self.post(key)
        self.assertEqual(response.status_code,200)
        self.assertIn('成本上限预览',response.get_data(as_text=True))
        self.assertEqual(db.list_jobs(),[])
        self.assertTrue(self.runner.queue.empty())
        call.assert_not_called()

    def test_confirm_job_chains_abstract_same_parent_and_preserves_sources(self):
        key, paper = self.direction(), self.paper()
        response = self.post(key,confirmed='1')
        self.assertEqual(response.status_code,302)
        job_id = db.list_jobs()[0]['id']
        with patch.object(services,'call_llm',side_effect=[self.classified((key,'possible')),self.response]) as call:
            self.runner._run_job(job_id)
        self.assertEqual(call.call_count,2)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        self.assertEqual({e['pipeline_job_id'] for e in db.list_evaluations(paper)},{job_id})
        classification = db.get_latest_evaluation(paper,'direction_classification')
        self.assertEqual(classification['classification_source'],'historical_backfill')
        self.assertEqual(json.loads(classification['input_snapshot_json'])['categories'],['cs.AI'])
        for path in (f'/jobs/{job_id}',f'/jobs/{job_id}?stage=classification&severity=all',f'/api/jobs/{job_id}/diagnostic'):
            self.assertEqual(self.client.get(path).status_code,200)
        html = self.client.get(f'/jobs/{job_id}').get_data(as_text=True)
        self.assertIn('关注方向分类',html)
        self.assertIn('重新预览此范围',html)

    def test_overlap_existing_classification_and_abstract_skip_both(self):
        key = self.direction()
        self.paper()
        with patch.object(services,'call_llm',side_effect=[self.classified((key,'matched')),self.response]) as call:
            self.run_backfill(key)
            repeated = self.run_backfill(key)
        self.assertEqual(call.call_count,2)
        self.assertEqual(repeated['progress_details']['classification']['already_classified'],1)
        self.assertEqual(repeated['progress_details']['abstract']['already_successful'],1)
        preview = services.direction_backfill_preview(key,'2026-09-01','2026-09-03')['counts']
        self.assertEqual((preview['executable'],preview['already_classified'],preview['already_abstract'],preview['max_additional_abstract']),(0,1,1,0))

    def test_prior_model_unmatched_success_never_overwritten_manual_preserved(self):
        key,paper = self.direction(),self.paper()
        with patch.object(services,'call_llm',return_value=self.classified((key,'unmatched'))):
            self.run_backfill(key)
        db.set_direction_decision(paper,key,'confirmed')
        original = db.paper_direction_results([paper])
        with patch.object(services,'call_llm') as call:
            self.run_backfill(key)
        call.assert_not_called()
        self.assertEqual(db.paper_direction_results([paper]),original)

    def test_only_failed_or_absent_retry_and_prior_manual_decision_survives(self):
        key,paper = self.direction(),self.paper()
        db.set_direction_decision(paper,key,'rejected')
        with patch.object(services,'call_llm',side_effect=LLMError('no')):
            first = self.run_backfill(key)
        self.assertEqual(first['status'],'partial_success')
        with patch.object(services,'call_llm',side_effect=[self.classified((key,'matched')),self.response]):
            second = self.run_backfill(key)
        self.assertEqual(second['status'],'success')
        row = db.paper_direction_results([paper])[paper][0]
        self.assertEqual((row['model_decision'],row['manual_decision'],row['effective']),('matched','rejected',False))

    def test_partial_failure_continues_other_papers_and_old_match_not_masked(self):
        old,new = self.direction('Old'),self.direction('New')
        first,second = self.paper(),self.paper(2)
        with db.connect() as conn:
            conn.execute("INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,created_at,updated_at) VALUES (?,?,'matched','t','t')",(first,old))
        def response(_profile,prompt,**_):
            if 'Paper 1' in prompt:
                raise LLMError('private')
            if 'directions' in prompt:
                return self.classified((new,'matched'))
            return self.response
        with patch.object(services,'call_llm',side_effect=response):
            job = self.run_backfill(new)
        self.assertEqual(job['status'],'partial_success')
        self.assertTrue(job['progress_details']['classification']['has_partial_failure'])
        self.assertIsNotNone(db.get_latest_successful_evaluation(second,'abstract_review'))
        self.assertIsNone(db.get_latest_successful_evaluation(first,'abstract_review'))

    def test_prior_abstract_success_or_running_is_reused(self):
        key,paper = self.direction(),self.paper()
        queued = db.create_job('abstract_eval',{'paper_id':paper})
        with patch.object(services,'call_llm',return_value=self.classified((key,'matched'))) as call:
            job = self.run_backfill(key)
        self.assertEqual(call.call_count,1)
        self.assertEqual(job['progress_details']['abstract']['evaluation_already_running'],1)
        self.assertEqual(db.get_job(queued)['status'],'pending')

    def test_archive_after_plan_no_mutation_of_running_snapshot(self):
        key,paper = self.direction(),self.paper()
        job_id = self.runner.enqueue_direction_backfill(key,'2026-09-01','2026-09-03')
        db.archive_attention_direction(key)
        with patch.object(services,'call_llm',return_value=self.classified((key,'unmatched'))):
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        self.assertEqual(self.post(key,confirmed='1').status_code,409)
        self.assertEqual(len(db.list_jobs()),1)

    def test_bad_dates_missing_direction_and_csrf_create_no_job(self):
        key = self.direction()
        for data in ({'date_from':'2026-02-30'},{'date_to':''},{'date_from':'2026-09-04'}, {'date_from':'20260901'}, {'confirmed':'maybe'}):
            self.assertEqual(self.post(key,**data).status_code,400)
        self.assertEqual(self.post(999).status_code,404)
        self.assertEqual(self.post(2**100).status_code,404)
        self.assertEqual(self.client.post(f'/attention-directions/{key}/backfill').status_code,403)
        self.assertEqual(db.list_jobs(),[])

    def test_inputs_frozen_at_confirmation_metadata_changes_do_not_drift(self):
        key,paper = self.direction(),self.paper()
        job_id = self.runner.enqueue_direction_backfill(key,'2026-09-01','2026-09-03')
        self.paper(abstract='Changed after confirmation',title='Later title')
        with patch.object(services,'call_llm',return_value=self.classified((key,'unmatched'))) as call:
            self.runner._run_job(job_id)
        prompt = call.call_args.args[1]
        self.assertNotIn('Changed after confirmation',prompt)
        self.assertIn('Paper 1',prompt)
        self.assertNotIn('Later title',json.loads(db.get_latest_evaluation(paper,'direction_classification')['input_snapshot_json'])['title'])

    def test_abstract_retries_capped_and_does_not_reclassify_on_explicit_repair(self):
        key = self.direction()
        self.paper()
        db.set_setting('llm.abstract_retries',5)
        with patch.object(services,'call_llm',side_effect=[self.classified((key,'matched'))]+[LLMError('retry',retryable=True)]*3) as call:
            job = self.run_backfill(key)
        self.assertEqual(call.call_count,4)
        self.assertEqual(job['progress_details']['abstract']['failed'],1)
        with patch.object(services,'call_llm',return_value=self.response) as call:
            repaired = self.run_backfill(key)
        self.assertEqual(call.call_count,1)
        self.assertEqual(repaired['status'],'success')


if __name__ == '__main__':
    unittest.main()
