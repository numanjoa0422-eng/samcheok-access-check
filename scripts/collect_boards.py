#!/usr/bin/env python3
"""Additional public boards: first-page notices, with evidence and failure continuity.

The access tester supplies robots-aware, rate-limited HTTP and real link patterns.
The optional attachment stage reads Samcheok HS documents after the notice stage.
Neither stage infers school grades or accepting applications from a remaining date.
"""
import argparse
import copy
import hashlib
import json
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import check_access as access
import collect_youth_center as youth

KST = timezone(timedelta(hours=9))
VOID = youth.VOID_TAGS
HIDDEN_TAGS = {'script', 'style', 'noscript', 'template'}
DEFAULT_EXCLUDE = ('채용', '인사발령', '입찰', '계약체결', '수의계약', '공유재산',
                   '업무추진비', '예산집행', '결산공개', '물품매각')
DEFAULT_INCLUDE = ('행사', '프로그램', '모집', '신청', '접수', '체험', '공연', '축제',
                   '강좌', '캠프', '특강', '교육', '학부모', '시민', '청소년', '어린이',
                   '휴관', '건강', '복지', '무료', '문화', '운동', '대회', '전시', '장학')


class BudgetExceeded(RuntimeError):
    pass


class BudgetClient(access.Client):
    """A six-minute run and 90-second source budget, including robots waits."""
    def __init__(self, seconds=360, source_seconds=90):
        super().__init__()
        self.deadline = time.monotonic() + seconds
        self.source_seconds = source_seconds
        self.source_deadline = self.deadline

    def begin_source(self):
        self.source_deadline = min(self.deadline, time.monotonic() + self.source_seconds)

    def remaining(self):
        return min(self.deadline, self.source_deadline) - time.monotonic()

    def safe_sleep(self, delay):
        if delay >= self.remaining():
            raise BudgetExceeded('수집 시간 예산 소진: 다음 실행에서 재확인')
        self.original_sleep(delay)

    def within_budget(self, method, *args):
        if self.remaining() <= 0:
            raise BudgetExceeded('수집 시간 예산 소진: 다음 실행에서 재확인')
        old_sleep = access.time.sleep
        # Nested robots -> raw_get calls keep the original underlying sleep.
        if old_sleep != self.safe_sleep:
            self.original_sleep = old_sleep
        access.time.sleep = self.safe_sleep
        try:
            return method(*args)
        finally:
            access.time.sleep = old_sleep

    def raw_get(self, url):
        old_timeout, old_attempts = access.TIMEOUT_SECONDS, access.MAX_GET_ATTEMPTS
        access.TIMEOUT_SECONDS = max(.1, min(8, self.remaining()))
        access.MAX_GET_ATTEMPTS = 1
        try:
            return self.within_budget(super().raw_get, url)
        finally:
            access.TIMEOUT_SECONDS, access.MAX_GET_ATTEMPTS = old_timeout, old_attempts

    def robots_policy(self, url):
        return self.within_budget(super().robots_policy, url)


def normalized(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def public_url(value, base_url, same_origin=False):
    full = urllib.parse.urljoin(base_url, value)
    parsed, origin = urllib.parse.urlsplit(full), urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username:
        return None
    if same_origin and (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        return None
    return full


class ScopedNotice(HTMLParser):
    """Only proven article/body classes are read; header/nav text is never evidence."""
    def __init__(self, raw_html, parser):
        super().__init__(convert_charrefs=True)
        self.parser = parser
        self.stack, self.title_parts, self.meta_parts = [], [], []
        self.chunks, self.chunk, self.images, self.attachments = [], [], [], []
        self.body_seen = False
        self.feed(raw_html)
        self.flush()

    def has(self, class_name):
        return any(class_name in attrs.get('class', '').split() for _, attrs in self.stack)

    def hidden(self):
        return any(tag in HIDDEN_TAGS or 'hidden' in attrs or re.search(
            r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I)
            for tag, attrs in self.stack)

    def in_article(self):
        if self.parser == 'youth_notice':
            return self.has('bbsView')
        if self.parser == 'school_cms':
            return self.has('board-text')
        if self.parser == 'foundation':
            return self.has('bbs1view1')
        return False

    def in_body(self):
        body = {'youth_notice': 'view_body', 'school_cms': 'viewBox',
                'foundation': 'substance'}.get(self.parser)
        return bool(body and self.in_article() and self.has(body))

    def in_title(self):
        if not self.in_article():
            return False
        if self.parser == 'youth_notice':
            return self.has('view_head') and any(tag == 'h3' for tag, _ in self.stack)
        if self.parser == 'school_cms':
            return self.has('tit') and not any(tag == 'strong' for tag, _ in self.stack)
        return any(attrs.get('id') == 'sns_bbs_title' for _, attrs in self.stack)

    def flush(self):
        value = normalized(''.join(self.chunk))
        if value:
            self.chunks.append(value)
        self.chunk = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.in_body() and tag in {'p', 'li', 'tr', 'div', 'br'}:
            self.flush()
        self.stack.append((tag, attrs))
        if not self.hidden() and self.in_body():
            self.body_seen = True
            if tag == 'img' and attrs.get('src'):
                self.images.append(attrs['src'])
        if not self.hidden() and self.in_article() and tag == 'a' and attrs.get('href'):
            file_scope = (self.has('file_wrap') if self.parser == 'youth_notice'
                          else self.has('fieldBox') if self.parser == 'school_cms'
                          else self.has('attach1'))
            if file_scope:
                self.attachments.append(attrs['href'])
        if tag in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.in_body() and tag in {'p', 'li', 'tr', 'div'}:
            self.flush()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if self.hidden():
            return
        if self.in_title():
            self.title_parts.append(value)
        if self.in_body():
            self.chunk.append(value)
        if self.in_article() and not self.in_body():
            self.meta_parts.append(value)


class FoundationList(HTMLParser):
    """The actual STCF list has body snippets inside each link; read strong.t1 only."""
    def __init__(self, raw_html):
        super().__init__(convert_charrefs=True)
        self.stack, self.links, self.anchor = [], [], None
        self.feed(raw_html)

    def has(self, name):
        return any(name in attrs.get('class', '').split() for _, attrs in self.stack)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.stack.append((tag, values))
        if tag == 'a' and self.has('list1f1t3i1') and self.has('lst1') and 'a1' in values.get('class', '').split():
            self.anchor = [values.get('href', ''), values.get('onclick', ''), []]
        if tag in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            self.links.append((self.anchor[0], self.anchor[1], normalized(''.join(self.anchor[2]))))
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if self.anchor and self.has('t1') and not any(tag == 'em' for tag, _ in self.stack):
            self.anchor[2].append(value)


FIELD_ALIASES = dict(youth.LABELS, **{
    '신청기한 및 방법': 'application_period', '신청기한': 'application_period',
    '접수마감': 'application_period', '운영기간': 'event_datetime',
    '운영일시': 'event_datetime', '교육기간': 'event_datetime',
    '운영장소': 'location', '교육장소': 'location',
    '전시기간': 'event_datetime', '전시장소': 'location', '공연장소': 'location',
    '신청방법': 'application_method', '접수방법': 'application_method',
    '참가비': 'cost', '관람료': 'cost', '문의처': 'contact',
})
FIELD_ALIASES = {re.sub(r'\s+', '', key): value for key, value in FIELD_ALIASES.items()}


def explicit_fields(chunks):
    """Labels only, with actual school numbered/spaced labels normalized.

    Colon times are values, never labels. Unrecognized labeled lines end the
    preceding field. Text outside the article is not passed into this routine.
    """
    result, waiting, app_values = {}, None, []
    for raw in chunks:
        value = normalized(re.sub(r'^\s*(?:[✔✓○●•*\-]+\s*)?(?:(?:\d{1,2}|[가-힣])\s*[.)]\s*)?', '', raw))
        match = re.match(r'^([^:：]{1,30})\s*[:：]\s*(.*)$', value)
        if match and not re.match(r'^\d{1,2}\s*:', value):
            waiting = None
            label = re.sub(r'\s+', '', match.group(1))
            field = FIELD_ALIASES.get(label)
            if field:
                if match.group(2).strip():
                    if field == 'application_period':
                        app_values.append(normalized(match.group(2)))
                    else:
                        result[field] = normalized(match.group(2))[:500]
                else:
                    waiting = field
            continue
        field = FIELD_ALIASES.get(re.sub(r'\s+', '', value))
        if field:
            waiting = field
        elif re.match(r'^\s*[✔✓○●•*]', raw):
            # A new unknown bullet heading ends the preceding application's
            # block; ordinary '-' value lines remain part of that block.
            waiting = None
        elif waiting and value:
            if waiting == 'application_period':
                app_values.append(value)
            else:
                result[waiting] = value[:500]
                waiting = None
    if app_values:
        result['application_period'] = '\n'.join(app_values)[:1000]
    return result


def application_dates(text, published_date, title, chunks):
    # Match the established youth parser's cautious range rule, without using
    # the current year. Explicit deadlines may yield an end, never a start.
    context_years = {int(y) for y in youth.YEAR_RE.findall(title + ' ' + ' '.join(chunks))}
    hint = next(iter(context_years)) if len(context_years) == 1 else None
    if not context_years and published_date:
        hint = date.fromisoformat(published_date).year
    start, end = youth.parse_date_range(text, hint)
    if start and end:
        return start, end, 'labeled_date_range'
    if text and re.search(r'까지|마감', text) and not re.search(r'부터|~|～|〜', text):
        clean = re.sub(r'\(\s*[월화수목금토일](?:요일)?\s*\)', '', text)
        # No inferred year for a single deadline; no date-only substitution
        # when an exact closing time is present.
        if not re.search(r'\d{1,2}\s*:\s*\d{2}|\d{1,2}\s*시|오전|오후', clean):
            matches = re.findall(r'(?<!\d)(\d{4})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})(?:\s*일|\.)?', clean)
            if len(matches) == 1:
                try:
                    return None, date(*(int(v) for v in matches[0])).isoformat(), 'labeled_explicit_deadline'
                except ValueError:
                    pass
    return None, None, 'needs_review'


def parse_detail(raw_html, url, parser):
    page = ScopedNotice(raw_html, parser)
    title = normalized(''.join(page.title_parts))
    fields = explicit_fields(page.chunks)
    publication = re.search(r'(?:작성일|작성일시|등록일|등록일시)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})', normalized(' '.join(page.meta_parts)))
    published_date = None
    if publication:
        try:
            published_date = date.fromisoformat(publication.group(1)).isoformat()
        except ValueError:
            pass
    images = []
    for image in page.images:
        full = public_url(image, url, same_origin=True)
        path = urllib.parse.urlsplit(full).path if full else ''
        if full and (re.search(r'\.(?:jpe?g|png|gif|webp)$', path, re.I) or
                     (parser == 'foundation' and path == '/board/image.do')):
            if full not in images:
                images.append(full)
    attachments = list(dict.fromkeys(full for href in page.attachments
                                    if (full := public_url(href, url))))
    app = fields.get('application_period')
    start, end, resolution = application_dates(app, published_date, title, page.chunks)
    valid = bool(title and page.body_seen and (page.chunks or images))
    only_repeated_title = bool(images and normalized(' '.join(page.chunks)) == title)
    content_type = 'text' if page.chunks and not only_repeated_title else 'image_only' if images else 'unverified'
    reasons = ['일정·대상·현재 접수 여부 자동 확정 안 함']
    if images:
        reasons.append('본문 이미지 내용 미추출')
    if attachments:
        reasons.append('첨부파일 내용 미추출')
    return {
        'title': title, 'target': fields.get('target'),
        'event_datetime': fields.get('event_datetime'),
        'application_period_text': app, 'application_start': start, 'application_end': end,
        'location': fields.get('location'), 'application_method': fields.get('application_method'),
        'cost': fields.get('cost'), 'contact': fields.get('contact'), 'published_date': published_date, 'url': url,
        'attachment_url': attachments[0] if attachments else None, 'attachment_urls': attachments,
        'body_image_urls': images, 'body_excerpt': '\n'.join(page.chunks)[:6000],
        'body_content_type': content_type, 'body_verified': valid,
        'body_hash': hashlib.sha256(('\n'.join(page.chunks) + '\n' + '\n'.join(images)).encode('utf-8')).hexdigest(),
        'image_content_extracted': False, 'attachments_downloaded': False,
        'availability_verified': False, 'review_state': 'needs_review', 'notice_type': 'review',
        'status': '확인필요', 'status_basis': '접수 가능 여부 미확정',
        'date_resolution': resolution, 'review_reason': '; '.join(reasons),
        '_parse_valid': valid,
        '_parse_note': '본문 범위·제목 구조 확인' if valid else '본문 범위·제목 구조 확인 필요',
    }


def relevance(title, body, source):
    exclusions = source.get('exclude_keywords', DEFAULT_EXCLUDE)
    hit = next((term for term in exclusions if term and term in (title or '')), None)
    if hit:
        return False, f'제목의 제외 키워드: {hit}'
    keywords = source.get('include_keywords', DEFAULT_INCLUDE)
    if not keywords:
        return True, '수집 대상 게시판: 별도 포함 키워드 제한 없음'
    content = (title or '') + ' ' + (body or '')
    hit = next((term for term in keywords if term and term in content), None)
    return (True, f'공개 프로그램·생활정보 키워드: {hit}') if hit else (False, '관련 키워드 미확인')


def stable_id(source_id, remote_id):
    return 'board:' + source_id + ':' + urllib.parse.quote(str(remote_id), safe='')


def metadata_only(item, note):
    return {
        'title': item['title'], 'url': item['url'], 'target': None, 'event_datetime': None,
        'application_period_text': None, 'application_start': None, 'application_end': None,
        'location': None, 'published_date': None, 'attachment_url': None, 'attachment_urls': [],
        'body_image_urls': [], 'body_excerpt': '', 'body_content_type': 'unverified',
        'body_verified': False, 'image_content_extracted': False, 'attachments_downloaded': False,
        'availability_verified': False, 'review_state': 'needs_review', 'notice_type': 'review',
        'status': '확인필요', 'status_basis': '목록 제목·원문 링크만 확인',
        'date_resolution': 'needs_review', 'review_reason': note,
    }


def collect_source(source, target, previous_records, previous_summary, client, now):
    """Return records+summary without file I/O; failure never advances success time."""
    records = {str(p['id']): copy.deepcopy(p) for p in previous_records}
    for record in records.values():
        record.update(source_status='not_seen_on_first_page', collection_state='stale', stale=True)
    summary = {
        'id': source['id'], 'org': source['org'], 'parser': source['parser'], 'enabled': True,
        'list_url': source.get('list_url') or target['list_url'], 'last_attempt_at': now,
        'last_success_at': previous_summary.get('last_success_at'),
        'collection_scope': {'pages': 1, 'max_items': min(10, max(1, int(source.get('max_items', 10)))),
                             'completeness': 'unverified'},
        'found_count': 0, 'relevant_count': 0, 'collected_count': 0,
        'metadata_only_count': 0, 'failed_count': 0, 'errors': [],
    }
    try:
        if hasattr(client, 'begin_source'):
            client.begin_source()
        response = client.fetch(summary['list_url'])
        verdict, note, page = access.html_page(response)
        if verdict != access.SUCCESS or page is None:
            raise ValueError(note)
        list_page = FoundationList(access.decode(response)) if source['parser'] == 'foundation' else page
        found = access.find_items(list_page, target, response.url)
        if not found:
            raise ValueError('목록 링크 0건: 주소/링크 패턴/사이트 구조 확인 필요')
    except Exception as exc:
        summary.update(status='failed', errors=[{'stage': 'list', 'reason': str(exc)}])
        for record in records.values():
            record.update(source_status='list_failed', last_attempt_at=now)
        return list(records.values()), summary

    items = list(found.items())[:summary['collection_scope']['max_items']]
    summary['found_count'] = len(items)
    for remote_id, item in items:
        record_id = stable_id(source['id'], remote_id)
        prior = records.get(record_id)
        if prior:
            prior.update(last_attempt_at=now, last_seen_at=now)
        title_included, why = relevance(item['title'], '', source)
        if why.startswith('제목의 제외 키워드'):
            continue
        try:
            response = client.fetch(item['url'])
            verdict, note, page = access.html_page(response)
            if verdict != access.SUCCESS or page is None:
                raise ValueError(note)
            detail = parse_detail(access.decode(response), response.url, source['parser'])
            if not detail.pop('_parse_valid'):
                raise ValueError(detail.pop('_parse_note'))
            detail.pop('_parse_note')
            included, reason = relevance(item['title'] + ' ' + detail['title'], detail['body_excerpt'], source)
            if not included:
                continue
            detail.update(
                id=record_id, remote_id=str(remote_id), source_id=source['id'],
                org=source['org'], institution=source['org'], source_name=source.get('name') or source['org'], source_list_url=summary['list_url'],
                list_title=item['title'], last_attempt_at=now, last_success_at=now,
                last_detail_success_at=now, last_success_basis='article_title_and_scoped_body',
                last_seen_at=now, source_status='current', collection_state='current', stale=False,
                relevance_reason=reason)
            if prior and prior.get('attachment_evidence'):
                detail['attachment_evidence'] = copy.deepcopy(prior['attachment_evidence'])
                detail['attachment_evidence']['status'] = 'stale'
                for doc in detail['attachment_evidence'].get('documents', []):
                    if doc.get('text'): doc['status'] = 'stale'
                if prior.get('attachment_content_signature'):
                    detail['attachment_content_signature'] = prior['attachment_content_signature']
            records[record_id] = detail
            summary['relevant_count'] += 1
            summary['collected_count'] += 1
        except Exception as exc:
            summary['failed_count'] += 1
            summary['errors'].append({'stage': 'detail', 'id': record_id, 'url': item['url'], 'reason': str(exc)})
            if prior:
                prior.update(source_status='detail_failed', collection_state='stale', stale=True)
            elif title_included:
                detail = metadata_only(item, '상세 수집 미완료: ' + str(exc))
                detail.update(id=record_id, remote_id=str(remote_id), source_id=source['id'],
                    org=source['org'], institution=source['org'], source_name=source.get('name') or source['org'], source_list_url=summary['list_url'],
                    list_title=item['title'], last_attempt_at=now, last_success_at=now,
                    last_detail_success_at=None, last_success_basis='first_page_title_and_link_only',
                    last_seen_at=now, source_status='current', collection_state='metadata_only', stale=False,
                    relevance_reason=why)
                records[record_id] = detail
                summary['metadata_only_count'] += 1
                summary['relevant_count'] += 1
    summary['status'] = 'partial' if summary['failed_count'] else 'success'
    if not summary['failed_count']:
        summary['last_success_at'] = now
    return list(records.values()), summary


def collect_all(config, access_targets, existing, client, now):
    if not isinstance(existing, dict) or not isinstance(existing.get('programs', []), list):
        raise ValueError('이전 board-programs.json 형식 오류: 덮어쓰기 중지')
    records = existing.get('programs', [])
    if any(not isinstance(p, dict) or not p.get('id') or not p.get('source_id') for p in records):
        raise ValueError('이전 공고 식별자 오류: 덮어쓰기 중지')
    sources = config.get('sources')
    if not isinstance(sources, list) or len({s.get('id') for s in sources}) != len(sources):
        raise ValueError('collection-targets.json sources 형식/중복 식별자 오류')
    targets = {target['name']: target for target in access_targets['targets']}
    configured = {s['id']: s for s in sources}
    prior_summaries = {s['id']: s for s in existing.get('sources', [])}
    old_by_source = {}
    for record in records:
        old_by_source.setdefault(record['source_id'], []).append(record)
    summaries, merged, handled = [], [], set()
    for source in sources:
        if not source.get('enabled', False):
            continue
        if source.get('parser') not in {'youth_notice', 'school_cms', 'foundation'}:
            raise ValueError(f"지원하지 않는 파서: {source.get('parser')}")
        if source.get('access_target_id') not in targets:
            raise ValueError(f"접근 시험 대상 없음: {source.get('access_target_id')}")
        handled.add(source['id'])
        current, summary = collect_source(source, targets[source['access_target_id']],
            old_by_source.get(source['id'], []), prior_summaries.get(source['id'], {}), client, now)
        merged.extend(current)
        summaries.append(summary)
    # Disabling/removing a configured source does not silently erase history.
    for source_id, old in old_by_source.items():
        if source_id not in handled:
            for record in copy.deepcopy(old):
                record.update(source_status='source_disabled', collection_state='stale', stale=True)
                merged.append(record)
    # Every retained source_id must still have a corresponding summary. Keep
    # inactive history separate from attempted/enabled collection results.
    inactive_ids = (set(configured) | set(old_by_source) | set(prior_summaries)) - handled
    for source_id in sorted(inactive_ids):
        configured_source = configured.get(source_id, {})
        previous = prior_summaries.get(source_id, {})
        old = old_by_source.get(source_id, [])
        first_record = old[0] if old else {}
        org = (configured_source.get('org') or previous.get('org') or
               first_record.get('org') or first_record.get('institution') or source_id)
        summary = copy.deepcopy(previous)
        summary.update(
            id=source_id, org=org,
            name=configured_source.get('name') or previous.get('name') or org,
            parser=configured_source.get('parser') or previous.get('parser') or 'unknown',
            enabled=False, status='disabled' if source_id in configured else 'unreported',
            list_url=configured_source.get('list_url') or previous.get('list_url') or first_record.get('source_list_url'),
            last_attempt_at=previous.get('last_attempt_at'),
            last_success_at=previous.get('last_success_at'),
            found_count=0, relevant_count=0, collected_count=0, metadata_only_count=0,
            failed_count=0, preserved_count=len(old), errors=[],
        )
        summaries.append(summary)
    merged.sort(key=lambda p: (p.get('published_date') or '', p['id']), reverse=True)
    enabled_summaries = [s for s in summaries if s['enabled']]
    full_success = bool(enabled_summaries) and all(s['status'] == 'success' for s in enabled_summaries)
    result = dict(existing, schema_version=1, last_attempt_at=now,
        last_success_at=now if full_success else existing.get('last_success_at'),
        status='success' if full_success else 'partial' if enabled_summaries else 'no_enabled_sources',
        collection_scope={'pages_per_source': 1, 'completeness': 'unverified'},
        programs=merged, sources=summaries)
    result['totals'] = {'stored_records': len(merged), 'enabled_sources': len(enabled_summaries),
        'successful_sources': sum(s['status'] == 'success' for s in enabled_summaries),
        'partial_sources': sum(s['status'] == 'partial' for s in enabled_summaries),
        'failed_sources': sum(s['status'] == 'failed' for s in enabled_summaries)}
    return result


def atomic_write(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/collection-targets.json')
    parser.add_argument('--access-config', type=Path, default=ROOT / 'config/targets.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/board-programs.json')
    parser.add_argument('--skip-attachments', action='store_true', help='첨부 확인을 별도 단계에서 실행할 때 사용')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        targets = json.loads(args.access_config.read_text(encoding='utf-8'))
        existing = json.loads(args.output.read_text(encoding='utf-8')) if args.output.exists() else {'programs': [], 'sources': []}
        result = collect_all(config, targets, existing, BudgetClient(), datetime.now(KST).isoformat())
        atomic_write(args.output, result)
        if not args.skip_attachments and any(p.get('source_id')=='samchokhs' and p.get('attachment_urls') for p in result['programs']):
            # Run after the notice write: an attachment failure must not roll back a successful notice collection.
            import subprocess
            attachment_run = subprocess.run([sys.executable, str(ROOT/'scripts/read_attachments.py'),
                '--input', str(args.output), '--source', 'samchokhs', '--limit', '3'], timeout=290)
            if attachment_run.returncode:
                print('첨부 단계 실패: 저장된 공고는 보존, 첨부 확인 로그를 참조하세요.', file=sys.stderr)
    except Exception as exc:
        print(f'추가 수집 처리 오류: {type(exc).__name__}: {exc} — 이전 결과 파일 유지', file=sys.stderr)
        return 1
    totals = result['totals']
    print(f"추가 게시판: {totals['enabled_sources']}곳, 저장 {totals['stored_records']}건, "
          f"완료 {totals['successful_sources']}곳 / 부분 {totals['partial_sources']}곳 / 실패 {totals['failed_sources']}곳", file=sys.stderr)
    # A partial HTTP result is recorded as data; infrastructure/config failure exits nonzero.
    enabled_sources = [s for s in result['sources'] if s['enabled']]
    return 1 if not enabled_sources or all(s['status'] == 'failed' for s in enabled_sources) else 0


if __name__ == '__main__':
    sys.exit(main())
