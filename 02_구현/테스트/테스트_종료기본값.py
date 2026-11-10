"""사용자가 선택한 당일/1시간 종료 기본값의 순수 경계 검사."""
from copy import deepcopy
import unittest
from 테스트.테스트_백엔드_계약 import draft
from 일정도우미.규칙 import complete_end_defaults, interval, payload


class EndDefaultTests(unittest.TestCase):
    def test_timed_default_is_one_hour_and_preserves_input_and_evidence(self):
        source='2026년 11월 11일 14:00 수업'
        value=draft(start_date='2026-11-11',end_date=None,end_time=None,evidence=[source],ambiguities=['종료 미정'])
        before=deepcopy(value)
        result=complete_end_defaults(value,source)
        self.assertEqual(result['end_date'],'2026-11-11')
        self.assertEqual(result['end_time'],'15:00')
        self.assertEqual(value,before)
        self.assertEqual({k for k in value if result[k]!=value[k]},{'end_date','end_time'})
        self.assertEqual(complete_end_defaults(result,source),result)
        self.assertEqual((interval(result)[1]-interval(result)[0]).total_seconds(),3600)

    def test_all_day_default_is_start_day_and_google_end_is_exclusive(self):
        source='2026년 11월 11일 종일 수업'
        value=draft(start_date='2026-11-11',end_date=None,start_time=None,end_time=None,all_day=True,evidence=[source])
        result=complete_end_defaults(value,source)
        self.assertEqual(result['end_date'],'2026-11-11')
        self.assertIsNone(result['end_time'])
        self.assertEqual(payload(result,'test')['end'],{'date':'2026-11-12'})

    def test_midnight_rollover_retains_a_real_one_hour_duration(self):
        source='2026년 11월 11일 23:30 수업'
        value=draft(start_date='2026-11-11',end_date=None,start_time='23:30',end_time=None,evidence=[source])
        result=complete_end_defaults(value,source)
        self.assertEqual(result['end_date'],'2026-11-12')
        self.assertEqual(result['end_time'],'00:30')
        self.assertEqual((interval(result)[1]-interval(result)[0]).total_seconds(),3600)

    def test_explicit_end_values_are_never_replaced(self):
        for values in [dict(end_date='2026-11-12',end_time='17:45'),dict(end_date='not-a-date',end_time='bad')]:
            with self.subTest(values=values):
                value=draft(**values)
                self.assertEqual(complete_end_defaults(value,value['evidence'][0]),value)

    def test_an_explicit_end_time_only_gets_a_same_day_date(self):
        source='2026년 11월 10일 14:00~17:00 수업'
        value=draft(end_date=None,end_time='17:00',evidence=[source])
        result=complete_end_defaults(value,source)
        self.assertEqual(result['end_date'],'2026-11-10')
        self.assertEqual(result['end_time'],'17:00')

    def test_unknown_or_invalid_start_is_not_fabricated(self):
        for values in [dict(start_date=None),dict(start_date='2026-02-30'),dict(start_time=None),dict(start_time='25:00')]:
            with self.subTest(values=values):
                value=draft(end_date=None,end_time=None,evidence=['시작 시각 확인 필요'],**values)
                self.assertEqual(complete_end_defaults(value,'시작 시각 확인 필요'),value)

    def test_explicit_source_end_label_or_date_range_is_not_hidden_by_defaults(self):
        for source in ['2026년 11월 11일 14:00 시작, 종료 시각은 17:00 수업',
                       '2026년 11월 11일부터 2026년 11월 13일까지 종일 수업']:
            with self.subTest(source=source):
                value=draft(start_date='2026-11-11',end_date=None,end_time=None,evidence=[source])
                self.assertEqual(complete_end_defaults(value,source),value)

    def test_explicit_source_time_range_is_not_replaced_by_a_default(self):
        source='2026년 11월 11일 14:00~17:00 수업'
        value=draft(start_date='2026-11-11',end_date=None,end_time=None,evidence=[source])
        self.assertEqual(complete_end_defaults(value,source),value)


if __name__=='__main__':
    unittest.main()
