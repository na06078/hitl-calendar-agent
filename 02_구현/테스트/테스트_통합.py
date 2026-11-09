"""실제 화면→서비스→SQLite/LangGraph를 검사합니다. 외부 제공자만 대역이며 실연동은 아닙니다."""
from copy import deepcopy
from datetime import date, time
import logging
import inspect
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from streamlit.testing.v1 import AppTest
import 테스트.테스트_백엔드_계약 as backend_harness
import 테스트.테스트_화면 as screen_harness
from 테스트.테스트_백엔드_서비스 import FakeResponses, FakeCalendar
from 일정도우미.설정 import Settings
from 일정도우미.형식 import ServiceError
from 일정도우미.저장소 import Store
from 일정도우미.모델연결 import OpenAIAdapter
from 일정도우미.서비스 import CalendarAgentService, build_service

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "02_구현/앱.py"
SOURCE = "2026년 11월 10일 14:00~15:00 수업"
DRAFT = {
    "title": "수업", "start_date": "2026-11-10", "end_date": "2026-11-10",
    "start_time": "14:00", "end_time": "15:00", "all_day": False,
    "location": "온라인", "evidence": [SOURCE], "ambiguities": [],
}


class ConsumerIntegrationTests(backend_harness.OfflineCase):
    def setUp(self):
        original_connect = socket.socket.connect
        socketpair_code = getattr(socket.socketpair, "__code__", None)
        with patch.object(backend_harness, "FIXTURES", ROOT / ".local/메인통합"):
            super().setUp()
        self.owned_ipc_connections = 0
        def guarded_connect(sock, address):
            caller = inspect.currentframe().f_back
            # Windows asyncio의 socketpair는 사설 loopback 쌍을 만듭니다.
            # 확인한 stdlib 코드의 자기 소유 csock→lsock 연결만 허용하고 제공자 접속은 차단합니다.
            if (socketpair_code is not None and caller.f_code is socketpair_code
                    and caller.f_locals.get("csock") is sock
                    and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")
                    and caller.f_locals["lsock"].getsockname()[:2] == address[:2]):
                self.owned_ipc_connections += 1
                return original_connect(sock, address)
            self.network_attempts.append("blocked_provider")
            raise AssertionError("실제 제공자 네트워크 금지")
        ipc_guard = patch("socket.socket.connect", new=guarded_connect)
        ipc_guard.start()
        self.addCleanup(ipc_guard.stop)
        logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
        bare_notice = lambda record: "missing ScriptRunContext" not in record.getMessage()
        logger.addFilter(bare_notice)
        self.addCleanup(logger.removeFilter, bare_notice)
        screen_harness.ScreenTests.clear_cache(self)
        self.addCleanup(screen_harness.ScreenTests.clear_cache, self)
        private = self.root / "비공개"
        private.mkdir()
        for name in ("구글_클라이언트.json", "구글_토큰.json"):
            (private / name).write_text("{}", encoding="utf-8")
        settings = Settings(self.root, api_key="integration-fake", calendar_id="test-calendar", llm_call_limit=5)
        (self.root / "data").mkdir()
        store = Store(self.root / "data/업무.sqlite")
        self.responses = FakeResponses()
        self.calendar = FakeCalendar()
        model = OpenAIAdapter(settings, store, SimpleNamespace(responses=self.responses))
        self.service = CalendarAgentService(settings, model=model, calendar=self.calendar, store=store)
        self.addCleanup(self.service.close)
        # 생산 factory의 외부 경계/실제 .env 대신 격리된 동일 서비스 인스턴스만 주입합니다.
        factory = patch("일정도우미.서비스.build_service", return_value=self.service)
        factory.start()
        self.addCleanup(factory.stop)

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assertFalse(app.exception, str(app.exception))
        return app

    def submit(self, app, value=None, source=SOURCE):
        self.responses.results.append({"events": [deepcopy(value or DRAFT)]})
        app.text_area(key="notice_text").input(source)
        app.date_input(key="notice_reference_date").set_value(date(2026, 11, 9))
        app.button(key="analyse_notice").click().run()
        self.assertFalse(app.exception, str(app.exception))
        jobs = self.service.list_jobs()
        self.assertEqual(len(jobs), 1)
        return jobs[0]

    def test_real_graph_review_edit_and_readback_through_the_screen(self):
        self.calendar.intervals = [{"start": "2026-11-10T14:30:00+09:00", "end": "2026-11-10T15:00:00+09:00"}]
        app = self.app()
        job = self.submit(app)
        self.assertEqual(job["status"], "needs_review")
        self.assertEqual(self.calendar.writes, [])
        suffix = job["id"] + "_" + str(job["version"])
        app.time_input(key="edit_start_time_" + suffix).set_value(time(16, 0))
        app.time_input(key="edit_end_time_" + suffix).set_value(time(17, 0))
        app.button(key="edit_approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        actual = self.service.get_job(job["id"])
        self.assertEqual(actual["status"], "created")
        self.assertEqual(actual["source_text"], SOURCE)
        self.assertEqual(actual["decisions"][0]["action"], "edit_approve")
        self.assertEqual(self.calendar.writes[0]["start"]["dateTime"], "2026-11-10T16:00:00+09:00")
        self.assertEqual(self.calendar.writes[0]["end"]["dateTime"], "2026-11-10T17:00:00+09:00")
        self.assertEqual(self.calendar.reads, [actual["google_event_id"]])
        app.run()
        self.assertEqual(len(self.calendar.writes), 1)
        self.assertEqual(len(self.responses.calls), 1)
        self.assertTrue(any("수업 — 등록 완료" in item.value for item in app.markdown))

    def test_regular_loopback_connection_is_still_blocked(self):
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, "실제 제공자"):
                connection.connect(("127.0.0.1", 1))
        self.assertEqual(self.network_attempts, ["blocked_provider"])
        # 이 시험이 의도적으로 발생시킨 한 번의 거절만 구분합니다. 다른 시도는 숨기지 않습니다.
        self.network_attempts.pop()


    def test_real_calendar_error_dto_displays_only_the_public_message(self):
        message = "충돌 조회에 실패했습니다. Google 연결을 다시 확인해 주세요."
        self.calendar.read_error = ServiceError("calendar_read", message)
        app = self.app()
        job = self.submit(app)
        self.assertEqual(job["status"], "needs_review")
        self.assertEqual(job["last_error"], {"code": "calendar_read", "message": message})
        self.assertEqual([item.value for item in app.error], [message])
        self.assertEqual(self.calendar.writes, [])


    def test_invalid_timezone_reaches_the_public_configuration_error_boundary(self):
        invalid_root = self.root / "잘못된설정"
        invalid_root.mkdir()
        (invalid_root / ".env").write_text("APP_TIMEZONE=Missing/Timezone\n", encoding="utf-8")
        caught = None
        try:
            unexpected = build_service(invalid_root)
        except Exception as error:
            caught = error
        else:
            unexpected.close()
        self.assertIsInstance(caught, ServiceError)
        self.assertEqual(caught.code, "configuration")
        self.assertIn("APP_TIMEZONE", caught.message)
        self.assertFalse((invalid_root / "data").exists())
        self.assertEqual(self.responses.calls, [])
        self.assertEqual(self.calendar.writes, [])


    def test_daily_recurrence_is_paused_instead_of_created_as_one_long_event(self):
        source = "2026년 11월 10일~2026년 11월 12일 매일 14:00~15:00 수업"
        value = deepcopy(DRAFT)
        value.update(end_date="2026-11-12", evidence=[source])
        app = self.app()
        job = self.submit(app, value, source)
        self.assertEqual(job["status"], "needs_review")
        self.assertIn("unsupported_grounding", [item["code"] for item in job["risk_reasons"]])
        self.assertEqual(self.calendar.writes, [])
        self.assertTrue(any("반복" in item.value for item in app.warning))
        self.assertTrue(self.service.graph.get_state({"configurable": {"thread_id": job["id"]}}).interrupts)


    def test_explicit_recurrence_variants_require_review(self):
        from 일정도우미.규칙 import grounded, risks
        for phrase in ("매일", "매 주", "매월", "매달", "매년", "격일", "격주", "주마다", "날마다", "반복", "daily", "every day"):
            with self.subTest(phrase=phrase):
                source = f"2026년 11월 10일 {phrase} 14:00~15:00 수업"
                value = deepcopy(DRAFT)
                value.update(evidence=[source])
                self.assertFalse(grounded(value, source))
                self.assertIn("unsupported_grounding", [item["code"] for item in risks(value, source)])
        self.assertEqual(self.responses.calls, [])
        self.assertEqual(self.calendar.writes, [])


    def test_cross_day_timed_candidate_is_not_automatic_without_explicit_repeat_words(self):
        source = "2026년 11월 10일~2026년 11월 12일 14:00~15:00 수업"
        value = deepcopy(DRAFT)
        value.update(end_date="2026-11-12", evidence=[source])
        app = self.app()
        job = self.submit(app, value, source)
        self.assertEqual(job["status"], "needs_review")
        self.assertEqual(self.calendar.writes, [])


if __name__ == "__main__":
    unittest.main()
