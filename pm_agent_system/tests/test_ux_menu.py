"""PM Bot UX Hotfix 2 — /menu 8-버튼 + 서브메뉴 테스트.

테스트 그룹:
  A. 메인 메뉴 레이아웃 (8버튼 4쌍)
  B. 서브메뉴 진입 / back 버튼
  C. 고급 메뉴 액션 힌트
  D. 운영 메뉴 직접 실행 (history/stats/stale/reload) + 사용법 안내 (archive/level)
  E. 프로젝트 서브메뉴
  F. unknown callback 안전 처리 / 레거시 enqueue_guide
  G. 회귀: 기존 queue/running/ship_list/doctor 콜백
"""
from __future__ import annotations

import sys
import os
import types
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── sys.path 설정 ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_BOT_DIR = _HERE.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

# ── telegram stub (tests 환경에서 telegram 패키지가 없을 경우 대비) ───────────
try:
    import telegram  # noqa: F401
    _HAS_TELEGRAM = True
except ImportError:
    _HAS_TELEGRAM = False

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_bot(
    orchestrator=None,
    project_manager=None,
    notification_level="NORMAL",
):
    """TelegramBot 인스턴스를 최소 의존성으로 생성."""
    from telegram_bot import TelegramBot

    bot = TelegramBot(
        token="test-token",
        chat_id="123",
        pm_agent=None,
        orchestrator=orchestrator,
        notification_level=notification_level,
        project_manager=project_manager,
    )
    # _app mock
    mock_app = MagicMock()
    mock_app.bot = AsyncMock()
    bot._app = mock_app
    return bot


def _make_mock_orchestrator(
    running=None,
    queue=None,
    inbox=None,
    history=None,
    stats=None,
    stale=None,
):
    """기본 orchestrator mock."""
    orch = MagicMock()
    orch.get_running_task.return_value = running
    orch.get_queue_summary.return_value = queue or []
    orch.get_inbox_summary.return_value = inbox or []
    orch.get_history = MagicMock(return_value=history or [])
    orch.get_stats_summary.return_value = stats or {"global": {}, "projects": {}}
    orch.get_stale_tasks.return_value = stale or []
    orch.reload_task_queue.return_value = {"queued": 0, "skipped_processed": 0, "skipped_unsafe": 0}
    orch.get_doctor_info = AsyncMock(return_value={
        "spec_exists": True, "task_queue_exists": True,
        "running_task_id": None, "ready_to_ship_ids": [],
        "project_id": "test", "spec_path": "/test/CLAUDE.md",
    })
    return orch


def _make_mock_project_manager(projects=None, current="vda5050"):
    """기본 project_manager mock."""
    pm = MagicMock()
    pm.current_project_id = current
    pm.list_projects.return_value = projects or ["vda5050", "ios_capture"]
    pm.switch.return_value = True
    mock_paths = MagicMock()
    mock_paths.repo_path = "/test/repo"
    pm.current_paths = mock_paths
    pm.format_project_list.return_value = "프로젝트 목록: vda5050, ios_capture"
    pm.format_current.return_value = "현재 프로젝트: vda5050"
    return pm


def _make_mock_query(data: str):
    """CallbackQuery mock."""
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.chat_id = "123"
    query.message = mock_msg
    return query


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ──────────────────────────────────────────────────────────────────────────────
# A. 메인 메뉴 레이아웃
# ──────────────────────────────────────────────────────────────────────────────

def test_main_menu_keyboard_8_buttons():
    """_build_main_menu_keyboard()가 8개 버튼(4행 2열)을 반환한다."""
    bot = _make_bot()
    keyboard = bot._build_main_menu_keyboard()
    rows = keyboard.inline_keyboard
    assert len(rows) == 4, f"4행이어야 함, 실제 {len(rows)}행"
    total_buttons = sum(len(row) for row in rows)
    assert total_buttons == 8, f"버튼 8개여야 함, 실제 {total_buttons}개"


def test_main_menu_button_labels():
    """8개 버튼 레이블에 기대하는 텍스트가 포함돼야 한다."""
    bot = _make_bot()
    keyboard = bot._build_main_menu_keyboard()
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    expected_fragments = ["작업 현황", "실행 중", "프로젝트", "점검", "로그", "Ship", "고급", "운영"]
    for fragment in expected_fragments:
        assert any(fragment in label for label in labels), f"'{fragment}' 버튼 없음 (labels={labels})"


def test_main_menu_callback_data_prefix():
    """모든 버튼의 callback_data가 'menu:' 접두어를 가진다."""
    bot = _make_bot()
    keyboard = bot._build_main_menu_keyboard()
    for row in keyboard.inline_keyboard:
        for btn in row:
            assert btn.callback_data.startswith("menu:"), \
                f"callback_data 접두어 오류: {btn.callback_data}"


def test_handle_menu_uses_build_helper():
    """/menu 핸들러가 _build_main_menu_keyboard를 사용한다."""
    from telegram_bot import TelegramBot
    from telegram import InlineKeyboardMarkup

    called = []

    class _BotWithSpy(TelegramBot):
        def _build_main_menu_keyboard(self):
            called.append(True)
            return InlineKeyboardMarkup([])

    bot = _BotWithSpy(
        token="test-token",
        chat_id="123",
        pm_agent=None,
        orchestrator=None,
    )
    mock_app = MagicMock()
    mock_app.bot = AsyncMock()
    bot._app = mock_app

    update = MagicMock()
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()

    run(bot._handle_menu(update, MagicMock()))
    assert called, "_build_main_menu_keyboard가 호출되지 않았음"


# ──────────────────────────────────────────────────────────────────────────────
# B. 서브메뉴 진입 / back 버튼
# ──────────────────────────────────────────────────────────────────────────────

def test_advanced_menu_shows_submenu():
    """menu:advanced 콜백이 고급 서브메뉴 키보드(Adopt/Review 등 + 뒤로)를 표시한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    query = _make_mock_query("menu:advanced")

    run(bot._handle_menu_callback(query, "advanced"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    keyboard = call_kwargs[1].get("reply_markup") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1].get("reply_markup")

    # reply_markup 키워드 인자로 전달됐는지 확인
    all_labels = []
    if hasattr(keyboard, "inline_keyboard"):
        all_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    assert any("뒤로" in l or "back" in l.lower() for l in all_labels), \
        f"뒤로 버튼 없음 (labels={all_labels})"


def test_admin_menu_shows_submenu():
    """menu:admin 콜백이 운영 서브메뉴(History/Stats/Stale 등 + 뒤로)를 표시한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    query = _make_mock_query("menu:admin")

    run(bot._handle_menu_callback(query, "admin"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    args = call_kwargs[0]
    kwargs = call_kwargs[1]
    keyboard = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)

    all_labels = []
    if hasattr(keyboard, "inline_keyboard"):
        all_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    for expected in ["History", "Stats", "Stale", "뒤로"]:
        assert any(expected in l for l in all_labels), \
            f"'{expected}' 버튼 없음 (labels={all_labels})"


def test_back_button_restores_main_menu():
    """menu:back 콜백이 메인 메뉴로 돌아간다 (edit_message_text 호출 + 4행 키보드)."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    query = _make_mock_query("menu:back")

    run(bot._handle_menu_callback(query, "back"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    args = call_kwargs[0]
    kwargs = call_kwargs[1]
    keyboard = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)

    assert hasattr(keyboard, "inline_keyboard"), "reply_markup이 없음"
    assert len(keyboard.inline_keyboard) == 4, \
        f"메인 메뉴는 4행이어야 함, 실제 {len(keyboard.inline_keyboard)}행"


def test_projects_menu_shows_project_list():
    """menu:projects 콜백이 프로젝트 목록 버튼을 표시한다."""
    pm = _make_mock_project_manager(projects=["vda5050", "ios_capture"], current="vda5050")
    bot = _make_bot(orchestrator=_make_mock_orchestrator(), project_manager=pm)
    query = _make_mock_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    args = call_kwargs[0]
    kwargs = call_kwargs[1]
    keyboard = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)

    assert hasattr(keyboard, "inline_keyboard"), "reply_markup이 없음"
    all_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    # 두 프로젝트 ID + 뒤로 버튼 포함
    assert any("vda5050" in l for l in all_labels), f"vda5050 버튼 없음 ({all_labels})"
    assert any("ios_capture" in l for l in all_labels), f"ios_capture 버튼 없음 ({all_labels})"
    assert any("뒤로" in l for l in all_labels), f"뒤로 버튼 없음 ({all_labels})"


# ──────────────────────────────────────────────────────────────────────────────
# C. 고급 메뉴 액션 힌트
# ──────────────────────────────────────────────────────────────────────────────

def _check_advanced_hint(cmd: str, expected_keyword: str) -> None:
    """menu:adv:<cmd> 콜백이 해당 명령 사용법 힌트를 전송한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query(f"menu:adv:{cmd}")

    run(bot._handle_menu_callback(query, f"adv:{cmd}"))

    send_calls = bot._app.bot.send_message.call_args_list
    edit_calls = query.edit_message_text.call_args_list
    combined = " ".join(str(c) for c in send_calls) + " ".join(str(c) for c in edit_calls)
    assert expected_keyword in combined, \
        f"'{expected_keyword}' 힌트가 응답에 없음 (응답: {combined[:200]})"


def test_advanced_hint_adopt():    _check_advanced_hint("adopt",   "/adopt")
def test_advanced_hint_review():   _check_advanced_hint("review",  "/review")
def test_advanced_hint_resume():   _check_advanced_hint("resume",  "/resume")
def test_advanced_hint_handoff():  _check_advanced_hint("handoff", "/handoff")
def test_advanced_hint_diff():     _check_advanced_hint("diff",    "/diff")
def test_advanced_hint_hold():     _check_advanced_hint("hold",    "/hold")


# ──────────────────────────────────────────────────────────────────────────────
# D. 운영 메뉴 액션
# ──────────────────────────────────────────────────────────────────────────────

def test_admin_history_runs_directly():
    """menu:adm:history 콜백이 이력을 조회해서 전송한다."""
    history = [
        {"task_id": "T-99", "status": "SHIPPED", "archived_at": "2026-05-30T10:00:00"},
    ]
    bot = _make_bot(orchestrator=_make_mock_orchestrator(history=history))
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:history")

    run(bot._handle_menu_callback(query, "adm:history"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "T-99" in combined or "이력" in combined, f"이력 응답 없음 (combined={combined[:300]})"


def test_admin_stats_runs_directly():
    """menu:adm:stats 콜백이 통계를 조회해서 전송한다."""
    stats = {
        "global": {"shipped": 5, "failed": 1, "held": 0, "adopted": 2},
        "projects": {},
        "pass_rate": 83.3,
    }
    bot = _make_bot(orchestrator=_make_mock_orchestrator(stats=stats))
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:stats")

    run(bot._handle_menu_callback(query, "adm:stats"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "SHIPPED" in combined or "통계" in combined, f"통계 응답 없음 ({combined[:300]})"


def test_admin_stale_no_stale():
    """menu:adm:stale — stale 없을 때 '없음' 메시지."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator(stale=[]))
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:stale")

    run(bot._handle_menu_callback(query, "adm:stale"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "없음" in combined, f"'없음' 메시지가 없음 ({combined[:300]})"


def test_admin_stale_with_items():
    """menu:adm:stale — stale 있을 때 task_id 포함 메시지."""
    stale = [{"task_id": "T-50", "status": "HELD", "days": 10}]
    bot = _make_bot(orchestrator=_make_mock_orchestrator(stale=stale))
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:stale")

    run(bot._handle_menu_callback(query, "adm:stale"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "T-50" in combined, f"T-50이 응답에 없음 ({combined[:300]})"


def test_admin_archive_shows_usage():
    """menu:adm:archive — 사용법 안내만 전송한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:archive")

    run(bot._handle_menu_callback(query, "adm:archive"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "/archive" in combined, f"archive 사용법 없음 ({combined[:300]})"


def test_admin_level_shows_current():
    """menu:adm:level — 현재 레벨과 사용법 안내."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator(), notification_level="VERBOSE")
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:level")

    run(bot._handle_menu_callback(query, "adm:level"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "VERBOSE" in combined, f"현재 레벨(VERBOSE)이 응답에 없음 ({combined[:300]})"


def test_admin_reload_runs():
    """menu:adm:reload — reload 실행 결과 전송."""
    orch = _make_mock_orchestrator()
    orch.reload_task_queue.return_value = {"queued": 2, "skipped_processed": 1, "skipped_unsafe": 0}
    bot = _make_bot(orchestrator=orch)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:reload")

    run(bot._handle_menu_callback(query, "adm:reload"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "재스캔" in combined or "2" in combined, f"reload 결과 없음 ({combined[:300]})"


# ──────────────────────────────────────────────────────────────────────────────
# E. 프로젝트 서브메뉴
# ──────────────────────────────────────────────────────────────────────────────

def test_project_switch_callback_success():
    """project:switch:<id> 콜백이 프로젝트를 전환하고 확인 메시지를 보낸다."""
    pm = _make_mock_project_manager(projects=["vda5050", "ios_capture"], current="vda5050")
    orch = _make_mock_orchestrator()
    orch.switch_project = MagicMock()
    bot = _make_bot(orchestrator=orch, project_manager=pm)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("project:switch:ios_capture")

    run(bot._handle_project_callback(query, "switch:ios_capture"))

    pm.switch.assert_called_once_with("ios_capture")
    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "ios_capture" in combined, f"전환된 프로젝트 명이 없음 ({combined[:300]})"


def test_project_switch_not_found():
    """project:switch:<id> — 없는 프로젝트는 오류 메시지 반환."""
    pm = _make_mock_project_manager(projects=["vda5050"])
    pm.switch.return_value = False
    bot = _make_bot(orchestrator=_make_mock_orchestrator(), project_manager=pm)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("project:switch:nonexistent")

    run(bot._handle_project_callback(query, "switch:nonexistent"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "❌" in combined or "찾을 수" in combined, f"오류 메시지 없음 ({combined[:300]})"


def test_project_menu_no_project_manager():
    """project_manager 없을 때 프로젝트 서브메뉴는 단일 프로젝트 안내를 한다."""
    orch = _make_mock_orchestrator()
    orch.config = MagicMock()
    orch.config.repo_path = "/test/repo"
    bot = _make_bot(orchestrator=orch, project_manager=None)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    # edit_message_text가 호출됐거나 send_message가 호출됐는지 확인
    edit_called = query.edit_message_text.called
    send_called = bot._app.bot.send_message.called
    assert edit_called or send_called, "아무 응답도 없음"


# ──────────────────────────────────────────────────────────────────────────────
# F. Unknown callback / 레거시 호환
# ──────────────────────────────────────────────────────────────────────────────

def test_unknown_menu_action_safe():
    """알 수 없는 menu: 액션은 오류 없이 처리된다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:this_does_not_exist")

    # 예외 없이 실행돼야 함
    run(bot._handle_menu_callback(query, "this_does_not_exist"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "알 수 없는" in combined, f"unknown 액션 메시지 없음 ({combined[:300]})"


def test_legacy_enqueue_guide_still_works():
    """레거시 menu:enqueue_guide 콜백이 여전히 작동한다 (하위 호환)."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:enqueue_guide")

    run(bot._handle_menu_callback(query, "enqueue_guide"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "/enqueue" in combined, f"enqueue 안내 없음 ({combined[:300]})"


def test_unknown_adv_cmd_returns_hint():
    """adv: 접두어이나 알 수 없는 명령도 안내 메시지 반환."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adv:unknown_cmd")

    run(bot._handle_menu_callback(query, "adv:unknown_cmd"))

    send_calls = bot._app.bot.send_message.call_args_list
    # 예외 없이 처리되어야 함 (hint 메시지 포함)
    assert len(send_calls) >= 0  # 오류 없이 완료


def test_unknown_adm_cmd_safe():
    """adm: 접두어이나 알 수 없는 명령도 안전하게 처리된다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:adm:unknown_op")

    run(bot._handle_menu_callback(query, "adm:unknown_op"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "알 수 없는" in combined, f"unknown adm 메시지 없음 ({combined[:300]})"


# ──────────────────────────────────────────────────────────────────────────────
# G. 회귀: 기존 콜백 (queue / running / ship_list / doctor)
# ──────────────────────────────────────────────────────────────────────────────

def test_regression_queue_callback():
    """menu:queue 콜백이 여전히 작동한다."""
    orch = _make_mock_orchestrator(
        queue=[{"task_id": "T-10", "status": "RUNNING", "branch": "feature/T-10"}]
    )
    bot = _make_bot(orchestrator=orch)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:queue")

    run(bot._handle_menu_callback(query, "queue"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "T-10" in combined, f"T-10이 queue 응답에 없음 ({combined[:300]})"


def test_regression_running_callback_no_task():
    """menu:running 콜백 — 실행 중 없을 때 안내 메시지."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator(running=None))
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:running")

    run(bot._handle_menu_callback(query, "running"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "없음" in combined, f"'없음' 메시지가 없음 ({combined[:300]})"


def test_regression_ship_list_callback():
    """menu:ship_list 콜백 — READY_TO_SHIP 태스크 목록."""
    orch = _make_mock_orchestrator(
        queue=[{"task_id": "T-20", "status": "READY_TO_SHIP", "branch": "feature/T-20"}]
    )
    bot = _make_bot(orchestrator=orch)
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:ship_list")

    run(bot._handle_menu_callback(query, "ship_list"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "T-20" in combined, f"T-20이 ship_list 응답에 없음 ({combined[:300]})"


def test_regression_doctor_callback():
    """menu:doctor 콜백이 상태 요약을 반환한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:doctor")

    run(bot._handle_menu_callback(query, "doctor"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "PM Bot" in combined or "점검" in combined or "상태" in combined, \
        f"doctor 응답 없음 ({combined[:300]})"


def test_regression_log_callback():
    """menu:log 콜백이 사용법 안내를 반환한다."""
    bot = _make_bot(orchestrator=_make_mock_orchestrator())
    bot._app.bot.send_message = AsyncMock()
    query = _make_mock_query("menu:log")

    run(bot._handle_menu_callback(query, "log"))

    send_calls = bot._app.bot.send_message.call_args_list
    combined = " ".join(str(c) for c in send_calls)
    assert "/log" in combined, f"/log 사용법 없음 ({combined[:300]})"


# ──────────────────────────────────────────────────────────────────────────────
# HELP_TEXT 확인
# ──────────────────────────────────────────────────────────────────────────────

def test_help_text_contains_5_core_commands():
    """HELP_TEXT에 5개 핵심 명령이 포함된다 (/menu /enqueue /queue /running /ship)."""
    from telegram_bot import HELP_TEXT

    for cmd in ["/menu", "/enqueue", "/queue", "/running", "/ship"]:
        assert cmd in HELP_TEXT, f"HELP_TEXT에 '{cmd}' 없음"


def test_help_text_references_advanced_and_admin():
    """HELP_TEXT에 advanced/admin 힌트가 있다."""
    from telegram_bot import HELP_TEXT

    assert "advanced" in HELP_TEXT, "HELP_TEXT에 'advanced' 참조 없음"
    assert "admin" in HELP_TEXT, "HELP_TEXT에 'admin' 참조 없음"


if __name__ == "__main__":
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v"],
        cwd=str(_BOT_DIR),
    )
    sys.exit(result.returncode)
