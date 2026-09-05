import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

from daily_coolpapers import db, services
from tests import test_team_tracking as data_tests


class ResearchEntityLibraryTests(unittest.TestCase):
    setUp = data_tests.TeamTrackingDataTests.setUp
    paper = data_tests.TeamTrackingDataTests.paper
    evaluate = data_tests.TeamTrackingDataTests.evaluate
    form = data_tests.TeamTrackingDataTests.form
    existing = data_tests.TeamTrackingDataTests.existing
    state = data_tests.TeamTrackingDataTests.state

    def seed(self):
        first, second, third = [self.paper(number, title=f'Paper {number}') for number in (1, 2, 3)]
        db.save_paper_team_tracking(first, self.form(author_category='academic', organization_type='university'))
        db.save_paper_team_tracking(second, self.form(author_mode='existing', author_id=1,
            organization_name='Company 2', organization_type='company', organization_region='中国'))
        db.save_paper_team_tracking(third, self.form(author_name='Grace', author_category='industry', organization_mode='existing', organization_id=2))
        db.archive_paper_team_tracking(second)
        return first, second, third

    def post(self, path, **values):
        return self.client.post(path, data={'csrf_token': 'test-library', **values}, follow_redirects=True)

    def test_views_counts_history_and_partner_links_are_correct(self):
        self.seed()
        authors = {row['id']: row for row in db.list_research_entities('author')}
        self.assertEqual((authors[1]['paper_count'], authors[1]['related_count'], authors[1]['tracking_count']), (2, 2, 1))
        self.assertEqual({row['name'] for row in authors[1]['related_entities']}, {'Example Lab', 'Company 2'})
        organizations = {row['id']: row for row in db.list_research_entities('organization')}
        self.assertEqual((organizations[2]['paper_count'], organizations[2]['related_count'], organizations[2]['tracking_count']), (2, 2, 1))
        for view, expected in [('tracking', 2), ('authors', 2), ('organizations', 2)]:
            response = self.client.get('/research-entities?view='+view)
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.get_data(as_text=True), 'html.parser')
            self.assertEqual(len(soup.select('.research-record')), expected)
            self.assertIn('研究对象', soup.select_one('.main-nav').get_text())

    def test_filters_name_category_type_and_state(self):
        self.seed()
        self.assertEqual([row['paper_id'] for row in db.list_team_tracking(query='ＡＤＡ')], [1])
        self.assertEqual([row['paper_id'] for row in db.list_team_tracking(query='Paper 2', status='all')], [2])
        self.assertEqual([row['paper_id'] for row in db.list_team_tracking(author_category='industry', organization_type='company')], [3])
        self.assertEqual([row['paper_id'] for row in db.list_team_tracking(status='archived')], [2])
        self.assertEqual([row['id'] for row in db.list_research_entities('author', organization_type='university')], [1])
        self.assertEqual([row['id'] for row in db.list_research_entities('organization', author_category='industry')], [2])
        self.assertEqual([row['id'] for row in db.list_research_entities('author', query='  ＡＤＡ ')], [1])
        db.update_research_entity('author', 1, 'archive')
        self.assertEqual([row['id'] for row in db.list_research_entities('author', status='archived')], [1])
        self.assertEqual(len(db.list_team_tracking()), 2)  # Entity archive is not stop-tracking.

    def test_entity_scoped_paper_lists_include_both_statuses_and_precise_pairs(self):
        self.seed()
        model = services.research_entities_model({'view': 'tracking', 'status': 'all', 'author_id': '1'})
        self.assertEqual({row['paper_id'] for row in model['rows']}, {1, 2})
        self.assertEqual(model['scoped']['author']['name'], 'Ada')
        paired = services.research_entities_model({'status': 'all', 'author_id': '1', 'organization_id': '2'})
        self.assertEqual([row['paper_id'] for row in paired['rows']], [2])
        for query, status in [('author_id=99999', 404), ('organization_id=99999', 404),
                              ('author_id=1 OR 1=1', 400), ('view=nope', 400), ('status=wrong', 400),
                              ('view=authors&author_category=nope', 400), ('organization_type=nope', 400)]:
            self.assertEqual(self.client.get('/research-entities?'+query).status_code, status)

    def test_recent_papers_are_three_by_publication_date_without_duplicate_partners(self):
        first = self.paper(1)
        db.save_paper_team_tracking(first, self.form())
        for number in range(2, 6):
            paper_id = self.paper(number)
            db.save_paper_team_tracking(paper_id, self.existing(first))
            with db.connect() as conn:
                conn.execute('UPDATE papers SET published_at=? WHERE id=?', (f'2026-09-{number:02}', paper_id))
        author = db.list_research_entities('author')[0]
        self.assertEqual([row['id'] for row in author['recent_papers']], [5, 4, 3])
        self.assertEqual((author['paper_count'], author['related_count'], len(author['related_entities'])), (5, 1, 1))

    def test_entity_management_post_renders_new_state_and_preserves_relations(self):
        self.seed()
        before = self.state()['paper_team_tracking']
        for kind, path in [('author', '/research-authors/1'), ('organization', '/research-organizations/1')]:
            response = self.post(path, action='update', name='Updated '+kind, author_category='hybrid', organization_type='research_institute', region='中国', notes='手工判断')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Updated '+kind, response.get_data(as_text=True))
            self.assertEqual(db.get_research_entity(kind, 1)['notes'], '手工判断')
            for action, status in [('archive', 'archived'), ('restore', 'active')]:
                self.assertEqual(self.post(path, action=action).status_code, 200)
                self.assertEqual(db.get_research_entity(kind, 1)['status'], status)
            self.assertEqual(self.state()['paper_team_tracking'], before)

    def test_entity_management_does_not_need_fulltext_and_has_no_delete(self):
        self.seed()
        with db.connect() as conn:
            conn.execute('DELETE FROM evaluations')
        self.assertEqual(self.post('/research-authors/1', action='archive').status_code, 200)
        self.assertEqual(self.post('/research-organizations/1', action='restore').status_code, 200)
        self.assertEqual(self.post('/research-authors/1', action='delete').status_code, 400)
        self.assertEqual(self.client.delete('/research-authors/1').status_code, 405)
        self.assertEqual(self.client.get('/research-entities?status=all').status_code, 200)

    def test_entity_rename_conflict_html_retains_old_identity(self):
        self.seed()
        before = self.state()
        response = self.client.post('/research-authors/2', data={'csrf_token': 'test-library', 'action': 'update', 'name': 'ADA'}, headers={'Accept': 'text/html'})
        self.assertEqual(response.status_code, 409)
        html = response.get_data(as_text=True)
        self.assertIn('作者 #1', html)
        self.assertIn('返回研究对象', html)
        self.assertIn('查看冲突记录', html)
        self.assertNotIn('使用该作者记录并重试', html)  # Editing must never merge paper identities.
        self.assertEqual(self.state(), before)

    def test_read_only_gets_and_batch_query_counts(self):
        for number in range(1, 13):
            paper_id = self.paper(number)
            db.save_paper_team_tracking(paper_id, self.form(author_name='Author '+str(number), organization_name='Org '+str(number)))
        original, statements = db.connect, []
        def read_only():
            conn = original()
            conn.execute('PRAGMA query_only=ON')
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, 'connect', side_effect=read_only):
            for operation, limit in [(lambda: db.list_team_tracking(), 1),
                                     (lambda: db.list_research_entities('author'), 3),
                                     (lambda: db.list_research_entities('organization'), 3)]:
                statements.clear()
                self.assertEqual(len(operation()), 12)
                selects = [sql for sql in statements if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
                self.assertLessEqual(len(selects), limit)
            for path in ('/research-entities', '/research-entities?view=authors', '/research-entities?view=organizations', '/papers/1'):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_text_escaping_search_binding_and_orphan_entities_remain_visible(self):
        paper_id = self.paper(title='<img src=x onerror=alert(1)>')
        db.save_paper_team_tracking(paper_id, self.form(author_name='<script>evil</script>', organization_name='Lab <img>', tracking_notes='<svg onload=evil()>'))
        for view in ('tracking', 'authors', 'organizations'):
            html = self.client.get('/research-entities?view='+view).get_data(as_text=True)
            self.assertNotIn('<script>evil</script>', html)
            self.assertNotIn('<svg onload=evil()>', html)
            self.assertNotIn('<img src=x onerror=alert(1)>', html)
        self.assertEqual(db.list_team_tracking(query="' OR 1=1 --"), [])
        with db.connect() as conn:
            conn.execute('DELETE FROM papers WHERE id=?', (paper_id,))
        author = db.list_research_entities('author')[0]
        self.assertEqual((author['paper_count'], author['related_count']), (0, 0))
        self.assertEqual(author['recent_papers'], [])


if __name__ == '__main__':
    unittest.main()
