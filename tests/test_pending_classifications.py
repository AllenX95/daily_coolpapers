import unittest
from bs4 import BeautifulSoup

from daily_coolpapers import db
from tests import test_personal_library as library


class PendingClassificationTests(unittest.TestCase):
    setUp = library.PersonalLibraryTests.setUp
    paper = library.PersonalLibraryTests.paper

    def direction(self, name='Agents'):
        return db.create_attention_direction(name, 'User-written scope')

    def model(self, paper_id, direction_id, reason='Original reason', state='possible'):
        with db.connect() as conn:
            conn.execute(
                '''INSERT INTO paper_direction_results
                   (paper_id,direction_id,model_decision,model_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)''',
                (paper_id, direction_id, state, reason, '2026-09-03', '2026-09-03'),
            )

    def set_crawl_date(self, paper_id, crawl_date):
        with db.connect() as conn:
            conn.execute('UPDATE paper_categories SET crawl_date=? WHERE paper_id=?', (crawl_date, paper_id))

    def post_batch(self, relations, decision='confirmed', **kwargs):
        return self.client.post(
            '/pending-classifications/batch',
            data={'csrf_token': 'test-library', 'decision': decision, 'relationship': relations},
            **kwargs,
        )

    def test_default_lists_all_dates_only_active_pending_and_keeps_pairs_distinct(self):
        first, second, resolved, archived = [self.paper(i, status=None) for i in range(1, 5)]
        old_direction, recent_direction, resolved_direction, archived_direction = [
            self.direction(name) for name in ('Old', 'Recent', 'Resolved', 'Archived')
        ]
        self.set_crawl_date(first, '2025-01-02')
        self.model(first, old_direction, '<model> reason')
        self.model(first, recent_direction, 'second direction reason')
        self.model(second, recent_direction)
        self.model(resolved, resolved_direction)
        db.set_direction_decision(resolved, resolved_direction, 'rejected')
        self.model(archived, archived_direction)
        db.archive_attention_direction(archived_direction)

        page = db.list_pending_direction_page(page_size=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['pending_relationships'], 3)
        self.assertEqual(page['pages'], 2)
        self.assertEqual(page['items'][0]['id'], second)
        self.assertEqual([item['direction_id'] for item in page['items'][0]['pending_directions']], [recent_direction])
        self.assertEqual(db.list_pending_direction_page(direction_id=old_direction)['total'], 1)
        self.assertEqual(db.list_pending_direction_page(date_from='2025-01-01', date_to='2025-12-31')['total'], 1)
        self.assertEqual(db.list_pending_direction_page(direction_id=resolved_direction)['total'], 0)
        self.assertEqual(db.list_pending_direction_page(direction_id=archived_direction)['total'], 0)

    def test_page_shows_each_direction_reason_and_selection_tokens(self):
        paper = self.paper(status=None)
        first, second = self.direction('Agents'), self.direction('<script>Vision</script>')
        self.model(paper, first, 'Agent reason')
        self.model(paper, second, '<script>escaped reason</script>')
        with db.connect() as conn:
            conn.execute("UPDATE paper_direction_results SET pro_decision='possible',pro_reason='Needs review',pro_confidence='low' WHERE paper_id=?", (paper,))

        response = self.client.get('/pending-classifications?status=manual')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.data, 'html.parser')
        self.assertTrue(soup.select_one('a.nav-link[aria-current="page"]'))
        checkboxes = soup.select('input.pending-classification-checkbox[name="relationship"]')
        self.assertEqual({item['value'] for item in checkboxes}, {
            f'{paper}:attention_direction:{first}', f'{paper}:attention_direction:{second}'})
        self.assertIn('Agent reason', response.get_data(as_text=True))
        self.assertIn('&lt;script&gt;escaped reason&lt;/script&gt;', response.get_data(as_text=True))
        self.assertNotIn('<script>escaped reason</script>', response.get_data(as_text=True))

    def test_batch_decisions_preserve_model_and_redirect_with_filters(self):
        paper = self.paper(status=None)
        first, second = self.direction('Agents'), self.direction('Vision')
        self.model(paper, first, 'Keep this reason')
        self.model(paper, second, 'Keep this too')
        response = self.post_batch(
            [f'{paper}:{first}', f'{paper}:{second}'],
            decision='confirmed',
            query_string={'target_type': 'attention_direction', 'target_id': first,
                          'date_from': '2026-09-01', 'date_to': '2026-09-05', 'page': 2, 'page_size': 15},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('target_type=attention_direction', response.location)
        self.assertIn('target_id=1', response.location)
        self.assertIn('date_from=2026-09-01', response.location)
        self.assertIn('page=2', response.location)
        rows = db.paper_direction_results([paper])[paper]
        self.assertEqual([row['manual_decision'] for row in rows], ['confirmed', 'confirmed'])
        self.assertEqual([row['model_decision'] for row in rows], ['possible', 'possible'])
        self.assertEqual([row['model_reason'] for row in rows], ['Keep this reason', 'Keep this too'])
        # Manual confirmation queues the missing abstract evaluation once per paper.
        self.assertFalse(self.runner.queue.empty())
        self.llm.assert_not_called()

    def test_stale_pair_rejects_the_whole_batch_and_csrf_and_bad_tokens_write_nothing(self):
        first_paper, second_paper = self.paper(1, status=None), self.paper(2, status=None)
        first_direction, second_direction = self.direction('First'), self.direction('Second')
        self.model(first_paper, first_direction)
        self.model(second_paper, second_direction)
        db.set_direction_decision(second_paper, second_direction, 'rejected')

        response = self.post_batch([f'{first_paper}:{first_direction}', f'{second_paper}:{second_direction}'])
        self.assertEqual(response.status_code, 409)
        self.assertIn('部分所选分类已处理', response.get_data(as_text=True))
        self.assertIsNone(db.paper_direction_results([first_paper])[first_paper][0]['manual_decision'])

        no_csrf = self.client.post('/pending-classifications/batch', data={
            'decision': 'confirmed', 'relationship': f'{first_paper}:{first_direction}',
        })
        self.assertEqual(no_csrf.status_code, 403)
        bad_pair = self.post_batch(['bad-token'])
        self.assertEqual(bad_pair.status_code, 400)
        empty = self.client.post('/pending-classifications/batch', data={
            'csrf_token': 'test-library', 'decision': 'rejected',
        })
        self.assertEqual(empty.status_code, 400)
        self.assertIsNone(db.paper_direction_results([first_paper])[first_paper][0]['manual_decision'])

    def test_non_pending_or_archived_pairs_and_duplicates_cannot_partially_write(self):
        pending_paper, matched_paper, unmatched_paper, archived_paper = [
            self.paper(i, status=None) for i in range(1, 5)
        ]
        active, archived = self.direction('Active'), self.direction('Archived')
        self.model(pending_paper, active)
        self.model(matched_paper, active, state='matched')
        self.model(unmatched_paper, active, state='unmatched')
        self.model(archived_paper, archived)
        db.archive_attention_direction(archived)
        valid_pair = f'{pending_paper}:{active}'

        mixed = self.post_batch([
            valid_pair,
            f'{matched_paper}:{active}',
            f'{unmatched_paper}:{active}',
            f'{archived_paper}:{archived}',
            f'999999:{active}',
        ])
        self.assertEqual(mixed.status_code, 409)
        self.assertIsNone(db.paper_direction_results([pending_paper])[pending_paper][0]['manual_decision'])

        duplicate = self.post_batch([valid_pair, valid_pair])
        self.assertEqual(duplicate.status_code, 400)
        self.assertIsNone(db.paper_direction_results([pending_paper])[pending_paper][0]['manual_decision'])

        rejected = self.post_batch([valid_pair], decision='rejected')
        self.assertEqual(rejected.status_code, 302)
        row = db.paper_direction_results([pending_paper])[pending_paper][0]
        self.assertEqual((row['model_decision'], row['manual_decision']), ('possible', 'rejected'))


if __name__ == '__main__':
    unittest.main()
