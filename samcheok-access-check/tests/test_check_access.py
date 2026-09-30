import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('checker', ROOT / 'scripts/check_access.py')
c = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = c
spec.loader.exec_module(c)


def html(text, url='https://example.test/list'):
    return c.Response(200, url, {'content-type': 'text/html; charset=utf-8'}, text.encode())


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
    def fetch(self, url):
        return self.responses[url]


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_state = c.STATE_DIR
        c.STATE_DIR = Path(self.temp.name)
        self.target = {'name': 'test', 'list_url': 'https://example.test/list',
                       'item_link_pattern': r'idx=(\d+)'}
    def tearDown(self):
        c.STATE_DIR = self.old_state
        self.temp.cleanup()
    def check(self, response, **overrides):
        target = dict(self.target, **overrides)
        responses = {target['list_url']: response}
        if target.get('sample_detail_url'):
            responses[target['sample_detail_url']] = html('<p>샘플 본문</p>')
        return c.check_target(target, FakeClient(responses))
    def test_script_marker_not_a_block_page(self):
        response = html('<script src="captcha.js"></script><a href="?idx=1">공고</a>')
        self.assertEqual(self.check(response)['list']['verdict'], c.SUCCESS)
    def test_empty_html_fails_without_exception(self):
        response = c.Response(204, self.target['list_url'])
        self.assertEqual(self.check(response)['list']['verdict'], c.FAILURE)
    def test_empty_attachment_not_success(self):
        response = c.Response(200, 'https://example.test/file', {'content-type': 'application/pdf'})
        self.assertEqual(c.attachment_verdict(response)[0], c.FAILURE)
    def test_json_attachment_not_success(self):
        response = c.Response(200, 'https://example.test/file', {'content-type': 'application/json'}, b'{"error":"denied"}')
        self.assertEqual(c.attachment_verdict(response)[0], c.FAILURE)
    def test_pdf_signature_scope(self):
        response = c.Response(200, 'https://example.test/file', {'content-type': 'application/pdf'}, b'%PDF-1.7\nfixture')
        verdict, note = c.attachment_verdict(response)
        self.assertEqual(verdict, c.SUCCESS)
        self.assertIn('내용 추출은 미시험', note)
    def test_zero_items_keeps_previous_snapshot(self):
        self.check(html('<a href="?idx=1">공고</a>'))
        before = c.snapshot_path('test').read_bytes()
        result = self.check(html('<h1>로그인</h1><p>아이디를 입력하세요</p>'))
        self.assertEqual(result['list']['verdict'], c.UNKNOWN)
        self.assertEqual(c.snapshot_path('test').read_bytes(), before)
        self.assertEqual(result['changes']['status'], '미검증')
    def test_first_run_is_baseline_then_title_change(self):
        first = self.check(html('<a href="?idx=1">공고</a>'))
        self.assertEqual(first['changes']['status'], '기준 생성')
        self.assertEqual(first['changes']['items'], [])
        changed = self.check(html('<a href="?idx=1">수정 공고</a>'))
        self.assertEqual(changed['changes']['items'][0]['status'], '제목 변경')
    def test_reappearing_id_is_not_new(self):
        self.check(html('<a href="?idx=1">하나</a><a href="?idx=2">둘</a>'))
        self.check(html('<a href="?idx=2">둘</a>'))
        result = self.check(html('<a href="?idx=1">하나</a><a href="?idx=2">둘</a>'))
        self.assertEqual(result['changes']['items'], [])
    def test_unverified_detail_not_success(self):
        result = self.check(html('<a href="?idx=1">공고</a>'), sample_detail_url='https://example.test/detail', sample_detail_verified=False)
        self.assertEqual(result['detail']['verdict'], c.UNKNOWN)
    def test_missing_detail_keyword_not_success(self):
        result = self.check(html('<a href="?idx=1">공고</a>'), sample_detail_url='https://example.test/detail')
        self.assertEqual(result['detail']['verdict'], c.UNKNOWN)
    def test_visible_block_message_inconclusive(self):
        result = self.check(html('<h1>웹방화벽 요청이 차단되었습니다</h1>'))
        self.assertEqual(result['list']['verdict'], c.UNKNOWN)
    def test_homepage_not_proven_board(self):
        result = self.check(html('<a href="?idx=1">공고</a>'), list_kind='homepage')
        self.assertEqual(result['list']['verdict'], c.UNKNOWN)
    def test_robots_unknown_does_not_fetch_content(self):
        client = c.Client()
        with patch.object(client, 'raw_get', return_value=c.Response(None, 'https://example.test/robots.txt', error='network unavailable')) as get:
            response = client.fetch('https://example.test/list')
            self.assertTrue(response.policy.startswith('보류:'))
            self.assertEqual(get.call_count, 1)
    def test_robots_disallow_does_not_fetch_content(self):
        client = c.Client()
        robots = c.Response(200, 'https://example.test/robots.txt', {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /\n')
        with patch.object(client, 'raw_get', return_value=robots) as get:
            response = client.fetch('https://example.test/list')
            self.assertTrue(response.policy.startswith('제한:'))
            self.assertEqual(get.call_count, 1)
    def test_youth_encoded_link_is_found(self):
        config = json.loads((ROOT / 'config/targets.json').read_text())
        target = config['targets'][2]
        page = c.Page('<a href="Notice.php?bbsData=bm89MzUw%7C%7C&amp;mode=view">공고</a>')
        self.assertEqual(len(c.find_items(page, target, target['list_url'])), 1)
    def test_youth_navigation_not_a_notice(self):
        config = json.loads((ROOT / 'config/targets.json').read_text())
        target = config['targets'][2]
        page = c.Page('<a href="Notice.php?bbsData=bm89MzUw%7C%7C&amp;mode=list">다음</a>')
        self.assertEqual(c.find_items(page, target, target['list_url']), {})
    def test_robot_plain_error_is_not_permission(self):
        client = c.Client()
        robots = c.Response(200, 'https://example.test/robots.txt', {'content-type':'text/plain'}, b'Access Denied')
        with patch.object(client, 'raw_get', return_value=robots) as get:
            response = client.fetch('https://example.test/list')
            self.assertTrue(response.policy.startswith('보류:'))
            self.assertEqual(get.call_count, 1)
    def test_expected_detail_phrase_in_visible_text(self):
        result = self.check(html('<a href="?idx=1">공고</a>'), sample_detail_url='https://example.test/detail', expected_keyword='샘플 본문')
        self.assertEqual(result['detail']['verdict'], c.SUCCESS)
    def test_no_attachments_claimed_from_missing_config(self):
        result = self.check(html('<a href="?idx=1">공고</a>'))
        self.assertEqual(result['attachment']['verdict'], c.UNTESTED)
    def test_main_continues_after_one_target_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / 'targets.json'
            config.write_text(json.dumps({'targets': [{'name':'bad'}, {'name':'good'}]}))
            ok = {'name': 'good', 'list': {'verdict': c.SUCCESS, 'note':'fixture'}, 'detail': {'verdict':c.UNTESTED, 'note':'fixture'}, 'attachment':{'verdict':c.UNTESTED,'note':'fixture'}, 'changes':{'status':'미검증','items':[]}}
            with patch.object(c, 'CONFIG_PATH', config), patch.object(c, 'RESULTS_DIR', base/'results'), patch.object(c, 'check_target', side_effect=[RuntimeError('fixture'), ok]):
                c.main()
            results = json.loads((base/'results/latest.json').read_text())
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]['list']['verdict'], c.UNKNOWN)
            self.assertEqual(results[1]['list']['verdict'], c.SUCCESS)


if __name__ == '__main__':
    unittest.main()
