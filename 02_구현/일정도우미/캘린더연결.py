"""공식 Google SDK 경계. import/생성 시 OAuth·네트워크를 실행하지 않습니다."""
from datetime import datetime, timedelta, timezone
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from .형식 import ServiceError

SCOPES = ['https://www.googleapis.com/auth/calendar.events.owned',
          'https://www.googleapis.com/auth/calendar.events.freebusy']

class GoogleCalendarAdapter:
    def __init__(self, settings, api=None):
        self.settings, self.api = settings, api

    def _refresh_credentials(self, credentials, path):
        import os
        import tempfile
        import httplib2
        from google_auth_httplib2 import Request
        transport = httplib2.Http(timeout=30)
        pending = None
        attempted = False
        try:
            request = Request(transport)
            def once(**kwargs):
                nonlocal attempted
                if attempted or kwargs.get('url') != 'https://oauth2.googleapis.com/token':
                    raise RuntimeError('허용한 Google 갱신 요청 한도를 초과했습니다.')
                attempted = True
                return request(**kwargs)
            credentials.refresh(once)
            if not credentials.valid:
                raise ValueError('갱신된 인증 자료가 유효하지 않습니다.')
            # 성공한 자료만 교체하며 실패 시 기존 갱신 권한을 지우지 않습니다.
            with tempfile.NamedTemporaryFile(prefix=path.name + '.갱신중_', dir=path.parent,
                                              mode='w', encoding='utf-8', delete=False) as stream:
                pending = stream.name
                stream.write(credentials.to_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
            pending = None
        except Exception:
            raise ServiceError('google_auth_refresh', '기존 Google 인증 갱신을 완료하지 못했습니다. 저장된 자료는 보존했습니다. 권한과 연결 상태를 확인하세요.') from None
        finally:
            if pending is not None:
                from pathlib import Path
                Path(pending).unlink(missing_ok=True)
            transport.close()

    def _api(self):
        if not self.settings.calendar_id.strip():
            raise ServiceError('calendar_target', '대상 캘린더 ID를 명시하세요. primary로 자동 등록하지 않습니다.')
        if self.api is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
            import httplib2
            path = self.settings.path(self.settings.token_file)
            if not path.is_file():
                raise ServiceError('google_auth', 'Google 연결 버튼으로 로그인하세요.')
            try:
                credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
                if not credentials.valid:
                    if not getattr(credentials, 'refresh_token', None):
                        raise ServiceError('google_auth_expired', 'Google 인증이 유효하지 않고 갱신 자료가 없습니다. 연결 버튼으로 다시 로그인하세요.')
                    self._refresh_credentials(credentials, path)
                self.api = build('calendar', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30), max_refresh_attempts=0),
                                 static_discovery=True, cache_discovery=False, num_retries=0)
            except ServiceError:
                raise
            except Exception:
                raise ServiceError('google_auth', 'Google 인증 자료를 읽지 못했습니다. 프로젝트 인증 파일을 확인하세요.') from None
        return self.api

    def _execute(self, request, operation):
        try:
            # googleapiclient의 execute 기본/자동 재시도에 의존하지 않습니다.
            return request.execute(num_retries=0)
        except HttpError as exc:
            if exc.resp.status in (401, 403):
                raise ServiceError('google_auth', 'Google 인증·권한·대상 캘린더를 확인한 뒤 다시 연결하세요.') from None
            if exc.resp.status == 404 and operation == 'get':
                raise ServiceError('event_not_found', '같은 ID의 일정을 아직 찾지 못했습니다. 새 일정을 생성하지 않고 나중에 다시 조회하세요.') from None
            raise ServiceError('calendar_' + operation, 'Google 요청을 확인하지 못했습니다. 연결 상태를 확인하세요.') from None
        except Exception:
            raise ServiceError('calendar_' + operation, 'Google 응답을 확인하지 못했습니다. 민감한 공급자 오류는 표시하지 않습니다.') from None

    def check_connection(self):
        start = datetime.now(timezone.utc)
        self.busy(start, start + timedelta(minutes=1))

    def busy(self, start, end):
        api = self._api()
        value = self._execute(api.freebusy().query(body={'timeMin': start.isoformat(), 'timeMax': end.isoformat(),
                                  'items': [{'id': self.settings.calendar_id}]}), 'read')
        try:
            if not isinstance(value, dict) or not isinstance(value.get('calendars'), dict):
                raise ValueError()
            calendar = value['calendars'][self.settings.calendar_id]
            if not isinstance(calendar, dict):
                raise ValueError()
            if value.get('errors') or calendar.get('errors') or not isinstance(calendar['busy'], list):
                raise ValueError()
            return calendar['busy']
        except (KeyError, TypeError, ValueError):
            raise ServiceError('calendar_read', '가용 시간 전체를 조회하지 못했습니다. 충돌 없음으로 처리하지 않습니다.') from None

    def create(self, body):
        api = self._api()
        return self._execute(api.events().insert(calendarId=self.settings.calendar_id, body=body, sendUpdates='none'), 'write')

    def get(self, event_id):
        api = self._api()
        return self._execute(api.events().get(calendarId=self.settings.calendar_id, eventId=event_id), 'get')

    def connect(self):
        from google_auth_oauthlib.flow import InstalledAppFlow
        path = self.settings.path(self.settings.client_file)
        if not path.is_file():
            raise ServiceError('google_client', '프로젝트의 Desktop OAuth 클라이언트 파일을 먼저 준비하세요.')
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
            # 로그인은 이 명시적 메서드에서만 실행합니다. 빈 캘린더 ID도 로그인 자체는 허용합니다.
            credentials = flow.run_local_server(host='localhost', port=0, timeout_seconds=120,
                authorization_prompt_message='', success_message='인증되었습니다. 앱으로 돌아가세요.', open_browser=True)
            token = self.settings.path(self.settings.token_file)
            token.parent.mkdir(parents=True, exist_ok=True)
            temporary = token.with_name(token.name + '.tmp')
            temporary.write_text(credentials.to_json(), encoding='utf-8')
            temporary.replace(token)
            self.api = None
            return {'connected': True, 'message': '인증 자료를 저장했습니다. 캘린더 접근은 분석 전에 별도로 검사합니다.'}
        except Exception:
            raise ServiceError('google_auth', 'Google 로그인을 완료하지 못했습니다. 클라이언트 유형·동의·시간 초과를 확인하세요.') from None

    def close(self):
        if self.api is not None and hasattr(self.api, 'close'):
            self.api.close()
