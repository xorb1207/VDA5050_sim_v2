"""
test_batch6.py — Batch 6 장기 운영 인프라 테스트

테스트 항목:
  1. ArchiveManager.archive_task — meta.json 생성 확인
  2. ArchiveManager.archive_task — 로그 파일 gzip 압축 확인
  3. ArchiveManager.list_history — 최신순 정렬 + status_filter
  4. ArchiveManager.is_archived — 아카이브 여부 확인
  5. ArchiveManager.cleanup_old_logs — retention 초과 .log 삭제
  6. StatsTracker.record — SHIPPED/FAILED/HELD 카운터
  7. StatsTracker.record — 지원하지 않는 status 무시
  8. StatsTracker.get_summary — pass_rate / avg_elapsed_s 계산
  9. StatsTracker.get_today_summary — 오늘 날짜 필터
  10. StatsTracker.get_period_summary — 기간 필터
  11. Orchestrator: hold_task → _held_tasks 등록
  12. Orchestrator: resume_task → _held_tasks 해제
  13. Orchestrator: get_stale_tasks — HELD 7일 초과 감지
  14. Orchestrator: get_stale_tasks — READY_TO_SHIP 3일 초과 감지
  15. Orchestrator: get_stale_tasks — 미달 태스크는 포함 안 함
  16. Orchestrator: _task_started_at — hold_task 시 stats HELD 기록
  17. Orchestrator: archive_task_manual — 알 수 없는 task → UNKNOWN 상태
  18. Orchestrator: get_history — ArchiveManager 위임 확인
  19. Orchestrator: get_stats_summary — StatsTracker 위임 확인
  20. Orchestrator: get_today_stats — StatsTracker 오늘 통계 위임 확인
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# pm_agent_system 디렉토리를 sys.path에 추가
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from archive_manager import ArchiveManager
from stats_tracker import StatsTracker
from config import Config
from orchestrator import Orchestrator


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

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
    orch.notify_fn = AsyncMock()
    return orch


def _make_archive_manager(tmp_path: Path) -> ArchiveManager:
    archive_dir = tmp_path / "archive"
    logs_dir = tmp_path / "logs"
    handoffs_dir = tmp_path / "handoffs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    return ArchiveManager(archive_dir=archive_dir, logs_dir=logs_dir, handoffs_dir=handoffs_dir)


# ─── 1. ArchiveManager.archive_task — meta.json 생성 ─────────────────────────

def test_archive_task_creates_meta_json():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mgr = _make_archive_manager(tmp_path)
        arc_path = mgr.archive_task("T-100", "SHIPPED", metadata={"commit_sha": "abc123"})

        meta_file = arc_path / "meta.json"
        assert meta_file.exists(), "meta.json이 생성되어야 합니다"
        meta = json.loads(meta_file.read_text())
        assert meta["task_id"] == "T-100"
        assert meta["status"] == "SHIPPED"
        assert meta["commit_sha"] == "abc123"
        assert "archived_at" in meta
    print("1. ArchiveManager.archive_task — meta.json 생성: PASS")


# ─── 2. ArchiveManager.archive_task — 로그 gzip 압축 ─────────────────────────

def test_archive_task_compresses_log():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mgr = _make_archive_manager(tmp_path)

        # 로그 파일 생성
        log_file = tmp_path / "logs" / "T-101.stdout.log"
        log_file.write_text("hello log output\n")

        arc_path = mgr.archive_task("T-101", "FAILED")
        gz_file = arc_path / "stdout.log.gz"
        assert gz_file.exists(), "stdout.log.gz가 생성되어야 합니다"

        import gzip
        with gzip.open(gz_file, "rt") as f:
            content = f.read()
        assert "hello log output" in content
    print("2. ArchiveManager.archive_task — 로그 gzip 압축: PASS")


# ─── 3. ArchiveManager.list_history — 최신순 + status_filter ─────────────────

def test_archive_list_history_filter():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mgr = _make_archive_manager(tmp_path)

        mgr.archive_task("T-200", "SHIPPED")
        time.sleep(0.01)  # 파일시스템 mtime 구분
        mgr.archive_task("T-201", "FAILED")
        time.sleep(0.01)
        mgr.archive_task("T-202", "SHIPPED")

        # 전체 목록 — 최신순
        all_hist = mgr.list_history(limit=10)
        assert len(all_hist) == 3
        assert all_hist[0]["task_id"] == "T-202"  # 최신

        # SHIPPED 필터
        shipped = mgr.list_history(status_filter="SHIPPED")
        assert all(h["status"] == "SHIPPED" for h in shipped)
        assert len(shipped) == 2

        # FAILED 필터
        failed = mgr.list_history(status_filter="FAILED")
        assert len(failed) == 1
        assert failed[0]["task_id"] == "T-201"
    print("3. ArchiveManager.list_history — 최신순 + status_filter: PASS")


# ─── 4. ArchiveManager.is_archived ───────────────────────────────────────────

def test_archive_is_archived():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mgr = _make_archive_manager(tmp_path)

        assert not mgr.is_archived("T-300")
        mgr.archive_task("T-300", "SHIPPED")
        assert mgr.is_archived("T-300")
    print("4. ArchiveManager.is_archived: PASS")


# ─── 5. ArchiveManager.cleanup_old_logs ──────────────────────────────────────

def test_archive_cleanup_old_logs():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mgr = _make_archive_manager(tmp_path)

        logs_dir = tmp_path / "logs"
        old_log = logs_dir / "T-old.stdout.log"
        new_log = logs_dir / "T-new.stdout.log"
        old_log.write_text("old")
        new_log.write_text("new")

        # old_log mtime을 35일 전으로 설정
        old_time = time.time() - (35 * 86_400)
        os.utime(old_log, (old_time, old_time))

        removed = mgr.cleanup_old_logs(retention_days=30)
        assert old_log.name in removed, "30일 초과 로그는 삭제되어야 합니다"
        assert not old_log.exists()
        assert new_log.exists(), "30일 이내 로그는 유지되어야 합니다"
    print("5. ArchiveManager.cleanup_old_logs: PASS")


# ─── 6. StatsTracker.record — 카운터 ──────────────────────────────────────────

def test_stats_record_counters():
    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        tracker = StatsTracker(stats_path)

        tracker.record("T-1", "SHIPPED", project_id="proj_a", elapsed_s=120.0)
        tracker.record("T-2", "FAILED",  project_id="proj_a")
        tracker.record("T-3", "SHIPPED", project_id="proj_a", elapsed_s=60.0)
        tracker.record("T-4", "HELD",    project_id="proj_b")

        data = json.loads(stats_path.read_text())
        g = data["global"]
        assert g["shipped"] == 2
        assert g["failed"] == 1
        assert g["held"] == 1

        proj_a = data["projects"]["proj_a"]
        assert proj_a["shipped"] == 2
        assert proj_a["failed"] == 1

        proj_b = data["projects"]["proj_b"]
        assert proj_b["held"] == 1
    print("6. StatsTracker.record — 카운터: PASS")


# ─── 7. StatsTracker.record — 지원하지 않는 status 무시 ───────────────────────

def test_stats_record_ignores_unknown_status():
    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        tracker = StatsTracker(stats_path)

        tracker.record("T-1", "RUNNING")    # 무시
        tracker.record("T-2", "QUEUED")     # 무시
        tracker.record("T-3", "SHIPPED")    # 기록

        summary = tracker.get_summary()
        assert summary["global"].get("shipped", 0) == 1
        assert summary["global"].get("running", 0) == 0
        assert summary["total_recorded"] == 1
    print("7. StatsTracker.record — 지원하지 않는 status 무시: PASS")


# ─── 8. StatsTracker.get_summary — pass_rate / avg_elapsed_s ─────────────────

def test_stats_get_summary_calculations():
    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        tracker = StatsTracker(stats_path)

        tracker.record("T-1", "SHIPPED", elapsed_s=100.0)
        tracker.record("T-2", "SHIPPED", elapsed_s=200.0)
        tracker.record("T-3", "FAILED",  elapsed_s=50.0)

        summary = tracker.get_summary()
        assert summary["pass_rate"] == pytest_approx(66.7, abs=0.5)
        assert summary["avg_elapsed_s"] == pytest_approx(150.0, abs=1.0)
        assert summary["total_recorded"] == 3
        assert "T-1" in summary["recent_shipped"] or "T-2" in summary["recent_shipped"]
        assert "T-3" in summary["recent_failed"]
    print("8. StatsTracker.get_summary — pass_rate / avg_elapsed_s: PASS")


import builtins as _builtins

def pytest_approx(value, abs=None):
    """간단한 approximate 비교 헬퍼."""
    tol = abs
    class _Approx:
        def __init__(self, v, a):
            self.v = v
            self.a = a
        def __eq__(self, other):
            return _builtins.abs(other - self.v) <= self.a
        def __repr__(self):
            return f"approx({self.v} ± {self.a})"
    return _Approx(value, tol)


# ─── 9. StatsTracker.get_today_summary ───────────────────────────────────────

def test_stats_get_today_summary():
    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        tracker = StatsTracker(stats_path)

        tracker.record("T-10", "SHIPPED")
        tracker.record("T-11", "FAILED")

        # 어제 날짜 타임스탬프로 직접 삽입
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        data = json.loads(stats_path.read_text())
        data["tasks"].append({
            "task_id": "T-OLD",
            "project_id": "",
            "status": "SHIPPED",
            "elapsed_s": 0,
            "retry_count": 0,
            "timestamp": yesterday,
        })
        stats_path.write_text(json.dumps(data))
        tracker._data = tracker._load()

        today = tracker.get_today_summary()
        assert today["shipped"] == 1
        assert today["failed"] == 1
        assert "T-10" in today["shipped_ids"]
        assert "T-OLD" not in today["shipped_ids"]
    print("9. StatsTracker.get_today_summary: PASS")


# ─── 10. StatsTracker.get_period_summary ─────────────────────────────────────

def test_stats_get_period_summary():
    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        tracker = StatsTracker(stats_path)

        tracker.record("T-20", "SHIPPED")

        # 10일 전 타임스탬프
        old_ts = (datetime.now() - timedelta(days=10)).isoformat()
        data = json.loads(stats_path.read_text())
        data["tasks"].append({
            "task_id": "T-VERY-OLD",
            "project_id": "",
            "status": "SHIPPED",
            "elapsed_s": 0,
            "retry_count": 0,
            "timestamp": old_ts,
        })
        stats_path.write_text(json.dumps(data))
        tracker._data = tracker._load()

        # 7일 기간 — T-VERY-OLD는 제외
        week = tracker.get_period_summary(days=7)
        assert week["shipped"] == 1
        assert week["period_days"] == 7

        # 14일 기간 — 둘 다 포함
        fortnight = tracker.get_period_summary(days=14)
        assert fortnight["shipped"] == 2
    print("10. StatsTracker.get_period_summary: PASS")


# ─── 11. Orchestrator: hold_task → _held_tasks 등록 ──────────────────────────

def test_orchestrator_hold_registers_held_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        task_id = "T-500"

        # hold_task는 _ready_to_ship에 있는 태스크만 처리
        orch._ready_to_ship[task_id] = {
            "task_id": task_id,
            "branch": "feature/T-500",
            "ready_at": datetime.now().isoformat(),
        }

        # hold_task는 sync
        orch.hold_task(task_id)

        assert task_id in orch._held_tasks, "_held_tasks에 등록되어야 합니다"
        assert orch._held_tasks[task_id]["branch"] == "feature/T-500"
        assert "held_at" in orch._held_tasks[task_id]
    print("11. Orchestrator: hold_task → _held_tasks 등록: PASS")


# ─── 12. Orchestrator: resume_task → _held_tasks 해제 ────────────────────────

def test_orchestrator_resume_clears_held_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        task_id = "T-501"

        # HELD 상태 수동 설정
        orch._held_tasks[task_id] = {
            "task_id": task_id,
            "branch": "feature/T-501",
            "held_at": datetime.now().isoformat(),
        }
        # _active_tasks에는 없어야 resume_task가 진행됨 (이미 실행 중 체크를 통과)
        orch._active_tasks.pop(task_id, None)

        # handoff 파일 준비 (resume_task가 읽음)
        handoff_path = tmp_path / "handoffs" / f"{task_id}.md"
        handoff_path.write_text("# Handoff\n## Current Status\nHELD\n")

        async def run():
            return await orch.resume_task(task_id)

        result = asyncio.run(run())
        assert task_id not in orch._held_tasks, "_held_tasks에서 제거되어야 합니다"
    print("12. Orchestrator: resume_task → _held_tasks 해제: PASS")


# ─── 13. Orchestrator: get_stale_tasks — HELD 7일 초과 ──────────────────────

def test_orchestrator_stale_held_detected():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        task_id = "T-600"

        # 8일 전에 HELD된 태스크
        eight_days_ago = (datetime.now() - timedelta(days=8)).isoformat()
        orch._held_tasks[task_id] = {
            "task_id": task_id,
            "branch": "feature/T-600",
            "held_at": eight_days_ago,
        }

        stale = orch.get_stale_tasks()
        stale_ids = [s["task_id"] for s in stale]
        assert task_id in stale_ids or any(task_id in sid for sid in stale_ids), \
            "8일 전 HELD 태스크는 stale이어야 합니다"
        stale_entry = next(s for s in stale if task_id in s["task_id"])
        assert stale_entry["status"] == "HELD"
        assert stale_entry["days"] >= 7
    print("13. Orchestrator: get_stale_tasks — HELD 7일 초과: PASS")


# ─── 14. Orchestrator: get_stale_tasks — READY_TO_SHIP 3일 초과 ──────────────

def test_orchestrator_stale_ready_to_ship_detected():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        task_id = "T-601"

        four_days_ago = (datetime.now() - timedelta(days=4)).isoformat()
        orch._ready_to_ship[task_id] = {
            "task_id": task_id,
            "branch": "feature/T-601",
            "ready_at": four_days_ago,
        }

        stale = orch.get_stale_tasks()
        assert any(task_id in s["task_id"] for s in stale), \
            "4일 전 READY_TO_SHIP 태스크는 stale이어야 합니다"
        stale_entry = next(s for s in stale if task_id in s["task_id"])
        assert stale_entry["status"] == "READY_TO_SHIP"
    print("14. Orchestrator: get_stale_tasks — READY_TO_SHIP 3일 초과: PASS")


# ─── 15. Orchestrator: get_stale_tasks — 미달 태스크 제외 ────────────────────

def test_orchestrator_stale_not_detected_for_fresh_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        # HELD 2일 (7일 미만) — stale 아님
        two_days_ago = (datetime.now() - timedelta(days=2)).isoformat()
        orch._held_tasks["T-FRESH"] = {
            "task_id": "T-FRESH",
            "branch": "feature/T-FRESH",
            "held_at": two_days_ago,
        }

        # READY_TO_SHIP 1일 (3일 미만) — stale 아님
        one_day_ago = (datetime.now() - timedelta(days=1)).isoformat()
        orch._ready_to_ship["T-FRESH2"] = {
            "task_id": "T-FRESH2",
            "branch": "feature/T-FRESH2",
            "ready_at": one_day_ago,
        }

        stale = orch.get_stale_tasks()
        stale_ids = [s["task_id"] for s in stale]
        assert not any("T-FRESH" in sid for sid in stale_ids), \
            "2일된 HELD 태스크는 stale이 아닙니다"
    print("15. Orchestrator: get_stale_tasks — 미달 태스크 제외: PASS")


# ─── 16. Orchestrator: hold_task → stats HELD 기록 ────────────────────────────

def test_orchestrator_hold_records_stats():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)
        task_id = "T-700"

        # hold_task는 _ready_to_ship에 있는 태스크만 처리
        orch._ready_to_ship[task_id] = {
            "task_id": task_id,
            "branch": "feature/T-700",
            "ready_at": datetime.now().isoformat(),
        }

        # hold_task는 sync
        orch.hold_task(task_id)

        stats = orch._stats.get_summary()
        assert stats["global"].get("held", 0) >= 1, "stats에 HELD가 기록되어야 합니다"
    print("16. Orchestrator: hold_task → stats HELD 기록: PASS")


# ─── 17. Orchestrator: archive_task_manual — 알 수 없는 task ─────────────────

def test_orchestrator_archive_manual_unknown_task():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        result = asyncio.run(orch.archive_task_manual("T-NONEXISTENT"))
        assert "archive 완료" in result or "UNKNOWN" in result
    print("17. Orchestrator: archive_task_manual — 알 수 없는 task: PASS")


# ─── 18. Orchestrator: get_history — ArchiveManager 위임 ─────────────────────

def test_orchestrator_get_history_delegates():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        # archive에 직접 항목 삽입
        orch._archive_mgr.archive_task("T-H1", "SHIPPED")
        orch._archive_mgr.archive_task("T-H2", "FAILED")

        history = orch.get_history(limit=10)
        task_ids = [h["task_id"] for h in history]
        assert "T-H1" in task_ids
        assert "T-H2" in task_ids
    print("18. Orchestrator: get_history — ArchiveManager 위임: PASS")


# ─── 19. Orchestrator: get_stats_summary — StatsTracker 위임 ─────────────────

def test_orchestrator_get_stats_summary_delegates():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        orch._stats.record("T-S1", "SHIPPED", elapsed_s=90.0)
        orch._stats.record("T-S2", "FAILED")

        summary = orch.get_stats_summary()
        assert "global" in summary
        assert summary["global"].get("shipped", 0) >= 1
        assert "pass_rate" in summary
    print("19. Orchestrator: get_stats_summary — StatsTracker 위임: PASS")


# ─── 20. Orchestrator: get_today_stats — StatsTracker 오늘 통계 위임 ──────────

def test_orchestrator_get_today_stats_delegates():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        orch._stats.record("T-TODAY1", "SHIPPED")
        orch._stats.record("T-TODAY2", "FAILED")

        today_stats = orch.get_today_stats()
        assert "shipped" in today_stats
        assert "failed" in today_stats
        assert today_stats["shipped"] >= 1
        assert today_stats["failed"] >= 1
    print("20. Orchestrator: get_today_stats — StatsTracker 오늘 통계 위임: PASS")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_archive_task_creates_meta_json,
        test_archive_task_compresses_log,
        test_archive_list_history_filter,
        test_archive_is_archived,
        test_archive_cleanup_old_logs,
        test_stats_record_counters,
        test_stats_record_ignores_unknown_status,
        test_stats_get_summary_calculations,
        test_stats_get_today_summary,
        test_stats_get_period_summary,
        test_orchestrator_hold_registers_held_tasks,
        test_orchestrator_resume_clears_held_tasks,
        test_orchestrator_stale_held_detected,
        test_orchestrator_stale_ready_to_ship_detected,
        test_orchestrator_stale_not_detected_for_fresh_tasks,
        test_orchestrator_hold_records_stats,
        test_orchestrator_archive_manual_unknown_task,
        test_orchestrator_get_history_delegates,
        test_orchestrator_get_stats_summary_delegates,
        test_orchestrator_get_today_stats_delegates,
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

    print(f"\n{'='*50}")
    print(f"결과: {passed}/{len(tests)} PASS, {failed} FAIL")
    if failed:
        sys.exit(1)
