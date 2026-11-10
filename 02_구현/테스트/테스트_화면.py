"""화면의 공개 서비스 호출을 검사합니다. 모든 데이터·외부 서비스는 명시적 대역입니다."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, time
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "02_구현" / "앱.py"
# AppTest는 실제 웹 서버 없이 실행되므로 bare-context 안내만 낮춥니다.
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)


class FakeServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def synthetic_job(status: str = "needs_review") -> dict:
    return {
        "id": "synthetic-job-1", "title": "발표 준비", "status": status,
        "version": 1, "source_text": "합성 공지: 2030년 1월 10일 14:00~15:00 발표 준비",
        "reference_date": "2030-01-01",
        "draft": {
            "title": "발표 준비", "start_date": "2030-01-10", "end_date": "2030-01-10",
            "start_time": "14:00", "end_time": "15:00", "all_day": False,
            "location": "온라인", "evidence": ["2030년 1월 10일 14:00~15:00"],
            "ambiguities": [],
        },
        "risk_reasons": [{"code": "overlap", "message": "기존 일정과 시간이 겹칩니다."}],
        "busy_intervals": [{"start": "2030-01-10T14:30:00+09:00", "end": "2030-01-10T15:00:00+09:00"}],
        "last_error": "", "decisions": [], "google_event_id": None,
        "google_event_url": None, "created_at": "2030-01-01T00:00:00+00:00",
        "updated_at": "2030-01-01T00:00:00+00:00", "rejudge_count": 0,
    }


class FakeService:
    def __init__(self):
        self.operations: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.ready = {
            "model": "gpt-5.6-luna", "api_key_set": True,
            "client_file_present": True, "token_file_present": True,
            "target_calendar_set": True, "llm_calls_used": 0,
            "llm_call_limit": 5, "can_analyse": True, "issues": [],
        }
        self.analysis_result: list[dict] = []
        self.decide_error: str | None = None

    def readiness(self):
        return deepcopy(self.ready)

    def list_jobs(self):
        return deepcopy(list(self.jobs.values()))

    def get_job(self, job_id):
        return deepcopy(self.jobs[job_id])

    def analyse(self, text, reference_date):
        self.operations.append({"method": "analyse", "text": text, "reference_date": reference_date})
        for job in self.analysis_result:
            self.jobs[job["id"]] = deepcopy(job)
        return deepcopy(self.analysis_result)

    def decide(self, job_id, expected_version, action, edits=None, reason="", instruction=""):
        self.operations.append({"method": "decide", "job_id": job_id,
                                "expected_version": expected_version, "action": action,
                                "edits": deepcopy(edits), "reason": reason, "instruction": instruction})
        if self.decide_error:
            raise FakeServiceError("stale_version", self.decide_error)
        job = self.jobs[job_id]
        job["version"] += 1
        if action == "reject":
            job["status"] = "rejected"
        elif action == "rejudge":
            job["rejudge_count"] += 1
        else:
            if edits is not None:
                job["draft"].update(edits)
                job["title"] = job["draft"]["title"]
            job["status"] = "created"
            job["google_event_id"] = "synthetic-event"
            job["google_event_url"] = "https://calendar.google.com/calendar/event?eid=synthetic"
        return deepcopy(job)

    def reconcile(self, job_id, expected_version):
        self.operations.append({"method": "reconcile", "job_id": job_id,
                                "expected_version": expected_version})
        self.jobs[job_id]["status"] = "created"
        return deepcopy(self.jobs[job_id])

    def connect_google(self):
        self.operations.append({"method": "connect_google"})
        return {"message": "연결을 확인했습니다."}

    def close(self):
        self.operations.append({"method": "close"})


class ScreenTests(unittest.TestCase):
    def clear_cache(self):
        logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
        previous = logger.level
        try:
            logger.setLevel(logging.ERROR)
            st.cache_resource.clear()
        finally:
            logger.setLevel(previous)

    def setUp(self):
        context_logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
        bare_notice = lambda record: "missing ScriptRunContext" not in record.getMessage()
        context_logger.addFilter(bare_notice)
        self.addCleanup(context_logger.removeFilter, bare_notice)
        self.clear_cache()
        self.service = FakeService()
        package = types.ModuleType("일정도우미")
        package.__path__ = []
        api = types.ModuleType("일정도우미.서비스")
        self.build_calls = []
        self.build_error = None
        def build_fake(root):
            self.build_calls.append(root)
            if self.build_error:
                raise FakeServiceError("configuration", self.build_error)
            return self.service
        api.build_service = build_fake
        api.CalendarAgentService = FakeService
        api.ServiceError = FakeServiceError
        self.modules = patch.dict(sys.modules, {"일정도우미": package, "일정도우미.서비스": api})
        self.modules.start()
        self.network_guards = [
            patch("socket.create_connection", side_effect=AssertionError("화면 시험의 외부 접속 금지")),
            patch("httpx.Client.send", side_effect=AssertionError("화면 시험의 모델 접속 금지")),
            patch("requests.sessions.Session.request", side_effect=AssertionError("화면 시험의 Google 접속 금지")),
        ]
        for guard in self.network_guards:
            guard.start()
        self.addCleanup(self.modules.stop)
        for guard in self.network_guards:
            self.addCleanup(guard.stop)
        self.addCleanup(self.clear_cache)

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assertFalse(app.exception, str(app.exception))
        return app

    def test_first_view_names_the_task_without_starting_operations(self):
        app = self.app()
        self.assertEqual(app.title[0].value, "일정 등록 에이전트")
        self.assertEqual(self.service.operations, [])

    def test_unlimited_usage_is_named_and_analysis_stays_available(self):
        self.service.ready.update(llm_call_limit=None, llm_calls_used=3)
        app = self.app()
        captions = [item.value for item in app.caption]
        self.assertTrue(any('모델 요청 사용 3회 / 호출 제한 없음' in value for value in captions))
        self.assertFalse(any('허용 None' in value for value in captions))
        self.assertFalse(app.button(key='analyse_notice').disabled)
        self.assertEqual(self.service.operations, [])

    def test_empty_view_separates_input_review_history_and_connection(self):
        app = self.app()
        self.assertEqual([tab.label for tab in app.tabs], ["새 공지", "승인 대기", "처리 기록", "연결 준비"])
        self.assertIn("notice_text", [element.key for element in app.text_area])
        self.assertIn("analyse_notice", [element.key for element in app.button])
        self.assertEqual(self.service.operations, [])

    def test_notice_submit_calls_service_once_and_refresh_does_not_repeat(self):
        app = self.app()
        app.text_area(key="notice_text").input("2030년 1월 10일 14:00 발표")
        app.date_input(key="notice_reference_date").set_value(date(2030, 1, 1))
        app.button(key="analyse_notice").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [{"method": "analyse", "text": "2030년 1월 10일 14:00 발표", "reference_date": "2030-01-01"}])
        app.run()
        self.assertEqual(len(self.service.operations), 1)

    def test_pending_view_shows_source_risk_consequence_and_four_choices(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        app = self.app()
        self.assertTrue(any("겹칩니다" in item.value for item in app.warning))
        self.assertTrue(any(item.value == job["source_text"] for item in app.text_area))
        self.assertTrue(any("승인하면" in item.value for item in app.info))
        keys = {element.key for element in app.button}
        for action in ["approve", "edit_approve", "reject", "rejudge"]:
            self.assertIn(action + "_" + job["id"], keys)
        self.assertEqual(self.service.operations, [])


    def test_default_end_is_identified_as_user_policy_without_a_new_action(self):
        job = synthetic_job()
        job['decisions'] = [{'action':'end_defaults_applied','policy':'same_day_or_60_minutes',
            'previous_end':{'end_date':None,'end_time':None},
            'resolved_end':{'end_date':'2030-01-10','end_time':'15:00'}}]
        self.service.jobs[job['id']] = job
        app = self.app()
        self.assertTrue(any('사용자 기본값' in item.value and '1시간' in item.value for item in app.caption))
        self.assertFalse(app.button(key='approve_'+job['id']).disabled)
        self.assertEqual(self.service.operations,[])

    def test_date_backfill_is_explained_without_creating_or_rejudging(self):
        job = synthetic_job()
        job['reference_date'] = '2026-11-10'
        job['draft'].update(start_date='2026-11-11',end_date='2026-11-11',end_time=None)
        job['decisions'] = [{'action':'relative_date_normalized'}]
        self.service.jobs[job['id']] = job
        app = self.app()
        self.assertTrue(any('기준 날짜로 날짜를 보완했습니다' in item.value for item in app.caption))
        self.assertTrue(any('2026-11-11' in item.value and '시간 미정' in item.value for item in app.info))
        self.assertEqual(self.service.operations, [])

    def test_approve_sends_the_reviewed_version_once(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        app = self.app()
        app.button(key="approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [{
            "method": "decide", "job_id": job["id"], "expected_version": 1,
            "action": "approve", "edits": None, "reason": "", "instruction": "",
        }])
        app.run()
        self.assertEqual(len(self.service.operations), 1)


    def test_edit_approval_sends_only_the_explicit_editable_fields(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        suffix = job["id"] + "_1"
        app = self.app()
        app.text_input(key="edit_title_" + suffix).input("수정 발표")
        app.text_input(key="edit_location_" + suffix).input("회의실")
        app.date_input(key="edit_start_date_" + suffix).set_value(date(2030, 1, 11))
        app.date_input(key="edit_end_date_" + suffix).set_value(date(2030, 1, 11))
        app.time_input(key="edit_start_time_" + suffix).set_value(time(16, 0))
        app.time_input(key="edit_end_time_" + suffix).set_value(time(17, 0))
        app.button(key="edit_approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(len(self.service.operations), 1)
        operation = self.service.operations[0]
        self.assertEqual(operation["action"], "edit_approve")
        self.assertEqual(operation["expected_version"], 1)
        self.assertEqual(operation["edits"], {
            "title": "수정 발표", "all_day": False, "location": "회의실",
            "start_date": "2030-01-11", "end_date": "2030-01-11",
            "start_time": "16:00", "end_time": "17:00",
        })


    def test_changed_fields_cannot_be_sent_with_plain_approval(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        app = self.app()
        app.text_input(key="edit_title_" + job["id"] + "_1").input("수정한 제목")
        app.button(key="approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [])
        self.assertTrue(any("수정 후 승인" in item.value for item in app.error))


    def test_reject_and_rejudge_require_an_explanation_without_a_service_call(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        for action, message in [("reject", "반려 사유"), ("rejudge", "다시 판정할 내용")]:
            with self.subTest(action=action):
                app = self.app()
                app.button(key=action + "_" + job["id"]).click().run()
                self.assertFalse(app.exception, str(app.exception))
                self.assertEqual(self.service.operations, [])
                self.assertTrue(any(message in item.value for item in app.error))


    def test_reject_and_rejudge_forward_only_their_own_explanation(self):
        for action, field, text in [("reject", "reject_reason_", "일정이 취소됐습니다."),
                                    ("rejudge", "rejudge_instruction_", "종료 시각을 원문에서 다시 확인해 주세요.")]:
            with self.subTest(action=action):
                job = synthetic_job()
                self.service.jobs[job["id"]] = job
                self.service.operations.clear()
                app = self.app()
                app.text_area(key=field + job["id"] + "_1").input(text)
                app.button(key=action + "_" + job["id"]).click().run()
                self.assertFalse(app.exception, str(app.exception))
                self.assertEqual(len(self.service.operations), 1)
                operation = self.service.operations[0]
                self.assertEqual(operation["action"], action)
                self.assertEqual(operation["expected_version"], 1)
                self.assertIsNone(operation["edits"])
                self.assertEqual(operation["reason"], text if action == "reject" else "")
                self.assertEqual(operation["instruction"], text if action == "rejudge" else "")


    def test_service_rejection_is_visible_and_preserves_the_edit(self):
        job = synthetic_job()
        self.service.jobs[job["id"]] = job
        self.service.decide_error = "최신 버전이 있습니다. 내용을 다시 확인해 주세요."
        app = self.app()
        key = "edit_title_" + job["id"] + "_1"
        app.text_input(key=key).input("유지할 수정안")
        app.button(key="edit_approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any("최신 버전" in item.value for item in app.error))
        self.assertEqual(app.text_input(key=key).value, "유지할 수정안")
        self.assertEqual(self.service.jobs[job["id"]]["status"], "needs_review")
        self.assertFalse(any("등록 완료" in item.value for item in app.info))


    def test_unknown_history_can_only_reconcile_the_existing_job(self):
        job = synthetic_job("unknown")
        self.service.jobs[job["id"]] = job
        app = self.app()
        key = "reconcile_" + job["id"]
        self.assertIn(key, {element.key for element in app.button})
        app.button(key=key).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [{"method": "reconcile", "job_id": job["id"], "expected_version": 1}])
        app.run()
        self.assertEqual(len(self.service.operations), 1)


    def test_google_connect_is_only_started_by_the_explicit_button(self):
        app = self.app()
        self.assertEqual(self.service.operations, [])
        app.button(key="connect_google").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [{"method": "connect_google"}])
        app.run()
        self.assertEqual(len(self.service.operations), 1)


    def test_saved_login_has_persistent_status_and_reconnect_label_without_relogin(self):
        self.service.ready["token_file_present"] = True
        app = self.app()
        self.assertTrue(any(item.value == "Google 로그인 정보가 저장되어 있습니다." for item in app.success))
        self.assertEqual(app.button(key="connect_google").label, "Google 계정 다시 연결")
        app.run()
        self.assertTrue(any(item.value == "Google 로그인 정보가 저장되어 있습니다." for item in app.success))
        self.assertEqual(self.service.operations, [])


    def test_missing_login_keeps_the_first_connect_action_without_a_success_status(self):
        self.service.ready["token_file_present"] = False
        app = self.app()
        self.assertEqual(app.button(key="connect_google").label, "Google 계정 연결")
        self.assertFalse(app.button(key="connect_google").disabled)
        self.assertFalse(app.success)
        self.assertEqual(self.service.operations, [])


    def test_analysis_summary_reads_the_current_state_after_review(self):
        job = synthetic_job()
        self.service.analysis_result = [deepcopy(job)]
        app = self.app()
        app.text_area(key="notice_text").input(job["source_text"])
        app.button(key="analyse_notice").click().run()
        app.button(key="approve_" + job["id"]).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any("발표 준비 — 등록 완료" in item.value for item in app.markdown))
        self.assertFalse(any("발표 준비 — 승인 대기" in item.value for item in app.markdown))


    def test_config_reload_closes_the_old_service_and_rebuilds_without_network(self):
        app = self.app()
        self.assertIn("reload_settings", {element.key for element in app.button})
        app.button(key="reload_settings").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.service.operations, [{"method": "close"}])
        self.assertEqual(len(self.build_calls), 2)


    def test_configuration_error_is_actionable_without_a_raw_traceback(self):
        self.build_error = "프로젝트 .env의 시간대 설정을 확인해 주세요."
        app = self.app()
        self.assertTrue(any("시간대 설정" in item.value for item in app.error))
        self.assertEqual(self.service.operations, [])


if __name__ == "__main__":
    unittest.main()
