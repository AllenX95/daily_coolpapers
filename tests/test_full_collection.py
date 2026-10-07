import unittest
from urllib.parse import parse_qs, urlparse
from daily_coolpapers.crawler import fetch_category_report
from tests.test_crawl_observability import FakeClient, FakeResponse, _html, _paper_block


class FullCollectionTests(unittest.TestCase):
    def report(self,total=3,*,second=None,first_date='2026-09-03',cap=10000):
        first=_html(_paper_block('2609.00001',1) if total else '',page_date=first_date,total=total)
        second=second if second is not None else _html(*[_paper_block(f'2609.{i:05}',i) for i in range(1,(total or 0)+1)],total=total)
        client=FakeClient(FakeResponse(first),FakeResponse(second))
        result=fetch_category_report('cs.AI',collection_mode='full',crawl_date='2026-09-03',client=client,full_max_papers=cap)
        return result,client

    def test_full_count_above_old_limit_and_shared_client(self):
        result,client=self.report(201)
        self.assertEqual(len(result.papers),201);self.assertEqual(result.status,'success')
        self.assertTrue(result.metrics['completeness_verified']);self.assertEqual(client.calls,2);self.assertFalse(client.closed)
        self.assertEqual(result.metrics['expected_count'],201);self.assertEqual(result.metrics['missing_count'],0)
        self.assertGreater(result.metrics['total_response_bytes'],result.metrics['response_bytes'])

    def test_partial_display_never_complete(self):
        result,_=self.report(3,second=_html(_paper_block('2609.00001',1),total=3))
        self.assertEqual(result.status,'warning');self.assertFalse(result.metrics['completeness_verified'])
        self.assertEqual(result.metrics['missing_count'],2)

    def test_duplicate_id_count_never_complete(self):
        result,_=self.report(3,second=_html(*[_paper_block('2609.00001',i) for i in range(1,4)],total=3))
        self.assertIn('source_duplicate_ids',result.error_codes);self.assertEqual(len(result.papers),1)
        self.assertEqual(result.metrics['source_duplicate_count'],2)

    def test_total_change_is_warning_even_when_second_count_matches(self):
        result,_=self.report(3,second=_html(_paper_block('2609.00001',1),_paper_block('2609.00002',2),total=2))
        self.assertIn('declared_total_changed',result.error_codes);self.assertFalse(result.metrics['completeness_verified'])

    def test_resource_cap_never_claims_full_success(self):
        result,_=self.report(3,cap=2)
        self.assertIn('full_resource_limit',result.error_codes);self.assertEqual(result.status,'warning')

    def test_only_matching_explicit_zero_is_empty_success(self):
        result,client=self.report(0)
        self.assertEqual(result.status,'empty_success');self.assertTrue(result.metrics['completeness_verified']);self.assertEqual(client.calls,1)
        for day in [None,'2026-09-04']:
            result,client=self.report(0,first_date=day)
            self.assertFalse(result.metrics['completeness_verified']);self.assertNotEqual(result.status,'empty_success')

    def test_unknown_total_and_date_wrong_never_request_expansion(self):
        for total,day in [(None,'2026-09-03'),(3,'2026-09-04')]:
            result,client=self.report(total,first_date=day)
            self.assertEqual(client.calls,1);self.assertFalse(result.metrics['completeness_verified'])

    def test_show_override_and_requested_date(self):
        urls=[]
        class Client:
            def get(self,url,**kwargs):
                urls.append(url)
                show=int(parse_qs(urlparse(url).query)['show'][0])
                return FakeResponse(_html(*[_paper_block(f'2609.{i:05}',i) for i in range(1,show+1)],total=3))
        result=fetch_category_report('cs.AI',collection_mode='full',sort_param='sort=1&show=1&date=2020-01-01',crawl_date='2026-09-03',client=Client())
        self.assertTrue(result.metrics['completeness_verified']);self.assertEqual(parse_qs(urlparse(urls[1]).query)['show'],['3'])
        self.assertTrue(all(parse_qs(urlparse(url).query)['date']==['2026-09-03'] for url in urls))

    def test_total_one_with_extra_heading_never_complete(self):
        client=FakeClient(FakeResponse(_html(_paper_block('2609.00001',1),_paper_block('2609.00002',2),total=1)))
        result=fetch_category_report('cs.AI',collection_mode='full',crawl_date='2026-09-03',client=client)
        self.assertFalse(result.metrics['completeness_verified']);self.assertIn('source_heading_count_mismatch',result.error_codes)

    def test_requests_have_distinct_attempt_event_identity(self):
        result,_=self.report(3)
        successes=[event for event in result.attempt_events if event['event']=='http_succeeded']
        self.assertEqual([event['request_index'] for event in successes],[1,2])

    def test_target_date_required(self):
        with self.assertRaises(ValueError):fetch_category_report('cs.AI',collection_mode='full',client=FakeClient())


if __name__ == '__main__':unittest.main()
