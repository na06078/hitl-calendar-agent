"""자동 근거: 연도 포함 한 날짜의 시간 범위 또는 명시적인 다일 종일 구간.
상대 날짜는 명시 기준일의 오늘/내일/모레 단일 표현만 지원합니다.
연도 생략·반복/변경·날짜를 넘는 시간 일정은 자동 해석하지 않고 검토합니다.
"""
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo
from .형식 import EventDraft, ServiceError

# 알려진 명시적 반복을 보수적으로 검토에 보냅니다. 모든 자연어 반복의 검출 보장은 아닙니다.
UNSUPPORTED_RECURRENCE = re.compile(
    r'매\s*(?:일|주|월|달|년)|격\s*(?:일|주)|(?:날|요일|주|월|달|년|해)\s*마다|반복'
    r'|\b(?:daily|weekly|monthly|yearly|annually)\b|every\s+(?:day|week|month|year)\b', re.I)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def interval(draft, timezone='Asia/Seoul'):
    try:
        d = EventDraft.model_validate(draft)
        if not d.title.strip():
            raise ValueError()
        for value in (d.start_date, d.end_date):
            if value is None or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError()
        start_day, end_day = date.fromisoformat(d.start_date), date.fromisoformat(d.end_date)
        zone = ZoneInfo(timezone)
        if d.all_day:
            if d.start_time is not None or d.end_time is not None:
                raise ValueError()
            start = datetime.combine(start_day, time(), zone)
            end = datetime.combine(end_day + timedelta(days=1), time(), zone)
        else:
            for value in (d.start_time, d.end_time):
                if value is None or not re.fullmatch(r'\d{2}:\d{2}', value):
                    raise ValueError()
            start = datetime.combine(start_day, time.fromisoformat(d.start_time), zone)
            end = datetime.combine(end_day, time.fromisoformat(d.end_time), zone)
            for dt in (start, end):
                if dt.replace(fold=1).utcoffset() != dt.utcoffset() or dt.astimezone(utc_timezone.utc).astimezone(zone).replace(tzinfo=None) != dt.replace(tzinfo=None):
                    raise ValueError()
        if end <= start:
            raise ValueError()
        return start, end
    except (ValueError, TypeError, OverflowError):
        raise ServiceError('invalid_datetime', '제목과 유효한 시작·종료 일시를 수정하세요. 종일 종료일은 마지막 포함 날짜입니다.') from None


def cross_day_timed(draft):
    return bool(not draft['all_day'] and draft.get('start_date') and draft.get('end_date')
                and draft['start_date'] != draft['end_date'])


RELATIVE_DAY = re.compile(
    r'(?<![\w])(오늘|내일|모레)(?=$|[^\w]|\d|'
    r'(?:은|는|에|엔|도|만|의|과|와|부터|까지)(?=$|[^\w]|\d|오전|오후)|'
    r'(?:오전|오후|아침|점심|저녁|밤|새벽)(?=$|[^\w]|\d))')


ABSOLUTE_DATE = re.compile(r'(\d{4})\s*(?:년\s*|-)(\d{1,2})\s*(?:월\s*|-)(\d{1,2})\s*일?')
# 연도 생략 날짜도 상대 날짜로 덮어 추정하지 않습니다.
OTHER_DATE = re.compile(r'\d\s*(?:년|월|일)|(?<![\d:])\d{1,4}[-/.]\d{1,2}(?![\d:])')
UNSUPPORTED_RELATIVE = re.compile(
    r'다음\s*주|이번\s*주|(?:오늘|내일|모레)\s*(?:까지|이후|이전|[~∼〜～–—-])'
    r'|(?:부터|까지)(?:오늘|내일|모레)')
OPEN_RELATIVE_START = re.compile(r'(?<!\w)(?:오늘|내일|모레)\s*부터')


def relative_date_from_quote(quote, reference_date):
    """단일 상대 날짜의 기준점을 계산합니다. '부터'는 시작점일 뿐 종료를 뜻하지 않습니다."""
    if not isinstance(quote, str) or not isinstance(reference_date, str):
        return None
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', reference_date):
        return None
    if OTHER_DATE.search(quote) or UNSUPPORTED_RELATIVE.search(quote):
        return None
    mentions = list(RELATIVE_DAY.finditer(quote))
    if len(mentions) != 1:
        return None
    try:
        return (date.fromisoformat(reference_date) + timedelta(
            days={'오늘': 0, '내일': 1, '모레': 2}[mentions[0].group(1)])).isoformat()
    except (ValueError, OverflowError):
        return None


def _relative_evidence_date(draft, source, reference_date):
    """날짜 보완의 근거만 검사합니다. 제목/시각의 자동 실행 검사는 별도입니다."""
    quotes = draft.get('evidence', [])
    if not quotes or any(not q or q not in source for q in quotes):
        return None
    dates = set()
    for quote in quotes:
        if OTHER_DATE.search(quote) or UNSUPPORTED_RELATIVE.search(quote):
            return None
        if RELATIVE_DAY.search(quote):
            resolved = relative_date_from_quote(quote, reference_date)
            if resolved is None:
                return None
            dates.add(resolved)
    return dates.pop() if len(dates) == 1 else None


def resolve_relative_dates(draft, source, reference_date):
    """호출자의 후보를 변경하지 않고 누락(None) 날짜만 보완합니다."""
    result = dict(draft)
    resolved = _relative_evidence_date(draft, source, reference_date)
    if resolved is not None and all(draft.get(key) in (None, resolved) for key in ('start_date', 'end_date')):
        fields = ('start_date',) if any(OPEN_RELATIVE_START.search(q) for q in draft['evidence']) else ('start_date','end_date')
        for key in fields:
            if draft.get(key) is None:
                result[key] = resolved
    return result


def grounded(draft, source, reference_date=None):
    # 보수적 제한이지 모든 자연어/인젝션의 의미를 판별하는 방어라고 주장하지 않습니다.
    if cross_day_timed(draft):
        return False
    if UNSUPPORTED_RECURRENCE.search(source):
        return False
    if any(OPEN_RELATIVE_START.search(q) for q in draft['evidence']):
        # 시작만 명시된 표현에서 단일 종료 날짜/반복 기간을 추정해 자동 등록하지 않습니다.
        return False
    if re.search(r'승인\s*없이|출력|무시|시스템|프롬프트|지침|등록하|예시|가정|아닙|아님|않|없|다음\s*주|이번\s*주|변경|취소|UTC|GMT|PST|EST|미국|뉴욕|ignore|instruction|system|prompt|example|not\s+an?\s+event', source, re.I):
        return False
    # 인용에서 빠진 상대 날짜 문맥을 절대 날짜만으로 무시하지 않습니다.
    # 여러 일정이 섞인 공지에서는 보수적인 검토(헛멈춤)가 생길 수 있습니다.
    if RELATIVE_DAY.search(source) and not any(RELATIVE_DAY.search(q) for q in draft['evidence']):
        return False
    # 절대 날짜 인용을 택하더라도 다른 인용의 상대 날짜와 모순되면 자동 근거가 아닙니다.
    for quote in draft['evidence']:
        mentions = list(RELATIVE_DAY.finditer(quote))
        if mentions:
            if quote not in source or len(mentions) != 1 or UNSUPPORTED_RELATIVE.search(quote):
                return False
            relative_day = relative_date_from_quote(mentions[0].group(1), reference_date)
            if relative_day is None or any(draft.get(key) != relative_day for key in ('start_date', 'end_date')):
                return False
    for quote in draft['evidence']:
        if not quote or quote not in source or draft['title'] not in quote:
            continue
        date_matches = list(ABSOLUTE_DATE.finditer(quote))
        dates = []
        for match in date_matches:
            try:
                dates.append(date(*map(int, match.groups())).isoformat())
            except ValueError:
                continue
        if not date_matches:
            relative_day = _relative_evidence_date(draft, source, reference_date)
            if relative_day is not None and relative_date_from_quote(quote, reference_date) == relative_day:
                dates = [relative_day]
        wanted = [draft['start_date']] if draft['start_date'] == draft['end_date'] else [draft['start_date'], draft['end_date']]
        if dates != wanted:
            continue
        separator = r'\s*(?:[~∼〜～–—-]|부터)\s*'
        if len(date_matches) == 2 and not re.fullmatch(separator, quote[date_matches[0].end():date_matches[1].start()]):
            continue
        times = []
        time_matches = list(re.finditer(r'(?:(오전|오후)\s*)?(\d{1,2})(?::(\d{2})|시(?:\s*(\d{1,2})분)?)', quote))
        for m in time_matches:
            ampm, hour, minute, krminute = m.groups()
            h, minute = int(hour), int(minute or krminute or 0)
            if ampm:
                if not 1 <= h <= 12:
                    continue
                h = h % 12 + (12 if ampm == '오후' else 0)
            times.append(f'{h:02d}:{minute:02d}')
        if draft['all_day']:
            if not times and '종일' in quote:
                return True
        elif times == [draft['start_time'], draft['end_time']] and len(time_matches) == 2 and re.fullmatch(separator, quote[time_matches[0].end():time_matches[1].start()]):
            return True
    return False


def risks(draft, source, timezone='Asia/Seoul', manual=False, reference_date=None):
    result = []
    try:
        interval(draft, timezone)
    except ServiceError as exc:
        result.append({'code': exc.code, 'message': exc.message})
    if not manual:
        if not draft['evidence'] or any(not q or q not in source for q in draft['evidence']):
            result.append({'code': 'evidence_mismatch', 'message': '원문에서 인용 근거를 확인할 수 없습니다.'})
        if draft['ambiguities']:
            result.append({'code': 'ambiguity', 'message': 'AI가 모호함을 표시했습니다. 내용을 검토하세요.'})
        if not grounded(draft, source, reference_date):
            if UNSUPPORTED_RECURRENCE.search(source):
                message = '반복 일정은 자동 해석하지 않습니다. 표시된 단일 구간이 맞는지 검토하고 필요하면 수정하거나 반려하세요.'
            elif cross_day_timed(draft):
                message = '날짜를 넘는 시간 일정은 자동 해석하지 않습니다. 반복인지 연속 일정인지 시작·종료 일시를 확인하세요.'
            else:
                message = '지원하는 명시적 연도·날짜·시간과 제목의 결합을 확인하지 못했습니다. 원문과 비교하세요.'
            result.append({'code': 'unsupported_grounding', 'message': message})
    return result


def overlaps(draft, busy, timezone='Asia/Seoul'):
    start, end = interval(draft, timezone)
    result = []
    try:
        if not isinstance(busy, list):
            raise ValueError()
        for item in busy:
            if not isinstance(item, dict) or not isinstance(item.get('start'), str) or not isinstance(item.get('end'), str):
                raise ValueError()
            a, b = datetime.fromisoformat(item['start'].replace('Z', '+00:00')), datetime.fromisoformat(item['end'].replace('Z', '+00:00'))
            if a.tzinfo is None or b.tzinfo is None or b <= a:
                raise ValueError()
            if start < b and end > a:
                result.append({'start': a.astimezone(utc_timezone.utc).isoformat(), 'end': b.astimezone(utc_timezone.utc).isoformat()})
    except (KeyError, ValueError, TypeError):
        raise ServiceError('calendar_read', '가용 시간 조회 결과를 검증하지 못했습니다. 연결을 확인하세요.') from None
    return sorted(result, key=lambda x: (x['start'], x['end']))


def payload(draft, event_id, timezone='Asia/Seoul'):
    start, end = interval(draft, timezone)
    body = {'id': event_id, 'summary': draft['title'], 'location': draft['location'], 'reminders': {'useDefault': False}}
    if draft['all_day']:
        body.update(start={'date': start.date().isoformat()}, end={'date': end.date().isoformat()})
    else:
        body.update(start={'dateTime': start.isoformat(), 'timeZone': timezone}, end={'dateTime': end.isoformat(), 'timeZone': timezone})
    return body


def matches(expected, actual):
    if not isinstance(actual, dict) or actual.get('id') != expected['id'] or actual.get('status') == 'cancelled':
        return False
    if any(actual.get(k, '') != expected.get(k, '') for k in ('summary', 'location')):
        return False
    if actual.get('attendees') or actual.get('attachments') or actual.get('conferenceData') or actual.get('reminders') != expected['reminders']:
        return False
    try:
        for key in ('start', 'end'):
            a, b = expected[key], actual[key]
            if 'date' in a:
                if b.get('date') != a['date'] or 'dateTime' in b:
                    return False
            elif datetime.fromisoformat(a['dateTime'].replace('Z', '+00:00')) != datetime.fromisoformat(b['dateTime'].replace('Z', '+00:00')):
                return False
        return True
    except (KeyError, ValueError, TypeError):
        return False


def metrics(truth, predicted):
    if len(truth) != len(predicted) or not truth:
        raise ValueError('같은 길이의 비어 있지 않은 정답과 판정을 입력하세요.')
    return {'intervention_rate': sum(bool(x) for x in predicted) / len(predicted),
            'misses': sum(bool(t) and not p for t, p in zip(truth, predicted)),
            'false_stops': sum(not t and bool(p) for t, p in zip(truth, predicted))}
