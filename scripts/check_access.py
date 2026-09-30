#!/usr/bin/env python3
"""Access and extraction probe, not a complete event crawler. Python 3.11+."""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config' / 'targets.json'
STATE_DIR = ROOT / 'state'
RESULTS_DIR = ROOT / 'results'
USER_AGENT = 'SamcheokKiwoomAccessCheck/1.1'
TIMEOUT_SECONDS = 15
MAX_BYTES = 8 * 1024 * 1024
MIN_REQUEST_INTERVAL = 1.5
SUCCESS, UNKNOWN, FAILURE, UNTESTED = '성공', '확인필요', '실패', '미시험'
BLOCK_MARKERS = ('웹방화벽', 'web application firewall', '차단되었습니다',
                 '접근이 거부', '접근이 차단', 'access denied', '비정상적인 접근')


@dataclass
class Response:
    status: int | None
    url: str
    headers: dict = field(default_factory=dict)
    raw: bytes = b''
    error: str | None = None
    policy: str | None = None


def decode(resp):
    match = re.search(r"charset=[\"']?([\w-]+)", resp.headers.get('content-type', ''), re.I)
    for encoding in ([match.group(1)] if match else []) + ['utf-8-sig', 'cp949', 'euc-kr']:
        try:
            return resp.raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return resp.raw.decode('utf-8', errors='replace')


class Page(HTMLParser):
    """Extract text and plain HTML links. JS-generated links are untested."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.parts, self.hidden = [], [], []
        self.anchor = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.hidden.append(tag)
            return
        if self.hidden:
            return
        if tag == 'a' and values.get('href'):
            self.anchor = [values['href'], []]
        if tag == 'br':
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                del self.hidden[self.hidden.index(tag):]
            return
        if tag == 'a' and self.anchor:
            self.links.append((self.anchor[0], ' '.join(self.anchor[1]).strip()))
            self.anchor = None
        self.parts.append(' ')

    def handle_data(self, value):
        if not self.hidden and value.strip():
            self.parts.append(value.strip())
            if self.anchor:
                self.anchor[1].append(value.strip())

    @property
    def text(self):
        return ' '.join(' '.join(self.parts).split())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())
        self.robots_cache, self.last_request = {}, 0.0

    def raw_get(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username:
            return Response(None, url, error='지원하지 않는 URL')
        time.sleep(max(0.0, MIN_REQUEST_INTERVAL - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                      'Accept': '*/*', 'Accept-Language': 'ko-KR,ko;q=0.9'})
        try:
            try:
                resp = self.opener.open(req, timeout=TIMEOUT_SECONDS)
            except urllib.error.HTTPError as exc:
                resp = exc
            with resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    return Response(resp.code, url, headers, error='응답 크기 제한 초과(8 MiB)')
                return Response(resp.code, url, headers, raw)
        except Exception as exc:
            return Response(None, url, error=f'{type(exc).__name__}: {exc}')

    def robots_policy(self, url):
        parsed = urllib.parse.urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self.robots_cache:
            robots_url = origin + '/robots.txt'
            resp = self.raw_get(robots_url)
            for _ in range(3):
                if resp.status not in {301, 302, 303, 307, 308}:
                    break
                location = resp.headers.get('location')
                if not location:
                    break
                robots_url = urllib.parse.urljoin(robots_url, location)
                resp = self.raw_get(robots_url)
            text = decode(resp)
            if resp.error:
                entry = (None, 'robots.txt 확인 실패: ' + resp.error)
            elif resp.status in {404, 410}:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
                entry = (parser, 'robots.txt 없음(404/410)')
            elif (resp.status == 200 and '<html' not in text.lower() and '<!doctype' not in text.lower()
                  and (not any(line.strip() and not line.lstrip().startswith('#') for line in text.splitlines())
                       or re.search(r'^\s*(?:User-agent|Allow|Disallow|Sitemap|Host|Crawl-delay|Request-rate)\s*:', text, re.I | re.M))):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
                entry = (parser, 'robots.txt 확인')
            else:
                entry = (None, f'robots.txt 규칙 확인 불가(HTTP {resp.status})')
            self.robots_cache[origin] = entry
        parser, note = self.robots_cache[origin]
        if parser is None:
            return False, '보류: ' + note
        if not parser.can_fetch(USER_AGENT, url):
            return False, '제한: robots.txt에서 이 경로의 자동 조회 제한'
        delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*')
        rate = parser.request_rate(USER_AGENT) or parser.request_rate('*')
        interval = max(MIN_REQUEST_INTERVAL, float(delay or 0),
                       rate.seconds / rate.requests if rate and rate.requests else 0)
        time.sleep(max(0.0, interval - (time.monotonic() - self.last_request)))
        return True, note

    def fetch(self, url):
        for _ in range(6):
            allowed, note = self.robots_policy(url)
            if not allowed:
                return Response(None, url, policy=note)
            resp = self.raw_get(url)
            if resp.status not in {301, 302, 303, 307, 308}:
                return resp
            location = resp.headers.get('location')
            if not location:
                return Response(resp.status, url, resp.headers, resp.raw, '리디렉션 목적지 없음')
            url = urllib.parse.urljoin(url, location)
        return Response(None, url, error='리디렉션 횟수 제한 초과')


def base_verdict(resp):
    if resp.policy:
        return UNKNOWN, resp.policy
    if resp.error:
        return FAILURE, resp.error
    if resp.status is None or not 200 <= resp.status < 300:
        return FAILURE, f'HTTP {resp.status}'
    if not resp.raw:
        return FAILURE, '빈 응답'
    return SUCCESS, 'HTTP 응답 수신'


def html_page(resp):
    verdict, note = base_verdict(resp)
    if verdict != SUCCESS:
        return verdict, note, None
    ctype = resp.headers.get('content-type', '').lower()
    if 'html' not in ctype and '<' not in decode(resp)[:200]:
        return UNKNOWN, 'HTML 페이지 여부 확인 필요', None
    page = Page(decode(resp))
    if not page.text:
        return UNKNOWN, '본문 텍스트 없음(JS/이미지 페이지 가능)', page
    hit = next((m for m in BLOCK_MARKERS if m in page.text.lower()), None)
    if hit:
        return UNKNOWN, f'본문에 차단 관련 문구({hit}); 원문 확인 필요', page
    return SUCCESS, '페이지 응답 확인', page


def metadata(resp, verdict, note):
    return {'verdict': verdict, 'note': note, 'status_code': resp.status,
            'final_url': resp.url, 'bytes': len(resp.raw),
            'content_type': resp.headers.get('content-type', ''), 'error': resp.error}


def find_items(page, target, url):
    pattern = re.compile(target['item_link_pattern'])
    found = {}
    for href, title in page.links:
        match = pattern.search(href)
        if match and title:
            full_url = urllib.parse.urljoin(url, href)
            if urllib.parse.urlsplit(full_url).scheme not in {'http', 'https'}:
                continue
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(full_url).query)
            if any(value not in query.get(key, [])
                   for key, value in target.get('item_required_query', {}).items()):
                continue
            found[match.group(1)] = {'title': title, 'url': full_url}
    return found


def snapshot_path(name):
    return STATE_DIR / (hashlib.sha256(name.encode()).hexdigest()[:16] + '.json')


def update_snapshot(name, found):
    path = snapshot_path(name)
    if not found:
        return {'status': '미검증', 'items': [], 'note': '공고 0건: 이전 기준 유지'}
    exists = path.exists()
    previous = json.loads(path.read_text(encoding='utf-8')) if exists else {}
    changes = []
    if exists:
        for key, item in found.items():
            if key not in previous:
                changes.append({'id': key, 'status': '신규', **item})
            elif previous[key]['title'] != item['title']:
                changes.append({'id': key, 'status': '제목 변경', **item,
                                'previous_title': previous[key]['title']})
    previous.update(found)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return {'status': '비교 완료' if exists else '기준 생성', 'items': changes,
            'note': '제목 변경만 감지; 본문/일정/첨부 수정과 누락 여부는 미검증'}


def attachment_verdict(resp):
    verdict, note = base_verdict(resp)
    if verdict != SUCCESS:
        return verdict, note
    ctype = resp.headers.get('content-type', '').lower()
    sample = resp.raw.lstrip()[:100].lower()
    if 'html' in ctype or sample.startswith((b'<', b'{', b'[')):
        return FAILURE, '파일 대신 HTML/JSON/XML 안내 응답'
    signatures = ((b'%PDF-', 'PDF'), (b'\x89PNG\r\n\x1a\n', 'PNG'),
                  (b'\xff\xd8\xff', 'JPEG'), (b'PK\x03\x04', 'ZIP/HWPX/Office'),
                  (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', 'OLE/HWP/Office'),
                  (b'GIF87a', 'GIF'), (b'GIF89a', 'GIF'))
    for signature, kind in signatures:
        if resp.raw.startswith(signature) and len(resp.raw) > len(signature):
            return SUCCESS, f'{kind} 서명과 비어 있지 않은 바이트 확인; 파일 유효성/내용 추출은 미시험'
    return UNKNOWN, '파일 서명 확인 필요(다운로드 가능성만 확인)'


def check_target(target, client):
    name = target['name']
    result = {'name': name, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'environment': 'GitHub Actions' if os.getenv('GITHUB_ACTIONS') else '현재 실행 환경',
              'list_url': target['list_url'], 'changes': {'status': '미검증', 'items': []}}
    resp = client.fetch(target['list_url'])
    verdict, note, page = html_page(resp)
    if verdict == SUCCESS:
        try:
            found = find_items(page, target, resp.url)
            if target.get('list_kind') == 'homepage':
                verdict, note = UNKNOWN, f'메인 화면만 시험; 발견 링크 {len(found)}건으로 목록 전체 미검증'
            elif not found:
                verdict, note = UNKNOWN, '공고 링크 0건: 주소/패턴/JS 여부 확인 필요; 이전 기준 유지'
            else:
                note = f'공고 링크 {len(found)}건 추출(페이지네이션/누락 여부 미검증)'
                result['changes'] = update_snapshot(name, found)
            result['list_items_found'] = len(found)
        except Exception as exc:
            verdict, note = UNKNOWN, f'목록 파싱/기준 저장 오류: {type(exc).__name__}: {exc}'
    result['list'] = metadata(resp, verdict, note)
    result['detail'] = {'verdict': UNTESTED, 'note': '샘플 URL 없음'}
    result['attachment'] = {'verdict': UNTESTED, 'note': '첨부 URL 없음'}
    if target.get('sample_detail_url'):
        detail = client.fetch(target['sample_detail_url'])
        dv, dn, dp = html_page(detail)
        if dv == SUCCESS:
            keyword = target.get('expected_keyword')
            if target.get('sample_detail_verified') is False:
                dv, dn = UNKNOWN, '추정된 상세 URL: 실제 공고 확인 필요'
            elif not keyword:
                dv, dn = UNKNOWN, '대조할 본문 문구 없음: 실제 공고 여부 미검증'
            elif ' '.join(keyword.split()) not in dp.text:
                dv, dn = UNKNOWN, '기대 문구 불일치: 잘못된 URL/이미지 본문/일정 변경 등 확인 필요'
            else:
                dn = f'샘플 본문의 기대 문구 확인({keyword}); 날짜/대상 필드 추출은 미시험'
        result['detail'] = metadata(detail, dv, dn)
    if target.get('attachment_url'):
        attachment = client.fetch(target['attachment_url'])
        av, an = attachment_verdict(attachment)
        result['attachment'] = metadata(attachment, av, an)
    return result


def safe_cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def render_markdown(results):
    now = datetime.now(timezone(timedelta(hours=9))).isoformat()
    lines = [f'# 삼척 키움지도 — 접근·추출 시험 ({now}, KST)', '',
             '실행 완료와 사이트 수집 성공은 다릅니다. 성공은 해당 칸의 제한된 시험만 통과했다는 뜻입니다.',
             '본문의 일정 변경, 모든 공고의 누락 여부, 첨부 내용 추출은 이 도구로 검증하지 않습니다.', '',
             '| 기관 | 목록 | 상세 샘플 | 첨부 다운로드 | 신규·제목 변경 |',
             '|---|---|---|---|---|']
    for result in results:
        change = result.get('changes', {'status': '미검증', 'items': []})
        cv = change['status']
        if cv == '비교 완료':
            cv += f"({len(change['items'])}건)"
        values = [result['name']] + [result[k]['verdict'] for k in ('list', 'detail', 'attachment')] + [cv]
        lines.append('| ' + ' | '.join(map(safe_cell, values)) + ' |')
    for result in results:
        lines += ['', '## ' + safe_cell(result['name'])]
        for key, label in [('list', '목록'), ('detail', '상세'), ('attachment', '첨부')]:
            item = result[key]
            lines.append(f"- {label}: {item['verdict']} — {safe_cell(item['note'])}")
            if item.get('final_url'):
                lines.append(f"  - 최종 URL: {safe_cell(item['final_url'])}")
            if 'bytes' in item:
                lines.append(f"  - HTTP {item.get('status_code')}, {item['bytes']} bytes")
        for item in result.get('changes', {}).get('items', []):
            lines.append(f"- {item['status']}: {safe_cell(item['title'])} ({safe_cell(item['url'])})")
    lines += ['', '## 해석', '',
              '- 확인필요: 규칙 확인 보류, 추정 URL, 링크 0건, 이미지/JS 등 원인 확인이 필요합니다. 자동화 불가능의 확정이 아닙니다.',
              '- 미시험: 샘플을 설정하지 않아 확인하지 않았습니다. 실패나 첨부가 없다는 뜻이 아닙니다.',
              '- 첫 정상 목록은 기준 생성입니다. 이후부터 신규·제목 변경을 비교합니다.',
              '- robots.txt 확인 실패/제한 시 내용 조회를 보류합니다. robots 허용은 이용약관/자료 이용 허락을 대신하지 않습니다.']
    return '\n'.join(lines) + '\n'


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    results, client = [], Client()
    for target in config['targets']:
        print(f"[확인 중] {target['name']}", file=sys.stderr)
        try:
            result = check_target(target, client)
        except Exception as exc:
            note = f'기관 처리 오류: {type(exc).__name__}: {exc}'
            result = {'name': target['name'], 'checked_at': datetime.now(timezone.utc).isoformat(),
                      'list': {'verdict': UNKNOWN, 'note': note},
                      'detail': {'verdict': UNTESTED, 'note': '기관 처리 오류로 미시험'},
                      'attachment': {'verdict': UNTESTED, 'note': '기관 처리 오류로 미시험'},
                      'changes': {'status': '미검증', 'items': []}}
        results.append(result)
    (RESULTS_DIR / 'latest.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    with (RESULTS_DIR / 'history.jsonl').open('a', encoding='utf-8') as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
    (RESULTS_DIR / 'latest.md').write_text(render_markdown(results), encoding='utf-8')
    print('결과: results/latest.md (사이트 판정은 표의 각 칸을 확인하세요)', file=sys.stderr)


if __name__ == '__main__':
    main()
