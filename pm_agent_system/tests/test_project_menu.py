"""PM Bot Project Menu 보강 테스트.

테스트 그룹:
  A. 프로젝트 서브메뉴 — 버튼 표시 (project:switch: 형식)
  B. project:switch 콜백 — 전환 성공 / 실패 / PM없음
  C. /current 문구 — 선택 프로젝트 기준 안내
  D. /running 문구 — 실행 작업 기준 + 선택/실행 프로젝트 차이 안내
  E. 회귀: 기존 menu:back / menu:queue 등 정상 동작
"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_HERE = Path(__file__).parent
_BOT_DIR = _HERE.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_bot(orchestrator=None, project_manager=None):
    from telegram_bot import TelegramBot
    bot = TelegramBot(
        token="test-token", chat_id="123",
        pm_agent=None, orchestrator=orchestrator,
        project_manager=project_manager,
    )
    mock_app = MagicMock()
    mock_app.bot = AsyncMock()
    bot._app = mock_app
    return bot


def _make_orch(running=None, queue=None):
    orch = MagicMock()
    orch.get_running_task.return_value = running
    orch.get_queue_summary.return_value = queue or []
    orch.get_inbox_summary.return_value = []
    orch.get_doctor_info = AsyncMock(return_value={
        "spec_exists": True, "task_queue_exists": True,
        "running_task_id": None, "ready_to_ship_ids": [],
        "project_id": "test",
    })
    orch.switch_project = MagicMock()
    return orch


def _make_pm(projects=None, current="vda5050", switch_ok=True):
    pm = MagicMock()
    pm.current_project_id = current
    pm.list_projects.return_value = projects or ["vda5050", "lidear"]
    pm.switch.return_value = switch_ok
    paths = MagicMock()
    paths.repo_path = f"/Users/tg/{current}"
    pm.current_paths = paths
    return pm


def _make_query(data: str = ""):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    msg = MagicMock()
    msg.chat_id = "123"
    query.message = msg
    return query


def _make_update(text: str = ""):
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _sent_text(bot) -> str:
    """bot._app.bot.send_message 호출 텍스트 전부 합침."""
    calls = bot._app.bot.send_message.call_args_list
    return " ".join(str(c) for c in calls)


def _replied_text(update) -> str:
    calls = update.effective_message.reply_text.call_args_list
    return " ".join(str(c) for c in calls)


# ──────────────────────────────────────────────────────────────────────────────
# A. 프로젝트 서브메뉴 버튼 표시
# ──────────────────────────────────────────────────────────────────────────────

def test_projects_submenu_uses_project_switch_callback():
    """프로젝트 서브메뉴 버튼이 project:switch: 형식 callback_data를 사용한다."""
    pm = _make_pm(projects=["vda5050", "lidear"], current="vda5050")
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    keyboard = call_kwargs[1].get("reply_markup") or (
        call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
    )
    assert keyboard is not None, "keyboard가 전달되지 않음"

    all_callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    switch_callbacks = [c for c in all_callbacks if c.startswith("project:switch:")]
    assert len(switch_callbacks) == 2, (
        f"project:switch: 콜백이 2개여야 함, 실제: {switch_callbacks}"
    )
    assert "project:switch:vda5050" in switch_callbacks
    assert "project:switch:lidear" in switch_callbacks


def test_projects_submenu_marks_current_with_checkmark():
    """현재 선택 프로젝트 버튼에 ✅ 마크가 붙는다."""
    pm = _make_pm(projects=["vda5050", "lidear"], current="vda5050")
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    call_kwargs = query.edit_message_text.call_args
    keyboard = call_kwargs[1].get("reply_markup") or (
        call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
    )
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("✅" in l and "vda5050" in l for l in labels), (
        f"현재 프로젝트(vda5050)에 ✅ 없음 (labels={labels})"
    )
    # lidear에는 ✅ 없어야 함
    assert not any("✅" in l and "lidear" in l for l in labels), (
        f"비선택 프로젝트(lidear)에 ✅가 있음 (labels={labels})"
    )


def test_projects_submenu_has_back_button():
    """프로젝트 서브메뉴에 ← 뒤로 버튼이 있다."""
    pm = _make_pm()
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    call_kwargs = query.edit_message_text.call_args
    keyboard = call_kwargs[1].get("reply_markup") or (
        call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
    )
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("뒤로" in l for l in labels), f"뒤로 버튼 없음 (labels={labels})"


def test_projects_submenu_shows_current_project_name():
    """서브메뉴 본문에 현재 선택 프로젝트 이름이 표시된다."""
    pm = _make_pm(current="lidear")
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("menu:projects")

    run(bot._handle_menu_callback(query, "projects"))

    call_kwargs = query.edit_message_text.call_args
    # 첫 번째 인자 (text)
    text_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("text", "")
    assert "lidear" in text_arg, f"현재 프로젝트(lidear)가 본문에 없음 (text={text_arg!r})"


# ──────────────────────────────────────────────────────────────────────────────
# B. project:switch 콜백
# ──────────────────────────────────────────────────────────────────────────────

def test_project_switch_callback_success():
    """project:switch:<id> 콜백이 프로젝트를 전환하고 완료 메시지를 전송한다."""
    pm = _make_pm(projects=["vda5050", "lidear"], current="vda5050")
    orch = _make_orch()
    bot = _make_bot(orchestrator=orch, project_manager=pm)
    query = _make_query("project:switch:lidear")

    run(bot._handle_project_callback(query, "switch:lidear"))

    pm.switch.assert_called_once_with("lidear")
    text = _sent_text(bot)
    assert "lidear" in text, f"lidear가 응답에 없음 ({text[:300]})"
    assert "완료" in text or "전환" in text, f"완료/전환 메시지 없음 ({text[:300]})"


def test_project_switch_calls_orchestrator_switch_project():
    """project:switch 콜백이 orchestrator.switch_project()를 호출한다."""
    pm = _make_pm(projects=["vda5050", "lidear"], current="vda5050")
    orch = _make_orch()
    bot = _make_bot(orchestrator=orch, project_manager=pm)
    query = _make_query("project:switch:lidear")

    run(bot._handle_project_callback(query, "switch:lidear"))

    orch.switch_project.assert_called_once()


def test_project_switch_success_message_has_next_actions():
    """전환 완료 메시지에 /doctor, /enqueue, /queue 다음 액션이 안내된다."""
    pm = _make_pm(projects=["vda5050", "lidear"])
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("project:switch:lidear")

    run(bot._handle_project_callback(query, "switch:lidear"))

    text = _sent_text(bot)
    for cmd in ["/doctor", "/enqueue", "/queue"]:
        assert cmd in text, f"'{cmd}'이 완료 메시지에 없음 ({text[:300]})"


def test_project_switch_not_found():
    """없는 프로젝트 ID는 오류 메시지를 반환한다."""
    pm = _make_pm(projects=["vda5050"], switch_ok=False)
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("project:switch:nonexistent")

    run(bot._handle_project_callback(query, "switch:nonexistent"))

    text = _sent_text(bot)
    assert "❌" in text, f"오류 메시지 없음 ({text[:300]})"


def test_project_switch_no_project_manager():
    """project_manager 없을 때 오류 메시지를 반환한다."""
    bot = _make_bot(orchestrator=_make_orch(), project_manager=None)
    query = _make_query("project:switch:vda5050")

    run(bot._handle_project_callback(query, "switch:vda5050"))

    text = _sent_text(bot)
    assert "❌" in text, f"오류 메시지 없음 ({text[:300]})"


def test_project_switch_empty_id():
    """빈 project_id는 오류 메시지를 반환한다."""
    pm = _make_pm()
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("project:switch:")

    run(bot._handle_project_callback(query, "switch:"))

    text = _sent_text(bot)
    assert "❌" in text, f"오류 메시지 없음 ({text[:300]})"


def test_project_callback_unknown_action():
    """switch: 이외의 project: 액션도 안전하게 처리된다."""
    pm = _make_pm()
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    query = _make_query("project:unknown:foo")

    run(bot._handle_project_callback(query, "unknown:foo"))

    text = _sent_text(bot)
    assert "알 수 없는" in text, f"unknown 메시지 없음 ({text[:300]})"


def test_callback_query_routes_project_prefix():
    """_handle_callback_query가 project: 콜백을 _handle_project_callback으로 라우팅한다."""
    pm = _make_pm()
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)

    called = []
    async def _fake_project_cb(query, action):
        called.append(action)
    bot._handle_project_callback = _fake_project_cb

    update = MagicMock()
    query = _make_query("project:switch:vda5050")
    update.callback_query = query

    run(bot._handle_callback_query(update, MagicMock()))

    assert called == ["switch:vda5050"], f"라우팅 실패: {called}"


# ──────────────────────────────────────────────────────────────────────────────
# C. /current 문구
# ──────────────────────────────────────────────────────────────────────────────

def test_current_shows_selected_project():
    """/current 응답에 현재 선택 프로젝트 이름이 포함된다."""
    pm = _make_pm(current="lidear")
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    update = _make_update("/current")
    context = MagicMock()

    run(bot._handle_current(update, context))

    text = _replied_text(update)
    assert "lidear" in text, f"lidear가 /current 응답에 없음 ({text[:300]})"


def test_current_mentions_enqueue_target():
    """/current 응답이 'enqueue 작업이 등록됩니다'를 안내한다."""
    pm = _make_pm()
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    update = _make_update("/current")
    context = MagicMock()

    run(bot._handle_current(update, context))

    text = _replied_text(update)
    assert "enqueue" in text.lower(), f"/current 응답에 enqueue 안내 없음 ({text[:300]})"


def test_current_shows_repo_path():
    """/current 응답에 repo 경로가 표시된다."""
    pm = _make_pm(current="vda5050")
    pm.current_paths.repo_path = "/Users/tg/vda5050_sim_v2"
    bot = _make_bot(orchestrator=_make_orch(), project_manager=pm)
    update = _make_update("/current")
    context = MagicMock()

    run(bot._handle_current(update, context))

    text = _replied_text(update)
    assert "/Users/tg" in text or "vda5050" in text, (
        f"repo 경로가 /current 응답에 없음 ({text[:300]})"
    )


def test_current_no_project_manager_single_mode():
    """/current — project_manager 없을 때 단일 모드 안내."""
    orch = _make_orch()
    orch.config = MagicMock()
    orch.config.repo_path = "/Users/tg/vda5050_sim_v2"
    bot = _make_bot(orchestrator=orch, project_manager=None)
    update = _make_update("/current")
    context = MagicMock()

    run(bot._handle_current(update, context))

    text = _replied_text(update)
    assert "단일" in text or "vda5050" in text, (
        f"단일 모드 안내 없음 ({text[:300]})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# D. /running 문구
# ──────────────────────────────────────────────────────────────────────────────

def test_running_shows_running_task():
    """/running 응답에 실행 중인 task_id가 표시된다."""
    running_info = {"task_id": "T-91", "phase": "RUNNING", "branch": "feature/T-91"}
    bot = _make_bot(orchestrator=_make_orch(running=running_info))
    update = _make_update("/running")
    context = MagicMock()

    run(bot._handle_running(update, context))

    text = _replied_text(update)
    assert "T-91" in text, f"T-91이 /running 응답에 없음 ({text[:300]})"


def test_running_no_task_shows_empty_message():
    """/running — 실행 중 없을 때 '없음' 메시지."""
    bot = _make_bot(orchestrator=_make_orch(running=None))
    update = _make_update("/running")
    context = MagicMock()

    run(bot._handle_running(update, context))

    text = _replied_text(update)
    assert "없음" in text or "없습니다" in text, (
        f"'없음' 메시지 없음 ({text[:300]})"
    )


def test_running_shows_project_difference_warning():
    """/running — 선택 프로젝트와 실행 프로젝트가 다를 때 경고가 표시된다."""
    running_info = {"task_id": "T-91", "phase": "RUNNING", "project_id": "vda5050"}
    pm = _make_pm(current="lidear")  # 선택: lidear, 실행: vda5050
    bot = _make_bot(orchestrator=_make_orch(running=running_info), project_manager=pm)
    update = _make_update("/running")
    context = MagicMock()

    run(bot._handle_running(update, context))

    text = _replied_text(update)
    assert "주의" in text or "다릅니다" in text or "⚠️" in text, (
        f"프로젝트 차이 경고 없음 ({text[:300]})"
    )


def test_running_shows_current_vs_running_hint():
    """/running — 선택=실행이 같아도 /current vs /running 힌트를 표시한다."""
    running_info = {"task_id": "T-91", "phase": "RUNNING", "project_id": "vda5050"}
    pm = _make_pm(current="vda5050")
    bot = _make_bot(orchestrator=_make_orch(running=running_info), project_manager=pm)
    update = _make_update("/running")
    context = MagicMock()

    run(bot._handle_running(update, context))

    text = _replied_text(update)
    assert "/current" in text or "/running" in text, (
        f"/current vs /running 힌트 없음 ({text[:300]})"
    )


def test_running_shows_project_prefixed_task_id():
    """/running — project_id가 있을 때 'project:task_id' 형식으로 표시한다."""
    running_info = {"task_id": "T-91", "phase": "RUNNING", "project_id": "vda5050"}
    bot = _make_bot(orchestrator=_make_orch(running=running_info))
    update = _make_update("/running")
    context = MagicMock()

    run(bot._handle_running(update, context))

    text = _replied_text(update)
    assert "vda5050:T-91" in text, (
        f"'vda5050:T-91' 형식이 없음 ({text[:300]})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# E. 회귀: 기존 동작 보존
# ──────────────────────────────────────────────────────────────────────────────

def test_regression_menu_back_still_works():
    """menu:back 콜백이 여전히 메인 메뉴로 복귀한다."""
    bot = _make_bot(orchestrator=_make_orch())
    query = _make_query("menu:back")

    run(bot._handle_menu_callback(query, "back"))

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    keyboard = call_kwargs[1].get("reply_markup") or (
        call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
    )
    assert len(keyboard.inline_keyboard) == 4, "메인 메뉴 4행이 아님"


def test_regression_menu_queue_still_works():
    """menu:queue 콜백이 여전히 작동한다."""
    bot = _make_bot(orchestrator=_make_orch(
        queue=[{"task_id": "T-10", "status": "RUNNING"}]
    ))
    query = _make_query("menu:queue")

    run(bot._handle_menu_callback(query, "queue"))

    text = _sent_text(bot)
    assert "T-10" in text, f"T-10이 queue 응답에 없음 ({text[:300]})"


def test_regression_help_advanced_text_has_current_running_note():
    """HELP_ADVANCED_TEXT에 /current vs /running 설명이 포함된다."""
    from telegram_bot import HELP_ADVANCED_TEXT
    assert "/current" in HELP_ADVANCED_TEXT, "HELP_ADVANCED_TEXT에 /current 없음"
    assert "/running" in HELP_ADVANCED_TEXT, "HELP_ADVANCED_TEXT에 /running 없음"


if __name__ == "__main__":
    import subprocess, sys as _sys
    result = subprocess.run(
        [_sys.executable, "-m", "pytest", __file__, "-v"],
        cwd=str(_BOT_DIR),
    )
    _sys.exit(result.returncode)
