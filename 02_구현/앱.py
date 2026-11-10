"""Google Calendar 일정 검토 화면. 서비스의 상태를 표시하고 명시적 제출만 전달합니다."""
from copy import deepcopy
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import streamlit as st
from 일정도우미.서비스 import ServiceError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_LABELS = {"needs_review": "승인 대기", "created": "등록 완료", "rejected": "반려",
                 "error": "처리 오류", "unknown": "등록 결과 확인 필요"}


@st.cache_resource(show_spinner=False)
def get_service():
    from 일정도우미.서비스 import build_service
    return build_service(PROJECT_ROOT)


def render_notice(service, readiness):
    st.subheader("공지에서 일정 정리")
    st.caption("명확하고 충돌 없는 일정은 바로 등록하고, 확인이 필요한 일정만 승인 대기로 보냅니다.")
    st.caption("종료 정보가 없으면 시간 일정은 1시간, 종일 일정은 시작일 당일을 기본값으로 사용합니다.")
    for issue in readiness.get("issues", []):
        st.warning(issue)
    with st.form("notice_form", clear_on_submit=False, enter_to_submit=False):
        source = st.text_area("공지 원문", key="notice_text", height=180,
                              placeholder="날짜와 시간이 포함된 공지를 붙여 넣으세요.")
        reference = st.date_input("공지 기준 날짜", value=datetime.now(ZoneInfo("Asia/Seoul")).date(),
                                  key="notice_reference_date", help="‘내일’ 같은 표현의 기준입니다. 과거 공지는 작성 날짜를 선택하세요.")
        submitted = st.form_submit_button("분석하고 일정 등록", key="analyse_notice", type="primary",
                                          disabled=not readiness.get("can_analyse", False))
    if submitted:
        if not source.strip() or reference is None:
            st.error("공지 원문과 공지 기준 날짜를 입력해 주세요.")
        else:
            st.session_state.pop("latest_analysis", None)
            try:
                with st.spinner("일정을 해석하고 등록 기준을 확인하고 있습니다."):
                    result = service.analyse(source, reference.isoformat())
                    st.session_state["latest_analysis"] = [job["id"] for job in result]
                st.rerun()
            except ServiceError as error:
                st.error(error.message)
    latest = st.session_state.get("latest_analysis")
    if latest is not None:
        st.subheader("이번 처리 결과")
        if not latest:
            st.info("일정 후보를 찾지 못했습니다. 날짜와 시간이 있는 공지인지 확인해 주세요.")
        for job_id in latest:
            job = service.get_job(job_id)
            st.write(f"{job['title']} — {STATUS_LABELS.get(job['status'], '상태 확인 필요')}")


def render_connection(service, readiness):
    st.subheader("연결 준비")
    st.write("사용 모델: " + readiness.get("model", "gpt-5.6-luna"))
    checks = [("OpenAI API 키", "api_key_set"), ("Google 클라이언트 파일", "client_file_present"),
              ("Google 인증 토큰 파일", "token_file_present"), ("등록 대상 캘린더", "target_calendar_set")]
    for label, key in checks:
        st.write(f"{label}: {'준비됨' if readiness.get(key) else '준비 필요'}")
    st.caption("저장된 인증 자료의 현황입니다. 실제 캘린더 조회·등록 성공과는 구분하며, 키와 토큰 값은 표시하지 않습니다.")
    login_saved = bool(readiness.get("token_file_present", False))
    if login_saved:
        st.success("Google 로그인 정보가 저장되어 있습니다.")
        st.caption("로그인을 반복할 필요는 없습니다. 계정을 다시 인증하려는 경우에만 아래 버튼을 사용하세요.")
    connect_label = "Google 계정 다시 연결" if login_saved else "Google 계정 연결"
    if st.button(connect_label, key="connect_google", disabled=not readiness.get("client_file_present", False)):
        try:
            with st.spinner("브라우저에서 Google 계정 연결을 완료해 주세요."):
                service.connect_google()
        except ServiceError as error:
            st.error(error.message)
        else:
            st.session_state["flash"] = "계정 연결 절차를 마쳤습니다. 파일 준비 상태와 등록 대상 설정을 확인해 주세요."
            st.rerun()
    used = readiness.get('llm_calls_used', 0)
    limit = readiness.get('llm_call_limit')
    st.caption(f"모델 요청 사용 {used}회 / 호출 제한 없음" if limit is None else f"모델 요청 사용 {used} / 허용 {limit}회")
    if st.button("설정 다시 불러오기", key="reload_settings",
                 help="프로젝트 .env를 변경했을 때 사용합니다. 저장된 작업과 승인 대기 기록은 삭제하지 않습니다."):
        try:
            service.close()
        except ServiceError as error:
            st.error(error.message)
        else:
            get_service.clear()
            st.session_state["flash"] = "프로젝트 설정을 다시 불러왔습니다. 연결 준비 상태를 확인해 주세요."
            st.rerun()


def date_value(value):
    try:
        return date.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def time_value(value):
    try:
        return time.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def show_public_error(value):
    if value is None or value == "":
        return
    message = value.get("message") if isinstance(value, dict) else value
    st.error(message if isinstance(message, str) and message else "처리 오류를 확인하지 못했습니다. 연결 상태와 처리 기록을 확인해 주세요.")


def show_candidate(job):
    draft = job["draft"]
    st.text_area("공지 원문", value=job["source_text"], key="source_" + job["id"],
                 height=100, disabled=True)
    st.caption("공지 기준 날짜: " + job["reference_date"])
    date_backfilled = bool(job.get("decisions")) and job["decisions"][-1].get("action") == "relative_date_normalized"
    if date_backfilled:
        st.caption("공지 기준 날짜로 날짜를 보완했습니다. 시간 등 남은 정보는 확인한 뒤 결정해 주세요.")
    end_defaulted = False
    for decision in reversed(job.get('decisions',[])):
        if decision.get('action') == 'end_defaults_applied':
            end_defaulted = decision.get('resolved_end') == {key:draft.get(key) for key in ('end_date','end_time')}
            if end_defaulted:
                if draft.get('all_day'):
                    text = '종료 날짜가 없어 시작일 당일로 설정했습니다.'
                elif decision.get('previous_end',{}).get('end_time') in (None,''):
                    text = '종료 시각이 없어 시작 시각 + 1시간으로 설정했습니다.'
                else:
                    text = '종료 날짜가 없어 시작일 당일로 설정했습니다.'
                st.caption('사용자 기본값 적용: '+text)
            break
        if decision.get('action') in {'rejudge','edit_approve','edit_pending_review'}:
            break
    for risk in job.get("risk_reasons", []):
        st.warning(risk["message"])
    if draft.get("all_day"):
        period = f"{draft.get('start_date') or '날짜 미정'} ~ {draft.get('end_date') or '날짜 미정'} · 종일"
    else:
        period = (f"{draft.get('start_date') or '날짜 미정'} {draft.get('start_time') or '시간 미정'} ~ "
                  f"{draft.get('end_date') or '날짜 미정'} {draft.get('end_time') or '시간 미정'}")
    st.info(f"승인하면 ‘{draft['title']}’ 일정을 {period}에 설정된 Google Calendar로 새로 등록합니다.")
    if draft.get("location"):
        st.write("장소: " + draft["location"])
    if job.get("busy_intervals"):
        with st.expander("충돌하는 시간 확인", expanded=True):
            for busy in job["busy_intervals"]:
                st.write(f"{busy['start']} ~ {busy['end']}")
    with st.expander("AI 추출 근거"):
        if date_backfilled or end_defaulted:
            st.caption("아래 모호함은 보완 전의 모델 응답 기록입니다. 현재 일시는 위 일정 정보에 표시됩니다.")
        for quote in draft.get("evidence", []):
            st.text(quote)
        for ambiguity in draft.get("ambiguities", []):
            st.write(ambiguity)
    show_public_error(job.get("last_error"))


def render_review_form(job, stale=False):
    draft = job["draft"]
    suffix = f"{job['id']}_{job['version']}"
    with st.form("review_" + suffix, clear_on_submit=False, enter_to_submit=False):
        with st.expander("일정 수정"):
            title = st.text_input("일정 제목", value=draft["title"], key="edit_title_" + suffix)
            all_day = st.checkbox("종일 일정", value=draft.get("all_day", False), key="edit_all_day_" + suffix)
            left, right = st.columns(2)
            with left:
                start_date = st.date_input("시작 날짜", value=date_value(draft.get("start_date")), key="edit_start_date_" + suffix)
                start_time = st.time_input("시작 시각", value=time_value(draft.get("start_time")), key="edit_start_time_" + suffix)
            with right:
                end_date = st.date_input("종료 날짜 · 종일 일정은 마지막 날", value=date_value(draft.get("end_date")), key="edit_end_date_" + suffix)
                end_time = st.time_input("종료 시각", value=time_value(draft.get("end_time")), key="edit_end_time_" + suffix)
            location = st.text_input("장소", value=draft.get("location", ""), key="edit_location_" + suffix)
            st.caption("수정한 내용은 ‘수정 후 승인’으로 제출할 때 반영됩니다. 종일 일정에서는 시각을 사용하지 않습니다.")
        with st.expander("반려 사유 · 재판정 지시"):
            reason = st.text_area("반려 사유", key="reject_reason_" + suffix, height=68)
            instruction = st.text_area("다시 판정할 내용", key="rejudge_instruction_" + suffix, height=68)
        left, right = st.columns(2)
        with left:
            approve = st.form_submit_button("승인", key="approve_" + job["id"], type="primary", disabled=stale)
            reject = st.form_submit_button("반려", key="reject_" + job["id"], disabled=stale)
        with right:
            edit = st.form_submit_button("수정 후 승인", key="edit_approve_" + job["id"], disabled=stale)
            rejudge = st.form_submit_button("다시 판정", key="rejudge_" + job["id"], disabled=stale)
    edits = {"title": title, "all_day": all_day, "location": location,
             "start_date": start_date.isoformat() if start_date else None,
             "end_date": end_date.isoformat() if end_date else None,
             "start_time": start_time.isoformat(timespec="minutes") if start_time and not all_day else None,
             "end_time": end_time.isoformat(timespec="minutes") if end_time and not all_day else None}
    action = "approve" if approve else "edit_approve" if edit else "reject" if reject else "rejudge" if rejudge else None
    return action, edits, reason, instruction


def render_pending(service, jobs):
    st.subheader("승인 대기")
    pending = [job for job in jobs if job["status"] == "needs_review"]
    if not pending:
        st.info("승인을 기다리는 일정이 없습니다.")
        return
    lookup = {job["id"]: job for job in pending}
    if st.session_state.get("pending_job_id") not in lookup:
        st.session_state.pop("pending_job_id", None)
    selected = st.selectbox("검토할 일정", options=list(lookup), format_func=lambda item: lookup[item]["title"], key="pending_job_id")
    current = service.get_job(selected)
    snapshots = st.session_state.setdefault("review_snapshots", {})
    if selected not in snapshots:
        snapshots[selected] = deepcopy(current)
    snapshot = snapshots[selected]
    stale = snapshot["version"] != current["version"]
    if stale:
        st.warning("이 일정의 내용이 바뀌었습니다. 최신 내용을 확인한 뒤 다시 판단해 주세요.")
        if st.button("최신 내용 불러오기 · 수정 입력 초기화", key="reload_" + selected):
            snapshots[selected] = deepcopy(current)
            st.rerun()
    show_candidate(snapshot)
    action, edits, reason, instruction = render_review_form(snapshot, stale)
    if action == "approve" and any(edits[key] != snapshot["draft"].get(key) for key in edits):
        st.error("수정한 내용이 있습니다. 변경 내용을 반영하려면 ‘수정 후 승인’을 눌러 주세요.")
        return
    if action == "reject" and not reason.strip():
        st.error("반려 사유를 입력해 주세요.")
        return
    if action == "rejudge" and not instruction.strip():
        st.error("다시 판정할 내용을 입력해 주세요.")
        return
    if action in {"approve", "edit_approve", "reject", "rejudge"} and not stale:
        try:
            result = service.decide(selected, snapshot["version"], action,
                                    edits=edits if action == "edit_approve" else None,
                                    reason=reason if action == "reject" else "",
                                    instruction=instruction if action == "rejudge" else "")
        except ServiceError as error:
            st.error(error.message)
            return
        snapshots.pop(selected, None)
        st.session_state["flash"] = STATUS_LABELS.get(result["status"], "처리 결과를 확인해 주세요.")
        st.rerun()


def render_history(service, jobs):
    st.subheader("처리 기록")
    records = [job for job in jobs if job["status"] != "needs_review"]
    if not records:
        st.info("아직 처리한 기록이 없습니다.")
        return
    lookup = {job["id"]: job for job in records}
    if st.session_state.get("history_job_id") not in lookup:
        st.session_state.pop("history_job_id", None)
    selected = st.selectbox("확인할 기록", options=list(lookup),
                            format_func=lambda item: f"{lookup[item]['title']} · {STATUS_LABELS.get(lookup[item]['status'], '상태 확인 필요')}",
                            key="history_job_id")
    job = service.get_job(selected)
    st.write("처리 상태: " + STATUS_LABELS.get(job["status"], "상태 확인 필요"))
    st.caption("기록 시각: " + job.get("updated_at", ""))
    show_public_error(job.get("last_error"))
    if job["status"] == "unknown":
        st.warning("등록 결과를 아직 확정하지 못했습니다. 아래 버튼은 기존 일정 ID만 조회하며 새 일정을 만들지 않습니다.")
        if st.button("등록 상태 확인", key="reconcile_" + selected):
            try:
                result = service.reconcile(selected, job["version"])
            except ServiceError as error:
                st.error(error.message)
            else:
                st.session_state["flash"] = STATUS_LABELS.get(result["status"], "처리 결과를 확인해 주세요.")
                st.rerun()
    url = job.get("google_event_url")
    if job["status"] == "created" and isinstance(url, str):
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in {"calendar.google.com", "www.google.com"}:
            st.link_button("Google Calendar에서 열기", url)
    with st.expander("기록 원문과 일정 정보"):
        st.text_area("기록 원문", value=job["source_text"], height=120,
                     key="history_source_" + selected, disabled=True)
        st.json(job["draft"])
    if job.get("decisions"):
        with st.expander("검토 기록"):
            st.json(job["decisions"])


def main():
    st.set_page_config(page_title="일정 등록 에이전트", layout="centered")
    st.title("일정 등록 에이전트")
    st.write("공지를 정리하고, 필요한 일정만 확인받아 Google Calendar에 등록합니다.")
    if flash := st.session_state.pop("flash", None):
        st.info(flash)
    try:
        service = get_service()
        readiness = service.readiness()
    except ServiceError as error:
        st.error(error.message)
        st.caption("설정 파일을 수정한 뒤 페이지를 새로고침해 주세요. 인증값은 채팅에 보내지 마세요.")
        return
    tabs = st.tabs(["새 공지", "승인 대기", "처리 기록", "연결 준비"])
    with tabs[0]:
        render_notice(service, readiness)
    jobs = service.list_jobs()
    with tabs[1]:
        render_pending(service, jobs)
    with tabs[2]:
        render_history(service, jobs)
    with tabs[3]:
        render_connection(service, service.readiness())


if __name__ == "__main__":
    main()
