"""
test_batch5.py — Batch 5 안정화/UX polish 테스트

테스트 항목:
  1. /adopt → ADOPTED 상태 (READY_TO_SHIP 직접 진입 금지)
  2. /doctor 정보 구조
  3. /queue 요약
  4. callback: 이미 SHIPPED인 task에서 ship 버튼
  5. callback: 존재하지 않는 task_id
  6. handoff dedup — Done 중복 방지
  7. handoff max length
  8. ship_task "already shipped" 안내
  9. hold_task "already shipped" 안내
"""
from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# pm_agent_system 디렉토리를 sys.path에 추가
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import Config
from orchestrator import Orchestrator, _extract_handoff_section, _replace_handoff_section
from schemas import ReviewVerdict


def _make_config(tmp_path: Path) -> Config:
    return Config(
        anthropic_api_key="test-key",
        repo_path=str(tmp_path),
        spec_path=str(tmp_path / "CLAUDE.md"),
        dry_run=True,
        auto_ship_after_review=False,
        task_queue_dir=str(tmp_path / "task_queue"),
        completed_dir=str(tmp_path / "completed"),
        state_path=str(tmp_path / "state/git_state.json"),
        logs_dir=str(tmp_path / "logs"),
    )


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    cfg = _make_config(tmp_path)
    (tmp_path / "task_queue").mkdir(parents=True, exist_ok=True)
    (tmp_path / "completed").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "handoffs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "CLAUDE.md").write_text("# Test Spec\n")
    orch = Orchestrator(config=cfg, git_manager=None, review_agent=None)
    # notify_fn: silent
    orch.notify_fn = AsyncMock()
    return orch


# ─── 1. /adopt → ADOPTED 상태 (READY_TO_SHIP 직접 진입 금지) ────────────────

def test_adopt_goes_to_adopted_not_ready_to_ship():
    """adopt_task()는 _adopted_tasks에 저장해야 하며 _ready_to_ship에 저장하면 안 됨."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        async def run():
            # git 명령 mock
            with patch.object(orch, "_get_current_branch", return_value="feature/T-91"), \
                 patch.object(orch, "_collect_git_diff", return_value={
                     "actual_diff": "diff --git a/foo.py b/foo.py\n+pass",
                     "changed_files": ["foo.py"],
                     "diff_stat": "1 file changed",
                     "diff_numstat": "1\t0\tfoo.py",
                     "name_status_raw": "M\tfoo.py",
                     "git_status": "M foo.py",
                 }), \
                 patch.object(orch, "_get_files_vs_main", return_value=["foo.py"]):
                result = await orch.adopt_task("T-91")

            assert "T-91" in orch._adopted_tasks, "ADOPTED에 없음"
            assert "T-91" not in orch._ready_to_ship, "READY_TO_SHIP에 있으면 안 됨"
            assert "ADOPTED" in result, f"응답에 ADOPTED 없음: {result}"
            print(f"  [PASS] adopt → ADOPTED 상태 (not READY_TO_SHIP)")
            return result

        asyncio.run(run())


def test_adopt_duplicate_returns_info():
    """이미 ADOPTED인 task를 다시 adopt하면 안내 메시지 반환."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        orch._adopted_tasks["T-91"] = {"task_id": "T-91", "branch": "feature/T-91"}

        async def run():
            result = await orch.adopt_task("T-91")
            assert "이미 ADOPTED" in result or "ADOPTED 상태" in result, f"예상 메시지 없음: {result}"
            print(f"  [PASS] 중복 adopt → 안내 메시지")

        asyncio.run(run())


def test_ship_blocked_on_adopted():
    """ADOPTED 상태 task에 ship_task()는 차단 메시지 반환."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        orch._adopted_tasks["T-91"] = {"task_id": "T-91", "branch": "feature/T-91"}

        async def run():
            result = await orch.ship_task("T-91")
            assert "ADOPTED" in result, f"ADOPTED 차단 메시지 없음: {result}"
            assert "/review" in result or "Review" in result, f"review 안내 없음: {result}"
            print(f"  [PASS] ship ADOPTED → 차단 메시지")

        asyncio.run(run())


# ─── 2. /doctor 정보 구조 ──────────────────────────────────────────────────

def test_doctor_info_structure():
    """`get_doctor_info()`가 필수 키를 모두 포함해야 함."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        async def run():
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                # git 명령 mock
                proc_mock = AsyncMock()
                proc_mock.communicate = AsyncMock(return_value=(b"main\n", b""))
                mock_exec.return_value = proc_mock

                info = await orch.get_doctor_info()

            required_keys = [
                "project_id", "repo_path", "repo_exists",
                "task_queue", "task_queue_exists",
                "running_task_id", "running_phase",
                "ready_to_ship_ids", "adopted_ids", "failed_ids",
                "git_branch", "git_status_short",
                "auto_ship_after_review", "spec_exists",
            ]
            for key in required_keys:
                assert key in info, f"누락된 키: {key}"

            assert info["repo_exists"] is True, "repo_path 폴더 존재 확인 실패"
            assert info["task_queue_exists"] is True
            assert isinstance(info["ready_to_ship_ids"], list)
            assert isinstance(info["adopted_ids"], list)
            assert isinstance(info["failed_ids"], list)
            assert info["auto_ship_after_review"] is False
            print(f"  [PASS] doctor_info 구조 검증")

        asyncio.run(run())


# ─── 3. /queue 요약 ─────────────────────────────────────────────────────────

def test_queue_summary_empty():
    """`get_queue_summary()`는 빈 상태에서 빈 리스트 반환."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))
        tasks = orch.get_queue_summary()
        assert tasks == [], f"비어 있어야 함: {tasks}"
        print("  [PASS] queue 빈 상태")


def test_queue_summary_all_states():
    """`get_queue_summary()`가 RUNNING/ADOPTED/READY_TO_SHIP/FAILED 모두 포함."""
    import tempfile
    from datetime import datetime
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        orch._running_task = {"task_id": "T-10", "phase": "RUNNING", "branch": "feat/T-10"}
        orch._adopted_tasks["T-20"] = {"branch": "feat/T-20"}
        orch._ready_to_ship["T-30"] = {"branch": "feat/T-30", "ready_at": datetime.now().isoformat()}
        orch._failed_tasks["T-40"] = {"branch": "", "failure_category": "CLI_ERROR"}

        tasks = orch.get_queue_summary()
        ids = [t["task_id"] for t in tasks]

        assert "T-10" in ids, f"RUNNING 없음: {ids}"
        assert "T-20" in ids, f"ADOPTED 없음: {ids}"
        assert "T-30" in ids, f"READY_TO_SHIP 없음: {ids}"
        assert "T-40" in ids, f"FAILED 없음: {ids}"

        statuses = {t["task_id"]: t["status"] for t in tasks}
        assert statuses["T-10"] == "RUNNING"
        assert statuses["T-20"] == "ADOPTED"
        assert statuses["T-30"] == "READY_TO_SHIP"
        assert "FAILED" in statuses["T-40"]
        print("  [PASS] queue 4가지 상태 모두 포함")


# ─── 4. ship_task "already shipped" 안내 ────────────────────────────────────

def test_ship_task_already_shipped():
    """완료된 task에 ship 버튼 클릭 시 '이미 배포 완료' 메시지."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        # git_manager mock: completed_tasks에 T-91 포함
        mock_gm = MagicMock()
        mock_gm.get_state.return_value = {
            "completed_tasks": [{"task_id": "T-91"}],
            "active_tasks": {},
        }
        orch.git_manager = mock_gm

        async def run():
            result = await orch.ship_task("T-91")
            assert "이미 배포 완료" in result or "이미" in result, f"예상 메시지 없음: {result}"
            print("  [PASS] ship already-shipped → '이미 배포 완료' 메시지")

        asyncio.run(run())


def test_hold_task_already_shipped():
    """완료된 task에 hold 버튼 클릭 시 '이미 배포 완료' 메시지."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        mock_gm = MagicMock()
        mock_gm.get_state.return_value = {
            "completed_tasks": [{"task_id": "T-91"}],
            "active_tasks": {},
        }
        orch.git_manager = mock_gm

        result = orch.hold_task("T-91")
        assert "이미" in result, f"예상 메시지 없음: {result}"
        print("  [PASS] hold already-shipped → 안내 메시지")


def test_ship_nonexistent_task():
    """존재하지 않는 task_id에 ship → READY_TO_SHIP 목록 안내."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        async def run():
            result = await orch.ship_task("T-999")
            assert "READY_TO_SHIP" in result, f"안내 메시지 없음: {result}"
            print("  [PASS] ship 없는 task → READY_TO_SHIP 안내")

        asyncio.run(run())


# ─── 5. handoff dedup — Done 중복 방지 ─────────────────────────────────────

def test_handoff_done_dedup():
    """동일 done_item이 두 번 추가되어도 Done 섹션에 한 번만 기록."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        orch._update_handoff("T-50", done_item="Review PASS")
        orch._update_handoff("T-50", done_item="Review PASS")  # 중복

        handoff_path = orch._handoffs_dir / "T-50.md"
        content = handoff_path.read_text(encoding="utf-8")
        done_section = _extract_handoff_section(content, "Done")

        count = done_section.count("Review PASS")
        assert count == 1, f"Done에 중복 기록됨 ({count}회): {done_section}"
        print("  [PASS] handoff Done 중복 방지")


# ─── 6. handoff max length ──────────────────────────────────────────────────

def test_handoff_max_length():
    """handoff 파일이 8,000자를 초과하지 않음."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))

        # 긴 내용으로 반복 업데이트
        for i in range(20):
            orch._update_handoff(
                "T-60",
                current_status=f"상태 업데이트 {i} " + "x" * 500,
                done_item=f"작업 {i} 완료",
                risks="x" * 300,
            )

        handoff_path = orch._handoffs_dir / "T-60.md"
        content = handoff_path.read_text(encoding="utf-8")
        assert len(content) <= 8200, f"handoff 너무 김: {len(content)}자"
        print(f"  [PASS] handoff 최대 길이 제한 ({len(content)}자 ≤ 8200)")


# ─── 7. adopt + review_adopted_task 흐름 (ADOPTED → REVIEWING → READY_TO_SHIP) ─

def test_adopt_review_pass_flow():
    """ADOPTED → /review PASS → READY_TO_SHIP 정상 흐름."""
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        # 수동으로 ADOPTED 상태 설정
        orch._adopted_tasks["T-91"] = {
            "task_id": "T-91",
            "branch": "feature/T-91",
            "task_content": "# T-91\n\ntest task",
            "diff_info": {},
            "adopted_at": "2026-05-25T00:00:00",
        }

        mock_verdict = ReviewVerdict(verdict="PASS", task_id="T-91", notes="looks good")
        mock_review_agent = MagicMock()
        mock_review_agent.review = AsyncMock(return_value=mock_verdict)
        orch.review_agent = mock_review_agent

        async def run():
            with patch.object(orch, "_collect_git_diff", return_value={
                "actual_diff": "diff --git a/foo.py b/foo.py\n+pass\n",
                "changed_files": ["foo.py"],
                "diff_stat": "",
                "diff_numstat": "",
                "name_status_raw": "M\tfoo.py",
                "git_status": "M foo.py",
            }), patch.object(orch, "_get_files_vs_main", return_value=[]):
                result = await orch.review_adopted_task("T-91")

            assert "T-91" not in orch._adopted_tasks, "ADOPTED에 남아있으면 안 됨"
            assert "T-91" in orch._ready_to_ship, "READY_TO_SHIP에 없음"
            assert "READY_TO_SHIP" in result or "통과" in result, f"결과 메시지 이상: {result}"
            print("  [PASS] ADOPTED → REVIEWING → READY_TO_SHIP")

        asyncio.run(run())


def test_adopt_review_fail_keeps_adopted():
    """FAIL 시 task가 ADOPTED에 남아 retry 가능."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orchestrator(Path(tmp))
        orch._adopted_tasks["T-91"] = {
            "task_id": "T-91",
            "branch": "feature/T-91",
            "task_content": "# T-91",
            "diff_info": {},
            "adopted_at": "2026-05-25T00:00:00",
        }

        mock_verdict = ReviewVerdict(verdict="FAIL", task_id="T-91", notes="missing test")
        mock_ra = MagicMock()
        mock_ra.review = AsyncMock(return_value=mock_verdict)
        orch.review_agent = mock_ra

        async def run():
            with patch.object(orch, "_collect_git_diff", return_value={
                "actual_diff": "diff --git a/foo.py b/foo.py\n+pass\n",
                "changed_files": ["foo.py"],
                "diff_stat": "", "diff_numstat": "",
                "name_status_raw": "M\tfoo.py", "git_status": "",
            }), patch.object(orch, "_get_files_vs_main", return_value=[]):
                result = await orch.review_adopted_task("T-91")

            assert "T-91" in orch._adopted_tasks, "FAIL 후 ADOPTED에서 사라지면 안 됨"
            assert "T-91" not in orch._ready_to_ship
            print("  [PASS] ADOPTED review FAIL → ADOPTED 유지 (retry 가능)")

        asyncio.run(run())


# ─── 실행 ───────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        ("1. adopt → ADOPTED 상태",          test_adopt_goes_to_adopted_not_ready_to_ship),
        ("1b. 중복 adopt 안내",               test_adopt_duplicate_returns_info),
        ("1c. ship ADOPTED 차단",             test_ship_blocked_on_adopted),
        ("2. doctor 정보 구조",               test_doctor_info_structure),
        ("3a. queue 빈 상태",                 test_queue_summary_empty),
        ("3b. queue 4가지 상태",              test_queue_summary_all_states),
        ("4a. ship already-shipped",         test_ship_task_already_shipped),
        ("4b. hold already-shipped",         test_hold_task_already_shipped),
        ("4c. ship 없는 task",               test_ship_nonexistent_task),
        ("5. handoff Done dedup",            test_handoff_done_dedup),
        ("6. handoff max length",            test_handoff_max_length),
        ("7a. ADOPTED→PASS→READY_TO_SHIP",   test_adopt_review_pass_flow),
        ("7b. ADOPTED→FAIL 유지",            test_adopt_review_fail_keeps_adopted),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] {name}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"결과: {passed} passed / {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
