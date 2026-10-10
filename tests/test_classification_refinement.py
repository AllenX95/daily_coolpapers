import json
import unittest
from unittest.mock import patch

from daily_coolpapers import app as app_module, db, services
from daily_coolpapers.llm import LLMError, LLMResponse
from tests import test_automatic_abstracts as automatic
from tests.test_crawl_observability import _paper


class ClassificationRefinementTests(unittest.TestCase):
    def setUp(self):
        automatic.AutomaticAbstractTests.setUp(self)
        self.app = app_module.create_app(runner=self.runner, secret_key='test-library')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'test-library'

    def refinement_profile(self):
        profile = db.get_llm_profile(self.profile_id)
        db.save_llm_profile({**profile, 'is_default_classification': 1, 'is_default_refinement': 1})

    def add_paper(self, number, title, abstract):
        return db.upsert_papers([{
            **_paper(f'2610.{number:05}', title),
            'abstract': abstract,
        }], 'cs.AI', '2026-10-10')[0]

    def add_possible(self, paper_id, direction_id):
        with db.connect() as conn:
            conn.execute('''INSERT INTO paper_direction_results
                (paper_id,direction_id,model_decision,model_reason,created_at,updated_at)
                VALUES (?,?,'possible','Concrete link; unresolved scope question.','2026-10-10','2026-10-10')''',
                (paper_id, direction_id))

    def test_jobrunner_payload_keeps_text_and_preserves_confidence_and_manual_priority(self):
        self.refinement_profile()
        direction_id = db.create_attention_direction('Runtime', 'Agent execution environment and recovery')
        accepted = self.add_paper(1, 'Medium confidence title', 'Medium confidence raw abstract.')
        low = self.add_paper(2, 'Low confidence title', 'Low confidence raw abstract.')
        manual = self.add_paper(3, 'Manual decision title', 'Manual decision raw abstract.')
        failed = self.add_paper(4, 'Failed refinement title', 'Failed refinement raw abstract.')
        for paper_id in (accepted, low, manual, failed):
            self.add_possible(paper_id, direction_id)

        prompts = []

        def respond(_profile, prompt, **_kwargs):
            prompts.append(prompt)
            if '论文范围精筛助手' not in prompt:
                return self.response
            if 'Medium confidence title' in prompt:
                result = {'targets': [{'target_type': 'attention_direction', 'target_id': direction_id,
                    'decision': 'matched', 'confidence': 'medium', 'reason': 'The abstract supports a core runtime contribution.',
                    'uncertainty': ''}]}
            elif 'Low confidence title' in prompt:
                result = {'targets': [{'target_type': 'attention_direction', 'target_id': direction_id,
                    'decision': 'matched', 'confidence': 'low', 'reason': 'The likely connection is plausible but weak.',
                    'uncertainty': ''}]}
            elif 'Manual decision title' in prompt:
                # A user decision made while Pro is running must retain priority.
                db.set_classification_decision(manual, 'attention_direction', direction_id, 'rejected')
                result = {'targets': [{'target_type': 'attention_direction', 'target_id': direction_id,
                    'decision': 'matched', 'confidence': 'medium', 'reason': 'The abstract supports the direction.',
                    'uncertainty': ''}]}
            else:
                raise LLMError('fixture provider failure', retryable=False)
            return LLMResponse(json.dumps(result), result, {'input_tokens': 5, 'output_tokens': 3})

        relations = [(paper_id, 'attention_direction', direction_id)
                     for paper_id in (accepted, low, manual, failed)]
        with patch.object(services, 'call_llm', side_effect=respond):
            job_id = self.runner.enqueue_classification_refinement(relations)
            self.runner._run_job(job_id)

        self.assertEqual(db.get_job(job_id)['status'], 'partial_success')
        for title, abstract in [
            ('Medium confidence title', 'Medium confidence raw abstract.'),
            ('Low confidence title', 'Low confidence raw abstract.'),
            ('Manual decision title', 'Manual decision raw abstract.'),
            ('Failed refinement title', 'Failed refinement raw abstract.'),
        ]:
            self.assertTrue(any(title in prompt and abstract in prompt for prompt in prompts))

        rows = {row['paper_id']: row for paper_id in (accepted, low, manual, failed)
                for row in db.classification_results([paper_id])[paper_id]}
        self.assertEqual((rows[accepted]['pro_decision'], rows[accepted]['pro_confidence'], rows[accepted]['effective']),
                         ('matched', 'medium', True))
        self.assertEqual((rows[low]['pro_decision'], rows[low]['pro_confidence'], rows[low]['effective'], rows[low]['pending']),
                         ('matched', 'low', False, True))
        self.assertEqual((rows[manual]['pro_decision'], rows[manual]['manual_decision'], rows[manual]['effective']),
                         ('matched', 'rejected', False))
        self.assertEqual((rows[failed]['pro_decision'], rows[failed]['model_decision'], rows[failed]['pending']),
                         ('failed', 'possible', True))
        self.assertIsNotNone(db.get_latest_evaluation(accepted, 'abstract_review'))
        for paper_id in (low, manual, failed):
            self.assertIsNone(db.get_latest_evaluation(paper_id, 'abstract_review'))

        review_plan = services.classification_review_plan(accepted, 'attention_direction', direction_id)
        review_result = {'targets': [{'target_type': 'attention_direction', 'target_id': direction_id,
            'decision': 'unmatched', 'confidence': 'high', 'reason': 'Review suggests the scope may be too broad.',
            'uncertainty': ''}]}
        with patch.object(services, 'call_llm', return_value=LLMResponse(
                json.dumps(review_result), review_result, {'input_tokens': 4, 'output_tokens': 2})):
            review_job_id = self.runner.enqueue_classification_review(review_plan)
            self.runner._run_job(review_job_id)
        accepted_after_review = db.classification_results([accepted])[accepted][0]
        self.assertEqual((accepted_after_review['pro_decision'], accepted_after_review['effective']), ('matched', True))
        review = db.latest_classification_refinement_review(accepted)
        self.assertEqual(review['proposed'][0]['decision'], 'unmatched')

    def test_manual_classification_without_fulltext_legacy_theme_projection_and_restore(self):
        paper_id = self.add_paper(10, 'Paper without fulltext', 'Raw abstract is available.')
        other_paper_id = self.add_paper(11, 'Paper without a theme result row', 'Another raw abstract.')
        direction_id = db.create_attention_direction('Runtime', 'Agent execution environment and recovery')
        theme_id = db.create_investment_theme('AI Infrastructure', 'Compute and serving infrastructure for AI systems')
        with db.connect() as conn:
            conn.execute('''INSERT INTO paper_direction_results
                (paper_id,direction_id,model_decision,model_reason,created_at,updated_at)
                VALUES (?,?,'unmatched','Flash said outside the scope.','2026-10-10','2026-10-10')''',
                (paper_id, direction_id))

        # Simulate the old association table, then run the migration's one-time projection.
        with db.connect() as conn:
            conn.execute('INSERT INTO paper_investment_themes(paper_id,theme_id,created_at) VALUES (?,?,?)',
                         (paper_id, theme_id, '2026-10-10'))
        db.init_db()
        db.set_classification_decision(paper_id, 'investment_theme', theme_id, 'rejected')
        db.init_db()
        after_migration = {row['target_type']: row for row in db.classification_results([paper_id])[paper_id]}
        self.assertEqual(after_migration['investment_theme']['manual_decision'], 'rejected')
        db.set_classification_decision(paper_id, 'investment_theme', theme_id, 'confirmed')
        response = self.client.post(f'/papers/{paper_id}/classification-decision', data={
            'csrf_token': 'test-library', 'target_type': 'attention_direction',
            'target_id': str(direction_id), 'decision': 'confirmed',
        })
        self.assertEqual(response.status_code, 302)
        rows = {row['target_type']: row for row in db.classification_results([paper_id])[paper_id]}
        self.assertTrue(rows['attention_direction']['effective'])
        self.assertEqual(rows['attention_direction']['effective_source'], '人工确认')
        self.assertTrue(rows['investment_theme']['effective'])
        self.assertEqual(rows['investment_theme']['manual_decision'], 'confirmed')
        self.assertIsNone(db.get_latest_evaluation(paper_id, 'fulltext_review'))

        db.remove_classification_targets([(paper_id, 'attention_direction', direction_id)], '范围不符', 'manual correction')
        after_remove = {row['target_type']: row for row in db.classification_results([paper_id])[paper_id]}
        self.assertTrue(after_remove['attention_direction']['manual_removed'])
        self.assertFalse(after_remove['attention_direction']['effective'])
        self.assertTrue(after_remove['investment_theme']['effective'])
        db.restore_classification_target(paper_id, 'attention_direction', direction_id)
        after_restore = {row['target_type']: row for row in db.classification_results([paper_id])[paper_id]}
        self.assertTrue(after_restore['attention_direction']['effective'])
        self.assertTrue(after_restore['investment_theme']['effective'])

        # A paper with no investment classification ledger row remains eligible for target backfill preview.
        preview = services.classification_target_backfill_preview(
            'investment_theme', theme_id, '2026-10-10', '2026-10-10')
        self.assertIn(other_paper_id, preview['paper_ids'])
        self.assertEqual(preview['counts']['input_incomplete'], 0)


if __name__ == '__main__':
    unittest.main()
