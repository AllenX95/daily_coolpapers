import unittest
from bs4 import BeautifulSoup

from daily_coolpapers import db, services
from tests import test_team_tracking as data_tests


class TeamTrackingRouteTests(unittest.TestCase):
    setUp = data_tests.TeamTrackingDataTests.setUp
    paper = data_tests.TeamTrackingDataTests.paper
    evaluate = data_tests.TeamTrackingDataTests.evaluate
    form = data_tests.TeamTrackingDataTests.form
    existing = data_tests.TeamTrackingDataTests.existing
    state = data_tests.TeamTrackingDataTests.state
    html_headers = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}

    def post(self, path, values=None, **kwargs):
        return self.client.post(path, data={'csrf_token': 'test-library', **(values or {})}, **kwargs)

    def save(self, paper_id, values=None, **kwargs):
        return self.post(f'/api/papers/{paper_id}/team-tracking', self.form() if values is None else values, **kwargs)

    def test_paper_prefill_is_explicit_and_can_be_corrected_without_metadata_write(self):
        paper_id = self.paper()
        html = self.client.get(f'/papers/{paper_id}').get_data(as_text=True)
        self.assertIn('data-testid="paper-team"', html)
        self.assertIn('不证明第一作者身份', html)
        self.assertEqual(BeautifulSoup(html, 'html.parser').select_one('input[name=author_name]')['value'], 'Ada')
        response = self.save(paper_id, self.form(author_name='User confirmed'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn('User confirmed', html)
        self.assertIn('团队跟踪已保存', html)
        self.assertEqual(db.get_paper(paper_id)['authors_list'], ['Ada'])

    def test_no_author_metadata_renders_empty_prefill(self):
        paper_id = self.paper()
        with db.connect() as conn:
            conn.execute("UPDATE papers SET authors='[]' WHERE id=?", (paper_id,))
        form = services.team_form_model(db.get_paper(paper_id))
        self.assertEqual(form['values']['author_name'], '')
        self.assertEqual(self.client.get(f'/papers/{paper_id}').status_code, 200)

    def test_historical_qualification_and_stop_restore_flow(self):
        paper_id = self.paper()
        self.evaluate(paper_id, 'failed')
        self.assertEqual(self.save(paper_id).status_code, 302)
        original = db.get_paper_team_tracking(paper_id)
        path = f'/api/papers/{paper_id}/team-tracking/archive'
        self.assertEqual(self.post(path).status_code, 302)
        self.assertEqual(self.post(path).status_code, 302)
        html = self.client.get(f'/papers/{paper_id}').get_data(as_text=True)
        self.assertIn('已停止跟踪', html)
        self.assertIn('重新跟踪团队', html)
        self.assertEqual(self.save(paper_id, self.existing(paper_id, tracking_notes='Return')).status_code, 302)
        after = db.get_paper_team_tracking(paper_id)
        self.assertEqual((original['id'], original['created_at']), (after['id'], after['created_at']))

    def test_invalid_no_qualification_missing_paper_and_entity_contracts(self):
        paper_id = self.paper(status=None)
        self.assertNotIn('data-testid="paper-team"', self.client.get(f'/papers/{paper_id}').get_data(as_text=True))
        self.assertEqual(self.save(paper_id).status_code, 409)
        self.assertEqual(self.post(f'/api/papers/{paper_id}/team-tracking/archive').status_code, 409)
        self.assertEqual(self.save(999).status_code, 404)
        self.assertEqual(self.save(2**100).status_code, 404)
        self.assertEqual(self.save(paper_id, self.form(author_mode='oops')).status_code, 400)
        self.evaluate(paper_id, 'success')
        response = self.save(paper_id, self.form(author_mode='existing', author_id=999))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(all(not values for values in self.state().values()))

    def test_csrf_origin_and_get_do_not_mutate(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form())
        before = self.state()
        paths = [f'/api/papers/{paper_id}/team-tracking', f'/api/papers/{paper_id}/team-tracking/archive',
                 '/research-authors/1', '/research-organizations/1']
        for path in paths:
            self.assertEqual(self.client.post(path, data=self.form()).status_code, 403)
            self.assertEqual(self.post(path, self.form(action='archive'), headers={'Origin': 'https://evil.example'}).status_code, 403)
            self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(self.state(), before)

    def test_duplicate_conflict_html_allows_explicit_reuse_with_preserved_draft(self):
        first, second = self.paper(), self.paper(2)
        self.save(first)
        before = self.state()
        response = self.save(second, self.form(author_name='ADA', organization_name='EXAMPLE LAB', tracking_notes='Keep this draft'), headers=self.html_headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state(), before)
        for kind in ('author', 'organization'):
            soup = BeautifulSoup(response.get_data(as_text=True), 'html.parser')
            conflict = next(item for item in soup.select('[data-testid=entity-conflict]') if ('作者' if kind == 'author' else '机构') in item.h2.get_text())
            retry = conflict.find('form')
            fields = {item['name']: item.get('value', '') for item in retry.select('input[name]')}
            self.assertEqual(fields[kind+'_mode'], 'existing')
            self.assertEqual(fields['tracking_notes'], 'Keep this draft')
            response = self.client.post(retry['action'], data=fields, headers=self.html_headers)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_paper_team_tracking(second)['notes'], 'Keep this draft')
        self.assertEqual([len(rows) for rows in self.state().values()], [1, 1, 2])

    def test_archived_entity_requires_explicit_restore_then_separate_save(self):
        paper_id = self.paper()
        self.save(paper_id)
        db.update_research_entity('author', 1, 'archive')
        db.archive_paper_team_tracking(paper_id)
        before = db.get_paper_team_tracking(paper_id)
        response = self.save(paper_id, self.existing(paper_id), headers=self.html_headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn('恢复该作者记录', response.get_data(as_text=True))
        restored = self.post('/research-authors/1', {'action': 'restore', 'return_paper_id': paper_id, 'next': 'https://evil.example'})
        self.assertEqual(restored.status_code, 302)
        self.assertEqual(restored.location, f'/papers/{paper_id}?team_author_id=1#fulltext-result')
        self.assertEqual(db.get_paper_team_tracking(paper_id)['status'], 'archived')
        html = self.client.get(restored.location).get_data(as_text=True)
        self.assertIn('尚未保存团队关系', html)
        self.assertNotIn('evil.example', html)
        self.save(paper_id, self.existing(paper_id))
        self.assertEqual(db.get_paper_team_tracking(paper_id)['created_at'], before['created_at'])

    def test_field_error_html_and_names_are_escaped(self):
        paper_id = self.paper()
        response = self.save(paper_id, self.form(author_name='<script>alert(1)</script>', organization_type=''), headers=self.html_headers)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('role="alert"', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn(f'/papers/{paper_id}#fulltext-result', html)

    def test_malicious_restore_return_is_validated_before_write(self):
        paper_id = self.paper()
        self.save(paper_id)
        db.update_research_entity('author', 1, 'archive')
        before = self.state()
        for value, status in [('https://evil.example', 400), ('99999', 404), (str(2**100), 400)]:
            self.assertEqual(self.post('/research-authors/1', {'action': 'restore', 'return_paper_id': value}).status_code, status)
            self.assertEqual(self.state(), before)

    def test_missing_restore_paper_preserves_json_404_contract(self):
        paper_id = self.paper()
        self.save(paper_id)
        db.update_research_entity('author', 1, 'archive')
        before = self.state()
        for accept in ('application/json', 'text/html'):
            response = self.post('/research-authors/1', {'action': 'restore', 'return_paper_id': '99999'}, headers={'Accept': accept})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.mimetype, accept)
            self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
