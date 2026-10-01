import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import app as app_module, db


class _HiddenFormParser(HTMLParser):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.active = False
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.active = attrs.get('action') == self.action
        elif self.active and tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name'):
            self.fields[attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form' and self.active:
            self.active = False


class CollectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'main.sqlite3'
        self.db_patch = patch.object(db, 'DB_PATH', self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.ensure_patch = patch.object(db, 'ensure_directories')
        self.ensure_patch.start()
        self.addCleanup(self.ensure_patch.stop)
        self.pdf_patch = patch.object(app_module, 'has_pdf', return_value=False)
        self.markdown_patch = patch.object(app_module, 'has_markdown', return_value=False)
        self.pdf_patch.start()
        self.markdown_patch.start()
        self.addCleanup(self.pdf_patch.stop)
        self.addCleanup(self.markdown_patch.stop)
        db.init_db()
        self.app = app_module.create_app(secret_key='collection-routes')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'collection-routes'

    def paper(self, number: int, score: int = 80) -> int:
        paper_id = db.upsert_papers([{
            'arxiv_id': f'2609.{number:05}',
            'title': f'Collection Paper {number}',
            'authors': ['Ada'],
            'abstract': 'A collection route fixture.',
            'subjects': ['cs.AI'],
            'published_at': '2026-09-01',
            'rank': number,
        }], 'cs.AI', '2026-09-01')[0]
        db.create_evaluation(
            paper_id, 'fulltext_review', None, None, None, 'fixture-model', 'success',
            {
                'score': score,
                'attention': 'read',
                'one_sentence_summary': 'Collection summary',
                'detailed_summary_zh': 'Collection detail',
                'vc_perspective': {'impact': 'Collection impact'},
            }, '{}', None,
        )
        return paper_id

    def post(self, path, **data):
        return self.client.post(
            path,
            data={'csrf_token': 'collection-routes', **data},
        )

    def hidden_form(self, html, action):
        parser = _HiddenFormParser(action)
        parser.feed(html)
        return parser.fields

    def test_three_collection_routes_report_global_totals_and_reset_filters(self):
        papers = [self.paper(number, score=number * 10) for number in range(1, 6)]
        for paper_id in papers:
            db.set_paper_decision(paper_id, 'favorite')
        theme_id = db.create_investment_theme('Route theme')
        for paper_id in papers[:3]:
            db.set_paper_investment_themes(paper_id, [theme_id])

        cases = [
            ('/favorites?page=2&page_size=2', '共 5 条', 'Collection Paper 3'),
            ('/reviewed-papers?decision=favorite&page=2&page_size=2', '共 5 条', 'Collection Paper 3'),
            (f'/investment-themes/{theme_id}/papers?page=2&page_size=2', '共 3 条', 'Collection Paper 1'),
        ]
        for path, count_text, paper_text in cases:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            self.assertIn(count_text, html)
            self.assertIn(paper_text, html)

        html = self.client.get('/reviewed-papers?decision=favorite&sort=title&page=99&page_size=2').get_data(as_text=True)
        self.assertIn('返回末页', html)
        self.assertIn('第 99 页超出范围', html)
        self.assertIn('name="page" value="1"', html)

    def test_detail_actions_return_to_recognized_collection_and_clamp_last_page(self):
        paper_ids = [self.paper(number) for number in range(1, 4)]
        for paper_id in paper_ids:
            db.set_paper_decision(paper_id, 'favorite')
        list_url = '/favorites?sort=title&page=2&page_size=2'
        detail = self.client.get(f'/papers/{paper_ids[-1]}?collection=favorites&sort=title&page=2&page_size=2')
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn('name="collection" value="favorites"', html)
        self.assertIn('name="page" value="2"', html)
        self.assertIn('href="/favorites?sort=title&amp;page=2&amp;page_size=2"', html)

        response = self.post(
            f'/api/papers/{paper_ids[-1]}/decision',
            decision='clear', collection='favorites', sort='title', page='2', page_size='2',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/favorites?sort=title&page=1&page_size=2')

        reviewed_detail = self.client.get(
            f'/papers/{paper_ids[0]}?collection=reviewed&decision=favorite&sort=title&page=2&page_size=2'
        )
        reviewed_html = reviewed_detail.get_data(as_text=True)
        self.assertIn('name="return_decision" value="favorite"', reviewed_html)
        decision_action = f'/api/papers/{paper_ids[0]}/decision'
        rendered_form = self.hidden_form(reviewed_html, decision_action)
        self.assertEqual(rendered_form['return_decision'], 'favorite')
        rendered_form['decision'] = 'skipped'
        response = self.client.post(decision_action, data=rendered_form)
        self.assertEqual(response.location, '/reviewed-papers?sort=title&decision=favorite&page=1&page_size=2')

        theme_id = db.create_investment_theme('Remove route theme')
        db.set_paper_investment_themes(paper_ids[0], [theme_id])
        response = self.post(
            f'/api/papers/{paper_ids[0]}/investment-themes/{theme_id}/remove',
            collection='theme', theme_id=str(theme_id), sort='added_desc', page='2', page_size='2',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, f'/investment-themes/{theme_id}/papers?sort=added_desc&page=1&page_size=2')

    def test_unrecognized_collection_context_cannot_redirect_off_site(self):
        paper_id = self.paper(1)
        response = self.post(
            f'/api/papers/{paper_id}/decision',
            decision='favorite', collection='https://evil.invalid', next='https://evil.invalid',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')


if __name__ == '__main__':
    unittest.main()
