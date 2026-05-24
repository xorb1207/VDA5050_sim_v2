#!/usr/bin/env python3
"""
main.py — PM Agent System 진입점

실행: python pm_agent_system/main.py [--dry-run] [--status]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

LOCK_FILE = Path("/tmp/pm_agent_system.lock")

# pm_agent_system 디렉토리를 sys.path에 추가
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# 프로젝트 루트도 추가
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _acquire_lock() -> bool:
    """락파일 획득. 이미 실행 중이면 False 반환."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # 프로세스가 실제로 살아있는지 확인
            os.kill(pid, 0)
            return False  # 살아있는 프로세스가 락을 보유 중
        except (ValueError, OSError):
            # 죽은 프로세스의 락파일 — 제거하고 재취득
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    """락파일 삭제."""
    LOCK_FILE.unlink(missing_ok=True)


def _format_status(git_state: dict, active_tasks: dict) -> str:
    """git_manager 상태를 사람이 읽기 좋은 문자열로 변환."""
    lines = ["=== PM Agent System 상태 ==="]

    active = git_state.get("active_tasks", {})
    if active:
        lines.append(f"\n활성 태스크 ({len(active)}개):")
        for task_id, entry in active.items():
            created_at = entry.get("created_at", "")
            elapsed_str = ""
            if created_at:
                try:
                    elapsed = int(
                        (datetime.now() - datetime.fromisoformat(created_at)).total_seconds()
                    )
                    mins, secs = divmod(elapsed, 60)
                    elapsed_str = f" — {mins}분 {secs}초 경과"
                except ValueError:
                    pass
            status = entry.get("status", "unknown")
            branch = entry.get("branch", "")
            files = entry.get("locked_files", [])
            lines.append(f"  [{task_id}] {status} | {branch}{elapsed_str}")
            if files:
                lines.append(f"    잠긴 파일: {', '.join(files)}")
    else:
        lines.append("\n활성 태스크: 없음")

    completed = git_state.get("completed_tasks", [])
    lines.append(f"\n완료 태스크: {len(completed)}개")

    pending = git_state.get("pending_merge", [])
    if pending:
        lines.append(f"병합 대기: {len(pending)}개")

    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="PM Agent System")
    parser.add_argument("--dry-run", action="store_true", help="실제 git/GitHub 작업 없이 시뮬레이션")
    parser.add_argument("--status", action="store_true", help="현재 상태 출력 후 종료")
    args = parser.parse_args()

    # 단일 인스턴스 락
    if not _acquire_lock():
        print("[ERROR] PM Agent System이 이미 실행 중입니다.")
        print(f"  락파일: {LOCK_FILE}")
        sys.exit(1)

    try:
        from config import load_config
        from git_manager import GitManager

        config = load_config()

        # CLI 플래그가 환경변수보다 우선
        if args.dry_run:
            config.dry_run = True

        git_manager = GitManager(
            repo_path=config.repo_path,
            state_path=config.state_path,
            github_token=config.github_token or None,
            github_repo=config.github_repo or None,
            dry_run=config.dry_run,
            no_auto_pr=config.no_auto_pr,
        )

        released = git_manager.release_stale_locks()
        if released:
            print(f"[startup] 오래된 락 해제: {released}")

        if args.status:
            state = git_manager.get_state()
            print(_format_status(state, state.get("active_tasks", {})))
            return

        # 나머지 컴포넌트 import (선택적 의존성)
        try:
            from orchestrator import Orchestrator
        except ImportError:
            print("[warn] orchestrator 모듈을 찾을 수 없습니다.")
            Orchestrator = None  # type: ignore[assignment, misc]

        try:
            from pm_agent import PMAgent
        except ImportError:
            print("[warn] pm_agent 모듈을 찾을 수 없습니다.")
            PMAgent = None  # type: ignore[assignment, misc]

        try:
            from telegram_bot import TelegramBot
        except ImportError:
            print("[error] telegram_bot 모듈을 찾을 수 없습니다. 종료합니다.")
            sys.exit(1)

        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not telegram_token or not telegram_chat_id:
            print("[error] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수가 없습니다.")
            sys.exit(1)

        orchestrator = Orchestrator(config=config, git_manager=git_manager) if Orchestrator else None
        pm_agent = PMAgent(config=config, git_manager=git_manager, orchestrator=orchestrator) if PMAgent else None

        # 시작 시 완료 이력 주입
        if pm_agent is not None:
            state = git_manager.get_state()
            completed_tasks = state.get("completed_tasks", [])
            if hasattr(pm_agent, "inject_project_status"):
                pm_agent.inject_project_status(completed_tasks=completed_tasks, open_prs=[])

        bot = TelegramBot(
            token=telegram_token,
            chat_id=telegram_chat_id,
            pm_agent=pm_agent,
            orchestrator=orchestrator,
            notification_level=config.notification_level,
        )

        print("[startup] PM Agent System 시작됨.")
        if config.dry_run:
            print("[startup] DRY-RUN 모드 활성화.")

        await bot.run()

    finally:
        _release_lock()


if __name__ == "__main__":
    asyncio.run(main())
