import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from daily_coolpapers import db, services
from daily_coolpapers.llm import LLMError, LLMResponse
from daily_coolpapers.crawler import CategoryFetchResult
from tests import test_automatic_abstracts as automatic
from tests.test_crawl_observability import _paper


class DirectionPipelineTests(unittest.TestCase):
    setUp = automatic.AutomaticAbstractTests.setUp
    fetch = automatic.AutomaticAbstractTests.fetch
    run_pipeline = automatic.AutomaticAbstractTests.run_pipeline

    def direction(self, name='Agents'):
        key = db.create_attention_direction(name, 'Natural text scope: ' + name)
        db.save_llm_profile({**db.get_llm_profile(self.profile_id),'is_default_classification':1})
        return key

    def classified(self, *pairs):
        result = {'directions':[{'direction_id':key,'decision':state,'reason':'Original metadata supports this.'} for key,state in pairs]}
        return LLMResponse(json.dumps(result),result,{'input_tokens':10,'output_tokens':5})

    def test_no_directions_keeps_original_chain_and_zero_classification(self):
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',return_value=self.response) as call:
            job = self.run_pipeline()
        self.assertEqual(call.call_count,1)
        self.assertEqual(db.count_job_events(job['id'],event_type='classification.skipped_no_active_directions'),1)
        self.assertEqual(job['progress_details']['classification']['status'],'skipped_no_active_directions')

    def test_all_directions_one_call_cross_categories_one_abstract_and_audit(self):
        first,second = self.direction(),self.direction('Robotics')
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[self.classified((first,'matched'),(second,'possible')),self.response]) as call:
            job = self.run_pipeline()
        self.assertEqual(job['status'],'success')
        self.assertEqual(call.call_count,2)
        rows = db.paper_direction_results([1])[1]
        self.assertEqual([r['effective'] for r in rows],[True,False])
        self.assertTrue(rows[1]['pending'])
        evaluation = db.get_latest_evaluation(1,'direction_classification')
        self.assertEqual(evaluation['attempt'],1)
        self.assertEqual(evaluation['pipeline_job_id'],job['id'])
        self.assertEqual(evaluation['classification_source'],'daily')
        metadata = json.loads(evaluation['input_snapshot_json'])
        self.assertEqual(metadata['categories'], sorted(c['category'] for c in self.categories))
        self.assertEqual(set(metadata),{'title','abstract','subjects','categories'})
        self.assertEqual(len(evaluation['input_fingerprint']),64)
        self.assertNotIn('encrypted_api_key_ref',evaluation['config_snapshot_json'])
        summary = job['progress_details']['classification']
        self.assertEqual((summary['calls'],summary['input_tokens'],summary['abstract_new']),(1,10,1))

    def test_possible_enters_abstract_without_effective_category(self):
        key = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[self.classified((key,'possible')),self.response]):
            job = self.run_pipeline()
        self.assertEqual(job['progress_details']['abstract']['success'],1)
        row = db.paper_direction_results([1])[1][0]
        self.assertEqual((row['pending'],row['effective']),(True,False))

    def test_unmatched_retains_metadata_and_never_auto_abstract(self):
        key = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',return_value=self.classified((key,'unmatched'))) as call:
            job = self.run_pipeline()
        self.assertEqual(call.call_count,1)
        self.assertIsNotNone(db.get_paper(1))
        self.assertIsNone(db.get_latest_evaluation(1,'abstract_review'))
        self.assertEqual(job['status'],'success')

    def test_contract_invalid_atomic_three_attempts_all_failed(self):
        first,second = self.direction(),self.direction('Vision')
        invalid = self.classified((first,'matched'))
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',return_value=invalid) as call:
            job = self.run_pipeline()
        self.assertEqual(call.call_count,3)
        rows = db.paper_direction_results([1])[1]
        self.assertEqual({r['model_decision'] for r in rows},{'failed'})
        self.assertEqual(len({r['classification_evaluation_id'] for r in rows}),1)
        self.assertEqual(job['status'],'partial_success')
        self.assertEqual(len(db.list_evaluations(1)),3)
        self.assertEqual(job['progress_details']['classification']['retry_count'],2)

    def test_contract_invalid_variants(self):
        good = {'direction_id':1,'decision':'matched','reason':'yes'}
        for value in ([],None,{}, {'directions':[good,good]}, {'directions':[{**good,'direction_id':999}]},
                      {'directions':[{**good,'direction_id':True}]},{'directions':[{**good,'decision':'yes'}]},
                      {'directions':[{**good,'decision':[]}]},{'directions':[{**good,'decision':{}}]},
                      {'directions':[{**good,'reason':' '}]}):
            with self.subTest(value=value),self.assertRaises(LLMError):
                services.validate_classification_result(value,[1])

    def test_retryable_provider_failure_recovers_and_redacts_exception(self):
        key = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[LLMError('SECRET https://key.invalid',retryable=True),self.classified((key,'matched')),self.response]):
            job = self.run_pipeline()
        self.assertEqual(job['status'],'success')
        summary = job['progress_details']['classification']
        self.assertEqual((summary['calls'],summary['call_failed'],summary['call_success']),(2,1,1))
        self.assertNotIn('SECRET',json.dumps(db.list_evaluations(1)))
        self.assertNotIn('SECRET',json.dumps(db.list_job_events(job['id'],limit=1000)))

    def test_terminal_failure_no_next_day_rerun_but_explicit_retry_works(self):
        key = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=LLMError('bad',retryable=False)) as call:
            first = self.run_pipeline()
            self.run_pipeline()
        self.assertEqual(call.call_count,1)
        with patch.object(services,'call_llm',side_effect=[self.classified((key,'matched')),self.response]) as call:
            retry = self.run_pipeline(retry_of_job_id=first['id'])
        self.assertEqual(retry['status'],'success')
        self.assertEqual(call.call_count,2)

    def test_abstract_exhaustion_not_implicitly_retried_next_day(self):
        key = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[self.classified((key,'matched')),LLMError('terminal')]) as call:
            self.run_pipeline()
            second = self.run_pipeline()
        self.assertEqual(call.call_count,2)
        self.assertEqual(second['progress_details']['abstract']['previous_failure'],1)

    def test_new_direction_does_not_classify_old_metadata_or_repeat_success(self):
        first = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[self.classified((first,'matched')),self.response]):
            self.run_pipeline()
        second = self.direction('New focus')
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm') as call:
            self.run_pipeline()
        call.assert_not_called()
        self.assertEqual([r['direction_id'] for r in db.paper_direction_results([1])[1]],[first])
        self.assertNotEqual(first,second)

    def test_existing_pre_module_paper_requires_explicit_backfill(self):
        db.upsert_papers([_paper('2609.00001')],'cs.AI','2026-09-01')
        self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm') as call:
            self.run_pipeline()
        call.assert_not_called()

    def test_missing_config_is_failed_not_unmatched(self):
        db.create_attention_direction('Unconfigured','Scope')
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm') as call:
            job = self.run_pipeline()
        call.assert_not_called()
        self.assertEqual(job['status'],'failed')
        self.assertEqual(db.paper_direction_results([1])[1][0]['model_decision'],'failed')
        self.assertEqual(db.get_latest_evaluation(1,'direction_classification')['error_code'],'evaluation_config_missing')

    def test_frozen_direction_prompt_profile_with_archive_during_run(self):
        key = self.direction()
        job_id,_ = self.runner.enqueue_pipeline('manual_latest',self.ids)
        original = db.get_job(job_id)['payload_data']
        db.archive_attention_direction(key)
        self.direction('Later')
        prompt = db.get_default_prompt('direction_classification')
        db.save_prompt({**prompt,'template':'Changed prompt'})
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch), patch.object(services,'call_llm',side_effect=[self.classified((key,'matched')),self.response]) as call:
            self.runner._run_job(job_id)
        self.assertEqual(db.get_job(job_id)['status'],'success')
        self.assertNotIn('Changed prompt',call.call_args_list[0].args[1])
        self.assertNotIn('Later',call.call_args_list[0].args[1])
        self.assertEqual(json.loads(db.get_latest_evaluation(1,'direction_classification')['direction_snapshot_json']),original['directions'])

    def test_missing_input_no_model_and_counted(self):
        self.direction()
        def fetch(*_):
            return CategoryFetchResult([{**_paper('2609.00001'),'abstract':''}],'warning',(),{},())
        with patch.object(services,'_fetch_category_from_config',side_effect=fetch), patch.object(services,'call_llm') as call:
            job = self.run_pipeline()
        call.assert_not_called()
        self.assertEqual(job['progress_details']['classification']['input_incomplete'],1)

    def test_concurrent_classification_claim_and_crash_recovery(self):
        key = self.direction()
        paper_id = db.upsert_papers([_paper('2609.00001')],'cs.AI','2026-09-03')[0]
        job_id = db.create_job('daily_pipeline',{})
        with ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(lambda _:db.claim_classification(paper_id,[key],job_id),range(4)))
        self.assertEqual(sum(bool(c[0]) for c in claims),1)
        plan = services.build_daily_pipeline_plan('manual_latest',self.ids)
        evaluation_id = db.start_classification_attempt(paper_id,job_id,'daily',plan['directions'],{'title':'T','abstract':'A'},plan['classification_config'],1)
        db.mark_unfinished_jobs_interrupted()
        row = db.paper_direction_results([paper_id])[paper_id][0]
        self.assertEqual((row['model_decision'],row['classification_evaluation_id']),('failed',evaluation_id))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM classification_claims').fetchone()[0],0)

    def test_retry_modes_cannot_classify_new_historical_directions(self):
        first = self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch),patch.object(services,'call_llm',side_effect=[self.classified((first,'matched')),LLMError('terminal')]):
            original = self.run_pipeline()
        second = self.direction('New direction')
        for mode in ('abstract_only','all'):
            with patch.object(services,'call_llm',return_value=self.response) as call:
                retry = self.run_pipeline(retry_of_job_id=original['id'],retry_mode=mode)
            self.assertEqual(retry['status'],'success')
            self.assertLessEqual(call.call_count,1)
            self.assertEqual([d['id'] for d in retry['payload_data']['directions']],[first])
            self.assertEqual([r['direction_id'] for r in db.paper_direction_results([1])[1]],[first])
        self.assertNotEqual(first,second)

    def test_abstract_only_retry_never_calls_classifier_even_on_failed_classification(self):
        self.direction()
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch),patch.object(services,'call_llm',side_effect=LLMError('terminal')):
            original = self.run_pipeline()
        with patch.object(services,'call_llm') as call:
            retry = self.run_pipeline(retry_of_job_id=original['id'],retry_mode='abstract_only')
        call.assert_not_called()
        self.assertEqual(retry['status'],'partial_success')

    def test_between_attempts_interruption_retains_failed_pairs_and_next_day_partial(self):
        first,second = self.direction(),self.direction('Second')
        paper_id = db.upsert_papers([_paper('2609.00001')],'cs.AI','2026-09-03')[0]
        plan = services.build_daily_pipeline_plan('manual_latest',self.ids)
        job_id = db.create_job('daily_pipeline',plan)
        db.claim_classification(paper_id,[first,second],job_id)
        evaluation_id = db.start_classification_attempt(paper_id,job_id,'daily',plan['directions'],{'title':'T','abstract':'A'},plan['classification_config'],1)
        db.finish_classification_attempt(evaluation_id,error_code='provider_retryable_error',retryable=True,terminal=False)
        self.assertEqual(db.paper_direction_results([paper_id]),{})
        db.mark_unfinished_jobs_interrupted()
        rows = db.paper_direction_results([paper_id])[paper_id]
        self.assertEqual([r['model_decision'] for r in rows],['failed','failed'])
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch),patch.object(services,'call_llm') as call:
            next_day = self.run_pipeline()
        call.assert_not_called()
        self.assertEqual(next_day['status'],'partial_success')
        self.assertEqual(next_day['progress_details']['classification']['failed_papers'],1)

    def test_openai_token_usage_aliases_are_counted(self):
        key = self.direction()
        response = self.classified((key,'unmatched'))
        response.usage = {'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}
        with patch.object(services,'_fetch_category_from_config',side_effect=self.fetch),patch.object(services,'call_llm',return_value=response):
            job = self.run_pipeline()
        self.assertEqual((job['progress_details']['classification']['input_tokens'],job['progress_details']['classification']['output_tokens']),(10,5))


if __name__ == '__main__':
    unittest.main()
