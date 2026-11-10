"""공개 동기 서비스. 단일 사용자·단일 프로세스의 SQLite/그래프를 직렬화합니다."""
import copy
import re
import sqlite3
from datetime import date
from pathlib import Path
from threading import RLock
from uuid import uuid4
from pydantic import ValidationError
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from .형식 import ServiceError, EventDraft, Extraction, EDIT_FIELDS
from .설정 import Settings, TimezoneSettingError
from .저장소 import Store, now
from .모델연결 import OpenAIAdapter
from .캘린더연결 import GoogleCalendarAdapter
from .그래프 import build_graph
from .규칙 import risks, interval, overlaps, payload, matches, canonical_hash, resolve_relative_dates

VIEW_FIELDS = ('id', 'title', 'status', 'version', 'source_text', 'reference_date', 'draft', 'risk_reasons',
               'busy_intervals', 'last_error', 'decisions', 'google_event_id', 'google_event_url',
               'created_at', 'updated_at', 'rejudge_count')

class CalendarAgentService:
    def __init__(self, settings: Settings, model=None, calendar=None, store=None):
        self.settings = settings
        self.lock = store.lock if store is not None else RLock()
        self.closed = False
        data = settings.path(settings.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        self.store = store or Store(settings.path(str(Path(settings.data_dir) / '업무.sqlite')), self.lock)
        self.model = model or OpenAIAdapter(settings, self.store)
        self.calendar = calendar or GoogleCalendarAdapter(settings)
        self.checkpoint_connection = sqlite3.connect(str(settings.path(str(Path(settings.data_dir) / '체크포인트.sqlite'))), check_same_thread=False)
        self.saver = SqliteSaver(self.checkpoint_connection)
        self.graph = build_graph(self.saver, self._assess, self._apply, self._execute, self.get_job)
        with self.lock:
            # 업무 트랜잭션과 체크포인트는 별도 DB입니다. 중간 종료/체크포인트 유실 시
            # 재추출·자동 재전송 대신 네트워크 없는 새 검토 중단점으로 복구합니다.
            for job in self.store.list_jobs():
                if job['status'] != 'needs_review':
                    continue
                config = {'configurable': {'thread_id': job['id']}}
                if self.graph.get_state(config).interrupts:
                    continue
                job['approved_hash'], job['approved_busy'] = None, []
                job['risk_reasons'].append({'code': 'recovery_review', 'message': '중단점을 복구했습니다. 전송 전 내용을 다시 검토하세요.'})
                self._save(job, bump=True)
                self.graph.update_state(config, {'job_id': job['id'], 'route': 'review'}, as_node='assess')
                self._invoke(None, config)

    def _invoke(self, value, config):
        from langsmith import tracing_context
        # 프로젝트에 승인하지 않은 상속 LangSmith 전송을 허용하지 않습니다.
        with tracing_context(enabled=False):
            return self.graph.invoke(value, config)

    def _open(self):
        if self.closed:
            raise ServiceError('service_closed', '종료된 서비스입니다. 앱을 다시 여세요.')

    def _save(self, job, bump=False, expected_version=None):
        if bump:
            job['version'] += 1
        job['title'] = job['draft']['title']
        job['updated_at'] = now()
        if expected_version is None:
            self.store.save_job(job)
        else:
            self.store.save_job(job, expected_version=expected_version)

    def _view(self, job):
        return copy.deepcopy({key: job[key] for key in VIEW_FIELDS})

    def readiness(self) -> dict:
        with self.lock:
            self._open()
            s = self.settings
            used = self.store.calls_used()
            client, token = s.path(s.client_file).is_file(), s.path(s.token_file).is_file()
            issues = []
            if not s.api_key.strip():
                issues.append('프로젝트 .env에 OpenAI 키가 필요합니다.')
            if not client:
                issues.append('과제용 Google Desktop OAuth 파일이 필요합니다.')
            if not token:
                issues.append('Google 연결 버튼으로 인증 자료를 준비하세요.')
            if not s.calendar_id.strip():
                issues.append('대상 캘린더 ID를 명시하세요.')
            if s.llm_call_limit is not None and used >= s.llm_call_limit:
                issues.append('승인된 모델 호출 잔여 횟수가 없습니다.')
            return dict(model=s.model, api_key_set=bool(s.api_key.strip()), client_file_present=client,
                        token_file_present=token, target_calendar_set=bool(s.calendar_id.strip()),
                        llm_calls_used=used, llm_call_limit=s.llm_call_limit, can_analyse=not issues, issues=issues)

    def _preflight(self, model=False):
        if not self.settings.calendar_id.strip():
            raise ServiceError('calendar_target', '대상 캘린더 ID를 먼저 설정하세요.')
        if model:
            ready = self.readiness()
            if not ready['can_analyse']:
                raise ServiceError('not_ready', ' '.join(ready['issues']))
        self.calendar.check_connection()

    def analyse(self, text: str, reference_date: str) -> list[dict]:
        with self.lock:
            self._open()
            if not isinstance(text, str) or not text.strip() or len(text) > self.settings.max_input_chars:
                raise ServiceError('invalid_input', '비어 있지 않은 상한 이내의 공지를 입력하세요. 입력을 자르지 않습니다.')
            try:
                if not isinstance(reference_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', reference_date):
                    raise ValueError()
                date.fromisoformat(reference_date)
            except ValueError:
                raise ServiceError('invalid_reference_date', '기준 날짜를 YYYY-MM-DD로 지정하세요.') from None
            self._preflight(model=True)
            values = self.model.extract(text, reference_date)
            try:
                drafts = Extraction.model_validate({'events': values}).model_dump()['events']
            except ValidationError:
                raise ServiceError('model_output', '모델 후보 형식 또는 최대 5개 제한을 확인하세요.') from None
            jobs = []
            for draft in drafts:
                stamp = now()
                jobs.append(dict(id=uuid4().hex, title=draft['title'], status='needs_review', version=1,
                    source_text=text, reference_date=reference_date, draft=draft, risk_reasons=[], busy_intervals=[],
                    last_error=None, decisions=[], google_event_id=None, google_event_url=None,
                    created_at=stamp, updated_at=stamp, rejudge_count=0, manual=False,
                    stable_event_id=uuid4().hex, approved_hash=None, approved_busy=[],
                    calendar_id=self.settings.calendar_id, timezone=self.settings.timezone))
            self.store.add_jobs(jobs)
            for job in jobs:
                self._invoke({'job_id': job['id']}, {'configurable': {'thread_id': job['id']}})
            return [self.get_job(job['id']) for job in jobs]

    def list_jobs(self) -> list[dict]:
        with self.lock:
            self._open()
            return [self._view(job) for job in self.store.list_jobs()]

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            self._open()
            return self._view(self.store.get_job(job_id))

    def resolve_pending_date(self, job_id: str, expected_version: int) -> dict:
        with self.lock:
            self._open()
            job = self._checked_job(job_id, expected_version)
            if job['status'] != 'needs_review' or job['manual'] or self.store.send_record(job_id) is not None:
                raise ServiceError('invalid_state', '전송되지 않은 원문 추출 대기 건에서만 누락 날짜를 보완할 수 있습니다.')
            config = {'configurable': {'thread_id': job_id}}
            if not self.graph.get_state(config).interrupts:
                raise ServiceError('graph_not_paused', '검토 중단점을 확인한 뒤 날짜를 보완하세요.')
            revised = resolve_relative_dates(job['draft'], job['source_text'], job['reference_date'])
            if revised == job['draft']:
                return self._view(job)
            previous = {key: job['draft'][key] for key in ('start_date','end_date')}
            job['draft'] = revised
            job['approved_hash'], job['approved_busy'], job['busy_intervals'] = None, [], []
            job['risk_reasons'] = risks(revised, job['source_text'], self.settings.timezone,
                                        reference_date=job['reference_date'])
            job['risk_reasons'].append({'code':'relative_date_normalized',
                'message':'공지 기준 날짜로 날짜를 보완했습니다. 남은 정보와 원문을 확인한 뒤 결정하세요.'})
            job['decisions'].append({'action':'relative_date_normalized','reference_date':job['reference_date'],
                'previous_dates':previous,'resolved_dates':{key:revised[key] for key in previous},'at':now()})
            # 이 경로는 날짜만 정정하고 멈춘 그래프를 실행하지 않습니다. 사용자 승인도 새로 필요합니다.
            self._save(job, bump=True, expected_version=expected_version)
            return self._view(job)

    def _assess(self, job_id):
        job = self.store.get_job(job_id)
        if not job['manual']:
            job['draft'] = resolve_relative_dates(job['draft'], job['source_text'], job['reference_date'])
        job['risk_reasons'] = risks(job['draft'], job['source_text'], self.settings.timezone, job['manual'],
                                    reference_date=job['reference_date'])
        job['busy_intervals'] = []
        if not any(x['code'] == 'invalid_datetime' for x in job['risk_reasons']):
            try:
                start, end = interval(job['draft'], self.settings.timezone)
                job['busy_intervals'] = overlaps(job['draft'], self.calendar.busy(start, end), self.settings.timezone)
                if job['busy_intervals']:
                    job['risk_reasons'].append({'code': 'busy_overlap', 'message': '기존 일정과 시간이 겹칩니다. 충돌 구간을 확인하세요.'})
            except ServiceError as exc:
                job['last_error'] = {'code': exc.code, 'message': exc.message}
                job['risk_reasons'].append({'code': 'calendar_read', 'message': '충돌 여부를 확인하지 못했습니다. 연결을 확인하세요.'})
        job['status'] = 'needs_review'
        self._save(job)
        return 'review' if job['risk_reasons'] else 'execute'

    def _checked_job(self, job_id, expected_version):
        job = self.store.get_job(job_id)
        if type(expected_version) is not int or expected_version != job['version']:
            raise ServiceError('stale_version', '화면 버전이 오래되었습니다. 최신 내용을 다시 확인하세요.')
        if job['calendar_id'] != self.settings.calendar_id or job['timezone'] != self.settings.timezone:
            raise ServiceError('configuration_changed', '이 작업을 분석할 때의 대상 캘린더·시간대와 현재 설정이 다릅니다. 원래 설정을 복원한 뒤 결정·조회하세요.')
        return job

    def decide(self, job_id: str, expected_version: int, action: str, edits: dict | None = None, reason: str = '', instruction: str = '') -> dict:
        with self.lock:
            self._open()
            job = self._checked_job(job_id, expected_version)
            if job['status'] != 'needs_review':
                raise ServiceError('invalid_state', '대기 작업에만 결정할 수 있습니다. 완료·결과 불명 작업은 다시 보내지 않습니다.')
            if action not in ('approve', 'edit_approve', 'reject', 'rejudge'):
                raise ServiceError('invalid_action', '지원하지 않는 결정입니다.')
            if action == 'reject' and (not isinstance(reason, str) or not reason.strip()):
                raise ServiceError('reason_required', '반려 사유를 입력하세요.')
            candidate = copy.deepcopy(job['draft'])
            if action == 'edit_approve':
                if not isinstance(edits, dict) or not edits or not set(edits).issubset(EDIT_FIELDS):
                    raise ServiceError('invalid_edits', '수정 가능한 일정 필드만 입력하세요.')
                try:
                    candidate = EventDraft.model_validate({**candidate, **edits}).model_dump()
                except ValidationError:
                    raise ServiceError('invalid_edits', '수정 값의 형식을 확인하세요.') from None
                if candidate == job['draft']:
                    raise ServiceError('no_op', '달라진 수정 값이 없습니다.')
            elif edits is not None:
                raise ServiceError('invalid_edits', '수정 후 승인에서만 edits를 사용하세요.')
            if action in ('approve', 'edit_approve'):
                interval(candidate, self.settings.timezone)
            config = {'configurable': {'thread_id': job_id}}
            if not self.graph.get_state(config).interrupts:
                raise ServiceError('graph_not_paused', '검토 중단점을 확인하지 못했습니다. 실행 상태를 확인하세요.')
            decision = {'action': action, 'reason': reason, 'expected_version': expected_version}
            if action in ('approve', 'edit_approve'):
                self._preflight()
                start, end = interval(candidate, self.settings.timezone)
                busy = overlaps(candidate, self.calendar.busy(start, end), self.settings.timezone)
                if any(item not in job['busy_intervals'] for item in busy):
                    if action == 'edit_approve':
                        job['decisions'].append(dict(action='edit_pending_review', edits=copy.deepcopy(edits), previous_draft=job['draft'], at=now()))
                        job['draft'], job['manual'] = candidate, True
                    job['busy_intervals'] = busy
                    job['risk_reasons'] = risks(candidate, job['source_text'], self.settings.timezone, job['manual'],
                                                reference_date=job['reference_date'])
                    job['risk_reasons'].append({'code': 'new_conflict', 'message': '새 충돌이 생겼습니다. 최신 구간을 확인하고 다시 결정하세요.'})
                    self._save(job, bump=True)
                    return self.get_job(job_id)
                decision.update(approved_hash=canonical_hash(candidate), approved_busy=busy)
                if action == 'edit_approve':
                    decision.update(draft=candidate, edits=copy.deepcopy(edits), previous_draft=job['draft'])
            if action == 'rejudge':
                if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > self.settings.max_input_chars:
                    raise ServiceError('instruction_required', '상한 이내의 재판정 지시를 입력하세요.')
                count = self.store.calls_used('rejudge:' + job_id)
                if max(count, job['rejudge_count']) >= self.settings.max_rejudgements:
                    raise ServiceError('rejudge_limit', '이 작업의 재판정 상한에 도달했습니다. 직접 수정하거나 반려하세요.')
                self._preflight(model=True)
                try:
                    revised = self.model.rejudge(job['source_text'], job['reference_date'], job['draft'], instruction, job_id=job_id)
                    revised = EventDraft.model_validate(revised).model_dump()
                except (ServiceError, ValidationError) as exc:
                    attempts = self.store.calls_used('rejudge:' + job_id)
                    if attempts > count:
                        job['rejudge_count'] = attempts
                        job['last_error'] = {'code': 'rejudge_failed', 'message': '재판정 응답을 확인하지 못했습니다. 시도 횟수와 예산은 소비되었습니다.'}
                        self._save(job, bump=True)
                    if isinstance(exc, ServiceError):
                        raise
                    raise ServiceError('model_output', '재판정 형식을 검증하지 못했습니다.') from None
                decision.update(draft=revised, instruction=instruction, previous_draft=job['draft'],
                                rejudge_count=max(count + 1, self.store.calls_used('rejudge:' + job_id)))
            self._invoke(Command(resume=decision), config)
            return self.get_job(job_id)

    def _apply(self, job_id, decision):
        job = self.store.get_job(job_id)
        if job['version'] != decision['expected_version']:
            return 'end' if job['status'] == 'rejected' else 'execute'
        if decision['action'] == 'rejudge':
            job.update(draft=decision['draft'], manual=False, approved_hash=None, approved_busy=[],
                       rejudge_count=decision['rejudge_count'], last_error=None)
        if decision['action'] == 'edit_approve':
            job['draft'], job['manual'] = decision['draft'], True
            job['risk_reasons'] = risks(job['draft'], job['source_text'], self.settings.timezone, True,
                                        reference_date=job['reference_date'])
        if decision['action'] in ('approve', 'edit_approve'):
            job['approved_hash'] = decision['approved_hash']
            job['approved_busy'] = decision['approved_busy']
        job['decisions'].append(dict(decision, at=now(), draft_hash=canonical_hash(job['draft'])))
        job['status'] = 'rejected' if decision['action'] == 'reject' else 'needs_review'
        self._save(job, bump=True)
        if decision['action'] == 'rejudge':
            return 'assess'
        return 'end' if decision['action'] == 'reject' else 'execute'

    def _execute(self, job_id):
        job = self.store.get_job(job_id)
        if job['status'] in ('created', 'rejected', 'unknown') or self.store.send_record(job_id):
            return 'end'
        current_hash = canonical_hash(job['draft'])
        if job['risk_reasons'] and job['approved_hash'] != current_hash:
            return 'review'
        try:
            start, end = interval(job['draft'], self.settings.timezone)
            busy = overlaps(job['draft'], self.calendar.busy(start, end), self.settings.timezone)
            reviewed = job['approved_busy'] if job['approved_hash'] == current_hash else []
            if any(item not in reviewed for item in busy):
                job['busy_intervals'] = busy
                job['risk_reasons'] = [{'code': 'new_conflict', 'message': '마지막 확인 이후 새 충돌이 생겼습니다. 새 구간을 검토하고 다시 결정하세요.'}]
                job['approved_hash'] = None
                self._save(job, bump=True)
                return 'review'
        except ServiceError as exc:
            job['last_error'] = {'code': exc.code, 'message': exc.message}
            job['risk_reasons'] = [{'code': 'calendar_read', 'message': '등록 직전 충돌 조회에 실패했습니다. 연결을 확인하세요.'}]
            job['approved_hash'] = None
            self._save(job, bump=True)
            return 'review'
        body = payload(job['draft'], job['stable_event_id'], self.settings.timezone)
        job['status'] = 'unknown'
        job['google_event_id'] = job['stable_event_id']
        job['version'] += 1
        job['updated_at'] = now()
        # 장부와 unknown을 같은 트랜잭션으로 먼저 영속화합니다. 응답 유실 후 재전송하지 않습니다.
        if not self.store.begin_create(job, body, canonical_hash(body)):
            return 'end'
        try:
            self.calendar.create(body)
            actual = self.calendar.get(body['id'])
            self._confirm(job, body, actual)
        except ServiceError as exc:
            job['last_error'] = {'code': exc.code, 'message': exc.message}
        except Exception:
            job['last_error'] = {'code': 'calendar_unknown', 'message': '등록 응답을 확인하지 못했습니다. 새 등록 대신 같은 ID의 결과 확인을 사용하세요.'}
        job['updated_at'] = now()
        self.store.finish_create(job)
        return 'end'

    def _confirm(self, job, body, actual):
        if not matches(body, actual):
            job['last_error'] = {'code': 'readback_mismatch', 'message': '같은 ID의 일정이 승인 내용과 일치하는지 확인하지 못했습니다. 다시 조회하세요.'}
            return
        job['status'] = 'created'
        job['last_error'] = None
        from urllib.parse import urlparse
        url = actual.get('htmlLink')
        if isinstance(url, str) and urlparse(url).scheme == 'https' and urlparse(url).hostname in ('calendar.google.com', 'www.google.com'):
            job['google_event_url'] = url

    def reconcile(self, job_id: str, expected_version: int) -> dict:
        with self.lock:
            self._open()
            job = self._checked_job(job_id, expected_version)
            record = self.store.send_record(job_id)
            if job['status'] != 'unknown' or record is None:
                raise ServiceError('invalid_state', '결과 불명인 전송 장부만 같은 ID로 재조회할 수 있습니다.')
            if canonical_hash(record['payload']) != record['payload_hash'] or record['payload']['id'] != job['google_event_id']:
                raise ServiceError('payload_changed', '전송 장부의 무결성을 확인하지 못했습니다. 새 일정을 생성하지 않습니다.')
            try:
                self._confirm(job, record['payload'], self.calendar.get(job['google_event_id']))
            except ServiceError as exc:
                job['last_error'] = {'code': exc.code, 'message': exc.message}
            except Exception:
                job['last_error'] = {'code': 'calendar_get', 'message': '같은 ID 조회를 완료하지 못했습니다. 나중에 다시 확인하세요.'}
            job['version'] += 1
            job['updated_at'] = now()
            self.store.finish_create(job)
            return self.get_job(job_id)

    def connect_google(self) -> dict:
        with self.lock:
            self._open()
            return self.calendar.connect()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.model.close()
                self.calendar.close()
            finally:
                self.checkpoint_connection.close()
                self.store.close()


def build_service(project_root: Path) -> CalendarAgentService:
    try:
        return CalendarAgentService(Settings.load(project_root))
    except TimezoneSettingError:
        raise ServiceError('configuration', '프로젝트 .env의 APP_TIMEZONE을 지원되는 시간대 이름으로 설정하세요. 예: Asia/Seoul') from None
    except (ValueError, OSError):
        raise ServiceError('configuration', '프로젝트 설정·경로·모델·상한을 확인하세요.') from None
