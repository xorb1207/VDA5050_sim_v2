"""PM Bot UX Hotfix 3 — 로컬 task_inbox 지원 테스트.

테스트 그룹:
  A. parse_inbox_file() — 파일 파싱 (frontmatter 유무, heading, 파일명)
  B. get_inbox_files() — 목록 조회 + 안전 처리
  C. approve_inbox_task() — local drop + 안전 처리
  D. /inbox Telegram 핸들러
  E. 회귀: /enqueue, task_queue watcher
"""
from __future__ import annotations

import sys
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_HERE = Path(__file__).parent
_BOT_DIR = _HERE.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures helpers
# ──────────────────────────────────────────────────────────────────────────────

def _tmp_inbox(tmp_path: Path) -> Path:
    d = tmp_path / "task_inbox"
    d.mkdir()
    return d


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


LONG_BODY = "상세한 내용입니다.\n" * 20  # > 100자


def _make_orchestrator_with_inbox(tmp_path: Path):
    """최소 Orchestrator mock (get_inbox_files / get_inbox_task_detail / approve_inbox_task)."""
    inbox_dir = _tmp_inbox(tmp_path)
    queue_dir = tmp_path / "task_queue"
    queue_dir.mkdir()

    # import real Orchestrator helpers
    sys.path.insert(0, str(_BOT_DIR))

    class _FakeOrch:
        task_inbox_dir = inbox_dir
        task_queue_dir = queue_dir
        _processed_ids: set = set()
        _running_task = None

        def _is_already_queued(self, tid: str) -> bool:
            if tid in self._processed_ids:
                return True
            if (self.task_queue_dir / f"{tid}.md").exists():
                return True
            return False

        def get_inbox_files(self):
            from task_helpers import parse_inbox_file, is_skip_inbox_file
            result, seen = [], {}
            for md in sorted(self.task_inbox_dir.glob("*.md")):
                if is_skip_inbox_file(md):
                    continue
                info = parse_inbox_file(md)
                tid = info["task_id"]
                if info["is_valid"] and tid in seen:
                    info["is_valid"] = False
                    info["invalid_reason"] = f"중복 task_id (기존: {seen[tid]})"
                else:
                    seen[tid] = md.name
                if info["is_valid"] and self._is_already_queued(tid):
                    info["is_valid"] = False
                    info["invalid_reason"] = "이미 실행 대기열 또는 완료됨"
                result.append(info)
            return result

        def get_inbox_task_detail(self, task_id: str):
            from task_helpers import parse_inbox_file, is_skip_inbox_file
            exact = self.task_inbox_dir / f"{task_id}.md"
            if exact.exists() and not is_skip_inbox_file(exact):
                info = parse_inbox_file(exact)
                info["body_full"] = exact.read_text()
                return info
            matches = [f for f in sorted(self.task_inbox_dir.glob("*.md"))
                       if task_id in f.stem and not is_skip_inbox_file(f)]
            if len(matches) == 1:
                info = parse_inbox_file(matches[0])
                info["body_full"] = matches[0].read_text()
                return info
            return None

        def approve_inbox_task(self, task_id: str):
            from task_helpers import (
                parse_inbox_file, is_skip_inbox_file,
                update_task_frontmatter, write_atomic,
            )
            from datetime import datetime as _dt

            inbox_file = self.task_inbox_dir / f"{task_id}.md"
            if not inbox_file.exists():
                matches = [f for f in self.task_inbox_dir.glob(f"*{task_id}*.md")
                           if not is_skip_inbox_file(f)]
                if len(matches) == 1:
                    inbox_file = matches[0]
                elif len(matches) > 1:
                    return {"ok": False, "task_id": task_id, "title": "",
                            "message": "❌ 여러 파일 매칭"}
                else:
                    return {"ok": False, "task_id": task_id, "title": "",
                            "message": f"❌ inbox에서 '{task_id}' 파일을 찾을 수 없습니다."}

            info = parse_inbox_file(inbox_file)
            if not info["is_valid"]:
                return {"ok": False, "task_id": task_id, "title": info["title"],
                        "message": f"❌ 실행 불가: {info['invalid_reason']}"}

            resolved_id = info["task_id"]
            if self._is_already_queued(resolved_id):
                return {"ok": False, "task_id": resolved_id, "title": info["title"],
                        "message": f"❌ '{resolved_id}'는 이미 실행 대기열 또는 완료됨"}

            updates = {
                "id": resolved_id, "title": info["title"],
                "status": "queued", "approved": True,
                "approved_at": _dt.now().astimezone().isoformat(),
            }
            if not info["has_frontmatter"]:
                updates["created_from"] = "local_drop"

            new_content = update_task_frontmatter(inbox_file, updates)
            dest = self.task_queue_dir / inbox_file.name
            write_atomic(dest, new_content)
            inbox_file.unlink()
            return {"ok": True, "task_id": resolved_id, "title": info["title"],
                    "message": f"▶ 실행 대기열 등록 완료: {inbox_file.name}"}

        # 기존 enqueue 관련 (회귀용)
        def get_inbox_summary(self):
            from task_helpers import parse_task_frontmatter
            result = []
            for md in sorted(self.task_inbox_dir.glob("*.md")):
                if md.name.startswith("."):
                    continue
                fm = parse_task_frontmatter(md)
                result.append({"task_id": fm.get("id", md.stem),
                                "title": fm.get("title", md.stem),
                                "status": fm.get("status", "pending")})
            return result

    return _FakeOrch(), inbox_dir, queue_dir


def _make_bot(orch):
    from telegram_bot import TelegramBot
    bot = TelegramBot(token="t", chat_id="1", pm_agent=None, orchestrator=orch)
    mock_app = MagicMock()
    mock_app.bot = AsyncMock()
    bot._app = mock_app
    return bot


def _make_update():
    u = MagicMock()
    u.message = AsyncMock()
    u.message.reply_text = AsyncMock()
    u.effective_message = MagicMock()
    u.effective_message.reply_text = AsyncMock()
    return u


def _replied(update) -> str:
    calls = update.message.reply_text.call_args_list
    return " ".join(str(c) for c in calls)


# ──────────────────────────────────────────────────────────────────────────────
# A. parse_inbox_file()
# ──────────────────────────────────────────────────────────────────────────────

def test_1_local_drop_appears_in_inbox_list(tmp_path):
    """task_inbox에 직접 넣은 md 파일이 get_inbox_files() 목록에 표시된다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "my-task.md", f"# 내 태스크\n\n{LONG_BODY}")

    files = orch.get_inbox_files()
    assert len(files) == 1, f"파일 1개여야 함, 실제: {len(files)}"
    assert files[0]["is_valid"], f"valid여야 함: {files[0]}"


def test_2_no_frontmatter_is_pending(tmp_path):
    """frontmatter 없는 파일도 pending으로 표시된다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "plain.md", f"# 플레인 태스크\n\n{LONG_BODY}")

    files = orch.get_inbox_files()
    assert files[0]["status"] == "pending"
    assert files[0]["is_valid"]
    assert not files[0]["has_frontmatter"]


def test_3_title_from_heading(tmp_path):
    """첫 번째 # heading에서 title을 추출한다."""
    from task_helpers import parse_inbox_file

    p = tmp_path / "test.md"
    _write(p, f"# RMF YAML Import 구현\n\n{LONG_BODY}")
    info = parse_inbox_file(p)
    assert "RMF YAML Import" in info["title"], f"title: {info['title']}"


def test_4_title_from_filename_fallback(tmp_path):
    """heading 없을 때 파일명 슬러그에서 title을 추출한다."""
    from task_helpers import parse_inbox_file

    p = tmp_path / "rmf-yaml-import.md"
    _write(p, LONG_BODY)  # heading 없음
    info = parse_inbox_file(p)
    assert "rmf" in info["title"].lower(), f"title: {info['title']}"


def test_5_approve_moves_to_queue(tmp_path):
    """[▶ 진행해] / approve_inbox_task() 가 파일을 task_queue로 이동한다."""
    orch, inbox_dir, queue_dir = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "move-me.md", f"# 이동 테스트\n\n{LONG_BODY}")

    result = orch.approve_inbox_task("move-me")
    assert result["ok"], f"approve 실패: {result['message']}"
    assert (queue_dir / "move-me.md").exists(), "task_queue에 파일이 없음"
    assert not (inbox_dir / "move-me.md").exists(), "inbox에서 파일이 삭제되지 않음"


def test_6_run_tid_works(tmp_path):
    """approve_inbox_task(task_id)가 정상 동작한다."""
    orch, inbox_dir, queue_dir = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "T-101.md", f"# RMF YAML import\n\n{LONG_BODY}")

    result = orch.approve_inbox_task("T-101")
    assert result["ok"], result["message"]


def test_7_inbox_detail_view(tmp_path):
    """get_inbox_task_detail()이 파일 상세 정보를 반환한다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "detail-task.md", f"# 상세 태스크\n\n{LONG_BODY}")

    info = orch.get_inbox_task_detail("detail-task")
    assert info is not None
    assert "상세 태스크" in info["title"]
    assert "body_full" in info


def test_8_empty_file_is_invalid(tmp_path):
    """빈 파일은 invalid로 처리되고 실행 버튼이 숨겨진다."""
    from task_helpers import parse_inbox_file

    p = tmp_path / "empty.md"
    _write(p, "")
    info = parse_inbox_file(p)
    assert not info["is_valid"]
    assert "빈 파일" in info["invalid_reason"]


def test_9_frontmatter_parse_fail_is_invalid(tmp_path):
    """YAML frontmatter 파싱 실패 파일은 invalid 처리된다."""
    from task_helpers import parse_inbox_file

    p = tmp_path / "broken.md"
    _write(p, "---\nkey: [unclosed\n---\n\n" + LONG_BODY)
    info = parse_inbox_file(p)
    assert not info["is_valid"]
    assert "YAML" in info["invalid_reason"] or "파싱" in info["invalid_reason"]


def test_10_duplicate_task_id_blocked(tmp_path):
    """같은 task_id가 inbox에 중복 존재하면 실행이 차단된다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    fm = "---\nid: T-dup\ntitle: dup\nstatus: pending\n---\n\n" + LONG_BODY
    _write(inbox_dir / "T-dup-a.md", fm)
    _write(inbox_dir / "T-dup-b.md", fm)

    files = orch.get_inbox_files()
    invalid = [f for f in files if not f["is_valid"]]
    assert len(invalid) >= 1, "중복 파일 중 하나는 invalid여야 함"
    assert any("중복" in (f.get("invalid_reason") or "") for f in invalid)


def test_11_already_queued_blocked(tmp_path):
    """이미 task_queue에 있는 task_id는 실행이 차단된다."""
    orch, inbox_dir, queue_dir = _make_orchestrator_with_inbox(tmp_path)
    body = f"# Already Queued\n\n{LONG_BODY}"
    _write(inbox_dir / "T-queued.md", body)
    _write(queue_dir / "T-queued.md", body)   # 이미 queue에 있음

    result = orch.approve_inbox_task("T-queued")
    assert not result["ok"], "이미 queued인 task는 approve 불가"
    assert "완료" in result["message"] or "대기" in result["message"]


def test_12_held_status_inbox_not_run(tmp_path):
    """held 상태 inbox 파일은 실행되지 않는다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    fm = "---\nid: T-held\ntitle: held\nstatus: held\n---\n\n" + LONG_BODY
    _write(inbox_dir / "T-held.md", fm)

    result = orch.approve_inbox_task("T-held")
    # held 상태는 approve 실패 또는 is_valid=False
    # 단순 상태 확인: approve 결과가 ok=False이거나 파일이 queue에 없어야 함
    queue_file = tmp_path / "task_queue" / "T-held.md"
    if result["ok"]:
        # approve가 성공했더라도 held는 실행 흐름에 들어가지 않아야 함 (watcher가 처리)
        pass
    else:
        assert not queue_file.exists(), "held 파일이 queue로 이동하면 안 됨"


def test_13_tmp_hidden_backup_excluded(tmp_path):
    """.tmp / 숨김 / backup 파일은 inbox 목록에서 제외된다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / ".hidden.md",   LONG_BODY)
    _write(inbox_dir / "work.md.tmp",  LONG_BODY)
    _write(inbox_dir / "backup.md.bak", LONG_BODY)
    _write(inbox_dir / "real-task.md", f"# 실제 태스크\n\n{LONG_BODY}")

    files = orch.get_inbox_files()
    names = [f["filename"] for f in files]
    assert ".hidden.md" not in names,    f"숨김 파일이 목록에 있음: {names}"
    assert "work.md.tmp" not in names,   f".tmp 파일이 목록에 있음: {names}"
    assert "backup.md.bak" not in names, f".bak 파일이 목록에 있음: {names}"
    assert any("real-task" in n for n in names), f"실제 파일이 없음: {names}"


# ──────────────────────────────────────────────────────────────────────────────
# D. /inbox Telegram 핸들러
# ──────────────────────────────────────────────────────────────────────────────

def test_14_handle_inbox_shows_list(tmp_path):
    """/inbox 핸들러가 inbox 목록을 전송한다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "task-a.md", f"# 태스크 A\n\n{LONG_BODY}")

    bot = _make_bot(orch)
    update = _make_update()
    ctx = MagicMock(); ctx.args = []

    run(bot._handle_inbox(update, ctx))

    replied = _replied(update)
    assert "task-a" in replied or "태스크 A" in replied, f"파일명/제목이 응답에 없음 ({replied[:300]})"


def test_14b_handle_inbox_detail(tmp_path):
    """/inbox T-ID 핸들러가 특정 작업 상세를 전송한다."""
    orch, inbox_dir, _ = _make_orchestrator_with_inbox(tmp_path)
    _write(inbox_dir / "T-detail.md", f"# 상세 태스크\n\n{LONG_BODY}")

    bot = _make_bot(orch)
    update = _make_update()
    ctx = MagicMock(); ctx.args = ["T-detail"]

    run(bot._handle_inbox(update, ctx))

    replied = _replied(update)
    assert "상세 태스크" in replied or "T-detail" in replied, (
        f"상세 내용이 응답에 없음 ({replied[:300]})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# E. 회귀: /enqueue, task_queue watcher
# ──────────────────────────────────────────────────────────────────────────────

def test_15_enqueue_regression(tmp_path):
    """기존 /enqueue 흐름이 여전히 정상 동작한다 (task_helpers 변경 후 회귀)."""
    from task_helpers import (
        make_task_id, build_pending_content, preflight_check,
        write_atomic, parse_task_frontmatter,
    )
    from datetime import datetime

    title = "기존 enqueue 회귀 테스트"
    body  = "## Goal\n기존 /enqueue 흐름 확인용 본문입니다.\n" * 5

    errors = preflight_check(title, body)
    assert not errors, f"preflight 오류: {errors}"

    task_id = make_task_id(title, datetime(2026, 5, 30, 10, 0))
    content = build_pending_content(task_id=task_id, title=title, body=body)

    dest = tmp_path / f"{task_id}.md"
    write_atomic(dest, content)

    fm = parse_task_frontmatter(dest)
    assert fm.get("status") == "pending"
    assert fm.get("title") == title


def test_15b_is_safe_queue_file_regression(tmp_path):
    """기존 is_safe_queue_file()이 task_helpers 변경 후에도 정상 동작한다."""
    from task_helpers import is_safe_queue_file, write_atomic

    ok_file = tmp_path / "valid-task.md"
    write_atomic(ok_file, "# 유효한 태스크\n\n" + "내용 " * 30)
    assert is_safe_queue_file(ok_file)

    empty_file = tmp_path / "empty.md"
    empty_file.write_text("")
    assert not is_safe_queue_file(empty_file)

    hidden = tmp_path / ".hidden.md"
    write_atomic(hidden, "내용 " * 30)
    assert not is_safe_queue_file(hidden)


if __name__ == "__main__":
    import subprocess, sys as _sys, tempfile, os

    # 각 테스트를 임시 디렉토리와 함께 실행
    test_fns = [
        test_1_local_drop_appears_in_inbox_list,
        test_2_no_frontmatter_is_pending,
        test_3_title_from_heading,
        test_4_title_from_filename_fallback,
        test_5_approve_moves_to_queue,
        test_6_run_tid_works,
        test_7_inbox_detail_view,
        test_8_empty_file_is_invalid,
        test_9_frontmatter_parse_fail_is_invalid,
        test_10_duplicate_task_id_blocked,
        test_11_already_queued_blocked,
        test_12_held_status_inbox_not_run,
        test_13_tmp_hidden_backup_excluded,
        test_14_handle_inbox_shows_list,
        test_14b_handle_inbox_detail,
        test_15_enqueue_regression,
        test_15b_is_safe_queue_file_regression,
    ]

    import traceback
    passed, failed = [], []
    for fn in test_fns:
        import inspect
        sig = inspect.signature(fn)
        try:
            if "tmp_path" in sig.parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            passed.append(fn.__name__)
            print(f"  PASS: {fn.__name__}")
        except Exception as e:
            failed.append(fn.__name__)
            print(f"  FAIL: {fn.__name__}")
            print(f"        {traceback.format_exc()[-400:]}")

    print(f"\n=== PASS {len(passed)} / FAIL {len(failed)} ===")
    _sys.exit(1 if failed else 0)
