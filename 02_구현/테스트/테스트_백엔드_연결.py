import json
from datetime import datetime
from types import SimpleNamespace
from googleapiclient.discovery import build
from httplib2 import Response
from 테스트.테스트_백엔드_계약 import OfflineCase, draft
from 일정도우미.설정 import Settings
from 일정도우미.형식 import ServiceError
from 일정도우미.캘린더연결 import GoogleCalendarAdapter

class HttpTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        self.calls.append(dict(uri=uri, method=method, body=json.loads(body) if body else None))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        status, value = result if isinstance(result, tuple) else (200, result)
        return Response({'status': str(status), 'content-type': 'application/json'}), json.dumps(value).encode()
    def close(self):
        pass

class OpenAITransportTests(OfflineCase):
    def test_extract_and_rejudge_send_explicit_relative_date_instructions(self):
        import httpx2
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        calls = []
        candidate = draft(start_date='2026-11-11', end_date='2026-11-11', evidence=['내일 14:00~15:00 수업'])
        def response(request):
            data = json.loads(request.content)
            calls.append(data)
            value = {'events': [candidate]} if data['text']['format']['name']=='extract' else candidate
            return httpx2.Response(200, json={'id':'resp_fake','object':'response','created_at':0,
                'model':'gpt-5.6-luna','status':'completed','output':[{'type':'message','id':'msg_fake',
                'role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(value,ensure_ascii=False),'annotations':[]}]}]})
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(response), trust_env=False))
        self.addCleanup(client.close)
        store = Store(self.root / '상대날짜지시.sqlite')
        self.addCleanup(store.close)
        adapter = OpenAIAdapter(Settings(self.root,api_key='project-fake',llm_call_limit=2),store,client)
        adapter.extract('내일 14:00~15:00 수업','2026-11-10')
        adapter.rejudge('내일 14:00~15:00 수업','2026-11-10',candidate,'기준일로 확인하세요.',job_id='relative-case')
        self.assertEqual(len(calls),2)
        for data in calls:
            self.assertEqual(json.loads(data['input'])['reference_date'],'2026-11-10')
            self.assertEqual(json.loads(data['input'])['timezone'],'Asia/Seoul')
            self.assertIn('오늘·내일·모레',data['instructions'])
            self.assertIn('reference_date',data['instructions'])
            self.assertIn('0·1·2일',data['instructions'])
            self.assertIn('시작 날짜만',data['instructions'])
        self.assertEqual(store.calls_used(),2)

    def test_saved_synthetic_candidates_replay_through_the_installed_sdk(self):
        import httpx2
        from pathlib import Path
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        # 실제 저장 후보의 내용 재생입니다. 아래 SDK 봉투는 대역이며 원시 HTTP 기록이 아닙니다.
        dataset = json.loads((Path(__file__).resolve().parents[2] / '03_검증/평가자료.json').read_text(encoding='utf-8'))
        expected = [case['extracted_draft'] for case in dataset['cases']]
        response = {'id': 'resp_replay', 'object': 'response', 'created_at': 0,
                    'model': 'gpt-5.6-luna', 'status': 'completed', 'output': [
                        {'type': 'message', 'id': 'msg_replay', 'role': 'assistant', 'status': 'completed',
                         'content': [{'type': 'output_text', 'text': json.dumps({'events': expected}, ensure_ascii=False), 'annotations': []}]}]}
        calls = []
        def replay(request):
            calls.append(request)
            return httpx2.Response(200, json=response)
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(replay), trust_env=False))
        self.addCleanup(client.close)
        store = Store(self.root / '저장후보재생.sqlite')
        self.addCleanup(store.close)
        actual = OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client).extract(dataset['source_text'], dataset['reference_date'])
        self.assertEqual(len(actual), 5)
        self.assertEqual(actual, expected)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.calls_used(), 1)

    def test_null_sdk_response_containers_become_safe_model_output_errors(self):
        import httpx2
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        outputs = [None, [{'type': 'message', 'id': 'msg_fake', 'role': 'assistant',
                           'status': 'completed', 'content': None}]]
        for index, output in enumerate(outputs):
            with self.subTest(index=index):
                calls = []
                def response(request):
                    calls.append(request)
                    return httpx2.Response(200, json={'id': 'resp_fake', 'object': 'response',
                        'created_at': 0, 'model': 'gpt-5.6-luna', 'status': 'completed', 'output': output})
                client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(response), trust_env=False))
                self.addCleanup(client.close)
                store = Store(self.root / f'응답컨테이너_{index}.sqlite')
                self.addCleanup(store.close)
                caught = None
                try:
                    OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client).extract('합성 공지', '2026-11-10')
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ServiceError)
                self.assertEqual(caught.code, 'model_output')
                self.assertTrue(caught.__suppress_context__)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.calls_used(), 1)
        # 선택적 reasoning.content=null은 message.content=null과 구별합니다.
        valid_output = [{'type': 'reasoning', 'id': 'rs_fake', 'summary': [], 'content': None},
                        {'type': 'message', 'id': 'msg_fake', 'role': 'assistant', 'status': 'completed',
                         'content': [{'type': 'output_text', 'text': '{"events": []}', 'annotations': []}]}]
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={
            'id': 'resp_fake', 'object': 'response', 'created_at': 0, 'model': 'gpt-5.6-luna',
            'status': 'completed', 'output': valid_output})), trust_env=False))
        self.addCleanup(client.close)
        store = Store(self.root / '정상추론.sqlite')
        self.addCleanup(store.close)
        self.assertEqual(OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client).extract('일정 없는 공지', '2026-11-10'), [])
        self.assertEqual(store.calls_used(), 1)

    def test_malformed_http_error_metadata_stays_inside_the_public_error_boundary(self):
        import httpx2
        import traceback
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        cases = [(['private-marker'], 'model'), ({'private': 'marker'}, 'model'),
                 ('model_not_found', ['private-marker']), ('model_not_found', {'private': 'marker'})]
        for index, (code, parameter) in enumerate(cases):
            with self.subTest(index=index):
                calls = []
                def failure(request):
                    calls.append(request)
                    return httpx2.Response(400, json={'error': {'message': 'private-provider-message',
                        'type': 'invalid_request_error', 'code': code, 'param': parameter}})
                client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(failure), trust_env=False))
                self.addCleanup(client.close)
                store = Store(self.root / f'오류메타_{index}.sqlite')
                self.addCleanup(store.close)
                caught = None
                try:
                    OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client).extract('합성 공지', '2026-11-10')
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ServiceError)
                self.assertEqual(caught.code, 'model_transport')
                self.assertIn('HTTP 400', caught.message)
                self.assertTrue(caught.__suppress_context__)
                self.assertNotIn('private-provider-message', ''.join(traceback.format_exception(caught)))
                self.assertNotIn('private-marker', caught.message)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.calls_used(), 1)

    def test_sdk_http_failure_exposes_only_safe_status_code_and_parameter(self):
        import httpx2
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        requests = []
        def unavailable(request):
            requests.append(request)
            return httpx2.Response(404, json={'error': {'message': 'not-for-public-display',
                'type': 'invalid_request_error', 'code': 'model_not_found', 'param': 'model'}})
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(unavailable), trust_env=False))
        self.addCleanup(client.close)
        store = Store(self.root / '상태.sqlite')
        self.addCleanup(store.close)
        with self.assertRaises(ServiceError) as caught:
            OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client).extract('합성 공지', '2026-11-10')
        self.assertEqual(caught.exception.code, 'model_transport')
        self.assertIn('HTTP 404', caught.exception.message)
        self.assertIn('model_not_found', caught.exception.message)
        self.assertIn('model', caught.exception.message)
        self.assertNotIn('not-for-public-display', caught.exception.message)
        self.assertEqual(len(requests), 1)
        self.assertEqual(store.calls_used(), 1)

    def test_both_real_sdk_adapters_run_through_service_with_only_http_fakes(self):
        import httpx2
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        from 일정도우미.서비스 import CalendarAgentService
        (self.root / '비공개').mkdir()
        for name in ('구글_클라이언트.json', '구글_토큰.json'):
            (self.root / '비공개' / name).write_text('{}', encoding='utf-8')
        settings = Settings(self.root, api_key='project-fake', calendar_id='chosen', llm_call_limit=1)
        events = {'events': [draft()]}
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(200, json={'id': 'resp_fake', 'object': 'response', 'created_at': 0, 'model': 'gpt-5.6-luna', 'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_fake', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': json.dumps(events, ensure_ascii=False), 'annotations': []}]}]})), trust_env=False))
        class CalendarHttp:
            def __init__(self):
                self.calls = []
                self.event = None
            def request(self, uri, method='GET', body=None, **kwargs):
                self.calls.append((method, uri))
                if '/freeBusy' in uri:
                    value = {'calendars': {'chosen': {'busy': []}}}
                elif method == 'POST':
                    self.event = json.loads(body)
                    value = self.event
                else:
                    value = self.event
                return Response({'status': '200', 'content-type': 'application/json'}), json.dumps(value).encode()
            def close(self):
                pass
        transport = CalendarHttp()
        api = build('calendar', 'v3', http=transport, static_discovery=True, cache_discovery=False, num_retries=0)
        store = Store(self.root / '격리.sqlite')
        service = CalendarAgentService(settings, model=OpenAIAdapter(settings, store, client), calendar=GoogleCalendarAdapter(settings, api), store=store)
        self.addCleanup(service.close)
        jobs = service.analyse('2026년 11월 10일 14:00~15:00 수업', '2026-11-09')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'created')
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual([x[0] for x in transport.calls], ['POST', 'POST', 'POST', 'POST', 'GET'])
        self.assertEqual(transport.event['start']['dateTime'], '2026-11-10T14:00:00+09:00')
        self.assertEqual(service.readiness()['llm_calls_used'], 1)

    def test_real_sdk_server_failure_has_one_attempt_and_persistent_budget(self):
        import httpx2
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        calls = []
        def fail(request):
            calls.append(request)
            return httpx2.Response(500, json={'error': {'message': 'sensitive-body', 'type': 'server_error'}})
        client = make_client('project-fake', http_client=httpx2.Client(transport=httpx2.MockTransport(fail), trust_env=False))
        self.addCleanup(client.close)
        path = self.root / '실패장부.sqlite'
        store = Store(path)
        settings = Settings(self.root, api_key='project-fake', llm_call_limit=1)
        with self.assertRaises(ServiceError) as cm:
            OpenAIAdapter(settings, store, client).extract('공지', '2026-11-09')
        self.assertEqual(cm.exception.code, 'model_transport')
        self.assertEqual(len(calls), 1)
        store.close()
        store = Store(path)
        self.addCleanup(store.close)
        with self.assertRaises(ServiceError) as cm:
            OpenAIAdapter(settings, store, client).extract('공지', '2026-11-09')
        self.assertEqual(cm.exception.code, 'model_budget')
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.calls_used(), 1)

    def test_real_sdk_uses_only_project_credentials_not_ambient_headers(self):
        import os
        import httpx2
        from unittest.mock import patch
        from 일정도우미.저장소 import Store
        from 일정도우미.모델연결 import OpenAIAdapter, make_client
        captured = []
        def transport(request):
            captured.append(request)
            return httpx2.Response(200, json={'id': 'resp_fake', 'object': 'response', 'created_at': 0, 'model': 'gpt-5.6-luna', 'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_fake', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': '{"events": []}', 'annotations': []}]}]})
        http = httpx2.Client(transport=httpx2.MockTransport(transport), trust_env=False)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'forbidden-inherited', 'OPENAI_ADMIN_KEY': 'forbidden-admin', 'OPENAI_WEBHOOK_SECRET': 'forbidden-webhook', 'OPENAI_CUSTOM_HEADERS': 'Authorization: Bearer forbidden-header\nX-Api-Key: forbidden-secondary'}):
            client = make_client('project-fake', http_client=http)
        self.addCleanup(client.close)
        store = Store(self.root / '전송.sqlite')
        self.addCleanup(store.close)
        adapter = OpenAIAdapter(Settings(self.root, api_key='project-fake', llm_call_limit=1), store, client)
        self.assertEqual(adapter.extract('일정 없는 공지', '2026-11-09'), [])
        self.assertEqual(captured[0].headers['Authorization'], 'Bearer project-fake')
        self.assertNotIn('forbidden', str(captured[0].headers))
        self.assertEqual(client.admin_api_key, '')
        self.assertEqual(client.webhook_secret, '')
        data = json.loads(captured[0].content)
        self.assertEqual(data['text']['format']['type'], 'json_schema')
        self.assertEqual(data['reasoning'], {'effort': 'none'})
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(store.calls_used(), 1)

class GoogleTests(OfflineCase):
    def adapter(self, responses):
        http = HttpTransport(responses)
        api = build('calendar', 'v3', http=http, static_discovery=True, cache_discovery=False, num_retries=0)
        return GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen'), api=api), http

    def test_malformed_freebusy_containers_are_public_read_errors(self):
        values = [None, [], {}, {'calendars': None}, {'calendars': []},
                  {'calendars': {'chosen': None}}, {'calendars': {'chosen': []}},
                  {'calendars': {'chosen': {'busy': None}}}]
        for index, value in enumerate(values):
            with self.subTest(index=index):
                adapter, http = self.adapter([value])
                self.addCleanup(adapter.close)
                caught = None
                try:
                    adapter.busy(datetime.fromisoformat('2026-11-10T00:00:00+09:00'), datetime.fromisoformat('2026-11-11T00:00:00+09:00'))
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ServiceError)
                self.assertEqual(caught.code, 'calendar_read')
                self.assertTrue(caught.__suppress_context__)
                self.assertEqual(len(http.calls), 1)
                self.assertTrue(all('/freeBusy' in call['uri'] for call in http.calls))

    def test_partial_freebusy_and_http_auth_errors_are_safe_no_retry(self):
        for response, code in [({'calendars': {'chosen': {'errors': [{'reason': 'internalError'}], 'busy': []}}}, 'calendar_read'), ((401, {'error': {'message': 'sensitive'}}), 'google_auth'), ((503, {'error': {'message': 'sensitive'}}), 'calendar_read')]:
            with self.subTest(code=code):
                adapter, http = self.adapter([response])
                with self.assertRaises(ServiceError) as cm:
                    adapter.busy(datetime.fromisoformat('2026-11-10T00:00:00+09:00'), datetime.fromisoformat('2026-11-11T00:00:00+09:00'))
                self.assertEqual(cm.exception.code, code)
                self.assertNotIn('sensitive', cm.exception.message)
                self.assertEqual(len(http.calls), 1)

    def test_expired_invalid_auth_and_missing_target_do_not_open_oauth(self):
        from unittest.mock import patch
        (self.root / '비공개').mkdir()
        (self.root / '비공개' / '구글_토큰.json').write_text('{}', encoding='utf-8')
        with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file', side_effect=AssertionError('자동 로그인 금지')):
            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=SimpleNamespace(valid=False)):
                adapter = GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen'))
                with self.assertRaises(ServiceError) as cm:
                    adapter.check_connection()
                self.assertEqual(cm.exception.code, 'google_auth_expired')
            with self.assertRaises(ServiceError) as cm:
                GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen')).check_connection()
            self.assertEqual(cm.exception.code, 'google_auth')
            with self.assertRaises(ServiceError) as cm:
                GoogleCalendarAdapter(Settings(self.root)).check_connection()
            self.assertEqual(cm.exception.code, 'calendar_target')

    def test_explicit_oauth_uses_official_flow_and_owned_private_token(self):
        from unittest.mock import patch, Mock
        from 일정도우미.캘린더연결 import SCOPES
        (self.root / '비공개').mkdir()
        client = self.root / '비공개' / '구글_클라이언트.json'
        client.write_text('{"installed": {"client_id": "fake"}}', encoding='utf-8')
        flow = Mock()
        flow.run_local_server.return_value = SimpleNamespace(to_json=lambda: '{"token":"explicit-fake"}')
        with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file', return_value=flow) as create:
            adapter = GoogleCalendarAdapter(Settings(self.root))
            create.assert_not_called()
            self.assertTrue(adapter.connect()['connected'])
            self.assertEqual(create.call_args.kwargs['scopes'], ['https://www.googleapis.com/auth/calendar.events.owned', 'https://www.googleapis.com/auth/calendar.events.freebusy'])
            self.assertEqual(flow.run_local_server.call_args.kwargs['timeout_seconds'], 120)
            self.assertTrue(flow.run_local_server.call_args.kwargs['open_browser'])
            self.assertEqual(json.loads((self.root / '비공개' / '구글_토큰.json').read_text(encoding='utf-8')), {'token': 'explicit-fake'})
            self.assertFalse((self.root / '비공개' / '구글_토큰.json.tmp').exists())

    def test_expired_token_refreshes_once_and_persists_without_opening_login(self):
        from unittest.mock import patch, Mock
        from 일정도우미.캘린더연결 import SCOPES
        private = self.root / '비공개'
        private.mkdir()
        token = private / '구글_토큰.json'
        token.write_text(json.dumps({'token': 'old-fake', 'refresh_token': 'r-fake',
            'client_id': 'id-fake', 'client_secret': 's-fake',
            'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2020-01-01T00:00:00Z',
            'scopes': SCOPES}), encoding='utf-8')
        requests = []
        def token_request(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(status=200, headers={}, data=json.dumps({
                'access_token': 'new-fake', 'expires_in': 3600, 'token_type': 'Bearer'}).encode())
        with patch('google_auth_httplib2.Request', return_value=token_request), \
             patch('googleapiclient.discovery.build', return_value=Mock()), \
             patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file', side_effect=AssertionError('재로그인 금지')):
            adapter = GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen'))
            self.addCleanup(adapter.close)
            error = None
            try:
                adapter._api()
            except ServiceError as exc:
                error = exc.code
            self.assertIsNone(error, '갱신 권한이 있는 만료 토큰은 로그인 창 없이 갱신해야 합니다.')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['url'], 'https://oauth2.googleapis.com/token')
        saved = json.loads(token.read_text(encoding='utf-8'))
        self.assertEqual(saved['token'], 'new-fake')
        self.assertEqual(saved['refresh_token'], 'r-fake')
        self.assertEqual(saved['client_secret'], 's-fake')

    def test_authorized_http_disables_implicit_auth_retry(self):
        from unittest.mock import patch, Mock
        (self.root / '비공개').mkdir()
        (self.root / '비공개' / '구글_토큰.json').write_text('{}', encoding='utf-8')
        with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=SimpleNamespace(valid=True)), patch('google_auth_httplib2.AuthorizedHttp') as authorized, patch('googleapiclient.discovery.build', return_value=Mock()):
            adapter = GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen'))
            adapter._api()
            self.assertEqual(authorized.call_args.kwargs.get('max_refresh_attempts'), 0)

    def test_refresh_failure_has_one_http_attempt_and_preserves_the_previous_file(self):
        from unittest.mock import patch
        private = self.root / '비공개'
        private.mkdir()
        token = private / '구글_토큰.json'
        original = json.dumps({'token': 'old-fake', 'refresh_token': 'r-fake',
            'client_id': 'id-fake', 'client_secret': 's-fake',
            'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2020-01-01T00:00:00Z'}).encode()
        for status in (400, 500):
            with self.subTest(status=status):
                token.write_bytes(original)
                calls = []
                def denied(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(status=status, headers={}, data=b'{"error":"invalid_grant","error_description":"not-for-public-display"}')
                with patch('google_auth_httplib2.Request', return_value=denied), \
                     patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file', side_effect=AssertionError('자동 로그인 금지')):
                    with self.assertRaises(ServiceError) as caught:
                        GoogleCalendarAdapter(Settings(self.root, calendar_id='chosen'))._api()
                self.assertEqual(caught.exception.code, 'google_auth_refresh')
                self.assertNotIn('not-for-public-display', caught.exception.message)
                self.assertEqual(len(calls), 1)
                self.assertEqual(token.read_bytes(), original)
                self.assertEqual(list(private.glob('*.갱신중_*')), [])

    def test_official_resource_freebusy_and_create_payload(self):
        from 일정도우미.규칙 import payload
        body = payload(draft(all_day=True, start_time=None, end_time=None, end_date='2026-11-12'), 'a' * 32)
        adapter, http = self.adapter([{'calendars': {'chosen': {'busy': [{'start': '2026-11-10T01:00:00Z', 'end': '2026-11-10T02:00:00Z'}]}}}, body, body])
        busy = adapter.busy(datetime.fromisoformat('2026-11-10T00:00:00+09:00'), datetime.fromisoformat('2026-11-13T00:00:00+09:00'))
        self.assertEqual(len(busy), 1)
        self.assertEqual(http.calls[0]['body']['items'], [{'id': 'chosen'}])
        self.assertEqual(http.calls[0]['body']['timeMax'], '2026-11-13T00:00:00+09:00')
        self.assertEqual(body['end'], {'date': '2026-11-13'})
        adapter.create(body)
        self.assertIn('sendUpdates=none', http.calls[1]['uri'])
        self.assertFalse(http.calls[1]['body']['reminders']['useDefault'])
        for key in ('attendees', 'attachments', 'conferenceData'):
            self.assertNotIn(key, http.calls[1]['body'])
        self.assertEqual(adapter.get('a' * 32), body)
        self.assertEqual(http.calls[2]['method'], 'GET')
