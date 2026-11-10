"""고정된 후보별 사실·독립 기대값으로 승인 기준을 비교합니다. 모델·캘린더를 호출하지 않습니다."""
import argparse
import json
from pathlib import Path
from 일정도우미.규칙 import metrics


def evaluate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('평가 사례는 비어 있지 않은 목록이어야 합니다.')
    ids = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'].strip():
            raise ValueError('모든 사례에 고유한 문자열 id가 필요합니다.')
        if any(type(case.get(key)) is not bool for key in ('needs_review', 'ambiguity', 'conflict')):
            raise ValueError('정답·정보 부족·충돌 값은 true/false여야 합니다.')
        ids.append(case['id'])
    if len(ids) != len(set(ids)):
        raise ValueError('사례 id가 중복되었습니다.')
    truth = [case['needs_review'] for case in cases]
    predicted = {
        'a_only': [case['ambiguity'] for case in cases],
        'b_only': [case['conflict'] for case in cases],
        'a_or_b': [case['ambiguity'] or case['conflict'] for case in cases],
        'all_review': [True for case in cases],
    }
    return {'case_count': len(cases), 'strategies': {name: metrics(truth, values) for name, values in predicted.items()}}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=root / '03_검증/평가자료.json')
    parser.add_argument('--output', type=Path, default=root / '03_검증/평가결과.json')
    args = parser.parse_args()
    try:
        dataset = json.loads(args.input.read_text(encoding='utf-8'))
        result = evaluate_cases(dataset['cases'])
        result['evaluation_kind'] = '같은 고정 사실에 대한 검토 분류 기준 비교. 실제 안전 가드를 끄거나 추가 등록하지 않음.'
        result['gold_author'] = dataset.get('gold_author', '미기재')
        result['limitations'] = dataset.get('limitations', [])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit('평가 자료의 경로·JSON·사례 형식을 확인하세요: ' + type(error).__name__) from None
