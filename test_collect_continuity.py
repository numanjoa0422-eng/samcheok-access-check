"""실제 전체 페이지와 부분 실패를 통한 자료 보존·검수 회귀시험."""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime as RealDatetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('collector_continuity', ROOT / 'scripts/collect_youth_center.py')
cy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cy
SPEC.loader.exec_module(cy)
base = cy.base
FIXTURE = (ROOT / 'tests/fixtures/youth_notice_sample.html').read_text(encoding='utf-8')
FULL_PAGE = (ROOT / 'tests/fixtures/youth_notice_full_page.html').read_text(encoding='utf-8')
LIST_URL = 'https://youth.samcheok.go.kr/youth/sub/Notice.php'
LAST_SUCCESS = '2026-09-29T08:50:00+09:00'


def detail_url(item_id):
    return LIST_URL + '?bbsData=' + item_id + '&mode=view'


def response(url, text, status=200):
    return base.Response(status, url, {'content-type': 'text/html; charset=utf-8'}, text.encode())


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'programs.json'
        self.old = {
            'institution': cy.TARGET_NAME, 'status': '수집 완료(3건)',
            'updated_at': LAST_SUCCESS, 'last_success_at': LAST_SUCCESS,
            'programs': [
                {'id': key, 'title': '기존 ' + key, 'target': '기존 대상',
                 'last_success_at': LAST_SUCCESS, 'url': detail_url(key)}
                for key in ['A', 'B', 'C']
            ],
        }
        self.path.write_text(json.dumps(self.old), encoding='utf-8')

    def tearDown(self):
        self.directory.cleanup()

    def run_main(self, details, list_text=None):
        if list_text is None:
            list_text = ''.join('<a href="' + detail_url(key) + '">공고 ' + key + '</a>' for key in details)
        class FakeClient:
            def fetch(self, url):
                return response(url, list_text) if url == LIST_URL else details[url]
        with patch.object(cy, 'DATA_PATH', self.path), patch.object(base, 'Client', FakeClient):
            code = cy.main()
        return code, json.loads(self.path.read_text(encoding='utf-8'))

    def test_partial_detail_failure_preserves_failed_and_older_records(self):
        code, result = self.run_main({
            detail_url('A'): response(detail_url('A'), FIXTURE),
            detail_url('B'): response(detail_url('B'), 'unavailable', 503),
        }, ''.join('<a href="' + detail_url(key) + '">공고</a>' for key in ['A', 'B']))
        records = {p['id']: p for p in result['programs']}
        self.assertEqual(code, 0)
        self.assertEqual(set(records), {'A', 'B', 'C'})
        self.assertEqual(records['B']['title'], '기존 B')
        self.assertEqual(records['B']['target'], '기존 대상')
        self.assertEqual(records['B']['last_success_at'], LAST_SUCCESS)
        self.assertEqual(records['B']['source_status'], 'detail_failed')
        self.assertEqual(records['C']['source_status'], 'not_seen_on_first_page')
        self.assertTrue(records['B']['stale'])
        self.assertEqual(result['last_success_at'], LAST_SUCCESS)
        self.assertEqual(result['quality']['preserved_records'], 2)
        self.assertEqual(result['quality']['failed_details'], 1)
        self.assertEqual(result['review_state'], 'needs_review')

    def test_all_detail_failures_preserve_records_and_signal_nonzero(self):
        code, result = self.run_main({detail_url('A'): response(detail_url('A'), 'unavailable', 503)},
                                     '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertEqual(code, 1)
        self.assertEqual({p['id'] for p in result['programs']}, {'A', 'B', 'C'})
        self.assertEqual(result['last_success_at'], LAST_SUCCESS)
        self.assertNotEqual(result['last_attempt_at'], LAST_SUCCESS)

    def test_legacy_attempt_timestamp_is_not_promoted_to_verified_success(self):
        self.old.pop('last_success_at')
        self.path.write_text(json.dumps(self.old), encoding='utf-8')
        _, result = self.run_main({detail_url('A'): response(detail_url('A'), 'unavailable', 503)},
                                  '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertIsNone(result['last_success_at'])

    def test_http200_waf_is_not_a_notice_or_successful_refresh(self):
        block = '<h3>웹방화벽 요청이 차단되었습니다</h3>'
        code, result = self.run_main({detail_url('A'): response(detail_url('A'), block)},
                                     '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertEqual(code, 1)
        self.assertEqual(result['programs'][0]['title'], '기존 A')
        self.assertEqual(result['last_success_at'], LAST_SUCCESS)

    def test_http200_wrong_layout_preserves_existing_fields(self):
        html = '<html><h3>메뉴 이름</h3><div class="view_body"><p>다른 페이지</p></div></html>'
        code, result = self.run_main({detail_url('A'): response(detail_url('A'), html)},
                                     '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertEqual(code, 1)
        self.assertEqual(result['programs'][0]['target'], '기존 대상')

    def test_uncertain_list_is_not_used_even_if_it_contains_links(self):
        code, result = self.run_main({}, '<p>웹방화벽 요청이 차단되었습니다</p>'
                                     '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertEqual(code, 1)
        self.assertEqual(result['quality']['successful_details'], 0)
        self.assertEqual(result['last_success_at'], LAST_SUCCESS)

    def test_legacy_unkeyed_records_survive_a_successful_refresh(self):
        self.old['programs'].append({'title': '예전 ID 없는 자료'})
        self.path.write_text(json.dumps(self.old), encoding='utf-8')
        _, result = self.run_main({detail_url('A'): response(detail_url('A'), FIXTURE)},
                                  '<a href="' + detail_url('A') + '">공고 A</a>')
        self.assertIn({'title': '예전 ID 없는 자료'}, result['programs'])

    def test_collection_clock_does_not_revive_an_old_notice(self):
        class Clock:
            @staticmethod
            def now(zone):
                return RealDatetime(2027, 10, 5, 12, 0, tzinfo=zone)
        with patch.object(cy, 'datetime', Clock):
            _, result = self.run_main({detail_url('A'): response(detail_url('A'), FIXTURE)},
                                      '<a href="' + detail_url('A') + '">공고 A</a>')
        record = next(p for p in result['programs'] if p['id'] == 'A')
        self.assertEqual(record['application_start'], '2026-10-01')
        self.assertEqual(record['status'], '마감')
        self.assertFalse(record['availability_verified'])
        self.assertEqual(record['notice_type'], 'review')

    def test_kst_midnight_changes_schedule_date(self):
        for instant, expected in [('2026-09-30T14:59:00+00:00', '접수예정'),
                                  ('2026-09-30T15:00:00+00:00', '모집중')]:
            class Clock:
                @staticmethod
                def now(zone):
                    return RealDatetime.fromisoformat(instant).astimezone(zone)
            with self.subTest(instant=instant), patch.object(cy, 'datetime', Clock):
                _, result = self.run_main({detail_url('A'): response(detail_url('A'), FIXTURE)},
                                          '<a href="' + detail_url('A') + '">공고 A</a>')
            self.assertEqual(next(p for p in result['programs'] if p['id'] == 'A')['status'], expected)


class FullPageTests(unittest.TestCase):
    def test_navigation_h3_does_not_replace_notice_title(self):
        detail = cy.parse_detail(FULL_PAGE, LIST_URL + '?mode=view')
        self.assertTrue(detail['_parse_valid'])
        self.assertEqual(detail['title'],
                         '2026년 삼척시청소년어울림마당 [우리 함께 놀토데이] 체험부스 및 공연 참가 청소년 모집')
        self.assertEqual(detail['published_date'], '2026-09-22')
        self.assertIn('10:00~18:00', detail['event_datetime'])
        self.assertNotIn('리허설', detail['event_datetime'])
        self.assertEqual(detail['application_start'], '2026-10-01')

    def test_standalone_label_stops_after_next_paragraph(self):
        fields = cy.extract_labeled_fields(['행사대상', '초등학생', '문의 : 570-0000', '추가 설명' * 100])
        self.assertEqual(fields['target'], '초등학생')

    def test_unknown_label_stops_a_pending_value(self):
        fields = cy.extract_labeled_fields(['모집기간', '신청방법 : 전화', '10월1일~16일 신청'])
        self.assertEqual(fields['application_period'], '')

    def test_image_only_body_is_inconclusive(self):
        html = '<section class="bbsView"><div class="view_head"><h3>공고</h3></div>' \
               '<div class="view_body"><img src="poster.png"></div></section>'
        self.assertFalse(cy.parse_detail(html, LIST_URL)['_parse_valid'])


if __name__ == '__main__':
    unittest.main()
