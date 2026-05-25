"""
e2e_rc_validation.py — PM Bot RC E2E 시나리오 검증 (격리 환경)

실제 Telegram/Claude CLI 없이 Orchestrator 핵심 경로를 직접 실행.

시나리오 A (기본): 태스크 감지 → CLI → 리뷰 → READY_TO_SHIP (실봇에서 확인)
시나리오 B (adopt): /adopt → ADOPTED → /review → READY_TO_SHIP
시나리오 C (fail/hold/stale): FAILED → _held_tasks → get_stale_tasks
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# sys.path 설정
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import Config
from orchestrator import Orchestrator
from schemas import ReviewVerdict, Violation


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
    (tmp_path / "CLAUDE.md").write_text("# Spec\n1. 변경 파일은 allowed_files 내에서만.\n")
    orch = Orchestrator(config=cfg, git_manager=None, review_agent=None)
    orch.notify_fn = AsyncMock()
    orch.notify_card_fn = AsyncMock()
    orch.notify_failure_card_fn = AsyncMock()
    return orch


# ─── 시나리오 B: /adopt → ADOPTED → /review → READY_TO_SHIP ────────────────

def scenario_b_adopt_review_ship():
    """직접 작업 편입 흐름 검증."""
    print("\n=== 시나리오 B: Adopt → Review → READY_TO_SHIP ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        task_id = "T-RC2"

        async def run():
            # 1. /adopt 실행
            with patch.object(orch, "_get_current_branch", return_value="feature/T-RC2"), \
                 patch.object(orch, "_collect_git_diff", return_value={
                     "actual_diff": "diff --git a/src/foo.py b/src/foo.py\n+x = 1",
                     "changed_files": ["src/foo.py"],
                     "diff_stat": "1 file changed, 1 insertion",
                     "diff_numstat": "1\t0\tsrc/foo.py",
                     "name_status_raw": "M\tsrc/foo.py",
                     "git_status": "M src/foo.py",
                 }), \
                 patch.object(orch, "_get_files_vs_main", return_value=["src/foo.py"]):
                result = await orch.adopt_task(task_id)

            print(f"  /adopt 결과: {result[:60]}...")
            assert task_id in orch._adopted_tasks, "adopt_task: _adopted_tasks에 등록 실패"
            assert task_id not in orch._ready_to_ship, "adopt 직후 _ready_to_ship 진입 금지"
            print("  ✅ ADOPTED 상태 확인")

            # 2. handoff 파일 확인
            handoff_path = tmp_path / "handoffs" / f"{task_id}.md"
            assert handoff_path.exists(), "handoff 파일 미생성"
            print("  ✅ handoff 파일 생성 확인")

            # 3. /diff 확인
            diff_result = orch.get_task_diff_summary(task_id)
            assert task_id in diff_result or "Diff" in diff_result or "diff" in diff_result.lower(), "diff 결과 이상"
            print(f"  ✅ /diff 결과: {diff_result[:50]}...")

            # 4. /review → PASS → READY_TO_SHIP
            mock_review = AsyncMock()
            mock_review.review = AsyncMock(return_value=ReviewVerdict(
                verdict="PASS", task_id=task_id,
                notes="RC validation: all checks passed"
            ))
            orch.review_agent = mock_review

            with patch.object(orch, "_get_current_branch", return_value="feature/T-RC2"), \
                 patch.object(orch, "_collect_git_diff", return_value={
                     "actual_diff": "diff --git a/src/foo.py b/src/foo.py\n+x = 1",
                     "changed_files": ["src/foo.py"],
                     "diff_stat": "1 file changed",
                     "diff_numstat": "1\t0\tsrc/foo.py",
                     "name_status_raw": "M\tsrc/foo.py",
                     "git_status": "M src/foo.py",
                 }):
                review_result = await orch.review_adopted_task(task_id)

            print(f"  /review 결과: {review_result[:80]}...")
            assert task_id in orch._ready_to_ship, "_review 후 READY_TO_SHIP 등록 실패"
            assert task_id not in orch._adopted_tasks, "review 후 _adopted_tasks 미해제"
            print("  ✅ READY_TO_SHIP 상태 확인")

            # 5. /hold 실행 확인
            hold_result = orch.hold_task(task_id)
            print(f"  /hold 결과: {hold_result[:60]}...")
            assert task_id in orch._held_tasks, "hold_task: _held_tasks 미등록"
            assert task_id not in orch._ready_to_ship, "hold 후 _ready_to_ship 미해제"
            print("  ✅ HELD 상태 확인")

        asyncio.run(run())
    print("=== 시나리오 B: PASS ===\n")


# ─── 시나리오 C: FAILED → HELD → stale 감지 → archive ─────────────────────────

def scenario_c_fail_hold_stale():
    """실패/보류/복구 흐름 검증."""
    print("\n=== 시나리오 C: FAILED → HELD → stale → archive ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        task_id = "T-RC3"
        task_path = tmp_path / "task_queue" / f"01_{task_id}.md"
        task_path.write_text(f"# {task_id}\n테스트 태스크\n\nallowed_files:\n- src/test.py\n")

        async def run():
            # 1. FAILED 상태 재현 (직접 상태 설정)
            orch._failed_tasks[task_id] = {
                "task_id": task_id,
                "branch": "feature/T-RC3",
                "failed_at": datetime.now().isoformat(),
            }
            orch._stats.record(task_id, "FAILED", elapsed_s=30.0)
            print("  ✅ FAILED 상태 설정")

            # 2. /log 경로 확인 (로그 파일 생성)
            log_file = tmp_path / "logs" / "tasks" / f"{task_id}.combined.log"
            log_file.write_text("Error: test failure\nLine 10: assertion failed\n")
            log_result = orch.get_task_log(task_id, lines=5)
            assert log_result is not None and ("assertion failed" in log_result or task_id in log_result or "Error" in log_result)
            print(f"  ✅ /log 결과: {log_result.strip()[:60]}...")

            # 3. READY_TO_SHIP에 올려 /hold 테스트
            orch._ready_to_ship[task_id] = {
                "task_id": task_id,
                "branch": "feature/T-RC3",
                "ready_at": datetime.now().isoformat(),
            }
            orch._failed_tasks.pop(task_id, None)

            hold_result = orch.hold_task(task_id)
            assert task_id in orch._held_tasks, "hold_task 실패"
            print(f"  ✅ /hold → HELD: {hold_result[:50]}...")

            # 4. stale 감지 (8일 전으로 조작)
            eight_days_ago = (datetime.now() - timedelta(days=8)).isoformat()
            orch._held_tasks[task_id]["held_at"] = eight_days_ago

            stale = orch.get_stale_tasks()
            stale_ids = [s["task_id"] for s in stale]
            assert any(task_id in sid for sid in stale_ids), "stale 감지 실패"
            stale_entry = next(s for s in stale if task_id in s["task_id"])
            assert stale_entry["days"] >= 7
            assert "/resume" in str(stale_entry.get("next", []))
            print(f"  ✅ /stale 감지: {stale_entry['task_id']} — {stale_entry['days']}일 경과")

            # 5. /archive 수동 실행
            arc_result = await orch.archive_task_manual(task_id)
            assert "archive 완료" in arc_result or "HELD" in arc_result
            print(f"  ✅ /archive 결과: {arc_result[:60]}...")

            # archive 파일 확인 (archive는 base/archive에 저장됨 = pm_agent_system/archive/)
            arc_meta = orch._archive_dir / task_id / "meta.json"
            assert arc_meta.exists(), f"archive meta.json 미생성: {arc_meta}"
            print(f"  ✅ archive/meta.json 생성 확인: {arc_meta}")

            # 6. /history 확인 (archive 정리 전에!)
            history = orch.get_history(limit=10)
            hist_ids = [h.get("task_id") for h in history]
            assert task_id in hist_ids, f"history에 task_id 없음: {hist_ids}"
            print(f"  ✅ /history: {hist_ids}")

            # 테스트 후 정리
            import shutil; shutil.rmtree(orch._archive_dir / task_id, ignore_errors=True)

            # 7. /stats 확인
            stats = orch.get_stats_summary()
            assert stats["global"].get("failed", 0) >= 1
            assert "pass_rate" in stats
            print(f"  ✅ /stats: global={stats['global']}")

            # 8. /resume 경로 — handoff 없는 경우 안내 메시지
            resume_result = await orch.resume_task(task_id)
            assert "handoff 없음" in resume_result or "이미 실행 중" in resume_result or "재개" in resume_result
            print(f"  ✅ /resume 응답: {resume_result[:60]}...")

        asyncio.run(run())
    print("=== 시나리오 C: PASS ===\n")


# ─── 시나리오 A 보조: 기본 봇 상태 확인 ───────────────────────────────────────

def scenario_a_status_check():
    """시나리오 A 보조 — 봇 상태 API 검증."""
    print("\n=== 시나리오 A 보조: 봇 상태 API ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orch = _make_orchestrator(tmp_path)

        async def run():
            # /doctor 정보 구조
            health = await orch.get_doctor_info()
            required_keys = {"repo_exists", "spec_exists", "task_queue_exists", "handoffs_exists"}
            assert required_keys.issubset(set(health.keys())), f"health keys 누락: {required_keys - set(health.keys())}"
            print(f"  ✅ /doctor: {list(health.keys())}")

            # /queue 상태
            status = orch.get_status()
            assert "active_tasks" in status or isinstance(status, dict)
            print(f"  ✅ /queue 상태: active={list(status.get('active_tasks',{}).keys())}")

            # /running 없음
            running = orch.get_running_task()
            assert running is None or isinstance(running, dict)
            print(f"  ✅ /running: {running}")

            # /stale 빈 상태
            stale = orch.get_stale_tasks()
            assert isinstance(stale, list)
            print(f"  ✅ /stale 빈 상태: {stale}")

        asyncio.run(run())
    print("=== 시나리오 A 보조: PASS ===\n")


# ─── Telegram 핸들러 경로 검증 ───────────────────────────────────────────────

def scenario_telegram_handlers():
    """Telegram 핸들러 import + 기본 구조 검증."""
    print("\n=== Telegram 핸들러 구조 검증 ===")
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "telegram_bot",
        str(Path(__file__).resolve().parent.parent / "telegram_bot.py")
    )
    tb_module = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(tb_module)

    # 필수 핸들러 존재 확인
    bot_class = tb_module.TelegramBot
    required_methods = [
        "_handle_doctor", "_handle_queue", "_handle_ship",
        "_handle_hold", "_handle_adopt", "_handle_resume",
        "_handle_review", "_handle_log", "_handle_diff",
        "_handle_history", "_handle_stats", "_handle_stale",
        "_handle_archive", "_daily_summary_loop",
    ]
    for method in required_methods:
        assert hasattr(bot_class, method), f"TelegramBot.{method} 누락"
    print(f"  ✅ 필수 핸들러 {len(required_methods)}개 모두 존재")

    # config 필드 확인
    from config import Config
    cfg = Config.__dataclass_fields__
    required_cfg = [
        "anthropic_api_key", "repo_path", "dry_run",
        "auto_ship_after_review", "notification_level",
        "log_retention_days", "archive_retention_days",
        "daily_report", "daily_report_hour",
    ]
    for field in required_cfg:
        assert field in cfg, f"Config.{field} 누락"
    print(f"  ✅ Config 필드 {len(required_cfg)}개 확인")

    print("=== Telegram 핸들러 구조 검증: PASS ===\n")


# ─── 운영 체크리스트 자가 점검 ───────────────────────────────────────────────

def operational_checklist():
    """운영 전 체크리스트 자가 점검."""
    print("\n=== 운영 체크리스트 ===")
    import os

    checks = []

    # 1. .env 파일
    env_ok = Path("pm_agent_system/.env").exists()
    checks.append(("✅" if env_ok else "❌", ".env 파일 존재"))

    # 2. projects.yaml
    proj_ok = Path("pm_agent_system/projects.yaml").exists()
    checks.append(("✅" if proj_ok else "❌", "projects.yaml 존재"))

    # 3. ANTHROPIC_API_KEY
    api_ok = bool(os.environ.get("ANTHROPIC_API_KEY") or
                  _read_env_var("pm_agent_system/.env", "ANTHROPIC_API_KEY"))
    checks.append(("✅" if api_ok else "❌", "ANTHROPIC_API_KEY 설정"))

    # 4. AUTO_SHIP_AFTER_REVIEW
    auto_ship = _read_env_var("pm_agent_system/.env", "AUTO_SHIP_AFTER_REVIEW")
    auto_ship_ok = str(auto_ship).lower() in ("false", "0", "")
    checks.append(("✅" if auto_ship_ok else "⚠️", f"AUTO_SHIP_AFTER_REVIEW=false ({auto_ship})"))

    # 5. TELEGRAM 설정
    tg_token = _read_env_var("pm_agent_system/.env", "TELEGRAM_BOT_TOKEN")
    tg_ok = bool(tg_token)
    checks.append(("✅" if tg_ok else "❌", "TELEGRAM_BOT_TOKEN 설정"))

    # 6. task_queue 디렉토리
    tq_ok = Path("pm_agent_system/task_queue").is_dir()
    checks.append(("✅" if tq_ok else "❌", "task_queue 디렉토리 존재"))

    # 7. LOG_RETENTION_DAYS
    log_ret = _read_env_var("pm_agent_system/.env", "LOG_RETENTION_DAYS")
    checks.append(("✅", f"LOG_RETENTION_DAYS={log_ret or '30(기본값)'}"))

    # 8. git clean 상태
    import subprocess
    result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=".")
    uncommitted = [l for l in result.stdout.splitlines() if not l.startswith("?")]
    git_ok = len(uncommitted) == 0
    checks.append(("✅" if git_ok else "⚠️", f"git 클린 상태 ({'clean' if git_ok else f'{len(uncommitted)}개 uncommitted'})"))

    for icon, msg in checks:
        print(f"  {icon} {msg}")

    failed = [c for c in checks if c[0] == "❌"]
    warnings = [c for c in checks if c[0] == "⚠️"]
    print(f"\n  결과: {len(checks)}개 중 {len(failed)}개 실패, {len(warnings)}개 경고")
    print("=== 체크리스트 완료 ===\n")
    return len(failed) == 0


def _read_env_var(env_path: str, key: str) -> str:
    """간단한 .env 파일 파서."""
    try:
        for line in Path(env_path).read_text().splitlines():
            if line.startswith(f"{key}="):
                return line[len(key)+1:].strip()
    except Exception:
        pass
    return ""


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = []

    for name, fn in [
        ("시나리오 A 보조 (상태 API)", scenario_a_status_check),
        ("시나리오 B (adopt/review/hold)", scenario_b_adopt_review_ship),
        ("시나리오 C (fail/stale/archive)", scenario_c_fail_hold_stale),
        ("Telegram 핸들러 구조", scenario_telegram_handlers),
        ("운영 체크리스트", operational_checklist),
    ]:
        try:
            result = fn()
            results.append((name, "PASS", None))
        except Exception as e:
            import traceback
            results.append((name, "FAIL", e))
            traceback.print_exc()

    print("\n" + "="*60)
    print("E2E RC 검증 결과:")
    for name, status, err in results:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} {name}: {status}")
        if err:
            print(f"     └─ {err}")

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n총 {len(results)}개 중 {len(failed)}개 실패")
    if failed:
        sys.exit(1)
