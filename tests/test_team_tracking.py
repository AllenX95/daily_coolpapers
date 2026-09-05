import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from daily_coolpapers import db
from daily_coolpapers.form_commands import FormValidationError
from tests import test_personal_library as library


class TeamTrackingDataTests(unittest.TestCase):
    setUp = library.PersonalLibraryTests.setUp
    paper = library.PersonalLibraryTests.paper
    evaluate = library.PersonalLibraryTests.evaluate
    snapshot = library.PersonalLibraryTests.snapshot

    def form(self, **changes):
        return {'author_mode': 'new', 'author_name': 'Ada', 'organization_mode': 'new',
                'organization_name': 'Example Lab', 'organization_type': 'company', **changes}

    def existing(self, paper_id, **changes):
        record = db.get_paper_team_tracking(paper_id)
        return self.form(author_mode='existing', author_id=record['lead_author_id'],
                         organization_mode='existing', organization_id=record['organization_id'], **changes)

    def state(self):
        with db.connect() as conn:
            return {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                    for table in ('research_authors', 'research_organizations', 'paper_team_tracking')}

    def test_create_defaults_and_no_metadata_or_business_side_effects(self):
        paper_id = self.paper()
        theme_id = db.create_investment_theme('Memory')
        db.set_paper_investment_themes(paper_id, [theme_id])
        db.set_paper_decision(paper_id, 'skipped')
        before = self.snapshot()
        db.save_paper_team_tracking(paper_id, self.form(author_name='Manual author'))
        record = db.get_paper_team_tracking(paper_id)
        self.assertEqual((record['author_name'], record['status']), ('Manual author', 'tracking'))
        self.assertEqual(db.get_research_entity('author', record['lead_author_id'])['author_category'], 'unknown')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'skipped')
        self.assertEqual(db.list_paper_investment_themes([paper_id])[paper_id][0]['id'], theme_id)
        self.llm.assert_not_called()
        self.fetch.assert_not_called()
        self.assertTrue(self.runner.queue.empty())

    def test_repeated_new_is_conflict_and_existing_save_is_idempotent(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form(tracking_notes='keep'))
        before = self.state()
        with self.assertRaises(db.ResearchEntityConflictError) as error:
            db.save_paper_team_tracking(paper_id, self.form())
        self.assertEqual(len(error.exception.conflicts), 2)
        with patch.object(db, 'now_iso', return_value='2099-01-01 00:00:00'):
            db.save_paper_team_tracking(paper_id, self.existing(paper_id, tracking_notes='keep'))
        self.assertEqual(self.state(), before)

    def test_stop_restore_keeps_identity_and_creation_time(self):
        paper_id = self.paper()
        db.archive_paper_team_tracking(paper_id)  # No relation is an idempotent no-op.
        db.save_paper_team_tracking(paper_id, self.form())
        before = db.get_paper_team_tracking(paper_id)
        with patch.object(db, 'now_iso', return_value='2099-01-01 00:00:00'):
            db.archive_paper_team_tracking(paper_id)
        archived = db.get_paper_team_tracking(paper_id)
        with patch.object(db, 'now_iso', return_value='2199-01-01 00:00:00'):
            db.archive_paper_team_tracking(paper_id)
        self.assertEqual(db.get_paper_team_tracking(paper_id), archived)
        db.save_paper_team_tracking(paper_id, self.existing(paper_id, tracking_notes='reactivated'))
        after = db.get_paper_team_tracking(paper_id)
        self.assertEqual((after['id'], after['created_at']), (before['id'], before['created_at']))
        self.assertEqual((after['status'], after['notes']), ('tracking', 'reactivated'))

    def test_normalized_duplicates_including_archived_have_zero_dml(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form(author_name='  ＡＤＡ  StraßE ', organization_name='ＡＩ\t  Lab'))
        record = db.get_paper_team_tracking(paper_id)
        db.update_research_entity('author', record['lead_author_id'], 'archive')
        other = self.paper(2)
        original, statements = db.connect, []
        def traced():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn
        before = self.state()
        with patch.object(db, 'connect', side_effect=traced), self.assertRaises(db.ResearchEntityConflictError) as error:
            db.save_paper_team_tracking(other, self.form(author_name='ada STRASSE', organization_name='ai lab'))
        self.assertEqual({item['status'] for item in error.exception.conflicts}, {'archived', 'active'})
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements))
        self.assertEqual(self.state(), before)

    def test_missing_or_archived_existing_rejects_before_new_entity_insertion(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form())
        record = db.get_paper_team_tracking(paper_id)
        db.update_research_entity('organization', record['organization_id'], 'archive')
        before = self.state()
        for entity_id, error in [(record['organization_id'], db.ResearchEntityConflictError), (9999, db.ResearchEntityNotFoundError)]:
            with self.assertRaises(error):
                db.save_paper_team_tracking(paper_id, self.form(author_name='Should not exist', organization_mode='existing', organization_id=entity_id))
            self.assertEqual(self.state(), before)
        self.assertEqual(db.get_paper_team_tracking(paper_id)['status'], 'tracking')
        self.assertEqual(db.research_entity_options('organization'), [])
        db.update_research_entity('organization', record['organization_id'], 'restore')
        db.save_paper_team_tracking(paper_id, self.existing(paper_id))

    def test_both_entities_and_relation_rollback_on_write_failure(self):
        paper_id = self.paper()
        with db.connect() as conn:
            conn.execute("CREATE TRIGGER reject_team BEFORE INSERT ON paper_team_tracking BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_paper_team_tracking(paper_id, self.form())
        self.assertTrue(all(not rows for rows in self.state().values()))

    def test_qualification_is_shared_transactional_and_historical(self):
        missing = self.paper(status=None)
        for operation in (lambda pid: db.save_paper_team_tracking(pid, self.form()), db.archive_paper_team_tracking):
            with self.assertRaises(db.FulltextRequiredError):
                operation(missing)
            for value in (9999, 2**100):
                with self.assertRaises(db.PaperNotFoundError):
                    operation(value)
        self.evaluate(missing, 'success')
        self.evaluate(missing, 'failed')
        original = db.has_successful_fulltext
        calls = []
        def checked(pid, *, conn=None):
            calls.append(conn.in_transaction)
            return original(pid, conn=conn)
        with patch.object(db, 'has_successful_fulltext', side_effect=checked):
            db.save_paper_team_tracking(missing, self.form())
            db.archive_paper_team_tracking(missing)
        self.assertEqual(calls, [True, True])

    def test_invalid_fields_are_zero_writes(self):
        paper_id = self.paper()
        for changes in ({'author_name': ''}, {'organization_name': '  '}, {'organization_type': ''},
                        {'organization_type': 'bad'}, {'author_category': 'bad'}, {'author_name': 'a\x00b'},
                        {'organization_notes': '\x00'}, {'organization_region': '\x00'}, {'tracking_notes': '\x00'},
                        {'author_mode': 'bad'}, {'author_mode': 'existing', 'author_id': 2**100},
                        {'author_mode': 'existing', 'author_id': '1 OR 1=1'}):
            with self.subTest(changes=changes), self.assertRaises(FormValidationError):
                db.save_paper_team_tracking(paper_id, self.form(**changes))
            self.assertTrue(all(not rows for rows in self.state().values()))

    def test_author_mobility_is_recorded_per_paper_and_no_fuzzy_merge(self):
        first, second = self.paper(), self.paper(2)
        db.save_paper_team_tracking(first, self.form(organization_name='MIT'))
        author_id = db.get_paper_team_tracking(first)['lead_author_id']
        db.save_paper_team_tracking(second, self.form(author_mode='existing', author_id=author_id,
            organization_name='Massachusetts Institute of Technology', organization_type='university'))
        self.assertEqual(db.get_paper_team_tracking(first)['organization_name'], 'MIT')
        self.assertEqual(len(self.state()['research_authors']), 1)
        self.assertEqual(len(self.state()['research_organizations']), 2)

    def test_entity_edit_archive_restore_does_not_rewrite_tracking(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form())
        record = db.get_paper_team_tracking(paper_id)
        before = self.state()['paper_team_tracking']
        for kind, entity_id in [('author', record['lead_author_id']), ('organization', record['organization_id'])]:
            db.update_research_entity(kind, entity_id, 'update', {'name': 'New '+kind, 'organization_type': 'other', 'region': '中国', 'notes': '手工备注'})
            for action in ('archive', 'archive', 'restore', 'restore'):
                db.update_research_entity(kind, entity_id, action)
            self.assertEqual(self.state()['paper_team_tracking'], before)
        self.assertEqual(db.get_paper_team_tracking(paper_id)['author_name'], 'New author')

    def test_entity_rename_collision_is_atomic_and_archive_keeps_name(self):
        first, second = self.paper(), self.paper(2)
        db.save_paper_team_tracking(first, self.form())
        db.save_paper_team_tracking(second, self.form(author_name='Grace', organization_name='Other'))
        a = db.get_paper_team_tracking(first)
        b = db.get_paper_team_tracking(second)
        for kind, key in [('author', 'lead_author_id'), ('organization', 'organization_id')]:
            db.update_research_entity(kind, a[key], 'archive')
            before = self.state()
            with self.assertRaises(db.ResearchEntityConflictError):
                db.update_research_entity(kind, b[key], 'update', {'name': 'ADA' if kind == 'author' else 'EXAMPLE LAB', 'organization_type': 'company'})
            self.assertEqual(self.state(), before)

    def test_parallel_new_and_existing_submissions_have_no_duplicates(self):
        paper_id = self.paper()
        def save_new(_):
            try:
                db.save_paper_team_tracking(paper_id, self.form())
                return 'saved'
            except db.ResearchEntityConflictError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(save_new, range(4))).count('saved'), 1)
            list(pool.map(lambda _: db.save_paper_team_tracking(paper_id, self.existing(paper_id)), range(4)))
        self.assertEqual([len(rows) for rows in self.state().values()], [1, 1, 1])

    def test_schema_constraints_foreign_keys_and_indexes(self):
        paper_id = self.paper()
        db.save_paper_team_tracking(paper_id, self.form())
        record = db.get_paper_team_tracking(paper_id)
        with db.connect() as conn:
            for statement in ["UPDATE research_authors SET author_category='bad'", "UPDATE research_organizations SET organization_type='bad'",
                              "UPDATE research_authors SET status='bad'", "UPDATE paper_team_tracking SET status='active'",
                              'UPDATE paper_team_tracking SET lead_author_id=9999', 'DELETE FROM research_authors',
                              'DELETE FROM research_organizations', 'UPDATE research_authors SET created_at=NULL']:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO paper_team_tracking(paper_id,lead_author_id,organization_id,created_at,updated_at) VALUES (?,?,?,?,?)',
                             (paper_id, record['lead_author_id'], record['organization_id'], 't', 't'))
            names = {row['name'] for row in conn.execute("PRAGMA index_list('paper_team_tracking')")}
            self.assertTrue({'idx_paper_team_tracking_status_updated', 'idx_paper_team_tracking_author', 'idx_paper_team_tracking_organization'} <= names)
            conn.execute('DELETE FROM papers WHERE id=?', (paper_id,))
        self.assertIsNone(db.get_paper_team_tracking(paper_id))
        self.assertEqual(len(self.state()['research_authors']), 1)

    def test_upgrade_from_b_and_reinitialization_preserve_existing_records(self):
        paper_id = self.paper()
        db.set_paper_decision(paper_id, 'favorite')
        before = self.snapshot()
        with db.connect() as conn:
            for table in ('paper_team_tracking', 'research_authors', 'research_organizations'):
                conn.execute(f'DROP TABLE {table}')
        db.init_db()
        self.assertTrue(all(not rows for rows in self.state().values()))
        self.assertEqual(self.snapshot(), before)
        db.save_paper_team_tracking(paper_id, self.form())
        db.archive_paper_team_tracking(paper_id)
        team = self.state()
        db.init_db()
        db.init_db()
        self.assertEqual(self.state(), team)
        self.assertEqual(db.get_paper_decision_state(paper_id)['decision'], 'favorite')


if __name__ == '__main__':
    unittest.main()
