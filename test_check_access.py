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
    def test_badge_and_viewcount_not_mistaken_for_title_change(self):
        # 삼척관광문화재단 실제 목록 행 구조를 그대로 재현: [공지] 배지, 제목, 날짜,
        # 작성자, 조회수가 전부 같은 <a> 안에 서로 다른 태그로 들어있다. 조회수만
        # 올라가도 '제목 변경'으로 오인하면 안 된다.
        def row(hit):
            return (f'<a href="?idx=1035"><span>[공지]</span>'
                    f'<span>「2026 삼척 관광동행 포럼」 개최 안내</span>'
                    f'<span>2026-09-23</span><span>삼척관광문화재단</span>'
                    f'<span>조회수 : {hit}</span></a>')
        first = self.check(html(row(127)))
        self.assertEqual(first['changes']['status'], '기준 생성')
        bumped = self.check(html(row(128)))
        self.assertEqual(bumped['changes']['items'], [])
    def test_title_keeps_its_own_leading_bracket(self):
        # [공지] 같은 사이트 배지는 걸러내되, 작성자가 제목에 직접 넣은
        # "[삼척 투어패스]" 같은 대괄호는 제목의 일부로 남아야 한다.
        page = c.Page('<a href="?idx=974"><span>[공지]</span>'
                       '<span>[삼척 투어패스] 티켓 한 장으로 끝내는 여행</span></a>')
        self.assertEqual(page.links[0][2], '[삼척 투어패스] 티켓 한 장으로 끝내는 여행')
    def test_inline_markup_keeps_whole_title(self):
        page = c.Page('<a href="?idx=1">2026 <strong>청소년</strong> 여름방학 프로그램 모집</a>')
        self.assertEqual(page.links[0][2], '2026 청소년 여름방학 프로그램 모집')
    def test_inline_markup_suffix_change_is_detected(self):
        self.check(html('<a href="?idx=1">2026 <strong>청소년</strong> 모집</a>'))
        changed = self.check(html('<a href="?idx=1">2026 <strong>청소년</strong> 모집 연장</a>'))
        self.assertEqual(changed['changes']['items'][0]['title'], '2026 청소년 모집 연장')
    def test_youth_heading_excludes_row_metadata(self):
        # 실제 청소년수련관 목록은 div.row 안의 h4가 제목이고,
        # p.rowInfo가 작성자/날짜/조회수이다. inline emphasis도 유지한다.
        def row(hit):
            return ('<a href="?idx=350"><div class="row"><h4>'
                    '2026년 삼척시청소년어울림마당 [우리 함께 <em>놀토데이</em>] 참가 모집'
                    '</h4><p class="rowInfo"><span>청소년수련관</span>'
                    f'<span>2026-09-22</span><span>{hit}</span></p></div></a>')
        first = self.check(html(row(173)))
        self.assertEqual(c.Page(row(173)).links[0][2],
                         '2026년 삼척시청소년어울림마당 [우리 함께 놀토데이] 참가 모집')
        self.assertEqual(self.check(html(row(174)))['changes']['items'], [])
    def test_stcf_title_excludes_summary_badge_and_metadata(self):
        # 실제 관광문화재단 목록은 wrap1texts 안의 strong.t1이 제목,
        # span.t2가 본문 요약이고 em.em2가 [공지] 배지이다.
        def row(hit):
            return ('<a href="?idx=1035"><span class="wrap1texts">'
                    '<strong class="t1"><em class="em2">[공지]</em>'
                    '「2026 삼척 <span>관광동행</span> 포럼」 개최 안내</strong>'
                    '<span class="t2">본문 요약과 행사일시</span>'
                    '<i class="wrap1t3"><span class="t3">2026-09-23</span>'
                    f'<span class="t3">삼척관광문화재단</span><span>조회수 : {hit}</span>'
                    '</i></span></a>')
        self.assertEqual(c.Page(row(127)).links[0][2], '「2026 삼척 관광동행 포럼」 개최 안내')
        self.check(html(row(127)))
        self.assertEqual(self.check(html(row(128)))['changes']['items'], [])
    def test_title_container_keeps_entities_and_inline_word_boundaries(self):
        page = c.Page('<a href="?idx=1"><span class="new">N</span>'
                      '<span class="subject">공연 &amp; 체험 <b>신청</b>서</span>'
                      '<span class="date">2026-09-30</span></a>')
        self.assertEqual(page.links[0][2], '공연 & 체험 신청서')
    def test_category_bracket_inside_title_is_not_badge(self):
        page = c.Page('<a href="?idx=1"><span>[공지]</span>'
                      '<span class="title"><span>[청소년]</span> 프로그램 모집</span></a>')
        self.assertEqual(page.links[0][2], '[청소년] 프로그램 모집')
    def test_new_state_on_title_container_does_not_hide_title(self):
        page = c.Page('<a href="?idx=1"><span class="subject new">'
                      '청소년 <b>공연</b> 모집<span class="new">N</span></span></a>')
        self.assertEqual(page.links[0][2], '청소년 공연 모집')
    def test_leading_date_metadata_does_not_hide_title(self):
        page = c.Page('<a href="?idx=1">\n<span>2026-09-30</span>'
                      '<span>행사 공고</span></a>')
        self.assertEqual(page.links[0][2], '행사 공고')
    def test_robots_most_specific_allow_overrides_broad_disallow(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Disallow: /', 'Allow: /public/'])
        self.assertTrue(parser.can_fetch(c.USER_AGENT, 'https://example.test/public/notice'))
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/private/notice'))
    def test_robots_most_specific_disallow_overrides_broad_allow(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Allow: /public/', 'Disallow: /public/private/'])
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/public/private/notice'))
    def test_robots_wildcard_and_terminal_marker(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Disallow: /*.pdf$'])
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/files/doc.pdf'))
        self.assertTrue(parser.can_fetch(c.USER_AGENT, 'https://example.test/files/doc.pdf?preview=1'))
    def test_robots_equal_specificity_allow_wins(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Disallow: /public/', 'Allow: /public/'])
        self.assertTrue(parser.can_fetch(c.USER_AGENT, 'https://example.test/public/notice'))
    def test_robots_matching_agent_groups_are_combined(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: SamcheokKiwoomAccessCheck', 'Disallow: /private/',
                      'User-agent: OtherBot', 'Disallow: /',
                      'User-agent: samcheokkiwoomaccesscheck', 'Disallow: /internal/',
                      'User-agent: *', 'Disallow: /'])
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/private/x'))
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/internal/x'))
        self.assertTrue(parser.can_fetch(c.USER_AGENT, 'https://example.test/public/x'))
    def test_robots_percent_encoding_and_query_match(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Disallow: /caf%C3%A9/', 'Disallow: /~private/',
                      'Disallow: /*?download='])
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/café/file'))
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/%7eprivate/file'))
        self.assertFalse(parser.can_fetch(c.USER_AGENT, 'https://example.test/file?download=1'))
    def test_robots_most_restrictive_delay_and_rate_are_kept(self):
        parser = c.RobotsRules()
        parser.parse(['User-agent: *', 'Crawl-delay: 2', 'Request-rate: 1/4',
                      'User-agent: *', 'Crawl-delay: 3.5', 'Request-rate: 2/20'])
        self.assertEqual(parser.crawl_delay(c.USER_AGENT), 3.5)
        self.assertEqual(parser.request_rate(c.USER_AGENT), c.urllib.robotparser.RequestRate(2, 20))
    def test_js_popup_link_resolves_real_detail_url(self):
        # 강원교육지원청처럼 href가 'javascript:'뿐이고 실제 게시글 번호는
        # onclick 안에만 있는 게시판 — onclick에서 번호를 읽어 상세주소를 조립한다.
        target = dict(self.target, item_link_pattern=r'boardSeq=(\d+)',
                      js_view={'onclick_pattern': r"goView\('\d+',\s*'(\d+)'",
                               'url_template': 'https://sce.gwe.go.kr/boardCnts/view.do?boardSeq={seq}'})
        page = c.Page('<a href="javascript:" onclick="javascript:goView(\'1595\',\'7715223\', \'0\')">'
                       '공유재산(폐교) 매각 계획 공고</a>')
        found = c.find_items(page, target, target['list_url'])
        self.assertEqual(found['7715223']['url'], 'https://sce.gwe.go.kr/boardCnts/view.do?boardSeq=7715223')
        self.assertEqual(found['7715223']['title'], '공유재산(폐교) 매각 계획 공고')
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
