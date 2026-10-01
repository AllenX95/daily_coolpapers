"""Safety and fixture contracts for the offline architecture baseline."""
import unittest
from pathlib import Path
from daily_coolpapers import db, config, memo_db
from tests.benchmark_architecture import isolated_database, populate, percentile, cases
from flask import template_rendered


class ArchitectureBaselineTests(unittest.TestCase):
    def test_synthetic_database_is_isolated_and_has_required_distribution(self):
        original = db.DB_PATH
        with isolated_database() as (app, root):
            self.assertTrue(db.DB_PATH.is_relative_to(root))
            self.assertTrue(db.LLM_PROFILES_DB_PATH.is_relative_to(root))
            self.assertEqual(config.DB_PATH, db.DB_PATH)
            populate(100)
            with db.connect() as conn:
                for table, expected in [('papers',100),('evaluations',300),('paper_dispositions',20),('paper_categories',200),('paper_investment_themes',200)]:
                    self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],expected)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
                before=conn.total_changes
                candidates, counts=memo_db.candidate_data(conn,{'mode':'investment_theme','id':1},{})
                self.assertEqual(len(candidates),20)
                self.assertEqual(counts['source_total'],100)
                self.assertEqual(counts['preselected'],20)
                self.assertEqual(conn.total_changes,before)
            response=app.test_client().get('/?date=2026-09-05&page_size=30')
            self.assertEqual(response.status_code,200)
        self.assertEqual(db.DB_PATH,original)
        self.assertFalse(root.exists())

    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(percentile(list(range(1,31)),.95),29)

    def test_home_benchmark_pages_use_filtered_total_and_are_not_empty(self):
        with isolated_database() as (app, root):
            populate(300)
            contexts = []
            def rendered(sender, template, context, **extra):
                contexts.append(context['paper_page'])
            with template_rendered.connected_to(rendered, app):
                for name, call in cases(app, 300):
                    if name.startswith('home/'):
                        call()
            self.assertEqual(len(contexts), 3)
            self.assertTrue(all(page['items'] for page in contexts))
            last = contexts[-1]
            self.assertLess(last['total'], 300)
            self.assertEqual(last['page'], (last['total'] + 99) // 100)
