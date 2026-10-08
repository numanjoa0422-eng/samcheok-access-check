"""Regression cases for dates that previously invented recruitment statuses."""
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location(
    'collector_date_regressions', ROOT / 'scripts' / 'collect_youth_center.py'
)
cy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cy
SPEC.loader.exec_module(cy)

FIXTURE_HTML = (ROOT / 'tests' / 'fixtures' / 'youth_notice_sample.html').read_text(
    encoding='utf-8'
)
SAMPLE_URL = 'https://youth.samcheok.go.kr/youth/sub/Notice.php?mode=view'


def notice(period, title='청소년 참가 모집', published=None, extra=''):
    metadata = f'작성일:<span>{published}</span>' if published else ''
    return (
        f'<section class="bbsView"><div class="view_head">'
        f'<h3>{title}</h3>{metadata}</div><div class="view_body">'
        f'{extra}<p>신청기간 : {period}</p></div></section>'
    )


class RecruitmentDateRegressions(unittest.TestCase):
    def test_explicit_korean_year_overrides_unrelated_hint(self):
        self.assertEqual(
            cy.parse_date_range('2025년 10월1일~16일 신청', 2026),
            ('2025-10-01', '2025-10-16'),
        )

    def test_explicit_dotted_year_does_not_need_hint(self):
        self.assertEqual(
            cy.parse_date_range('2025.10.1.~2025.10.16.'),
            ('2025-10-01', '2025-10-16'),
        )

    def test_weekday_parentheses_do_not_collapse_range_to_first_day(self):
        self.assertEqual(
            cy.parse_date_range('2026년 10월 1일(목) ~ 10월 16일(금)'),
            ('2026-10-01', '2026-10-16'),
        )

    def test_from_until_words_do_not_collapse_range_to_first_day(self):
        self.assertEqual(
            cy.parse_date_range('10월 1일부터 10월 16일까지', 2026),
            ('2026-10-01', '2026-10-16'),
        )

    def test_deadline_alone_does_not_invent_a_start_date(self):
        self.assertEqual(
            cy.parse_date_range('10월 16일까지 선착순 접수', 2026),
            (None, None),
        )

    def test_bare_single_date_does_not_invent_one_day_period(self):
        self.assertEqual(cy.parse_date_range('10월 16일 접수', 2026), (None, None))

    def test_explicit_one_day_recruitment_is_supported(self):
        for qualifier in ('하루', '당일', '1일간'):
            with self.subTest(qualifier=qualifier):
                self.assertEqual(
                    cy.parse_date_range(f'2026년 10월 16일 {qualifier} 접수'),
                    ('2026-10-16', '2026-10-16'),
                )

    def test_missing_year_does_not_use_collection_clock(self):
        self.assertEqual(cy.parse_date_range('10월1일~16일 신청'), (None, None))

    def test_time_limited_period_does_not_get_day_only_status(self):
        for text in (
            '2026.10.1.~2026.10.16. 18:00까지',
            '2026년 10월 1일~16일 10시까지',
            '2026년 10월 1일~16일 오전 접수',
            '2026년 10월 1일~16일 오후 접수',
        ):
            with self.subTest(text=text):
                self.assertEqual(cy.parse_date_range(text), (None, None))

    def test_multiple_rounds_do_not_report_only_first_round(self):
        self.assertEqual(
            cy.parse_date_range('1차 10월1일~5일, 2차 10월10일~16일', 2026),
            (None, None),
        )

    def test_reversed_and_invalid_dates_stay_blank(self):
        for text in (
            '2026.10.16.~2026.10.1.',
            '2026년 10월16일~10월1일',
            '2026.2.30.~2026.3.5.',
            '2026년 13월1일~13월5일',
        ):
            with self.subTest(text=text):
                self.assertEqual(cy.parse_date_range(text), (None, None))

    def test_cross_year_requires_explicit_endpoint_years(self):
        for text in ('2026.12.28.~1.5.', '12월28일~1월5일'):
            with self.subTest(text=text):
                self.assertEqual(cy.parse_date_range(text, 2026), (None, None))
        for text in (
            '2026.12.28.~2027.1.5.',
            '2026년 12월28일~2027년 1월5일',
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    cy.parse_date_range(text), ('2026-12-28', '2027-01-05')
                )


class NoticeYearRegressions(unittest.TestCase):
    def test_real_notice_retains_2026_without_collection_year(self):
        detail = cy.parse_detail(FIXTURE_HTML, SAMPLE_URL, written_year=None)
        self.assertEqual(detail['application_start'], '2026-10-01')
        self.assertEqual(detail['application_end'], '2026-10-16')
        self.assertEqual(
            cy.compute_status(
                detail['application_start'], detail['application_end'], '2027-10-05'
            ),
            '마감',
        )

    def test_posted_year_is_used_when_body_has_no_year(self):
        detail = cy.parse_detail(
            notice('10월1일~16일 신청', published='2025-09-22'),
            SAMPLE_URL,
            written_year=None,
        )
        self.assertEqual(detail['application_start'], '2025-10-01')
        self.assertEqual(detail['application_end'], '2025-10-16')

    def test_conflicting_context_years_leave_undated_period_blank(self):
        detail = cy.parse_detail(
            notice(
                '10월1일~16일 신청',
                title='2026년 및 2027년 청소년 모집 안내',
                published='2026-09-22',
            ),
            SAMPLE_URL,
            written_year=None,
        )
        self.assertIsNone(detail['application_start'])
        self.assertIsNone(detail['application_end'])

    def test_explicit_application_year_wins_over_other_notice_years(self):
        detail = cy.parse_detail(
            notice(
                '2027년 1월1일~16일 신청',
                title='2026년 청소년 모집 안내',
                published='2026-12-22',
            ),
            SAMPLE_URL,
            written_year=None,
        )
        self.assertEqual(detail['application_start'], '2027-01-01')
        self.assertEqual(detail['application_end'], '2027-01-16')


if __name__ == '__main__':
    unittest.main()
