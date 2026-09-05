import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

from daily_coolpapers import db
from tests import test_personal_library as library


class DirectionDecisionTests(unittest.TestCase):
    setUp = library.PersonalLibraryTests.setUp
    paper = library.PersonalLibraryTests.paper
    evaluate = library.PersonalLibraryTests.evaluate

    def direction(self, name='Agents'):
        return db.create_attention_direction(name,'User-written scope')

    def model(self,paper_id,direction_id,state='possible'):
        with db.connect() as conn:
            conn.execute('''INSERT INTO paper_direction_results(paper_id,direction_id,model_decision,model_reason,created_at,updated_at)
                VALUES (?,?,?,'Original reason','2026-09-03','2026-09-03')''',(paper_id,direction_id,state))

    def decide(self,paper_id,direction_id,decision='confirmed',**kwargs):
        return self.client.post(f'/api/papers/{paper_id}/direction-decisions',
            data={'csrf_token':'test-library','direction_id':direction_id,'decision':decision},**kwargs)

    def ids(self,**filters):
        return [r['id'] for r in db.list_paper_page(**filters)['items']]

    def test_no_active_directions_old_list_not_empty(self):
        paper = self.paper(status=None)
        self.assertEqual(self.ids(),[paper])
        self.assertIn('当前未启用关注方向',self.client.get('/').get_data(as_text=True))

    def test_default_uses_effective_and_pending_not_raw_matched(self):
        direction = self.direction()
        papers = [self.paper(i,status=None) for i in range(1,5)]
        for paper,state in zip(papers,['matched','possible','unmatched','failed']):
            self.model(paper,direction,state)
        self.assertEqual(self.ids(),papers[:2])
        self.decide(papers[0],direction,'rejected')
        self.assertEqual(self.ids(),[papers[1]])
        self.decide(papers[1],direction,'rejected')
        self.assertEqual(self.ids(),[])
        self.decide(papers[2],direction)
        self.assertEqual(self.ids(),[papers[2]])
        self.assertEqual(self.ids(model_state='matched'),[papers[0]])
        self.assertEqual(self.ids(model_state='failed'),[papers[3]])

    def test_manual_only_other_direction_and_multiple_keep_original_model(self):
        paper = self.paper(status=None)
        first,second = self.direction(),self.direction('Vision')
        self.model(paper,first)
        self.assertEqual(self.decide(paper,first).status_code,302)
        self.assertEqual(self.decide(paper,second).status_code,302)
        rows = db.paper_direction_results([paper])[paper]
        self.assertEqual([r['model_decision'] for r in rows],['possible',None])
        self.assertEqual([r['manual_decision'] for r in rows],['confirmed','confirmed'])
        self.assertTrue(all(r['effective'] for r in rows))
        self.assertEqual(rows[0]['model_reason'],'Original reason')
        self.assertTrue(self.runner.queue.empty())
        self.llm.assert_not_called()
        self.assertEqual(db.list_paper_investment_themes([paper]),{paper:[]})

    def test_pending_rejected_filters_and_same_direction_predicates(self):
        paper = self.paper(status=None)
        first,second = self.direction(),self.direction('Second')
        self.model(paper,first,'possible')
        self.model(paper,second,'matched')
        self.decide(paper,second,'rejected')
        self.assertEqual(self.ids(manual_state='pending'),[paper])
        self.assertEqual(self.ids(direction_id=first,manual_state='rejected'),[])
        self.assertEqual(self.ids(direction_id=second,manual_state='rejected'),[paper])
        self.assertEqual(self.ids(direction_id=second),[])

    def test_archive_preserves_history_not_active_default(self):
        paper = self.paper(status=None)
        first = self.direction()
        self.model(paper,first,'matched')
        self.direction('Still active')
        db.archive_attention_direction(first)
        self.assertEqual(self.ids(),[])
        self.assertEqual(self.ids(direction_id=first),[paper])
        self.assertEqual(self.ids(direction_view='all'),[paper])
        self.assertEqual(self.decide(paper,first).status_code,409)
        html = self.client.get(f'/papers/{paper}').get_data(as_text=True)
        self.assertIn('已归档 · 历史记录',html)

    def test_invalid_missing_archived_and_csrf_zero_write(self):
        paper,direction = self.paper(status=None),self.direction()
        self.assertEqual(self.decide(paper,direction,decision='maybe').status_code,400)
        self.assertEqual(self.decide(999,direction).status_code,404)
        self.assertEqual(self.decide(2**100,direction).status_code,404)
        self.assertEqual(self.decide(paper,999).status_code,404)
        self.assertEqual(self.decide(paper,direction,headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.post(f'/api/papers/{paper}/direction-decisions',data={'direction_id':direction,'decision':'confirmed'}).status_code,403)
        self.assertEqual(db.paper_direction_results([paper]),{})

    def test_idempotent_manual_timestamp_and_get_read_only(self):
        paper,direction = self.paper(status=None),self.direction()
        self.decide(paper,direction)
        before = db.paper_direction_results([paper])
        with patch.object(db,'now_iso',return_value='2099-01-01'):
            self.decide(paper,direction)
        self.assertEqual(db.paper_direction_results([paper]),before)
        original = db.connect
        def readonly():
            conn = original()
            conn.execute('PRAGMA query_only=ON')
            return conn
        with patch.object(db,'connect',side_effect=readonly):
            for path in ('/',f'/papers/{paper}','/attention-directions'):
                self.assertEqual(self.client.get(path).status_code,200)

    def test_filters_before_pagination_and_sort_export_preserve_scope(self):
        direction = self.direction()
        for i in range(1,7):
            paper = self.paper(i,status=None)
            self.model(paper,direction,'possible' if i%2==0 else 'unmatched')
        page = db.list_paper_page(page=2,page_size=2)
        self.assertEqual(page['total'],3)
        self.assertEqual([p['id'] for p in page['items']],[6])
        response = self.client.get(f'/?model_state=unmatched&direction_id={direction}&page_size=1')
        soup = BeautifulSoup(response.data,'html.parser')
        for link in soup.select('a.sort-link, nav.pagination a'):
            self.assertIn('model_state=unmatched',link['href'])
            self.assertIn(f'direction_id={direction}',link['href'])
        export = next(link['href'] for link in soup.select('a') if link.get_text()=='导出 CSV')
        csv = self.client.get(export).get_data(as_text=True)
        self.assertIn('Library Paper 1',csv)
        self.assertNotIn('Library Paper 2',csv)

    def test_detail_displays_original_manual_and_escaped_reason(self):
        paper,direction = self.paper(status=None),self.direction('<script>x</script>')
        self.model(paper,direction)
        self.decide(paper,direction,'rejected')
        html = self.client.get(f'/papers/{paper}').get_data(as_text=True)
        self.assertIn('possible',html)
        self.assertIn('已否决',html)
        self.assertIn('Original reason',html)
        self.assertNotIn('<script>x</script>',html)
        self.assertIn('手动摘要评估',html)
        self.assertNotIn('id="paper-decision"',html)


if __name__ == '__main__':
    unittest.main()
