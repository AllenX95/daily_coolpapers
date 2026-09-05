import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from daily_coolpapers import app as app_module, db, services
from daily_coolpapers.jobs import JobRunner


class PersonalLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(db, 'DB_PATH', Path(self.tmp.name)/'main.sqlite3'))
        self.stack.enter_context(patch.object(db, 'LLM_PROFILES_DB_PATH', Path(self.tmp.name)/'profiles.sqlite3'))
        self.stack.enter_context(patch.object(db, 'ensure_directories'))
        self.stack.enter_context(patch.object(app_module, 'has_pdf', return_value=False))
        self.stack.enter_context(patch.object(app_module, 'has_markdown', return_value=False))
        self.llm = self.stack.enter_context(patch.object(services, 'call_llm', side_effect=AssertionError('LLM forbidden')))
        self.fetch = self.stack.enter_context(patch.object(services, 'fetch_category_report', side_effect=AssertionError('HTTP forbidden')))
        db.init_db()
        db.init_llm_profiles_db()
        self.runner = JobRunner()
        self.app = app_module.create_app(runner=self.runner, secret_key='test-library')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'test-library'

    def paper(self, number=1, title=None, status='success', score=80):
        paper_id = db.upsert_papers([{
            'arxiv_id': f'2609.{number:05}', 'title': title or f'Library Paper {number}',
            'authors': ['Ada'], 'abstract': 'A synthetic research paper for local acceptance.',
            'subjects': ['cs.AI'], 'published_at': '2026-09-03', 'rank': number,
        }], 'cs.AI', '2026-09-03')[0]
        if status:
            self.evaluate(paper_id, status, score=score)
        return paper_id

    def evaluate(self, paper_id, status, kind='fulltext_review', score=80):
        return db.create_evaluation(paper_id, kind, None, None, None, 'fixture-model', status,
            {'score': score, 'attention': 'read', 'one_sentence_summary': 'Synthetic finding',
             'detailed_summary_zh': '隔离验收样本：论文结果与个人判断分别存储。',
             'vc_perspective': {'impact': '仅供产品验收，不代表投资建议。'}} if status == 'success' else None,
            '{}', 'Synthetic failure' if status == 'failed' else None)

    def decide(self, paper_id, decision, **kwargs):
        return self.client.post(f'/api/papers/{paper_id}/decision',
            data={'csrf_token': 'test-library', 'decision': decision}, **kwargs)

    def snapshot(self):
        with db.connect() as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                    for table in ('papers', 'evaluations', 'paper_categories', 'jobs')}

    def test_history_stays_undecided_and_favorites_are_explicit(self):
        paper_id = self.paper()
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'undecided')
        self.assertEqual(services.favorite_papers_page_model()['papers'], [])
        reviewed = services.reviewed_papers_page_model()['papers']
        self.assertEqual([item['id'] for item in reviewed], [paper_id])
        self.assertEqual(reviewed[0]['decision_label'], '未处理')
        self.assertNotIn('Library Paper 1', self.client.get('/favorites').get_data(as_text=True))
        self.assertIn('Library Paper 1', self.client.get('/reviewed-papers').get_data(as_text=True))
        self.decide(paper_id, 'favorite')
        self.assertIn('Library Paper 1', self.client.get('/favorites?decision=all').get_data(as_text=True))
        self.decide(paper_id, 'skipped')
        self.assertNotIn('Library Paper 1', self.client.get('/favorites?decision=all').get_data(as_text=True))

    def test_decision_cycle_redirect_feedback_and_no_side_effects(self):
        paper_id = self.paper()
        before = self.snapshot()
        for submitted, expected, button in [('favorite', 'favorite', '取消收藏，恢复未处理'),
                                             ('skipped', 'skipped', '恢复未处理'),
                                             ('favorite', 'favorite', '已收藏'),
                                             ('clear', 'undecided', '收藏'), ('clear', 'undecided', '收藏')]:
            response = self.decide(paper_id, submitted)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')
            self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], expected)
            html = self.client.get(response.location).get_data(as_text=True)
            self.assertIn(button, html)
            self.assertIn('decision-feedback', html)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.runner.queue.empty())
        self.llm.assert_not_called()
        self.fetch.assert_not_called()

    def test_timestamp_idempotency_and_clear_recreates_record(self):
        paper_id = self.paper()
        for decision, timestamp in [('favorite', '2026-09-01 01:00:00'), ('favorite', '2026-09-02 01:00:00')]:
            with patch.object(db, 'now_iso', return_value=timestamp):
                db.set_paper_decision(paper_id, decision)
        state = db.get_paper_decision_state(paper_id)
        self.assertEqual((state['created_at'], state['updated_at']), ('2026-09-01 01:00:00',)*2)
        with patch.object(db, 'now_iso', return_value='2026-09-03 01:00:00'):
            db.set_paper_decision(paper_id, 'skipped')
        state = db.get_paper_decision_state(paper_id)
        self.assertEqual(state['created_at'], '2026-09-01 01:00:00')
        self.assertEqual(state['updated_at'], '2026-09-03 01:00:00')
        db.set_paper_decision(paper_id, 'clear')
        with patch.object(db, 'now_iso', return_value='2026-09-04 01:00:00'):
            db.set_paper_decision(paper_id, 'favorite')
        self.assertEqual(db.get_paper_decision_state(paper_id)['created_at'], '2026-09-04 01:00:00')

    def test_no_fulltext_or_only_failed_or_only_abstract_is_ineligible(self):
        for number, status in enumerate((None, 'failed', 'pending', 'running'), 1):
            paper_id = self.paper(number, status=status)
            self.evaluate(paper_id, 'success', kind='abstract_review')
            html = self.client.get(f'/papers/{paper_id}').get_data(as_text=True)
            self.assertNotIn('data-testid="personal-decision"', html)
            for decision in ('favorite', 'skipped', 'clear'):
                self.assertEqual(self.decide(paper_id, decision).status_code, 409)
            self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'undecided')
        self.assertEqual(services.reviewed_papers_page_model()['papers'], [])

    def test_latest_failed_retains_success_eligibility_and_decision(self):
        paper_id = self.paper(score=88)
        self.decide(paper_id, 'favorite')
        self.evaluate(paper_id, 'failed')
        html = self.client.get(f'/papers/{paper_id}').get_data(as_text=True)
        self.assertIn('最新一次全文任务失败', html)
        self.assertIn('data-testid="personal-decision"', html)
        self.assertIn('已收藏', html)
        self.assertEqual(services.favorite_papers_page_model()['papers'][0]['score_text'], '88')
        self.assertEqual(self.decide(paper_id, 'skipped').status_code, 302)
        self.assertTrue(db.has_successful_fulltext(paper_id))

    def test_route_contract_security_unknown_and_invalid_zero_writes(self):
        paper_id = self.paper()
        url = f'/api/papers/{paper_id}/decision'
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, data={'decision': 'favorite'}).status_code, 403)
        self.assertEqual(self.decide(paper_id, 'favorite', headers={'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(self.decide(paper_id, 'favorite', headers={'Referer': 'https://evil.invalid/'}).status_code, 403)
        for decision in ('', 'invalid', 'undecided', "favorite'; DELETE FROM papers;--"):
            self.assertEqual(self.decide(paper_id, decision).status_code, 400)
        for decision in ('favorite', 'skipped', 'clear'):
            self.assertEqual(self.decide(99999, decision).status_code, 404)
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'undecided')
        self.assertIsNotNone(db.get_paper(paper_id))
        self.assertEqual(self.client.get('/reviewed-papers?decision=bad').status_code, 400)

    def test_return_url_cannot_redirect_off_site(self):
        paper_id = self.paper()
        response = self.client.post(f'/api/papers/{paper_id}/decision?next=https://evil.invalid',
            data={'csrf_token': 'test-library', 'decision': 'favorite', 'next': '//evil.invalid'})
        self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')

    def test_db_validation_and_eligibility_use_same_write_transaction(self):
        paper_id = self.paper()
        original = db.has_successful_fulltext
        def checked(paper_id, *, conn=None):
            self.assertIsNotNone(conn)
            self.assertTrue(conn.in_transaction)
            return original(paper_id, conn=conn)
        with patch.object(db, 'has_successful_fulltext', side_effect=checked) as check:
            db.set_paper_decision(paper_id, 'favorite')
            self.assertTrue(services.paper_decision_model(paper_id)['eligible'])
        self.assertEqual(check.call_count, 2)
        with self.assertRaises(ValueError):
            db.set_paper_decision(paper_id, 'bad')
        with self.assertRaises(db.PaperNotFoundError):
            db.set_paper_decision(99999, 'favorite')
        with db.connect() as conn:
            conn.execute('DELETE FROM evaluations WHERE paper_id=?', (paper_id,))
        with self.assertRaises(db.FulltextRequiredError):
            db.set_paper_decision(paper_id, 'skipped')
        self.assertEqual(self.decide(paper_id, 'clear').status_code, 409)
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'favorite')

    def test_database_failure_rolls_back_prior_decision(self):
        paper_id = self.paper()
        db.set_paper_decision(paper_id, 'favorite')
        before = db.get_paper_decision_state(paper_id)
        with db.connect() as conn:
            conn.execute("CREATE TRIGGER fail_decision BEFORE UPDATE ON paper_dispositions BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            db.set_paper_decision(paper_id, 'skipped')
        self.assertEqual(db.get_paper_decision_state(paper_id), before)

    def test_simultaneous_duplicate_decisions_remain_one_record(self):
        paper_id = self.paper()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: db.set_paper_decision(paper_id, 'favorite'), range(8)))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_dispositions').fetchone()[0], 1)

    def test_filters_sorting_and_latest_success_deduplicate_papers(self):
        alpha = self.paper(1, title='Alpha paper', score=70)
        beta = self.paper(2, title='Beta paper', score=90)
        gamma = self.paper(3, title='Gamma paper', score=80)
        self.evaluate(alpha, 'success', score=95)
        self.evaluate(alpha, 'failed')
        db.set_paper_decision(alpha, 'favorite')
        db.set_paper_decision(beta, 'skipped')
        for decision, expected in [('all', [alpha, beta, gamma]), ('undecided', [gamma]), ('favorite', [alpha]), ('skipped', [beta])]:
            model = services.reviewed_papers_page_model('title', decision)
            self.assertEqual([p['id'] for p in model['papers']], expected)
            html = self.client.get(f'/reviewed-papers?decision={decision}&sort=title').get_data(as_text=True)
            self.assertIn('reviewed-papers', html)
            for name, paper_id in [('Alpha paper', alpha), ('Beta paper', beta), ('Gamma paper', gamma)]:
                self.assertEqual(name in html, paper_id in expected)
        scored = services.reviewed_papers_page_model('score_desc')['papers']
        self.assertEqual([p['id'] for p in scored], [alpha, beta, gamma])
        self.assertEqual(scored[0]['score_text'], '95')
        with self.assertRaises(ValueError):
            db.list_fulltext_reviewed_papers(decision='bad')

    def test_list_queries_batch_decisions_without_n_plus_one(self):
        for number in range(1, 13):
            self.paper(number)
        original, queries = db.connect, []
        def traced(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn
        with patch.object(db, 'connect', side_effect=traced), patch.object(db, 'get_paper_decision_state', side_effect=AssertionError('N+1')):
            self.assertEqual(len(services.reviewed_papers_page_model()['papers']), 12)
        selects = [q for q in queries if q.lstrip().upper().startswith(('SELECT ', 'WITH '))]
        self.assertLessEqual(len(selects), 3)  # Paper/decision, categories, and investment themes are batched.

    def test_get_routes_read_only_and_escape_paper_content(self):
        paper_id = self.paper(title='<script>unsafe()</script>')
        db.set_paper_decision(paper_id, 'favorite')
        original = db.connect
        def readonly(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.execute('PRAGMA query_only=ON')
            return conn
        with patch.object(db, 'connect', side_effect=readonly):
            for url in ('/favorites', '/reviewed-papers', f'/papers/{paper_id}'):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('<script>unsafe()', response.get_data(as_text=True))

    def test_arxiv_links_reject_dangerous_protocols_hosts_and_credentials(self):
        paper_id = self.paper()
        db.set_paper_decision(paper_id, 'favorite')
        for value in ('javascript:alert(1)', 'data:text/html,unsafe', '//evil.invalid/abs/2609.00001',
                      'https://arxiv.org.evil.invalid/abs/2609.00001', 'https://user:secret@arxiv.org/abs/2609.00001',
                      'https://arxiv.org/redirect/unsafe', 'https://arxiv.org/abs/2609.00001\n', 'https://[invalid'):
            self.assertEqual(services.safe_arxiv_abstract_url(value), '')
            with db.connect() as conn:
                conn.execute('UPDATE papers SET abs_url=? WHERE id=?', (value, paper_id))
            for path in ('/favorites', '/reviewed-papers', f'/papers/{paper_id}'):
                html = self.client.get(path).get_data(as_text=True)
                self.assertNotIn('>arXiv</a>', html)
                self.assertNotIn('>打开 arXiv</a>', html)
        self.assertEqual(services.safe_arxiv_abstract_url('http://www.arxiv.org/abs/2609.00001v2?token=secret#x'),
                         'https://arxiv.org/abs/2609.00001v2')
        self.assertEqual(services.safe_arxiv_abstract_url('https://arxiv.org/abs/hep-th/9901001'),
                         'https://arxiv.org/abs/hep-th/9901001')
        with db.connect() as conn:
            conn.execute('UPDATE papers SET abs_url=? WHERE id=?', ('https://arxiv.org/abs/2609.00001?token=secret', paper_id))
        for path in ('/favorites', '/reviewed-papers', f'/papers/{paper_id}'):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('href="https://arxiv.org/abs/2609.00001"', html)
            self.assertNotIn('token=secret', html)

    def test_schema_constraints_foreign_key_index_and_cascade(self):
        paper_id = self.paper()
        other_id = self.paper(2)
        db.set_paper_decision(paper_id, 'favorite')
        with db.connect() as conn:
            indexes = {row['name'] for row in conn.execute("PRAGMA index_list('paper_dispositions')")}
            self.assertIn('idx_paper_dispositions_decision_updated', indexes)
            foreign_key = conn.execute("PRAGMA foreign_key_list('paper_dispositions')").fetchone()
            self.assertEqual((foreign_key['table'], foreign_key['from'], foreign_key['on_delete']), ('papers', 'paper_id', 'CASCADE'))
            for values in [(paper_id, 'favorite'), (99999, 'favorite'), (other_id, 'invalid'), (other_id, None)]:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO paper_dispositions VALUES (?,?,'now','now')", values)
            conn.execute('DELETE FROM papers WHERE id=?', (paper_id,))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_dispositions').fetchone()[0], 0)

    def test_initialization_is_idempotent_preserving_existing_decisions(self):
        paper_id = self.paper()
        db.set_paper_decision(paper_id, 'favorite')
        before, state = self.snapshot(), db.get_paper_decision_state(paper_id)
        db.init_db()
        db.init_db()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_paper_decision_state(paper_id), state)

    def test_old_schema_migrates_without_rewriting_history_or_auto_favorites(self):
        legacy = Path(self.tmp.name)/'legacy.sqlite3'
        with db.connect(legacy) as conn:
            conn.executescript("""
                CREATE TABLE papers(id INTEGER PRIMARY KEY, arxiv_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]', abstract TEXT NOT NULL DEFAULT '',
                    subjects TEXT NOT NULL DEFAULT '[]', published_at TEXT, pdf_url TEXT, abs_url TEXT,
                    papers_cool_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE evaluations(id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL,
                    evaluation_type TEXT NOT NULL, prompt_id INTEGER, prompt_version INTEGER,
                    llm_profile_id INTEGER, model TEXT, status TEXT NOT NULL, result_json TEXT,
                    raw_output TEXT, error_message TEXT, created_at TEXT NOT NULL);
                INSERT INTO papers(id,arxiv_id,title,created_at,updated_at) VALUES (42,'2601.00042','Historical Paper','old','old');
                INSERT INTO evaluations(id,paper_id,evaluation_type,status,result_json,created_at)
                    VALUES (84,42,'fulltext_review','success','{"score":88}','old');
            """)
            before = tuple(conn.execute('SELECT * FROM papers').fetchone())
        with patch.object(db, 'DB_PATH', legacy):
            db.init_db()
            db.init_db()
            with db.connect() as conn:
                self.assertEqual(tuple(conn.execute('SELECT * FROM papers').fetchone()), before)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_dispositions').fetchone()[0], 0)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(services.favorite_papers_page_model()['papers'], [])
            self.assertEqual(services.reviewed_papers_page_model()['papers'][0]['id'], 42)
            db.set_paper_decision(42, 'favorite')


if __name__ == '__main__':
    unittest.main()
