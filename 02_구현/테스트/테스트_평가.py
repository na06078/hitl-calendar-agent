"""합성 기대값으로 평가 수치와 자료 검증을 검사합니다. 외부 서비스는 사용하지 않습니다."""
import unittest

class EvaluationTests(unittest.TestCase):
    def test_criteria_comparison_matches_independent_five_case_expectations(self):
        from 평가실행 import evaluate_cases
        cases=[
            {'id':'clear','needs_review':False,'ambiguity':False,'conflict':False},
            {'id':'overlap','needs_review':True,'ambiguity':False,'conflict':True},
            {'id':'missing_edit','needs_review':True,'ambiguity':True,'conflict':False},
            {'id':'missing_reject','needs_review':True,'ambiguity':True,'conflict':False},
            {'id':'missing_rejudge','needs_review':True,'ambiguity':True,'conflict':False},
        ]
        actual=evaluate_cases(cases)
        expected={'case_count':5,'strategies':{
            'a_only':{'intervention_rate':3/5,'misses':1,'false_stops':0},
            'b_only':{'intervention_rate':1/5,'misses':3,'false_stops':0},
            'a_or_b':{'intervention_rate':4/5,'misses':0,'false_stops':0},
            'all_review':{'intervention_rate':1,'misses':0,'false_stops':1},
        }}
        self.assertEqual(actual,expected)

    def test_empty_duplicate_and_non_boolean_labels_are_rejected(self):
        from 평가실행 import evaluate_cases
        valid={'id':'one','needs_review':True,'ambiguity':True,'conflict':False}
        for cases in [[],[valid,valid],[{**valid,'needs_review':'yes'}],[{**valid,'ambiguity':1}]]:
            with self.subTest(cases=cases):
                with self.assertRaises(ValueError):
                    evaluate_cases(cases)
