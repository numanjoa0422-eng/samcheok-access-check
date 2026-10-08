"""실제 이미지 공지와 빈·장식 본문을 구분하고 검토 대기 상태를 유지한다."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('collector_images', ROOT / 'scripts/collect_youth_center.py')
cy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cy
SPEC.loader.exec_module(cy)
base = cy.base
LIST_URL = 'https://youth.samcheok.go.kr/youth/sub/Notice.php'
IMAGE_ID = 'bm89MzM4||'
IMAGE_URL = LIST_URL + '?code=yu_notice&bbsData=' + IMAGE_ID + '&mode=view'
IMAGE_HTML = (ROOT / 'tests/fixtures/youth_notice_image_338.html').read_text(encoding='utf-8')
TEXT_HTML = (ROOT / 'tests/fixtures/youth_notice_sample.html').read_text(encoding='utf-8')
UPLOAD = '../bbsDown/editor_up/2026021321127312915.jpg'


def notice(body, title='공개 공고', published='2026-02-13'):
    date_meta = '<li>작성일:<span>' + published + '</span></li>' if published is not None else ''
    return ('<section class="bbsView"><div class="view_head"><h3>' + title + '</h3>' +
            date_meta + '</div><div class="view_body">' + body + '</div></section>')


def response(url, text, status=200):
    return base.Response(status, url, {'content-type': 'text/html; charset=utf-8'}, text.encode())


class ImageNoticeParsingTests(unittest.TestCase):
    def test_real_338_captures_metadata_and_original_image_reference(self):
        item = cy.parse_detail(IMAGE_HTML, IMAGE_URL)
        self.assertTrue(item['_parse_valid'])
        self.assertEqual(item['title'], '2026 삼척시청소년수련관 상반기 교육문화 프로그램 참가자 모집')
        self.assertEqual(item['published_date'], '2026-02-13')
        self.assertEqual(item['body_content_type'], 'image_only')
        self.assertEqual(item['body_image_urls'], [
            'https://youth.samcheok.go.kr/youth/bbsDown/editor_up/2026021321127312915.jpg'])
        self.assertEqual(len(item['attachment_urls']), 2)
        self.assertFalse(item['body_images_downloaded'])
        self.assertFalse(item['image_content_extracted'])
        self.assertIn('이미지 내용 미추출', item['_parse_note'])

    def test_image_metadata_never_becomes_verified_program_fields(self):
        item = cy.parse_detail(IMAGE_HTML, IMAGE_URL)
        for key in ['target', 'event_datetime', 'application_period_text', 'application_start',
                    'application_end', 'location']:
            self.assertIsNone(item[key], key)
        self.assertEqual(item['review_state'], 'needs_review')
        self.assertEqual(item['notice_type'], 'review')
        self.assertFalse(item['availability_verified'])
        self.assertEqual(item['date_resolution'], 'needs_review')
        self.assertIn('내용 자동 추출 미실행', item['review_reason'])
        self.assertIsNone(cy.compute_status(item['application_start'], item['application_end'], '2026-10-01'))

    def test_image_requires_a_valid_publication_date(self):
        for published in [None, '', '2026-02-30', '확인 필요']:
            with self.subTest(published=published):
                html = notice('<img src="' + UPLOAD + '">', published=published)
                self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_image_requires_a_notice_heading(self):
        html = notice('<img src="' + UPLOAD + '">', title='')
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_publication_outside_the_notice_heading_cannot_verify_image_notice(self):
        html = notice('<img src="' + UPLOAD + '">', published=None)
        html += '<footer>작성일:<span>2026-02-13</span></footer>'
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_image_outside_notice_body_does_not_rescue_empty_body(self):
        html = notice('') + '<img src="' + UPLOAD + '">'
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_navigation_heading_and_image_do_not_rescue_empty_body(self):
        html = '<nav><h3>메뉴</h3><img src="' + UPLOAD + '"></nav>' + notice('')
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_decorative_or_foreign_images_are_inconclusive(self):
        for src in ['poster.png', '/youth/images/logo.jpg', '/youth/bbsDown/editor_up/icon.svg',
                    'https://other.example/youth/bbsDown/editor_up/poster.jpg',
                    'http://youth.samcheok.go.kr/youth/bbsDown/editor_up/poster.jpg',
                    'data:image/png;base64,AAAA', 'javascript:alert(1)']:
            with self.subTest(src=src):
                item = cy.parse_detail(notice('<img src="' + src + '">'), IMAGE_URL)
                self.assertFalse(item['_parse_valid'])
                self.assertEqual(item['body_image_urls'], [])

    def test_template_image_is_not_visible_notice_content(self):
        html = notice('<template><img src="' + UPLOAD + '"></template>')
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])

    def test_hidden_image_is_not_visible_notice_content(self):
        for body in ['<img hidden src="' + UPLOAD + '">',
                     '<img style="display:none" src="' + UPLOAD + '">',
                     '<div style="visibility: hidden"><img src="' + UPLOAD + '"></div>']:
            with self.subTest(body=body):
                self.assertFalse(cy.parse_detail(notice(body), IMAGE_URL)['_parse_valid'])

    def test_attachment_only_and_empty_body_are_still_invalid(self):
        html = notice('')
        html = html.replace('</div><div class="view_body">',
                            '<ul class="file_wrap"><li><a href="notice.hwp">첨부</a></li></ul>'
                            '</div><div class="view_body">')
        self.assertFalse(cy.parse_detail(html, IMAGE_URL)['_parse_valid'])
        self.assertFalse(cy.parse_detail(notice(''), IMAGE_URL)['_parse_valid'])

    def test_text_notice_keeps_existing_date_and_target_extraction(self):
        item = cy.parse_detail(TEXT_HTML, IMAGE_URL)
        self.assertTrue(item['_parse_valid'])
        self.assertEqual(item['body_content_type'], 'text')
        self.assertEqual(item['target'], '삼척시 청소년 누구나')
        self.assertEqual(item['application_start'], '2026-10-01')
        self.assertEqual(item['review_state'], 'pending')
        self.assertFalse(item['availability_verified'])


class ImageNoticePipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'programs.json'
        self.old_success = '2026-09-30T08:50:00+09:00'
        self.path.write_text(json.dumps({
            'institution': cy.TARGET_NAME, 'last_success_at': self.old_success,
            'programs': [{'id': 'OLD', 'title': '이전 공고', 'target': '이전 대상'}],
        }), encoding='utf-8')

    def tearDown(self):
        self.directory.cleanup()

    def run_collection(self, items):
        list_html = ''.join('<a href="' + url + '">공고</a>' for url in items)
        class FakeClient:
            def fetch(self, url):
                return response(url, list_html) if url == LIST_URL else items[url]
        with patch.object(cy, 'DATA_PATH', self.path), patch.object(base, 'Client', FakeClient):
            code = cy.main()
        return code, json.loads(self.path.read_text(encoding='utf-8'))

    def test_image_only_capture_updates_metadata_success_but_keeps_review(self):
        code, result = self.run_collection({IMAGE_URL: response(IMAGE_URL, IMAGE_HTML)})
        self.assertEqual(code, 0)
        records = {p['id']: p for p in result['programs']}
        self.assertEqual(set(records), {'OLD', IMAGE_ID})
        item = records[IMAGE_ID]
        self.assertEqual(item['review_state'], 'needs_review')
        self.assertIsNone(item['status'])
        self.assertIsNone(item['application_start'])
        self.assertFalse(item['stale'])
        self.assertEqual(item['source_status'], 'current')
        self.assertEqual(item['last_success_at'], result['last_attempt_at'])
        self.assertEqual(result['last_success_at'], result['last_attempt_at'])
        self.assertEqual(result['review_state'], 'needs_review')
        self.assertEqual(result['quality']['successful_details'], 1)
        self.assertEqual(result['quality']['image_only_details'], 1)
        self.assertEqual(result['quality']['text_details'], 0)
        self.assertEqual(result['quality']['preserved_records'], 1)
        self.assertEqual(result['detail_failures'], [])
        self.assertIn('본문 텍스트 0건·이미지 본문 1건', result['status'])
        self.assertIn('이미지 내용 미추출·검토 필요', result['status'])

    def test_mixed_text_and_image_counts_are_explicit(self):
        text_url = LIST_URL + '?bbsData=TEXT&mode=view'
        code, result = self.run_collection({IMAGE_URL: response(IMAGE_URL, IMAGE_HTML),
                                             text_url: response(text_url, TEXT_HTML)})
        self.assertEqual(code, 0)
        self.assertEqual(result['quality']['successful_details'], 2)
        self.assertEqual(result['quality']['text_details'], 1)
        self.assertEqual(result['quality']['image_only_details'], 1)
        self.assertIn('공고 2건 저장', result['status'])
        self.assertEqual(result['review_state'], 'needs_review')

    def test_real_failed_detail_still_preserves_old_metadata_success_time(self):
        bad_url = LIST_URL + '?bbsData=OLD&mode=view'
        code, result = self.run_collection({IMAGE_URL: response(IMAGE_URL, IMAGE_HTML),
                                             bad_url: response(bad_url, notice(''))})
        self.assertEqual(code, 0)
        records = {p['id']: p for p in result['programs']}
        self.assertEqual(records['OLD']['title'], '이전 공고')
        self.assertEqual(records['OLD']['target'], '이전 대상')
        self.assertEqual(records['OLD']['source_status'], 'detail_failed')
        self.assertEqual(result['last_success_at'], self.old_success)
        self.assertEqual(result['quality']['successful_details'], 1)
        self.assertEqual(result['quality']['failed_details'], 1)
        self.assertIn('부분 실패 1건 — 이전 자료 보존', result['status'])


if __name__ == '__main__':
    unittest.main()
