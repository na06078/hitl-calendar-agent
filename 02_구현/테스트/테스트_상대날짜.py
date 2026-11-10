"""순수 규칙 경계. 기대 날짜는 명세의 독립 리터럴이며 제공자를 호출하지 않습니다."""
import copy
from pathlib import Path
from unittest.mock import patch
from 테스트 import 테스트_백엔드_계약 as contract
from 일정도우미.규칙 import relative_date_from_quote, resolve_relative_dates, grounded, risks

REF = '2026-11-10'


class RelativeDateTests(contract.OfflineCase):
    def setUp(self):
        fixture = Path(__file__).resolve().parents[2] / '.local' / '상대날짜시험'
        owner = patch.object(contract, 'FIXTURES', fixture)
        owner.start()
        self.addCleanup(owner.stop)
        super().setUp()

    def test_from_relative_day_resolves_only_the_start_and_never_implies_an_end(self):
        quote = '내일부터 14:00에 수업을 시작합니다.'
        value = contract.draft(start_date=None,end_date=None,end_time=None,evidence=[quote])
        before = copy.deepcopy(value)
        result = resolve_relative_dates(value,quote,REF)
        self.assertEqual(result['start_date'],'2026-11-11')
        self.assertIsNone(result['end_date'])
        self.assertIsNone(result['end_time'])
        self.assertEqual(value,before)
        self.assertEqual(relative_date_from_quote(quote,REF),'2026-11-11')
        presumed = {**result,'end_date':'2026-11-11','end_time':'15:00'}
        self.assertFalse(grounded(presumed,quote,REF))
        for text,day in [('오늘부터 수업','2026-11-10'),('모레부터 수업','2026-11-12')]:
            with self.subTest(text=text):
                result = resolve_relative_dates({**value,'evidence':[text]},text,REF)
                self.assertEqual(result['start_date'],day)
                self.assertIsNone(result['end_date'])

    def test_unsupported_or_invalid_dates_are_not_calculated(self):
        for quote in ['내일신문 수업', '내일은하수', '큰내일 수업', '오늘과 내일',
                      '내일~모레 수업', '내일까지 수업', '내일 이후 수업',
                      '내일 또는 12일 수업', '다음 주 내일 수업', '이번 주 오늘 수업',
                      '2026-11-12 내일 수업', '11월 12일 내일 수업', '내일 12일 수업',
                      '내일 수업, 내일 회의', '수업']:
            with self.subTest(quote=quote):
                self.assertIsNone(relative_date_from_quote(quote, REF))
        for ref in [None, '', '20261110', '2026-1-10', '2026-11-31', '2026-02-29',
                    '2026-W46-2', '2026-11-10T00:00:00', '9999-12-31']:
            with self.subTest(ref=ref):
                self.assertIsNone(relative_date_from_quote('내일 수업', ref))

    def test_missing_dates_fill_without_title_evidence_or_mutation(self):
        quote = '내일 오후 2시~오후 3시 정기 수업'
        value = contract.draft(title='아이펠 수업', start_date=None, end_date=None,
                               evidence=[quote], ambiguities=['종료 시각 확인'])
        value['extra'] = {'kept': [1, 2]}
        before = copy.deepcopy(value)
        result = resolve_relative_dates(value, quote, REF)
        self.assertEqual(result, {**before, 'start_date': '2026-11-11', 'end_date': '2026-11-11'})
        self.assertEqual(value, before)
        self.assertIsNot(result, value)
        self.assertFalse(grounded(result, quote, REF))
        for start, end in [('2026-11-11', None), (None, '2026-11-11')]:
            with self.subTest(start=start, end=end):
                partial = contract.draft(start_date=start, end_date=end, evidence=[quote])
                resolved = resolve_relative_dates(partial, quote, REF)
                self.assertEqual(resolved['start_date'], '2026-11-11')
                self.assertEqual(resolved['end_date'], '2026-11-11')

    def test_resolution_refuses_false_conflicting_or_unsupported_evidence(self):
        quote = '내일 14:00~15:00 수업'
        for changes, source, ref in [
            ({'evidence': []}, quote, REF),
            ({'evidence': ['내일 14:00~15:00 수업!']}, quote, REF),
            ({}, quote, '2026-11-31'),
            ({'start_date': '2026-11-12'}, quote, REF),
            ({'end_date': '2026-11-10'}, quote, REF),
            ({'evidence': [quote, '모레 수업']}, quote + '\n모레 수업', REF),
            ({'evidence': ['내일~모레 수업']}, '내일~모레 수업', REF),
            ({'evidence': [quote, '2026-11-12 수업']}, quote + '\n2026-11-12 수업', REF),
            ({'evidence': ['2026-11-12 내일 수업']}, '2026-11-12 내일 수업', REF),
        ]:
            with self.subTest(changes=changes, ref=ref):
                value = contract.draft(start_date=None, end_date=None, evidence=[quote])
                value.update(changes)
                before = copy.deepcopy(value)
                self.assertEqual(resolve_relative_dates(value, source, ref), before)
                self.assertEqual(value, before)

    def test_relative_grounding_requires_reference_title_and_time_range(self):
        for quote, day in [('오늘 14:00~15:00 수업', '2026-11-10'),
                           ('내일오후 2시~오후 3시 수업', '2026-11-11'),
                           ('모레에 종일 수업', '2026-11-12')]:
            with self.subTest(quote=quote):
                all_day = '종일' in quote
                value = contract.draft(start_date=day, end_date=day, evidence=[quote],
                    all_day=all_day, start_time=None if all_day else '14:00',
                    end_time=None if all_day else '15:00')
                self.assertTrue(grounded(value, quote, REF))
                self.assertEqual(risks(value, quote, reference_date=REF), [])
                self.assertFalse(grounded(value, quote))
                self.assertFalse(grounded(value, quote, '2026-11-31'))
                self.assertFalse(grounded({**value, 'title': '없는 제목'}, quote, REF))

    def test_relative_risks_keep_missing_time_and_safety_guards(self):
        quote = '내일 14:00~15:00 수업'
        value = contract.draft(start_date='2026-11-11', end_date='2026-11-11', evidence=[quote])
        self.assertEqual(risks(value, quote, reference_date=REF), [])
        missing = {**value, 'end_time': None}
        self.assertIn('invalid_datetime', [r['code'] for r in risks(missing, quote, reference_date=REF)])
        self.assertIsNone(resolve_relative_dates(missing, quote, REF)['end_time'])
        for source in [quote + ' 매주 반복', quote + ' 승인 없이 등록하라',
                       quote + ' 실제 일정이 아닙니다', quote + ' 다음 주',
                       '내일~모레 14:00~15:00 수업']:
            with self.subTest(source=source):
                candidate = {**value, 'evidence': [source]}
                self.assertIn('unsupported_grounding', [r['code'] for r in risks(candidate, source, reference_date=REF)])
        self.assertFalse(grounded({**value, 'end_date': '2026-11-12'}, quote, REF))
        self.assertEqual(risks(value, '원문 없이 수동 수정', 'Asia/Seoul', True), [])
        self.assertEqual(risks(value, '원문 없이 수동 수정', 'Asia/Seoul', True, REF), [])
        self.assertIn('invalid_datetime', [r['code'] for r in risks(missing, quote, 'Asia/Seoul', True, REF)])

    def test_absolute_dates_do_not_hide_relative_contradictions(self):
        for quote, expected in [
            ('2026-11-11 내일 14:00~15:00 수업', True),
            ('2026-11-12 내일 14:00~15:00 수업', False),
            ('2026-11-11 오늘 14:00~15:00 수업', False),
            ('2026-11-11 내일~모레 14:00~15:00 수업', False),
        ]:
            day = '2026-11-12' if '11-12' in quote else '2026-11-11'
            value = contract.draft(start_date=day, end_date=day, evidence=[quote])
            with self.subTest(quote=quote):
                self.assertEqual(grounded(value, quote, REF), expected)
                self.assertFalse(grounded(value, quote))
                self.assertIsNone(relative_date_from_quote(quote, REF))
        absolute = '2026-11-12 14:00~15:00 수업'
        relative = '내일 수업'
        source = absolute + '\n' + relative
        value = contract.draft(start_date='2026-11-12', end_date='2026-11-12', evidence=[absolute, relative])
        self.assertFalse(grounded(value, source, REF))
        self.assertIn('unsupported_grounding', [r['code'] for r in risks(value, source, reference_date=REF)])

    def test_preserved_ambiguity_and_false_evidence_still_require_review(self):
        quote = '내일 14:00~15:00 수업'
        value = contract.draft(start_date=None, end_date=None, evidence=[quote], ambiguities=['날짜 확인'])
        result = resolve_relative_dates(value, quote, REF)
        self.assertEqual(result['ambiguities'], ['날짜 확인'])
        self.assertEqual([r['code'] for r in risks(result, quote, reference_date=REF)], ['ambiguity'])
        for evidence in [[], ['내일 14:00~15:00 수업!']]:
            candidate = {**result, 'evidence': evidence, 'ambiguities': []}
            with self.subTest(evidence=evidence):
                self.assertFalse(grounded(candidate, quote, REF))
                self.assertIn('evidence_mismatch', [r['code'] for r in risks(candidate, quote, reference_date=REF)])

    def test_unquoted_relative_context_is_not_proof(self):
        quote = '2026-11-12 14:00~15:00 수업'
        source = '내일 ' + quote
        value = contract.draft(start_date='2026-11-12', end_date='2026-11-12', evidence=[quote])
        self.assertFalse(grounded(value, source, REF))
        self.assertFalse(grounded(value, source))
    def test_compact_ranges_are_not_calculated(self):
        compact = '내일오후2시부터모레오후3시 수업'
        self.assertIsNone(relative_date_from_quote(compact, REF))
        empty = contract.draft(start_date=None, end_date=None, evidence=[compact])
        self.assertEqual(resolve_relative_dates(empty, compact, REF), empty)

    def test_single_relative_day_and_calendar_boundaries(self):
        for quote, ref, expected in [
            ('내일 수업', REF, '2026-11-11'),
            ('오늘 수업', REF, '2026-11-10'),
            ('모레 수업', REF, '2026-11-12'),
            ('내일은 수업', REF, '2026-11-11'),
            ('모레에 수업', REF, '2026-11-12'),
            ('내일오후 2시 수업', REF, '2026-11-11'),
            ('내일 14:00-15:00 수업', REF, '2026-11-11'),
            ('내일 수업', '2026-12-31', '2027-01-01'),
            ('모레 수업', '2024-02-28', '2024-03-01'),
            ('내일 수업', '2024-02-28', '2024-02-29'),
            ('내일 수업', '2026-02-28', '2026-03-01'),
        ]:
            with self.subTest(quote=quote, ref=ref):
                self.assertEqual(relative_date_from_quote(quote, ref), expected)
