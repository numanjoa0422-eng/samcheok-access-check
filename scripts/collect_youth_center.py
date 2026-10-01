#!/usr/bin/env python3
"""청소년수련관 공지 첫 페이지 수집. 프로그램 승인·전체 게시판 완전 수집은 미검증."""
import json
import re
import sys
import urllib.parse
from datetime import date, datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import check_access as base

CONFIG_PATH = ROOT / 'config' / 'targets.json'
DATA_PATH = ROOT / 'data' / 'programs.json'
TARGET_NAME = '삼척시청소년수련관_공지사항'
KST = timezone(timedelta(hours=9))
LABELS = {
    '대상': 'target', '행사대상': 'target', '참가대상': 'target', '모집대상': 'target',
    '행사일시': 'event_datetime', '행사 및 모집일시': 'event_datetime',
    '일시': 'event_datetime', '행사일': 'event_datetime',
    '신청기간': 'application_period', '신청날짜': 'application_period',
    '접수기간': 'application_period', '모집기간': 'application_period',
    '장소': 'location', '행사장소': 'location',
}
YEAR_RE = re.compile(r'(?<!\d)(\d{4})\s*(?:년|[./-])')
DATE_META_RE = re.compile(r'작성일\s*[:：]?\s*<span[^>]*>\s*(\d{4}-\d{2}-\d{2})', re.S)
KOREAN_RANGE = re.compile(
    r'(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:~|부터)\s*'
    r'(?:(\d{4})\s*년\s*)?(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일(?:\s*까지)?(?!\d)')
DOTTED_RANGE = re.compile(
    r'(?:(\d{4})\s*[./-]\s*)?(\d{1,2})\s*[./-]\s*(\d{1,2})\.?\s*~\s*'
    r'(?:(\d{4})\s*[./-]\s*)?(?:(\d{1,2})\s*[./-]\s*)?(\d{1,2})\.?(?!\d)')
SINGLE_KOREAN = re.compile(r'(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일(?!\d)')
SINGLE_DOTTED = re.compile(r'(?:(\d{4})\s*[./-]\s*)?(\d{1,2})\s*[./-]\s*(\d{1,2})\.?(?!\d)')
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class NoticePage(HTMLParser):
    """공지 section 안의 제목/문단/첨부만 읽어 네비게이션 h3를 제외한다."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack, self.title_parts, self.chunks, self.chunk = [], [], [], []
        self.body_seen = False
        self.attachments = []
        self.feed(html)
        self.flush()

    def has_class(self, name):
        return any(name in attrs.get('class', '').split() for _, attrs in self.stack)

    def in_body(self):
        return self.has_class('bbsView') and self.has_class('view_body')

    def flush(self):
        value = re.sub(r'\s+', ' ', ''.join(self.chunk)).strip()
        if value:
            self.chunks.append(value)
        self.chunk = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.in_body() and tag in {'p', 'li', 'tr', 'div', 'br'}:
            self.flush()
        if tag not in VOID_TAGS:
            self.stack.append((tag, attrs))
        if self.in_body():
            self.body_seen = True
        if tag == 'a' and self.has_class('bbsView') and self.has_class('file_wrap') and attrs.get('href'):
            self.attachments.append(attrs['href'])

    def handle_endtag(self, tag):
        if self.in_body() and tag in {'p', 'li', 'tr', 'div'}:
            self.flush()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if any(tag in {'script', 'style', 'noscript', 'template'} for tag, _ in self.stack):
            return
        if self.has_class('bbsView') and self.has_class('view_head') and any(tag == 'h3' for tag, _ in self.stack):
            self.title_parts.append(value)
        if self.in_body():
            self.chunk.append(value)

    @property
    def title(self):
        return re.sub(r'\s+', ' ', ''.join(self.title_parts)).strip()


def clean_label(text):
    return re.sub(r'\s+', ' ', re.sub(r'^[\s✔✓○●•\-*]+', '', text)).strip()


def match_field(text):
    return LABELS.get(clean_label(text))


def extract_labeled_fields(chunks):
    fields = {name: [] for name in set(LABELS.values())}
    waiting = None
    for raw in chunks:
        chunk = clean_label(raw)
        if not chunk:
            continue
        colon = re.match(r'^([^:：]{1,30})\s*[:：]\s*(.*)$', chunk)
        if colon:
            # 독립 라벨 뒤의 날짜·시각은 값이다. 10:00의 콜론을 새 라벨로 보지 않는다.
            if waiting and re.match(r'^\d', chunk):
                fields[waiting].append(chunk)
                waiting = None
                continue
            # 알려지지 않은 라벨도 경계로 삼아 문의처 등을 이전 값에 붙이지 않는다.
            waiting = None
            field = match_field(colon.group(1))
            if field and colon.group(2).strip():
                fields[field].append(colon.group(2).strip())
            elif field:
                waiting = field
            continue
        field = match_field(chunk)
        if field:
            waiting = field
            continue
        if waiting:
            # 독립 라벨 바로 다음 문단만 값으로 읽고 후속 설명을 끌어오지 않는다.
            fields[waiting].append(chunk)
            waiting = None
    result = {}
    for field, values in fields.items():
        limit = 500 if field == 'event_datetime' else 300
        value = ' '.join(values).strip()
        result[field] = value if len(value) <= limit else ''
    return result


def parse_date_range(text, year_hint=None):
    """시각·마감일만·복수 기간·역순은 추정하지 않는다. 연도는 공고 근거로만 채운다."""
    if not text or re.search(r'\d{1,2}\s*:\s*\d{2}|\d{1,2}\s*시|오전|오후|정오|자정', text):
        return None, None
    text = re.sub(r'\(\s*[월화수목금토일](?:요일)?\s*\)', '', text)
    text = text.replace('～', '~').replace('〜', '~').replace('–', '~').replace('—', '~')
    # 날짜의 하이픈과 구간 하이픈을 구분하기 위해 두 날짜 사이의 띄운 구간만 정규화.
    text = re.sub(r'\s+-\s+', '~', text)
    matches = list(KOREAN_RANGE.finditer(text)) + list(DOTTED_RANGE.finditer(text))
    if len(matches) == 1:
        match = matches[0]
        y1, m1, d1, y2, m2, d2 = match.groups()
        y1 = int(y1) if y1 else year_hint
        if y1 is None:
            return None, None
        y2 = int(y2) if y2 else y1
        m2 = int(m2) if m2 else int(m1)
        remaining = text[:match.start()] + text[match.end():]
        if SINGLE_KOREAN.search(remaining) or SINGLE_DOTTED.search(remaining):
            return None, None
        try:
            start, end = date(int(y1), int(m1), int(d1)), date(int(y2), m2, int(d2))
            if end < start:
                return None, None
            return start.isoformat(), end.isoformat()
        except (ValueError, TypeError):
            return None, None
    if matches or not re.search(r'하루|당일|1\s*일간', text) or re.search(r'까지|부터|~', text):
        return None, None
    singles = list(SINGLE_KOREAN.finditer(text)) + list(SINGLE_DOTTED.finditer(text))
    if len(singles) != 1:
        return None, None
    year, month, day = singles[0].groups()
    year = int(year) if year else year_hint
    if year is None:
        return None, None
    try:
        value = date(int(year), int(month), int(day)).isoformat()
        return value, value
    except (ValueError, TypeError):
        return None, None


def compute_status(start_iso, end_iso, today_iso):
    try:
        start, end, today = (date.fromisoformat(value) for value in (start_iso, end_iso, today_iso))
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    return '접수예정' if today < start else ('모집중' if today <= end else '마감')


def parse_detail(raw_html, url, written_year=None):
    page = NoticePage(raw_html)
    fields = extract_labeled_fields(page.chunks)
    publication = DATE_META_RE.search(raw_html)
    published_date = None
    if publication:
        try:
            published_date = date.fromisoformat(publication.group(1)).isoformat()
        except ValueError:
            pass
    context_years = {int(y) for y in YEAR_RE.findall(page.title + ' ' + ' '.join(page.chunks))}
    app_text = fields.get('application_period', '')
    app_years = YEAR_RE.findall(app_text)
    year_source = None
    if app_years:
        hint = None
        year_source = 'application_explicit'
    elif len(context_years) == 1:
        hint = next(iter(context_years))
        year_source = 'notice_explicit'
    elif len(context_years) > 1:
        hint = None
        year_source = 'conflicting_notice_years'
    elif published_date:
        hint = date.fromisoformat(published_date).year
        year_source = 'published_date'
    else:
        hint = written_year
        year_source = 'provided_notice_year' if written_year is not None else None
    start, end = parse_date_range(app_text, hint)
    attachments = []
    for href in page.attachments:
        full = urllib.parse.urljoin(url, href)
        if urllib.parse.urlsplit(full).scheme in {'http', 'https'}:
            attachments.append(full)
    valid = bool(page.title and page.body_seen and page.chunks)
    return {
        'title': page.title,
        'target': fields.get('target') or None,
        'event_datetime': fields.get('event_datetime') or None,
        'application_period_text': app_text or None,
        'application_start': start, 'application_end': end,
        'location': fields.get('location') or None,
        'attachment_url': attachments[0] if attachments else None,
        'attachment_urls': attachments, 'attachments_downloaded': False,
        'url': url, 'published_date': published_date,
        'application_year_source': year_source,
        'notice_type': 'review',
        'review_state': 'pending' if start and end else 'needs_review',
        'availability_verified': False,
        'status_basis': '공고 접수일 기준; 정원/선착순 조기마감 여부 미검증',
        'date_resolution': 'date_range' if start and end else 'needs_review',
        '_parse_valid': valid,
        '_parse_note': '공지 제목/본문 구조 확인' if valid else '공지 제목/본문 구조 또는 본문 텍스트 미확인',
    }


def load_existing():
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding='utf-8'))
    return {'institution': TARGET_NAME, 'updated_at': None, 'status': '미실행', 'programs': []}


def save_result(result):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(DATA_PATH)


def main():
    try:
        existing = load_existing()
    except Exception as exc:
        print(f'이전 자료 읽기 실패: {type(exc).__name__}: {exc} — 파일을 덮어쓰지 않습니다.', file=sys.stderr)
        return 1
    now = datetime.now(KST)
    attempted_at, today = now.isoformat(), now.date().isoformat()
    old_programs = existing.get('programs', [])
    records = {str(p['id']): dict(p) for p in old_programs if isinstance(p, dict) and p.get('id') is not None}
    legacy = [p for p in old_programs if not isinstance(p, dict) or p.get('id') is None]
    previous_success = existing.get('last_success_at')
    # 옛 updated_at은 시도 시각일 수 있으므로 성공 시각으로 승격하지 않는다.
    result = dict(existing, institution=TARGET_NAME, updated_at=attempted_at,
                  last_attempt_at=attempted_at, last_success_at=previous_success,
                  review_state='needs_review',
                  collection_scope={'pages': 1, 'completeness': 'unverified'},
                  quality={'listed_details': 0, 'successful_details': 0,
                           'failed_details': 0, 'preserved_records': len(old_programs)},
                  detail_failures=[])
    for record in records.values():
        record['source_status'] = 'not_checked'
        record['stale'] = True
        record['review_state'] = 'needs_review'
        record.setdefault('last_success_at', previous_success)
        record['status'] = compute_status(record.get('application_start'), record.get('application_end'), today)

    def finish_failure(note):
        result['status'] = f'수집 실패: {note} — 이전 자료 유지'
        result['programs'] = list(records.values()) + legacy
        save_result(result)
        print(result['status'], file=sys.stderr)
        return 1

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        target = next(t for t in config['targets'] if t['name'] == TARGET_NAME)
        result['collection_scope']['list_url'] = target['list_url']
        client = base.Client()
        response = client.fetch(target['list_url'])
        verdict, note, page = base.html_page(response)
        if verdict != base.SUCCESS or page is None:
            return finish_failure('목록 확인 불가: ' + note)
        found = base.find_items(page, target, response.url)
        if not found:
            return finish_failure('목록 공고 0건: 주소/파서/페이지 구조 확인 필요')
    except Exception as exc:
        return finish_failure(f'목록 처리 예외: {type(exc).__name__}: {exc}')

    quality = result['quality']
    quality['listed_details'] = len(found)
    successful = 0
    for record_id, record in records.items():
        if record_id not in found:
            record['source_status'] = 'not_seen_on_first_page'
    for item_id, item in found.items():
        item_id = str(item_id)
        if item_id in records:
            records[item_id]['last_attempt_at'] = attempted_at
            records[item_id]['last_seen_at'] = attempted_at
        try:
            response = client.fetch(item['url'])
            verdict, note, page = base.html_page(response)
            if verdict != base.SUCCESS or page is None:
                raise ValueError('상세 확인 불가: ' + note)
            detail = parse_detail(base.decode(response), response.url)
            if not detail.pop('_parse_valid'):
                raise ValueError(detail.pop('_parse_note'))
            detail.pop('_parse_note')
            detail.update(id=item_id, status=compute_status(detail['application_start'], detail['application_end'], today),
                          last_attempt_at=attempted_at, last_success_at=attempted_at,
                          last_seen_at=attempted_at, source_status='current', stale=False)
            records[item_id] = detail
            successful += 1
        except Exception as exc:
            result['detail_failures'].append({'id': item_id, 'url': item['url'],
                                             'reason': f'{type(exc).__name__}: {exc}'})
            if item_id in records:
                records[item_id]['source_status'] = 'detail_failed'
    quality.update(successful_details=successful, failed_details=len(result['detail_failures']),
                   preserved_records=len(old_programs) - sum(1 for p in old_programs
                    if isinstance(p, dict) and str(p.get('id')) in found
                    and records.get(str(p.get('id')), {}).get('source_status') == 'current'))
    if not successful:
        return finish_failure(f'상세 {len(found)}건 모두 검증 실패')
    if not result['detail_failures']:
        result['last_success_at'] = attempted_at
        result['review_state'] = ('needs_review' if any(p.get('review_state') == 'needs_review'
                                 for p in records.values()) else 'pending')
    result['status'] = f'수집 완료(첫 페이지 상세 {successful}건 확인)'
    if result['detail_failures']:
        result['status'] += f", 부분 실패 {len(result['detail_failures'])}건 — 이전 자료 보존"
    result['programs'] = sorted(records.values(),
                                key=lambda p: (p.get('published_date') or '', str(p['id'])),
                                reverse=True) + legacy
    save_result(result)
    print(result['status'], file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
