import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from daily_coolpapers import db, services
from daily_coolpapers.form_commands import FormValidationError
from tests import test_personal_library as library


class AttentionDirectionTests(unittest.TestCase):
    setUp = library.PersonalLibraryTests.setUp
    paper = library.PersonalLibraryTests.paper
    evaluate = library.PersonalLibraryTests.evaluate

    def post(self, path, **data):
        return self.client.post(path, data={'csrf_token': 'test-library', **data})

    def test_create_free_text_is_immutable_and_zero_automatic_work(self):
        text = '不限定格式。\n可关注具身智能，也排除仅改变产品包装。' * 100
        self.assertEqual(self.post('/attention-directions', name=' Robotics ', scope_text=text).status_code, 302)
        direction = db.list_attention_directions()[0]
        self.assertEqual(direction['scope_text'], text)
        self.assertEqual(direction['name'], 'Robotics')
        self.assertEqual(self.post(f"/attention-directions/{direction['id']}/archive", name='mutate').status_code, 400)
        self.assertEqual(self.post(f"/attention-directions/{direction['id']}", scope_text='mutate').status_code, 404)
        self.assertEqual(db.list_attention_directions()[0], direction)
        self.assertTrue(self.runner.queue.empty())
        self.llm.assert_not_called()

    def test_invalid_input_zero_write(self):
        for name, scope in [('', 'abc'), ('a', ''), ('a\x00', 'b'), ('a', '\x00')]:
            with self.assertRaises(FormValidationError):
                db.create_attention_direction(name, scope)
        self.assertEqual(db.list_attention_directions(), [])

    def test_normalized_unique_active_only_archive_terminal_and_idempotent(self):
        first = db.create_attention_direction('ＡＩ  StraßE', 'one')
        with self.assertRaises(db.DirectionConflictError):
            db.create_attention_direction('ai\t STRASSE', 'two')
        db.archive_attention_direction(first)
        snapshot = db.list_attention_directions()
        with patch.object(db, 'now_iso', return_value='2099-01-01'):
            db.archive_attention_direction(first)
        self.assertEqual(db.list_attention_directions(), snapshot)
        second = db.create_attention_direction('ai strasse', 'new')
        self.assertNotEqual(first, second)
        self.assertEqual(len(db.list_attention_directions(active_only=True)), 1)
        self.assertEqual(self.post(f'/attention-directions/{first}/restore').status_code, 404)
        with db.connect() as conn, self.assertRaises(db.DirectionConflictError):
            db.require_direction(conn, first, active=True)

    def test_concurrent_duplicates(self):
        def create(_):
            try:
                return db.create_attention_direction('Same', 'Scope')
            except db.DirectionConflictError:
                return None
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(bool(x) for x in executor.map(create, range(4))), 1)

    def test_routes_csrf_origin_404_and_escape(self):
        self.assertEqual(self.client.post('/attention-directions', data={'name': 'x','scope_text':'x'}).status_code, 403)
        self.assertEqual(self.client.post('/attention-directions', headers={'Origin':'https://evil.example'}, data={'csrf_token':'test-library'}).status_code, 403)
        self.assertEqual(self.post('/attention-directions/999/archive').status_code, 404)
        self.assertEqual(self.post(f'/attention-directions/{2**100}/archive').status_code, 404)
        direction = db.create_attention_direction('<script>name</script>', '<img src=x onerror=alert(1)>')
        html = self.client.get('/attention-directions').get_data(as_text=True)
        self.assertIn('&lt;script&gt;name', html)
        self.assertNotIn('<img src=x', html)
        db.archive_attention_direction(direction)
        self.assertNotIn('&lt;script&gt;name', self.client.get('/attention-directions').get_data(as_text=True))
        self.assertIn('&lt;script&gt;name', self.client.get('/attention-directions?archived=1').get_data(as_text=True))

    def test_profile_is_independent_and_prompt_type_renders(self):
        data = {'name':'Abstract', 'provider':'openai_compatible', 'base_url':'https://example.invalid',
                'model':'fixture', 'enabled':1, 'is_default_abstract':1}
        first = db.save_llm_profile(data)
        self.assertIsNone(db.get_default_llm_profile('direction_classification'))
        with self.assertRaises(ValueError):
            services.resolve_evaluation_config('direction_classification')
        second = db.save_llm_profile({**data, 'name':'Classifier','is_default_abstract':0,'is_default_classification':1})
        self.assertEqual(db.get_default_llm_profile('abstract_review')['id'], first)
        self.assertEqual(services.resolve_evaluation_config('direction_classification').profile_id, second)
        db.save_llm_profile({**db.get_llm_profile(first), 'is_default_classification':1})
        self.assertEqual(db.get_default_llm_profile('direction_classification')['id'], first)
        self.assertFalse(db.get_llm_profile(second)['is_default_classification'])
        for path in ('/llm-profiles','/prompts'):
            self.assertIn('分类', self.client.get(path).get_data(as_text=True))
        self.assertEqual(self.post('/api/prompts', name='Mine',type='direction_classification',template='{{ metadata_json }}',enabled='1').status_code,302)

    def test_idempotent_migration_preserves_data_and_old_profiles(self):
        self.paper()
        db.create_attention_direction('Preserve', 'Free text')
        with db.connect() as conn:
            before = {t:[tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in ('papers','evaluations','attention_directions','prompts')}
        db.init_db()
        db.init_llm_profiles_db()
        with db.connect() as conn:
            after = {t:[tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in before}
        self.assertEqual(before, after)
        self.assertEqual(len(db.list_prompts('direction_classification')), 1)
        with db.connect_llm_profiles() as conn:
            conn.execute('DROP INDEX idx_profiles_classification')
            conn.execute('ALTER TABLE llm_profiles DROP COLUMN is_default_classification')
        db.init_llm_profiles_db()
        with db.connect_llm_profiles() as conn:
            self.assertIn('is_default_classification', {r['name'] for r in conn.execute('PRAGMA table_info(llm_profiles)')})


if __name__ == '__main__':
    unittest.main()
