"""
test_rc_hotfix1.py — RC Hotfix 1: ReviewAgent 타임아웃 보호 테스트

테스트 항목:
  1. ReviewAgent 타임아웃 → verdict FAIL (rule=review.timeout)
  2. REVIEW_TIMEOUT category 분류 정확성
  3. 타임아웃 시 main push 호출 안 됨 (SHIPPED 상태 진입 금지)
  4. 타임아웃 시 handoff가 FAILED 상태로 업데이트
  5. 타임아웃 시 실패 카드 전송됨 (notify_failure_card_fn 호출)
  6. 타임아웃 시 stats에 FAILED 기록
  7. Config: REVIEW_TIMEOUT_SECONDS 로드
  8. review.timeout 은 retry 대상이 아님 (_NO_RETRY_RULES)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import Config
from orchestrator import Orchestrator, _classify_failure
from schemas import ReviewVerdict, Violation


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

def _make_config(tmp_path: Path, review_timeout: int = 5) -> Config:
    """review_timeout_seconds가 짧은 테스트용 Config."""
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
        review_timeout_seconds=review_timeout,
    )


def _make_orchestrator(tmp_path: Path, review_timeout: int = 5) -> Orchestrator:
    cfg = _make_config(tmp_path, review_timeout)
    (tmp_path / "task_queue").mkdir(parents=True, exist_ok=True)
    (tmp_path / "completed").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "handoffs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "CLAUDE.md").write_text("# Spec\n")
    orch = Orchestrator(config=cfg, git_manager=None, review_agent=None)
    orch.notify_fn = AsyncMock()
    orch.notify_card_fn = AsyncMock()
    orch.notify_failure_card_fn = AsyncMock()
    return orch


def _make_slow_review_agent(delay: float = 30.0):
    """delay초 후 응답하는 mock ReviewAgent (타임아웃 테스트용)."""
    mock = MagicMock()

    async def _slow_review(**kwargs):
        await asyncio.sleep(delay)
        return ReviewVerdict(verdict="PASS", task_id="T-TEST")

    mock.review = _slow_review
    return mock


# ─── 1. ReviewAgent 타임아웃 → verdict FAIL (rule=review.timeout) ─────────────

def test_review_timeout_produces_fail_verdict():
    """review_timeout_seconds(5s) 이내 응답 없으면 review.timeout FAIL 생성."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)  # 1초 타임아웃
        orch.review_agent = _make_slow_review_agent(delay=10.0)  # 10초 응답

        task_id = "T-TIMEOUT"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        from schemas import CompletedPacket

        packet = CompletedPacket(
            task_id=task_id,
            agent_id="cli",
            files_changed=[],
            code_diff="",
            test_result="",
        )

        async def run():
            orch._running_task = {"task_id": task_id, "branch": "feature/T-TIMEOUT", "phase": "REVIEWING"}
            orch._task_started_at[task_id] = datetime.now()

            # _process_task_inner 내 review 블록만 추출해 직접 테스트
            _review_timeout = getattr(orch.config, "review_timeout_seconds", 120)
            spec_context = orch._load_spec_context()
            try:
                verdict = await asyncio.wait_for(
                    orch.review_agent.review(
                        spec_context=spec_context,
                        completed_packet=packet,
                        allowed_files=None,
                    ),
                    timeout=float(_review_timeout),
                )
            except asyncio.TimeoutError:
                from schemas import ReviewVerdict as RV, Violation as V
                verdict = RV(
                    verdict="FAIL",
                    task_id=task_id,
                    violations=[V(
                        rule="review.timeout",
                        description=f"Review Agent가 {_review_timeout}초 내 응답하지 않았습니다.",
                        severity="ERROR",
                    )],
                    notes=f"REVIEW_TIMEOUT after {_review_timeout}s",
                )
            return verdict

        verdict = asyncio.run(run())
        assert verdict.verdict == "FAIL"
        rules = {v.rule for v in verdict.violations}
        assert "review.timeout" in rules, f"review.timeout 없음: {rules}"
        assert "REVIEW_TIMEOUT" in verdict.notes
    print("1. ReviewAgent 타임아웃 → verdict FAIL (rule=review.timeout): PASS")


# ─── 2. REVIEW_TIMEOUT category 분류 ─────────────────────────────────────────

def test_classify_failure_review_timeout():
    """review.timeout violation → REVIEW_TIMEOUT 분류."""
    verdict = ReviewVerdict(
        verdict="FAIL",
        task_id="T-X",
        violations=[Violation(rule="review.timeout", description="timeout", severity="ERROR")],
    )
    category = _classify_failure(None, verdict)
    assert category == "REVIEW_TIMEOUT", f"expected REVIEW_TIMEOUT, got {category}"
    print("2. REVIEW_TIMEOUT category 분류: PASS")


# ─── 3. 타임아웃 시 main push 호출 안 됨 ─────────────────────────────────────

def test_timeout_does_not_trigger_ship():
    """타임아웃 FAIL → _execute_ship / commit_and_merge 호출 없음."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)
        orch.review_agent = _make_slow_review_agent(delay=10.0)

        task_id = "T-NOPUSH"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        # git_manager mock
        mock_gm = MagicMock()
        mock_gm.create_branch = MagicMock(return_value="feature/T-NOPUSH")
        mock_gm.commit_and_merge = MagicMock()
        mock_gm.release_lock = MagicMock()
        mock_gm.get_state = MagicMock(return_value={"active_tasks": {}, "completed_tasks": [], "pending_merge": []})
        orch.git_manager = mock_gm

        async def run():
            # _run_cli mock: 빠르게 완료
            stdout_log = tmp_path / "logs" / "tasks" / f"{task_id}.stdout.log"
            stderr_log = tmp_path / "logs" / "tasks" / f"{task_id}.stderr.log"
            combined_log = tmp_path / "logs" / "tasks" / f"{task_id}.combined.log"

            with patch.object(orch, "_run_cli", new=AsyncMock(return_value=("파일 생성 완료", 0))), \
                 patch.object(orch, "_collect_git_diff", new=AsyncMock(return_value={
                     "actual_diff": "",
                     "changed_files": [],
                     "diff_stat": "",
                     "diff_numstat": "",
                     "name_status_raw": "",
                     "git_status": "",
                     "branch": "feature/T-NOPUSH",
                 })), \
                 patch.object(orch, "_prune_worktrees", return_value=None):
                await orch._process_task(task_path)

        asyncio.run(run())

        # commit_and_merge (ship) 절대 호출 안 됨
        mock_gm.commit_and_merge.assert_not_called()
        print("3. 타임아웃 시 commit_and_merge 호출 없음: PASS")

        # SHIPPED 상태 진입 금지
        assert task_id not in orch._ready_to_ship, "타임아웃 후 READY_TO_SHIP 진입 금지"
        assert task_id in orch._failed_tasks, "타임아웃 후 _failed_tasks 등록 필요"
        category = orch._failed_tasks[task_id].get("failure_category")
        assert category == "REVIEW_TIMEOUT", f"failure_category={category}"
        print("3. 타임아웃 시 main push 호출 안 됨: PASS")


# ─── 4. 타임아웃 시 handoff FAILED 업데이트 ──────────────────────────────────

def test_timeout_updates_handoff():
    """타임아웃 → _on_fail → handoff FAILED 상태로 업데이트."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)
        orch.review_agent = _make_slow_review_agent(delay=10.0)

        task_id = "T-HF"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        async def run():
            with patch.object(orch, "_run_cli", new=AsyncMock(return_value=("완료", 0))), \
                 patch.object(orch, "_collect_git_diff", new=AsyncMock(return_value={
                     "actual_diff": "",
                     "changed_files": [],
                     "diff_stat": "",
                     "diff_numstat": "",
                     "name_status_raw": "",
                     "git_status": "",
                     "branch": "",
                 })), \
                 patch.object(orch, "_prune_worktrees", return_value=None):
                await orch._process_task(task_path)

        asyncio.run(run())

        handoff_path = tmp_path / "handoffs" / f"{task_id}.md"
        assert handoff_path.exists(), "handoff 파일 미생성"
        content = handoff_path.read_text()
        assert "FAILED" in content, f"handoff에 FAILED 없음: {content[:200]}"
        print("4. 타임아웃 시 handoff FAILED 업데이트: PASS")


# ─── 5. 타임아웃 시 실패 카드 전송됨 ─────────────────────────────────────────

def test_timeout_sends_failure_card():
    """타임아웃 → notify_failure_card_fn 또는 notify_fn 호출됨."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)
        orch.review_agent = _make_slow_review_agent(delay=10.0)

        task_id = "T-CARD"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        async def run():
            with patch.object(orch, "_run_cli", new=AsyncMock(return_value=("완료", 0))), \
                 patch.object(orch, "_collect_git_diff", new=AsyncMock(return_value={
                     "actual_diff": "",
                     "changed_files": [],
                     "diff_stat": "",
                     "diff_numstat": "",
                     "name_status_raw": "",
                     "git_status": "",
                     "branch": "",
                 })), \
                 patch.object(orch, "_prune_worktrees", return_value=None):
                await orch._process_task(task_path)

        asyncio.run(run())

        # notify_failure_card_fn 또는 notify_fn 중 하나 호출됨
        failure_called = orch.notify_failure_card_fn.called
        notify_called = orch.notify_fn.called
        assert failure_called or notify_called, "실패 알림 미전송"

        # 카드 내용에 REVIEW_TIMEOUT 포함
        if failure_called:
            card_text = orch.notify_failure_card_fn.call_args[0][0]
        else:
            card_text = orch.notify_fn.call_args[0][0]
        assert "REVIEW_TIMEOUT" in card_text, f"카드에 REVIEW_TIMEOUT 없음: {card_text[:200]}"
        print("5. 타임아웃 시 실패 카드 전송됨: PASS")


# ─── 6. 타임아웃 시 stats FAILED 기록 ───────────────────────────────────────

def test_timeout_records_stats_failed():
    """타임아웃 → stats에 FAILED 기록."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)
        orch.review_agent = _make_slow_review_agent(delay=10.0)

        task_id = "T-STAT"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        async def run():
            with patch.object(orch, "_run_cli", new=AsyncMock(return_value=("완료", 0))), \
                 patch.object(orch, "_collect_git_diff", new=AsyncMock(return_value={
                     "actual_diff": "",
                     "changed_files": [],
                     "diff_stat": "",
                     "diff_numstat": "",
                     "name_status_raw": "",
                     "git_status": "",
                     "branch": "",
                 })), \
                 patch.object(orch, "_prune_worktrees", return_value=None):
                await orch._process_task(task_path)

        asyncio.run(run())

        summary = orch._stats.get_summary()
        assert summary["global"].get("failed", 0) >= 1, "stats에 FAILED 미기록"
        print("6. 타임아웃 시 stats FAILED 기록: PASS")


# ─── 7. Config: REVIEW_TIMEOUT_SECONDS 로드 ──────────────────────────────────

def test_config_review_timeout_default():
    """Config 기본값 120, 필드 존재 확인."""
    cfg = Config(
        anthropic_api_key="k",
        repo_path="/tmp",
        spec_path="/tmp/spec.md",
    )
    assert hasattr(cfg, "review_timeout_seconds"), "Config.review_timeout_seconds 필드 없음"
    assert cfg.review_timeout_seconds == 120
    print("7. Config.review_timeout_seconds 기본값 120: PASS")


def test_config_review_timeout_custom():
    """Config 커스텀 값 설정."""
    cfg = Config(
        anthropic_api_key="k",
        repo_path="/tmp",
        spec_path="/tmp/spec.md",
        review_timeout_seconds=60,
    )
    assert cfg.review_timeout_seconds == 60
    print("7. Config.review_timeout_seconds 커스텀값 60: PASS")


# ─── 8. review.timeout은 retry 대상 아님 ─────────────────────────────────────

def test_review_timeout_no_retry():
    """review.timeout이 _NO_RETRY_RULES에 포함되어 재시도 없이 즉시 FAIL."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path, review_timeout=1)

        call_count = 0

        async def _counting_slow_review(**kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(10.0)  # 타임아웃 유발
            return ReviewVerdict(verdict="PASS", task_id="T-X")

        mock = MagicMock()
        mock.review = _counting_slow_review
        orch.review_agent = mock

        task_id = "T-NORETRY"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트\n")

        async def run():
            with patch.object(orch, "_run_cli", new=AsyncMock(return_value=("완료", 0))), \
                 patch.object(orch, "_collect_git_diff", new=AsyncMock(return_value={
                     "actual_diff": "",
                     "changed_files": [],
                     "diff_stat": "",
                     "diff_numstat": "",
                     "name_status_raw": "",
                     "git_status": "",
                     "branch": "",
                 })), \
                 patch.object(orch, "_prune_worktrees", return_value=None):
                await orch._process_task(task_path)

        asyncio.run(run())

        # 재시도가 없으므로 review.review()는 1번만 호출
        assert call_count == 1, f"review 1번만 호출해야 하는데 {call_count}번 호출됨"
        print("8. review.timeout → retry 없이 1회만 호출: PASS")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_review_timeout_produces_fail_verdict,
        test_classify_failure_review_timeout,
        test_timeout_does_not_trigger_ship,
        test_timeout_updates_handoff,
        test_timeout_sends_failure_card,
        test_timeout_records_stats_failed,
        test_config_review_timeout_default,
        test_config_review_timeout_custom,
        test_review_timeout_no_retry,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n{'='*55}")
    print(f"결과: {passed}/{len(tests)} PASS, {failed} FAIL")
    if failed:
        sys.exit(1)
