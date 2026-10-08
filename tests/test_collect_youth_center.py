import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

spec = importlib.util.spec_from_file_location('checker', ROOT / 'scripts/check_access.py')
base = importlib.util.module_from_spec(spec)
sys.modules['checker'] = base
spec.loader.exec_module(base)

spec2 = importlib.util.spec_from_file_location('collector', ROOT / 'scripts/collect_youth_center.py')
cy = importlib.util.module_from_spec(spec2)
sys.modules[spec2.name] = cy
spec2.loader.exec_module(cy)
# collect_youth_center.py 안에서 'import check_access as base'로 다시 불러온 사본이 아니라,
# 이 테스트 파일이 이미 로드해 둔 것과 같은 check_access 인스턴스를 쓰도록 맞춘다
# (exec_module 이후에 덮어써야 한다 — 먼저 하면 모듈 자체의 import 문이 다시 덮어써 버린다).
cy.base = base

FIXTURE_HTML = (Path(__file__).parent / 'fixtures' / 'youth_notice_sample.html').read_text(encoding='utf-8')
SAMPLE_URL = 'https://youth.samcheok.go.kr/youth/sub/Notice.php?code=yu_notice&bbsData=bm89MzUw||&mode=view'


def resp(html_text, url=SAMPLE_URL, status=200):
    return base.Response(status, url, {'content-type': 'text/html; charset=utf-8'}, html_text.encode())


class ExtractionTests(unittest.TestCase):
    def test_real_notice_fields_extracted(self):
        # youth.samcheok.go.kr에서 실제로 받아온 공고(2026-09-30 확인) 그대로.
        detail = cy.parse_detail(FIXTURE_HTML, SAMPLE_URL, written_year=2026)
        self.assertEqual(detail['title'],
                          '2026년 삼척시청소년어울림마당 [우리 함께 놀토데이] 체험부스 및 공연 참가 청소년 모집')
        self.assertEqual(detail['target'], '삼척시 청소년 누구나')
        self.assertEqual(detail['application_period_text'], '10월1일~16일 신청')
        self.assertEqual(detail['application_start'], '2026-10-01')
        self.assertEqual(detail['application_end'], '2026-10-16')
        self.assertIn('bbs_download.php', detail['attachment_url'])
        self.assertIn('no=350', detail['attachment_url'])
    def test_unlabeled_location_stays_blank(self):
        # 이 공고는 '장소'라는 라벨이 따로 없다 — 본문 어딘가에 장소로 보이는
        # 말이 섞여 있어도 추측해서 채우면 안 된다.
        detail = cy.parse_detail(FIXTURE_HTML, SAMPLE_URL, written_year=2026)
        self.assertIsNone(detail['location'])


class DateRangeTests(unittest.TestCase):
    def test_korean_range_day_only_end_reuses_start_month(self):
        self.assertEqual(cy.parse_date_range('10월1일~16일 신청', 2026), ('2026-10-01', '2026-10-16'))
    def test_korean_range_both_months(self):
        self.assertEqual(cy.parse_date_range('접수기간 9월30일~10월5일', 2026), ('2026-09-30', '2026-10-05'))
    def test_dotted_range_with_years(self):
        self.assertEqual(cy.parse_date_range('2026.10.1.~2026.10.16.', 2026), ('2026-10-01', '2026-10-16'))
    def test_explicit_one_day_date_becomes_one_day_range(self):
        self.assertEqual(cy.parse_date_range('10월 31일 하루만 진행', 2026), ('2026-10-31', '2026-10-31'))
    def test_unparseable_text_returns_none(self):
        self.assertEqual(cy.parse_date_range('추후 별도 공지', 2026), (None, None))
    def test_empty_text_returns_none(self):
        self.assertEqual(cy.parse_date_range('', 2026), (None, None))
    def test_invalid_calendar_date_does_not_crash(self):
        # 월/일 숫자가 우연히 섞여 있어도(예: 13월) 잘못된 날짜를 만들지 않는다.
        self.assertEqual(cy.parse_date_range('13월 40일', 2026), (None, None))


class StatusTests(unittest.TestCase):
    def test_before_start_is_upcoming(self):
        self.assertEqual(cy.compute_status('2026-10-01', '2026-10-16', '2026-09-30'), '접수예정')
    def test_within_range_is_open(self):
        self.assertEqual(cy.compute_status('2026-10-01', '2026-10-16', '2026-10-10'), '모집중')
    def test_on_boundary_dates_is_open(self):
        self.assertEqual(cy.compute_status('2026-10-01', '2026-10-16', '2026-10-01'), '모집중')
        self.assertEqual(cy.compute_status('2026-10-01', '2026-10-16', '2026-10-16'), '모집중')
    def test_after_end_is_closed(self):
        self.assertEqual(cy.compute_status('2026-10-01', '2026-10-16', '2026-10-20'), '마감')
    def test_missing_dates_leaves_status_blank(self):
        self.assertIsNone(cy.compute_status(None, None, '2026-10-10'))


class MainPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_path = cy.DATA_PATH
        cy.DATA_PATH = Path(self.temp.name) / 'programs.json'
    def tearDown(self):
        cy.DATA_PATH = self.old_data_path
        self.temp.cleanup()

    def test_list_fetch_failure_preserves_existing_data(self):
        cy.DATA_PATH.write_text(json.dumps({'institution': cy.TARGET_NAME, 'programs': ['이전 자료']}),
                                 encoding='utf-8')
        before_version = cy.DATA_PATH.read_text(encoding='utf-8')

        class FailingClient:
            def fetch(self, url):
                return base.Response(None, url, error='네트워크 오류(시험용)')
        with patch.object(base, 'Client', FailingClient):
            cy.main()
        after = json.loads(cy.DATA_PATH.read_text(encoding='utf-8'))
        self.assertEqual(after['programs'], ['이전 자료'])
        self.assertIn('수집 실패', after['status'])
        self.assertNotEqual(cy.DATA_PATH.read_text(encoding='utf-8'), before_version)  # status/updated_at는 갱신됨

    def test_zero_items_found_preserves_existing_data(self):
        cy.DATA_PATH.write_text(json.dumps({'institution': cy.TARGET_NAME, 'programs': ['이전 자료']}),
                                 encoding='utf-8')
        list_html = '<html><body>공지사항이 없습니다</body></html>'

        class EmptyClient:
            def fetch(self, url):
                return resp(list_html, url=url)
        with patch.object(base, 'Client', EmptyClient):
            cy.main()
        after = json.loads(cy.DATA_PATH.read_text(encoding='utf-8'))
        self.assertEqual(after['programs'], ['이전 자료'])
        self.assertIn('수집 실패', after['status'])

    def test_successful_run_writes_parsed_program(self):
        list_url = 'https://youth.samcheok.go.kr/youth/sub/Notice.php'
        list_html = (f'<a href="{SAMPLE_URL}">2026년 삼척시청소년어울림마당 [우리 함께 놀토데이] '
                      f'체험부스 및 공연 참가 청소년 모집</a>')

        class FakeClient:
            def fetch(self, url):
                if url == list_url:
                    return resp(list_html, url=list_url)
                return resp(FIXTURE_HTML, url=SAMPLE_URL)
        with patch.object(base, 'Client', FakeClient):
            cy.main()
        after = json.loads(cy.DATA_PATH.read_text(encoding='utf-8'))
        self.assertEqual(len(after['programs']), 1)
        program = after['programs'][0]
        self.assertEqual(program['target'], '삼척시 청소년 누구나')
        self.assertEqual(program['application_start'], '2026-10-01')
        self.assertIn('수집 완료', after['status'])


if __name__ == '__main__':
    unittest.main()
