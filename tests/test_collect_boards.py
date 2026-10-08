import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import check_access as access
import collect_boards as collector

FIXTURES = ROOT / 'tests' / 'fixtures'
NOW = '2026-10-02T16:30:00+09:00'
OLD = '2026-10-01T16:30:00+09:00'
SCHOOL_URL = 'https://samcheok.gwe.es.kr/boardCnts/view.do?boardID=37778&boardSeq=9669994'
SOURCE = {
    'id': 'samcheokes', 'enabled': True, 'parser': 'school_cms',
    'access_target_id': '삼척초', 'org': '삼척초등학교',
    'list_url': 'https://samcheok.gwe.es.kr/list', 'max_items': 10,
    'include_keywords': ['캠프', '학부모'], 'exclude_keywords': ['채용', '공유재산'],
}
TARGET = {'name': '삼척초', 'list_url': SOURCE['list_url'], 'item_link_pattern': r'boardSeq=(\d+)'}


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def response(url, raw='', **kwargs):
    return access.Response(200, url, {'content-type': 'text/html; charset=utf-8'}, raw.encode('utf-8'), **kwargs)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


class DetailEvidenceTests(unittest.TestCase):
    def test_korean_numbered_labels_and_exhibition_aliases(self):
        fields = collector.explicit_fields(['가. 운영일시: 2026. 10. 17.(토) 13:00~16:00',
            '나. 운영장소: 동해 체험센터', '바. 신청방법: 누리집 접수'])
        self.assertEqual(fields['location'], '동해 체험센터')
        self.assertEqual(fields['application_method'], '누리집 접수')
        self.assertEqual(fields['event_datetime'], '2026. 10. 17.(토) 13:00~16:00')
        fields = collector.explicit_fields(['전시기간 : 2026. 9. 9. ~ 10. 30.', '전시장소 : 도계로 47'])
        self.assertEqual(fields['location'], '도계로 47')

    def test_actual_school_scoped_fields_deadline_and_no_grade_invention(self):
        result = collector.parse_detail(fixture('school_actual_9669994.html'), SCHOOL_URL, 'school_cms')
        self.assertTrue(result['_parse_valid'])
        self.assertEqual(result['title'], '2026 삼척 독서캠프 운영 계획 알림 및 참가 신청 안내')
        self.assertEqual(result['target'], '희망 1∼6학년 초등학생 및 1∼2학년 학부모(선착순)')
        self.assertEqual(result['event_datetime'], '2026. 10. 17.(토) 09:40~12:00')
        self.assertEqual(result['location'], '삼척남초등학교')
        self.assertEqual(result['application_end'], '2026-10-01')
        self.assertIsNone(result['application_start'])
        self.assertEqual(result['published_date'], '2026-09-28')
        self.assertNotIn('grades', result)
        self.assertFalse(result['availability_verified'])
        self.assertEqual(result['status'], '확인필요')

    def test_school_navigation_hidden_data_and_scripts_are_not_evidence(self):
        raw = fixture('school_actual_9669994.html').replace("<div class='viewBox'>",
            "<div class='viewBox'><p hidden>대상: 대학생 전용</p><script>장소: 가짜</script>")
        result = collector.parse_detail(raw, SCHOOL_URL, 'school_cms')
        self.assertNotIn('이전글', result['body_excerpt'])
        self.assertNotIn('강원대학교 해양교육원', result['body_excerpt'])
        self.assertNotIn('대학생 전용', result['body_excerpt'])
        self.assertNotIn('가짜', result['body_excerpt'])

    def test_actual_school_attachment_links_are_not_content_extraction(self):
        result = collector.parse_detail(fixture('school_actual_9669994.html'), SCHOOL_URL, 'school_cms')
        self.assertEqual(len(result['attachment_urls']), 1)
        self.assertIn('/boardCnts/fileDown.do?', result['attachment_url'])
        self.assertFalse(result['attachments_downloaded'])

    def test_actual_education_uses_same_verified_cms_scope(self):
        result = collector.parse_detail(fixture('new-board-education-detail-7715223.html'),
            'https://sce.gwe.go.kr/boardCnts/view.do?boardID=1595&boardSeq=7715223', 'school_cms')
        self.assertTrue(result['_parse_valid'])
        self.assertIn('공유재산', result['title'])
        self.assertFalse(collector.relevance(result['title'], result['body_excerpt'], SOURCE)[0])

    def test_actual_wdyouth_multiple_program_deadlines_are_not_flattened(self):
        result = collector.parse_detail(fixture('new-board-wdyouth-detail-141.html'),
            'https://wdyouth.samcheok.go.kr/wdyouth/sub/Notice.php?mode=view', 'youth_notice')
        self.assertTrue(result['_parse_valid'])
        self.assertIn('8/10~8/14', result['application_period_text'])
        self.assertIn('8/8', result['application_period_text'])
        self.assertIsNone(result['application_start'])
        self.assertIsNone(result['application_end'])
        self.assertTrue(result['body_image_urls'][0].startswith('https://wdyouth.samcheok.go.kr/wdyouth/'))

    def test_actual_dgyouth_poster_keeps_image_without_claiming_ocr(self):
        result = collector.parse_detail(fixture('new-board-dgyouth-detail-204.html'),
            'https://dgyouth.samcheok.go.kr/dgyouth/sub/Notice.php?mode=view', 'youth_notice')
        self.assertTrue(result['_parse_valid'])
        self.assertEqual(result['body_content_type'], 'image_only')
        self.assertTrue(result['body_image_urls'])
        self.assertIsNone(result['target'])
        self.assertIsNone(result['application_end'])
        self.assertFalse(result['image_content_extracted'])

    def test_actual_foundation_fields_stream_images_and_attachments(self):
        result = collector.parse_detail(fixture('new-board-stcf-detail-1035.html'),
            'https://www.stcf.or.kr/01298/01315.web?idx=1035&amode=view', 'foundation')
        self.assertTrue(result['_parse_valid'])
        self.assertEqual(result['title'], '「2026 삼척 관광동행 포럼」 개최 안내')
        self.assertEqual(result['event_datetime'], '2026. 10. 23.(금) 13:30~17:30')
        self.assertIn('소상공인', result['target'])
        self.assertTrue(result['body_image_urls'][0].startswith('https://www.stcf.or.kr/board/image.do?'))
        self.assertEqual(len(result['attachment_urls']), 3)
        self.assertNotIn('삼척시청', result['body_excerpt'])

    def test_actual_foundation_list_only_ten_rows_no_snippet_or_footer_link(self):
        page = collector.FoundationList(fixture('new-board-stcf-list.html'))
        items = access.find_items(page, {'item_link_pattern': r'idx=(\d+)'}, 'https://www.stcf.or.kr/01298/01315.web')
        self.assertEqual(len(items), 10)
        self.assertEqual(items['1035']['title'], '「2026 삼척 관광동행 포럼」 개최 안내')
        self.assertNotIn('987', items)
        self.assertNotIn('관광트렌드', items['1035']['title'])

    def test_actual_cms_javascript_list_links_are_resolved(self):
        config = json.loads((ROOT / 'config/targets.json').read_text())
        target = next(t for t in config['targets'] if '교육지원청' in t['name'])
        items = access.find_items(access.Page(fixture('new-board-education-list.html')), target, target['list_url'])
        self.assertEqual(len(items), 10)
        self.assertIn('7716310', items)
        self.assertIn('boardSeq=7716310', items['7716310']['url'])

    def test_changed_body_changes_hash_but_hidden_content_does_not(self):
        raw = fixture('school_actual_9669994.html')
        original = collector.parse_detail(raw, SCHOOL_URL, 'school_cms')['body_hash']
        changed = collector.parse_detail(raw.replace('삼척남초등학교', '다른 장소'), SCHOOL_URL, 'school_cms')['body_hash']
        hidden = collector.parse_detail(raw.replace("<div class='viewBox'>", "<div class='viewBox'><p hidden>숨은 값</p>"), SCHOOL_URL, 'school_cms')['body_hash']
        self.assertNotEqual(original, changed)
        self.assertEqual(original, hidden)


class ContinuityTests(unittest.TestCase):
    def old(self, remote='9669994'):
        return {'id': collector.stable_id('samcheokes', remote), 'source_id': 'samcheokes',
                'title': '기존 검증된 캠프', 'url': SCHOOL_URL, 'target': '확인된 대상',
                'last_success_at': OLD, 'last_detail_success_at': OLD, 'source_status': 'current', 'stale': False}

    def list_html(self, remote='9669994', title='2026 삼척 독서캠프'):
        return f'<a href="{SCHOOL_URL.replace("9669994", remote)}">{title}</a>'

    def collect(self, raw_list=None, detail=None, previous=None):
        client = FakeClient({SOURCE['list_url']: response(SOURCE['list_url'], self.list_html() if raw_list is None else raw_list),
                             SCHOOL_URL: detail or response(SCHOOL_URL, fixture('school_actual_9669994.html'))})
        records, summary = collector.collect_source(SOURCE, TARGET, previous or [], {'last_success_at': OLD}, client, NOW)
        return records, summary, client

    def test_full_success_keeps_old_missing_first_page_record(self):
        old = self.old('1')
        records, summary, _ = self.collect(previous=[old])
        self.assertEqual(len(records), 2)
        kept = next(r for r in records if r['id'] == old['id'])
        self.assertTrue(kept['stale'])
        self.assertEqual(kept['last_success_at'], OLD)
        self.assertEqual(summary['last_success_at'], NOW)
        self.assertEqual(old['source_status'], 'current')

    def test_detail_failure_preserves_prior_fields_and_success_timestamp(self):
        records, summary, _ = self.collect(detail=access.Response(None, SCHOOL_URL, error='接속 실패'), previous=[self.old()])
        self.assertEqual(records[0]['target'], '확인된 대상')
        self.assertEqual(records[0]['last_success_at'], OLD)
        self.assertEqual(records[0]['source_status'], 'detail_failed')
        self.assertTrue(records[0]['stale'])
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['last_success_at'], OLD)

    def test_empty_list_preserves_prior_no_fake_zero_change(self):
        records, summary, _ = self.collect(raw_list='<html>학교 홈페이지</html>', previous=[self.old()])
        self.assertEqual(records[0]['title'], '기존 검증된 캠프')
        self.assertEqual(records[0]['last_success_at'], OLD)
        self.assertEqual(summary['status'], 'failed')
        self.assertIn('0건', summary['errors'][0]['reason'])

    def test_robots_failure_never_fetches_list_or_details_unsafely(self):
        client = FakeClient({SOURCE['list_url']: access.Response(None, SOURCE['list_url'], policy='제한: robots.txt')})
        records, summary = collector.collect_source(SOURCE, TARGET, [self.old()], {}, client, NOW)
        self.assertEqual(client.fetched, [SOURCE['list_url']])
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(records[0]['last_success_at'], OLD)

    def test_new_failed_detail_retains_honest_list_title_link_candidate(self):
        records, summary, _ = self.collect(detail=access.Response(None, SCHOOL_URL, error='SSL EOF'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['body_content_type'], 'unverified')
        self.assertEqual(records[0]['last_success_basis'], 'first_page_title_and_link_only')
        self.assertIsNone(records[0]['last_detail_success_at'])
        self.assertIsNone(records[0]['target'])
        self.assertEqual(records[0]['status'], '확인필요')
        self.assertEqual(summary['status'], 'partial')

    def test_obvious_contract_notice_not_fetched_or_added(self):
        records, summary, client = self.collect(raw_list=self.list_html(title='공유재산 매각 계약'))
        self.assertEqual(records, [])
        self.assertEqual(client.fetched, [SOURCE['list_url']])
        self.assertEqual(summary['status'], 'success')

    def test_title_without_keyword_can_be_included_from_scoped_body(self):
        records, summary, _ = self.collect(raw_list=self.list_html(title='알림'))
        self.assertEqual(summary['collected_count'], 1)
        self.assertEqual(records[0]['title'], '2026 삼척 독서캠프 운영 계획 알림 및 참가 신청 안내')

    def test_two_institutions_same_remote_id_are_distinct(self):
        self.assertNotEqual(collector.stable_id('samcheokes', '1'), collector.stable_id('jeongraes', '1'))
        self.assertIn('%7C', collector.stable_id('dgyouth', 'bm89|'))

    def test_disabled_source_preserves_history_as_stale(self):
        result = collector.collect_all({'sources': [dict(SOURCE, enabled=False)]}, {'targets': [TARGET]},
                                      {'programs': [self.old()]}, FakeClient({}), NOW)
        self.assertEqual(result['programs'][0]['source_status'], 'source_disabled')
        self.assertEqual(result['programs'][0]['last_success_at'], OLD)
        self.assertEqual(result['sources'][0]['id'], 'samcheokes')
        self.assertEqual(result['sources'][0]['org'], SOURCE['org'])
        self.assertFalse(result['sources'][0]['enabled'])
        self.assertEqual(result['sources'][0]['status'], 'disabled')
        self.assertEqual(result['totals']['enabled_sources'], 0)
        self.assertEqual(result['status'], 'no_enabled_sources')

    def test_removed_source_summary_retained_with_enabled_other_and_correct_totals(self):
        other = dict(SOURCE, id='jeongraes', org='정라초등학교')
        previous_summary = {'id': 'samcheokes', 'org': SOURCE['org'], 'parser': 'school_cms',
                            'enabled': True, 'status': 'success', 'last_success_at': OLD,
                            'last_attempt_at': OLD, 'list_url': SOURCE['list_url']}
        client = FakeClient({SOURCE['list_url']: response(SOURCE['list_url'], self.list_html()),
                             SCHOOL_URL: response(SCHOOL_URL, fixture('school_actual_9669994.html'))})
        result = collector.collect_all({'sources': [other]}, {'targets': [TARGET]},
            {'programs': [self.old()], 'sources': [previous_summary]}, client, NOW)
        summaries = {s['id']: s for s in result['sources']}
        self.assertEqual(set(summaries), {'samcheokes', 'jeongraes'})
        self.assertEqual(summaries['samcheokes']['status'], 'unreported')
        self.assertFalse(summaries['samcheokes']['enabled'])
        self.assertEqual(summaries['samcheokes']['last_success_at'], OLD)
        self.assertEqual(summaries['samcheokes']['last_attempt_at'], OLD)
        self.assertEqual(summaries['samcheokes']['org'], SOURCE['org'])
        self.assertEqual(result['totals']['enabled_sources'], 1)
        self.assertEqual(result['totals']['successful_sources'], 1)
        self.assertEqual(result['totals']['failed_sources'], 0)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(all(p['source_id'] in summaries for p in result['programs']))

    def test_disabled_source_summary_without_history_still_reports_disabled_not_success(self):
        result = collector.collect_all({'sources': [dict(SOURCE, enabled=False)]}, {'targets': [TARGET]},
            {'programs': []}, FakeClient({}), NOW)
        self.assertEqual(result['sources'][0]['status'], 'disabled')
        self.assertEqual(result['sources'][0]['last_success_at'], None)
        self.assertEqual(result['sources'][0]['last_attempt_at'], None)
        self.assertEqual(result['totals']['enabled_sources'], 0)

    def test_all_enabled_failure_exit_not_masked_by_disabled_source_summary(self):
        disabled = dict(SOURCE, id='disabled-school', enabled=False)
        with tempfile.TemporaryDirectory() as tmp:
            cfg, targets, out = (Path(tmp) / name for name in ('cfg.json', 'targets.json', 'out.json'))
            cfg.write_text(json.dumps({'sources': [SOURCE, disabled]}))
            targets.write_text(json.dumps({'targets': [TARGET]}))
            client = FakeClient({SOURCE['list_url']: access.Response(None, SOURCE['list_url'], policy='제한: robots.txt')})
            with patch.object(collector, 'BudgetClient', return_value=client):
                self.assertEqual(collector.main(['--config', str(cfg), '--access-config', str(targets), '--output', str(out)]), 1)
            result = json.loads(out.read_text())
            self.assertEqual(result['totals']['enabled_sources'], 1)
            self.assertEqual(result['totals']['failed_sources'], 1)
            self.assertEqual(len(result['sources']), 2)

    def test_corrupt_existing_records_fail_before_network_or_file_overwrite(self):
        with self.assertRaisesRegex(ValueError, '식별자'):
            collector.collect_all({'sources': [SOURCE]}, {'targets': [TARGET]}, {'programs': [{}]}, FakeClient({}), NOW)

    def test_atomic_write_and_invalid_existing_main_preserves_exact_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'board-programs.json'
            collector.atomic_write(out, {'programs': []})
            self.assertEqual(json.loads(out.read_text()), {'programs': []})
            self.assertFalse(out.with_name(out.name + '.tmp').exists())
            out.write_text('invalid old content')
            cfg = Path(tmp) / 'cfg.json'
            cfg.write_text(json.dumps({'sources': [SOURCE]}))
            targets = Path(tmp) / 'targets.json'
            targets.write_text(json.dumps({'targets': [TARGET]}))
            self.assertEqual(collector.main(['--config', str(cfg), '--access-config', str(targets), '--output', str(out)]), 1)
            self.assertEqual(out.read_text(), 'invalid old content')


class BudgetTests(unittest.TestCase):
    def test_expired_budget_refuses_http_and_keeps_previous_records(self):
        client = collector.BudgetClient(seconds=-1)
        records, summary = collector.collect_source(SOURCE, TARGET, [ContinuityTests().old()], {}, client, NOW)
        self.assertEqual(summary['status'], 'failed')
        self.assertIn('시간 예산', summary['errors'][0]['reason'])
        self.assertEqual(records[0]['last_success_at'], OLD)

    def test_crawl_delay_longer_than_budget_is_not_bypassed(self):
        client = collector.BudgetClient(seconds=5)
        client.original_sleep = lambda delay: self.fail('must not sleep beyond budget')
        with self.assertRaises(collector.BudgetExceeded):
            client.safe_sleep(10)


if __name__ == '__main__':
    unittest.main()
