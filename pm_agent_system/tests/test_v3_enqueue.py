"""PM Bot V3 — /enqueue + task_inbox 스모크 테스트.

시나리오:
1. /enqueue → task_inbox/ 에 pending 파일 생성
2. pending 파일은 즉시 실행되지 않음
3. 승인 → task_queue/ 로 이동
4. 이동된 파일은 frontmatter status=queued
5. watcher: .tmp 파일 무시
6. watcher: 비어있거나 너무 짧은 .md 무시
7. 기존 직접 task_queue 투입 방식 여전히 동작 (is_safe_queue_file)
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile
from pathlib import Path

from task_helpers import (
    slugify_title, make_task_id, write_atomic,
    parse_task_frontmatter, update_task_frontmatter,
    build_pending_content, preflight_check,
    is_safe_queue_file, MIN_BODY_CHARS,
)

PASS = "PASS"
FAIL = "FAIL"
_results: list[tuple[str, str]] = []

def check(name: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    _results.append((name, status))
    print(f"{len(_results)}. {name}: {status}")


# ── 헬퍼 유닛 테스트 ──────────────────────────────────────────────────────

def test_slugify() -> None:
    # '/' 는 특수문자로 제거됨 → "importexport" (붙음)
    check("slugify 기본", slugify_title("RMF building_map YAML import/export")
          == "rmf-building-map-yaml-importexport")
    check("slugify 특수문자 제거", slugify_title("Task #1: Hello World!") == "task-1-hello-world")
    check("slugify max_len", len(slugify_title("a" * 100, max_len=20)) == 20)


def test_make_task_id() -> None:
    from datetime import datetime
    now = datetime(2026, 5, 26, 20, 1)
    tid = make_task_id("RMF YAML import", now)
    check("task_id 형식", tid == "2026-05-26_2001_rmf-yaml-import")


def test_write_atomic(tmp_path: Path) -> None:
    dest = tmp_path / "test.md"
    write_atomic(dest, "hello world")
    check("atomic write 파일 존재", dest.exists())
    check("atomic write 내용", dest.read_text() == "hello world")
    check("atomic write .tmp 없음", not (tmp_path / "test.md.tmp").exists())


def test_frontmatter_roundtrip(tmp_path: Path) -> None:
    content = "---\nid: T-1\nstatus: pending\n---\n\n# Task: hello\n"
    f = tmp_path / "task.md"
    f.write_text(content)
    fm = parse_task_frontmatter(f)
    check("frontmatter 파싱 id", fm.get("id") == "T-1")
    check("frontmatter 파싱 status", fm.get("status") == "pending")

    updated = update_task_frontmatter(f, {"status": "queued", "approved": True})
    f.write_text(updated)
    fm2 = parse_task_frontmatter(f)
    check("frontmatter 업데이트 status", fm2.get("status") == "queued")
    check("frontmatter 업데이트 approved", fm2.get("approved") is True)
    check("frontmatter body 보존", "# Task: hello" in updated)


def test_preflight() -> None:
    good_body = "A" * MIN_BODY_CHARS
    check("preflight 정상", preflight_check("제목", good_body) == [])
    check("preflight 빈 제목", len(preflight_check("", good_body)) > 0)
    check("preflight 짧은 body", len(preflight_check("제목", "short")) > 0)
    check("preflight 금지 키워드", len(preflight_check("제목 .env", good_body)) > 0)


def test_is_safe_queue_file(tmp_path: Path) -> None:
    # 정상 파일
    good = tmp_path / "task.md"
    good.write_text("x" * MIN_BODY_CHARS)
    check("safe: 정상 .md", is_safe_queue_file(good))

    # .tmp 파일
    tmp_file = tmp_path / "task.md.tmp"
    tmp_file.write_text("x" * MIN_BODY_CHARS)
    check("safe: .tmp 거부", not is_safe_queue_file(tmp_file))

    # 숨김 파일
    hidden = tmp_path / ".hidden.md"
    hidden.write_text("x" * MIN_BODY_CHARS)
    check("safe: 숨김 파일 거부", not is_safe_queue_file(hidden))

    # 빈 파일
    empty = tmp_path / "empty.md"
    empty.write_text("")
    check("safe: 빈 파일 거부", not is_safe_queue_file(empty))

    # 너무 짧은 파일
    short = tmp_path / "short.md"
    short.write_text("hi")
    check("safe: 짧은 파일 거부", not is_safe_queue_file(short))

    # .txt 파일
    txt = tmp_path / "task.txt"
    txt.write_text("x" * MIN_BODY_CHARS)
    check("safe: .txt 거부", not is_safe_queue_file(txt))


# ── Orchestrator 통합 테스트 ──────────────────────────────────────────────

def _make_orchestrator(tmp_path: Path):
    """테스트용 최소 Orchestrator 인스턴스."""
    from unittest.mock import MagicMock, AsyncMock
    from config import Config
    from orchestrator import Orchestrator

    cfg = Config(
        anthropic_api_key="test",
        repo_path=str(tmp_path),
        spec_path=str(tmp_path / "CLAUDE.md"),
        task_queue_dir=str(tmp_path / "task_queue"),
        completed_dir=str(tmp_path / "completed"),
        logs_dir=str(tmp_path / "logs"),
        dry_run=True,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "CLAUDE.md").write_text("# spec")

    git_mock = MagicMock()
    git_mock.get_state.return_value = {"completed_tasks": []}
    git_mock.repo_path = tmp_path

    orch = Orchestrator(config=cfg, git_manager=git_mock)
    # inbox 디렉토리를 tmp 기반으로 override
    orch.task_inbox_dir = tmp_path / "task_inbox"
    orch.task_inbox_dir.mkdir(parents=True, exist_ok=True)
    orch.task_queue_dir = tmp_path / "task_queue"
    orch.task_queue_dir.mkdir(parents=True, exist_ok=True)
    return orch


def test_enqueue_creates_pending(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    body = "## Goal\n" + "상세한 내용입니다 " * 20
    result = orch.enqueue_task(title="RMF YAML import", body=body)

    check("enqueue: errors 없음", result["errors"] == [])
    check("enqueue: task_id 존재", bool(result["task_id"]))
    check("enqueue: 파일 존재", Path(result["path"]).exists())

    fm = parse_task_frontmatter(Path(result["path"]))
    check("enqueue: status=pending", fm.get("status") == "pending")
    check("enqueue: approved=False", fm.get("approved") is False)
    check("enqueue: created_from=telegram", fm.get("created_from") == "telegram")


def test_pending_not_in_queue(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    body = "## Goal\n" + "상세한 내용입니다 " * 20
    result = orch.enqueue_task(title="테스트 작업", body=body)

    task_id = result["task_id"]
    queue_file = orch.task_queue_dir / f"{task_id}.md"
    check("pending: task_queue에 파일 없음", not queue_file.exists())


def test_approve_moves_to_queue(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    body = "## Goal\n" + "상세한 내용입니다 승인 테스트 " * 10
    result = orch.enqueue_task(title="승인 테스트", body=body)
    task_id = result["task_id"]
    inbox_file = Path(result["path"])

    approve_result = orch.approve_inbox_task(task_id)
    check("approve: ok=True", approve_result["ok"] is True)
    check("approve: inbox 파일 삭제", not inbox_file.exists())

    queue_file = orch.task_queue_dir / f"{task_id}.md"
    check("approve: queue 파일 생성", queue_file.exists())

    fm = parse_task_frontmatter(queue_file)
    check("approve: status=queued", fm.get("status") == "queued")
    check("approve: approved=True", fm.get("approved") is True)
    check("approve: approved_at 존재", bool(fm.get("approved_at")))


def test_approve_nonexistent_task(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    result = orch.approve_inbox_task("no-such-task-id")
    check("approve 없는 task: ok=False", result["ok"] is False)
    check("approve 없는 task: 에러 메시지", "찾을 수 없" in result["message"])


def test_get_inbox_summary(tmp_path: Path) -> None:
    import time
    orch = _make_orchestrator(tmp_path)
    body = "## Goal\n" + "상세한 내용입니다 " * 20  # MIN_BODY_CHARS 충분히 초과

    orch.enqueue_task(title="작업 A", body=body)
    time.sleep(0.01)  # task_id 타임스탬프 충돌 방지
    orch.enqueue_task(title="작업 B", body=body, priority="high")

    inbox = orch.get_inbox_summary()
    check("inbox_summary: 2개", len(inbox) == 2)
    titles = [t["title"] for t in inbox]
    check("inbox_summary: 작업 A 포함", "작업 A" in titles)
    check("inbox_summary: 작업 B 포함", "작업 B" in titles)
    priorities = {t["title"]: t["priority"] for t in inbox}
    check("inbox_summary: priority high", priorities.get("작업 B") == "high")


def test_direct_queue_still_works(tmp_path: Path) -> None:
    """기존 직접 task_queue 투입 방식 — is_safe_queue_file 통과."""
    content = "# Task: 직접 투입\n\n## Goal\n" + "내용 " * 30
    queue_file = tmp_path / "task_queue" / "01_T-direct.md"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(content)
    check("직접투입: is_safe_queue_file PASS", is_safe_queue_file(queue_file))


# ── 실행 ──────────────────────────────────────────────────────────────────

def run_all() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        test_slugify()
        test_make_task_id()
        (tmp / "atomic").mkdir(exist_ok=True)
        test_write_atomic(tmp / "atomic")
        (tmp / "fm").mkdir(exist_ok=True)
        test_frontmatter_roundtrip(tmp / "fm")
        test_preflight()
        (tmp / "safe").mkdir(exist_ok=True)
        test_is_safe_queue_file(tmp / "safe")
        test_enqueue_creates_pending(tmp / "enqueue")
        test_pending_not_in_queue(tmp / "not_in_queue")
        test_approve_moves_to_queue(tmp / "approve")
        test_approve_nonexistent_task(tmp / "no_task")
        test_get_inbox_summary(tmp / "inbox_summary")
        test_direct_queue_still_works(tmp / "direct")

    passed = sum(1 for _, s in _results if s == PASS)
    failed = sum(1 for _, s in _results if s == FAIL)
    print(f"\n{'='*55}")
    print(f"결과: {passed}/{len(_results)} PASS, {failed} FAIL")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
