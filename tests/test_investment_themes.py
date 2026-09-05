import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from werkzeug.datastructures import MultiDict

from daily_coolpapers import db, services
from daily_coolpapers.form_commands import FormValidationError, InvestmentThemeCommand
from tests import test_personal_library as library


class InvestmentThemeTests(unittest.TestCase):
    setUp = library.PersonalLibraryTests.setUp
    paper = library.PersonalLibraryTests.paper
    evaluate = library.PersonalLibraryTests.evaluate
    snapshot = library.PersonalLibraryTests.snapshot

    def post(self, path, fields=None, **kwargs):
        data = MultiDict(fields or {})
        data['csrf_token'] = 'test-library'
        return self.client.post(path, data=data, **kwargs)

    def assign(self, paper_id, ids):
        return self.post(f'/api/papers/{paper_id}/investment-themes', [('theme_ids', value) for value in ids])

    def members(self, paper_id):
        return db.list_paper_investment_themes([paper_id])[paper_id]

    def test_theme_create_without_papers_and_unicode_duplicate_names(self):
        theme_id = db.create_investment_theme('  ＡＩ\t  Infra  ', ' 自然语言描述\n不限制范围字段 ')
        self.assertEqual(db.get_investment_theme(theme_id)['normalized_name'], 'ai infra')
        for name in ('ai infra', 'AI\nINFRA', 'Ａｉ　Ｉｎｆｒａ'):
            response = self.post('/investment-themes', {'name': name})
            self.assertEqual(response.status_code, 400)
            self.assertIn('name', response.get_json()['errors'])
        db.update_investment_theme(theme_id, 'archive')
        with self.assertRaises(FormValidationError):
            db.create_investment_theme('ai infra')
        db.create_investment_theme('Straße')
        with self.assertRaises(FormValidationError):
            db.create_investment_theme('STRASSE')
        self.assertEqual(len(db.list_investment_themes()), 2)
        self.assertEqual(self.client.get('/investment-themes').status_code, 200)

    def test_name_description_validation_before_writes(self):
        for name, description, field in [('', '', 'name'), (' \t\n', '', 'name'), ('x'*81, '', 'name'), ('ok', 'x'*501, 'description')]:
            response = self.post('/investment-themes', {'name': name, 'description': description})
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.get_json()['errors'])
        self.assertEqual(db.list_investment_themes(), [])
        theme_id = db.create_investment_theme('名'*80, '说'*500)
        before = db.get_investment_theme(theme_id)
        response = self.post(f'/investment-themes/{theme_id}', {'action': 'update', 'name': 'new', 'description': 'x'*501})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_investment_theme(theme_id), before)

    def test_rename_archive_restore_preserve_relationships_and_times(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Robotics', 'old')
        db.set_paper_investment_themes(paper_id, [theme_id])
        before = self.members(paper_id)[0]['added_at']
        with patch.object(db, 'now_iso', return_value='2099-01-01 00:00:00'):
            db.update_investment_theme(theme_id, 'update', 'Embodied AI', 'new')
        self.assertEqual(self.members(paper_id)[0]['name'], 'Embodied AI')
        self.assertEqual(self.members(paper_id)[0]['added_at'], before)
        for action in ('archive', 'archive', 'restore', 'restore'):
            self.assertEqual(self.post(f'/investment-themes/{theme_id}', {'action': action}).status_code, 302)
            self.assertEqual(len(self.members(paper_id)), 1)
        state = db.get_investment_theme(theme_id)
        with patch.object(db, 'now_iso', return_value='2199-01-01 00:00:00'):
            db.update_investment_theme(theme_id, 'restore')
            db.update_investment_theme(theme_id, 'update', 'Embodied AI', 'new')
        self.assertEqual(db.get_investment_theme(theme_id), state)

    def test_nul_fields_return_validation_errors_without_writes(self):
        theme_id = db.create_investment_theme('Valid', 'Original')
        before = db.list_investment_themes()
        for name, description, field in [('\x00', '', 'name'), ('a\x00b', '', 'name'), ('Valid', '\x00', 'description')]:
            for path in ('/investment-themes', f'/investment-themes/{theme_id}'):
                response = self.post(path, {'name': name, 'description': description, 'action': 'update'})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.get_json()['errors'])
                self.assertEqual(db.list_investment_themes(), before)

    def test_browser_error_feedback_keeps_status_and_safe_recovery_links(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Valid')
        headers = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
        response = self.post('/investment-themes', {'name': 'VALID', 'next': 'https://evil.example'}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.mimetype, 'text/html')
        html = response.get_data(as_text=True)
        self.assertIn('主题名称', html)
        self.assertIn('返回投资主题设置', html)
        self.assertNotIn('evil.example', html)
        db.set_paper_investment_themes(paper_id, [theme_id])
        db.update_investment_theme(theme_id, 'archive')
        before = self.members(paper_id)
        response = self.post(f'/api/papers/{paper_id}/investment-themes', {'theme_ids': theme_id}, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn(f'href="/papers/{paper_id}#fulltext-result"', response.get_data(as_text=True))
        self.assertIn('role="alert"', response.get_data(as_text=True))
        self.assertEqual(self.members(paper_id), before)
        response = self.post(f'/investment-themes/{theme_id}', {'action': 'update', 'name': 'x'*81}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(f'href="/investment-themes#theme-{theme_id}"', response.get_data(as_text=True))
        # Existing callers without HTML negotiation retain the API contract.
        self.assertIsNotNone(self.assign(paper_id, [theme_id]).get_json())

    def test_rename_collision_keeps_original_theme_and_members(self):
        first, second = db.create_investment_theme('First'), db.create_investment_theme('Second')
        paper_id = self.paper()
        db.set_paper_investment_themes(paper_id, [first])
        db.update_investment_theme(second, 'archive')
        before = db.get_investment_theme(first)
        response = self.post(f'/investment-themes/{first}', {'action': 'update', 'name': 'SECOND', 'description': 'changed'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_investment_theme(first), before)
        self.assertEqual(self.members(paper_id)[0]['id'], first)

    def test_multiselect_replace_duplicate_idempotency_and_added_timestamp(self):
        paper_id = self.paper()
        first, second, third = [db.create_investment_theme(name) for name in ('A','B','C')]
        self.assertEqual(self.assign(paper_id, [first, first, second]).status_code, 302)
        before = self.members(paper_id)
        with patch.object(db, 'now_iso', return_value='2099-01-01 00:00:00'):
            self.assign(paper_id, [second, first])
        self.assertEqual(self.members(paper_id), before)
        response = self.assign(paper_id, [second, third])
        self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')
        self.assertEqual({item['id'] for item in self.members(paper_id)}, {second, third})
        self.assertEqual(self.members(paper_id)[0]['added_at'], before[0]['added_at'])

    def test_empty_save_preserves_archived_until_explicit_remove(self):
        paper_id = self.paper()
        active, archived = db.create_investment_theme('Active'), db.create_investment_theme('Archived')
        db.set_paper_investment_themes(paper_id, [active, archived])
        db.update_investment_theme(archived, 'archive')
        self.assertEqual(self.assign(paper_id, []).status_code, 302)
        self.assertEqual([item['id'] for item in self.members(paper_id)], [archived])
        model = services.paper_themes_model(paper_id)
        self.assertEqual([item['id'] for item in model['active']], [active])
        self.assertEqual(model['assigned'][0]['status'], 'archived')
        html = self.client.get(f'/papers/{paper_id}').get_data(as_text=True)
        self.assertIn('Archived · 已归档', html)
        self.assertNotIn(f'name="theme_ids" value="{archived}"', html)
        for _ in range(2):
            response = self.post(f'/api/papers/{paper_id}/investment-themes/{archived}/remove')
            self.assertEqual(response.location, f'/papers/{paper_id}#fulltext-result')
        self.assertEqual(self.members(paper_id), [])
        self.assertEqual(db.get_investment_theme(archived)['status'], 'archived')

    def test_stale_form_with_archived_or_unknown_theme_is_atomic(self):
        paper_id = self.paper()
        first, archived, other = [db.create_investment_theme(name) for name in ('A','B','C')]
        db.set_paper_investment_themes(paper_id, [first, archived])
        db.update_investment_theme(archived, 'archive')
        before = self.members(paper_id)
        self.assertEqual(self.assign(paper_id, [archived, other]).status_code, 409)
        self.assertEqual(self.assign(paper_id, [other, 99999]).status_code, 404)
        self.assertEqual(self.members(paper_id), before)
        self.assertEqual(self.assign(paper_id, [other]).status_code, 302)
        self.assertEqual({row['id'] for row in self.members(paper_id)}, {archived, other})

    def test_every_paper_write_requires_historical_success_even_clear_and_remove(self):
        theme_id = db.create_investment_theme('Theme')
        paper_id = self.paper(status='failed')
        self.evaluate(paper_id, 'success', kind='abstract_review')
        for ids in ([], [theme_id]):
            self.assertEqual(self.assign(paper_id, ids).status_code, 409)
        self.assertEqual(self.post(f'/api/papers/{paper_id}/investment-themes/{theme_id}/remove').status_code, 409)
        self.assertNotIn('data-testid="paper-themes"', self.client.get(f'/papers/{paper_id}').get_data(as_text=True))
        self.evaluate(paper_id, 'success')
        self.evaluate(paper_id, 'failed')
        self.assertEqual(self.assign(paper_id, [theme_id]).status_code, 302)
        self.assertIn('data-testid="paper-themes"', self.client.get(f'/papers/{paper_id}').get_data(as_text=True))
        original = db.has_successful_fulltext
        def checked(paper_id, *, conn=None):
            self.assertTrue(conn.in_transaction)
            return original(paper_id, conn=conn)
        with patch.object(db, 'has_successful_fulltext', side_effect=checked) as check:
            db.set_paper_investment_themes(paper_id, [])
            db.remove_paper_investment_theme(paper_id, theme_id)
        self.assertEqual(check.call_count, 2)

    def test_decisions_and_theme_relations_remain_independent_no_jobs_or_llm(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Independent')
        before = self.snapshot()
        db.set_paper_investment_themes(paper_id, [theme_id])
        for decision in ('favorite', 'skipped', 'clear'):
            db.set_paper_decision(paper_id, decision)
            self.assertEqual(len(self.members(paper_id)), 1)
        db.set_paper_decision(paper_id, 'skipped')
        for action in ('archive', 'restore'):
            db.update_investment_theme(theme_id, action)
        db.remove_paper_investment_theme(paper_id, theme_id)
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'skipped')
        self.assertEqual(self.snapshot(), before)
        self.llm.assert_not_called()
        self.fetch.assert_not_called()
        self.assertTrue(self.runner.queue.empty())

    def test_csrf_same_origin_methods_and_missing_entities(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Theme')
        operations = [('/investment-themes', {'name': 'New'}), (f'/investment-themes/{theme_id}', {'action': 'archive'}),
                      (f'/api/papers/{paper_id}/investment-themes', {'theme_ids': str(theme_id)}),
                      (f'/api/papers/{paper_id}/investment-themes/{theme_id}/remove', {})]
        for path, fields in operations:
            self.assertEqual(self.client.post(path, data=fields).status_code, 403)
            self.assertEqual(self.post(path, fields, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
            self.assertEqual(self.client.delete(path).status_code, 405)
        self.assertEqual(self.client.get(f'/api/papers/{paper_id}/investment-themes').status_code, 405)
        for theme_id in (99999, 2**80):
            self.assertEqual(self.client.get(f'/investment-themes/{theme_id}/papers').status_code, 404)
            self.assertEqual(self.post(f'/investment-themes/{theme_id}', {'action': 'archive'}).status_code, 404)
            self.assertEqual(self.post(f'/api/papers/{paper_id}/investment-themes/{theme_id}/remove').status_code, 404)
        self.assertEqual(self.assign(99999, []).status_code, 404)
        self.assertEqual(self.post(f'/investment-themes/{theme_id}', {'action': 'delete'}).status_code, 400)

    def test_malformed_ids_are_rejected_without_removing_relationships(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Theme')
        db.set_paper_investment_themes(paper_id, [theme_id])
        for value in ('', 'bad', '1.5', '-1', '0', str(2**80), '1 OR 1=1'):
            self.assertEqual(self.assign(paper_id, [value]).status_code, 400)
            self.assertEqual(len(self.members(paper_id)), 1)

    def test_transaction_rolls_back_removal_when_new_insertion_fails(self):
        paper_id = self.paper()
        first, second = db.create_investment_theme('A'), db.create_investment_theme('B')
        db.set_paper_investment_themes(paper_id, [first])
        before = self.members(paper_id)
        with db.connect() as conn:
            conn.execute("CREATE TRIGGER fail_theme BEFORE INSERT ON paper_investment_themes BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            db.set_paper_investment_themes(paper_id, [second])
        self.assertEqual(self.members(paper_id), before)

    def test_parallel_name_and_membership_submissions_are_serialized(self):
        def create(_):
            try:
                return db.create_investment_theme('Shared')
            except FormValidationError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(8)))
        self.assertEqual(sum(value is not None for value in ids), 1)
        paper_id, theme_id = self.paper(), next(value for value in ids if value)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: db.set_paper_investment_themes(paper_id, [theme_id]), range(8)))
        self.assertEqual(len(self.members(paper_id)), 1)

    def test_theme_papers_sort_latest_success_and_archived_lists(self):
        theme_id = db.create_investment_theme('Topic')
        alpha, beta, gamma = [self.paper(i, title=title, score=score) for i, title, score in [(1,'Alpha',70),(2,'Beta',90),(3,'Gamma',80)]]
        for paper_id, day in [(alpha, '01'),(beta, '02'),(gamma, '03')]:
            with patch.object(db, 'now_iso', return_value=f'2026-09-{day} 00:00:00'):
                db.set_paper_investment_themes(paper_id, [theme_id])
        self.evaluate(alpha, 'success', score=95)
        self.evaluate(alpha, 'failed')
        db.set_paper_decision(beta, 'skipped')
        db.update_investment_theme(theme_id, 'archive')
        for sort, expected in [('added_desc',[gamma,beta,alpha]),('score_desc',[alpha,beta,gamma]),('title',[alpha,beta,gamma])]:
            model = services.investment_theme_papers_model(theme_id, sort)
            self.assertEqual([p['id'] for p in model['papers']], expected)
            self.assertEqual(self.client.get(f'/investment-themes/{theme_id}/papers?sort={sort}').status_code, 200)
        self.assertEqual(db.list_investment_themes()[0]['paper_count'], 3)
        html = self.client.get(f'/investment-themes/{theme_id}/papers').get_data(as_text=True)
        self.assertIn('已归档', html)
        self.assertIn('已跳过', html)
        self.assertIn('加入时间', html)

    def test_history_membership_remains_visible_when_success_record_is_unavailable(self):
        paper_id, theme_id = self.paper(), db.create_investment_theme('Topic')
        db.set_paper_investment_themes(paper_id, [theme_id])
        with db.connect() as conn:
            conn.execute('DELETE FROM evaluations WHERE paper_id=?', (paper_id,))
        model = services.investment_theme_papers_model(theme_id)
        self.assertEqual(len(model['papers']), 1)
        self.assertFalse(model['papers'][0]['has_fulltext'])
        self.assertIn('成功全文评估已不可用', self.client.get(f'/investment-themes/{theme_id}/papers').get_data(as_text=True))

    def test_counts_and_cards_batch_queries_and_gets_are_read_only(self):
        themes = [db.create_investment_theme(f'Topic {i}') for i in range(4)]
        for number in range(1, 13):
            db.set_paper_investment_themes(self.paper(number), themes)
        statements, original = [], db.connect
        def readonly(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.execute('PRAGMA query_only=ON')
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, 'connect', side_effect=readonly):
            for read, maximum in [(db.list_investment_themes, 1), (lambda: services.paper_themes_model(1), 1),
                                  (lambda: services.investment_theme_papers_model(themes[0]), 4),
                                  (services.reviewed_papers_page_model, 3)]:
                statements.clear()
                read()
                self.assertLessEqual(sum(q.lstrip().upper().startswith(('SELECT ', 'WITH ')) for q in statements), maximum)
            for path in ('/investment-themes', f'/investment-themes/{themes[0]}/papers', '/papers/1', '/reviewed-papers', '/favorites'):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_schema_constraints_and_foreign_key_delete_semantics(self):
        paper_id = self.paper()
        first, second = db.create_investment_theme('A'), db.create_investment_theme('B')
        db.set_paper_investment_themes(paper_id, [first])
        with db.connect() as conn:
            for sql, params in [
                ("UPDATE investment_themes SET status='invalid' WHERE id=?", (second,)),
                ("UPDATE investment_themes SET normalized_name='a' WHERE id=?", (second,)),
                ("UPDATE investment_themes SET name='' WHERE id=?", (second,)),
                ("UPDATE investment_themes SET description=? WHERE id=?", ('x'*501, second)),
                ("INSERT INTO paper_investment_themes VALUES (?,?,'now')", (paper_id, first)),
                ("INSERT INTO paper_investment_themes VALUES (?,?,'now')", (99999, second)),
                ("INSERT INTO paper_investment_themes VALUES (?,?,'now')", (paper_id, 99999)),
                ("DELETE FROM investment_themes WHERE id=?", (first,)),
            ]:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
            fks = {row['from']: row['on_delete'] for row in conn.execute("PRAGMA foreign_key_list('paper_investment_themes')")}
            self.assertEqual(fks, {'paper_id': 'CASCADE', 'theme_id': 'RESTRICT'})
            conn.execute('DELETE FROM papers WHERE id=?', (paper_id,))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_investment_themes').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM investment_themes').fetchone()[0], 2)

    def test_migration_from_first_stage_and_repeated_initialization_preserve_history(self):
        paper_id = self.paper()
        db.set_paper_decision(paper_id, 'favorite')
        before, decision = self.snapshot(), db.get_paper_decision_state(paper_id)
        with db.connect() as conn:
            conn.execute('DROP TABLE paper_investment_themes')
            conn.execute('DROP TABLE investment_themes')
        db.init_db()
        db.init_db()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_paper_decision_state(paper_id), decision)
        self.assertEqual(db.list_investment_themes(), [])
        theme_id = db.create_investment_theme('Persisted')
        db.set_paper_investment_themes(paper_id, [theme_id])
        db.update_investment_theme(theme_id, 'archive')
        theme, members = db.get_investment_theme(theme_id), self.members(paper_id)
        db.init_db()
        self.assertEqual(db.get_investment_theme(theme_id), theme)
        self.assertEqual(self.members(paper_id), members)

    def test_create_return_link_is_local_and_does_not_automatically_assign(self):
        paper_id = self.paper()
        response = self.post('/investment-themes', {'name': 'Created', 'paper_id': paper_id, 'next': 'https://evil.invalid'})
        self.assertEqual(response.location, f'/investment-themes?paper_id={paper_id}#theme-1')
        self.assertEqual(self.members(paper_id), [])
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn(f'href="/papers/{paper_id}#fulltext-result"', html)
        for value, code in [('bad',400), ('99999',404), (str(2**80),400)]:
            self.assertEqual(self.post('/investment-themes', {'name': 'No write', 'paper_id': value}).status_code, code)
        self.assertEqual(len(db.list_investment_themes()), 1)

    def test_templates_escape_names_and_show_tags_without_changing_favorites(self):
        name, description = '<script>bad()</script>', '</textarea><script>bad()</script>'
        theme_id = db.create_investment_theme(name, description)
        paper_id = self.paper()
        db.set_paper_investment_themes(paper_id, [theme_id])
        db.set_paper_decision(paper_id, 'favorite')
        db.update_investment_theme(theme_id, 'archive')
        for path in ('/investment-themes', f'/investment-themes/{theme_id}/papers', f'/papers/{paper_id}', '/favorites', '/reviewed-papers'):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn('<script>bad()', html)
            self.assertIn('&lt;script&gt;bad()', html)
        self.assertEqual(len(services.favorite_papers_page_model()['papers']), 1)

    def test_large_multiselect_is_chunked_and_deduplicated(self):
        paper_id = self.paper()
        with db.connect() as conn:
            conn.executemany("INSERT INTO investment_themes(name,normalized_name,created_at,updated_at) VALUES (?,?,'now','now')",
                             [(f'Theme {i}', f'theme {i}') for i in range(501)])
        ids = [theme['id'] for theme in db.list_investment_themes()]
        db.set_paper_investment_themes(paper_id, [*ids, *ids[:2]])
        self.assertEqual(len(self.members(paper_id)), 501)
        db.set_paper_investment_themes(paper_id, ids[:1])
        self.assertEqual(len(self.members(paper_id)), 1)


if __name__ == '__main__':
    unittest.main()
