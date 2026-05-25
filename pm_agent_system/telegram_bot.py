"""
telegram_bot.py — Telegram Bot interface for PM Agent System

슬래시 명령:
  /approve  — 현재 대기중인 작업 승인
  /status   — 현재 시스템 상태
  /reload   — 태스크 큐 재스캔
  /level    — 알림 레벨 변경 (VERBOSE/NORMAL/QUIET)
  /help     — 도움말

  [Phase 0]
  /running  — 현재 실행 중인 태스크 상태 확인
  /log T-ID — 해당 태스크의 최근 로그 출력

  [Phase 1]
  /ship T-ID — READY_TO_SHIP 태스크 배포 승인

  [Phase 4]
  /diff T-ID — 해당 태스크의 git diff 요약 출력
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import BotCommand, CallbackQuery

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

NOTIFICATION_LEVELS = ("VERBOSE", "NORMAL", "QUIET")

HELP_TEXT = """\
사용 가능한 명령:
  /approve    — 현재 대기중인 작업 승인
  /status     — 현재 시스템 상태 확인
  /reload     — 태스크 큐 재스캔
  /level VERBOSE|NORMAL|QUIET — 알림 레벨 변경
  /help       — 이 도움말

[점검/조회]
  /doctor     — PM Bot 상태 점검 (health check)
  /queue      — 전체 작업 대기열 요약
  /running    — 현재 실행 중인 태스크 확인
  /log T-ID   — 태스크 최근 로그 출력 (최대 50줄)
  /diff T-ID  — 태스크 git diff 요약 출력

[배포 제어]
  /ship T-ID  — READY_TO_SHIP 태스크 main 배포 승인
  /hold T-ID  — READY_TO_SHIP 태스크 보류 (branch 유지)

[Adopt / Resume]
  /adopt T-ID  — 직접 작업한 내용을 PM Bot에 편입 (ADOPTED)
  /review T-ID — ADOPTED 태스크 Review Agent 검토 (→ READY_TO_SHIP)
  /resume T-ID — Handoff 기반 중단 작업 재개

[멀티 프로젝트]
  /projects         — 등록된 프로젝트 목록
  /project ID       — 프로젝트 전환  예: /project ios_capture
  /current          — 현재 활성 프로젝트 확인

[Handoff]
  /handoff T-ID     — 태스크 Handoff 파일 생성/업데이트

알림 레벨:
  VERBOSE — 모든 알림
  NORMAL  — 완료/실패/승인요청만 (기본값)
  QUIET   — 실패만
"""


def _is_raw_json(text: str) -> bool:
    """PM Agent 응답이 내부 JSON이면 True — 사용자에게 노출 금지."""
    stripped = text.strip()
    return stripped.startswith("{") and '"task_id"' in stripped


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        pm_agent: Any,
        orchestrator: Any,
        notification_level: str = "NORMAL",
        project_manager: Any = None,   # Phase 0.5: ProjectManager (선택)
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.pm_agent = pm_agent
        self.orchestrator = orchestrator
        self.project_manager = project_manager  # Phase 0.5
        self.notification_level = notification_level.upper()
        if self.notification_level not in NOTIFICATION_LEVELS:
            self.notification_level = "NORMAL"

        self._app: Application | None = None

    # ── 공개 API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Application 빌드 후 polling 시작."""
        self._app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        self._app.add_handler(CommandHandler("approve", self._handle_approve))
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("reload", self._handle_reload))
        self._app.add_handler(CommandHandler("level", self._handle_level))
        self._app.add_handler(CommandHandler("help", self._handle_help))

        # Phase 0 명령
        self._app.add_handler(CommandHandler("running", self._handle_running))
        self._app.add_handler(CommandHandler("log", self._handle_log))

        # Phase 1 명령
        self._app.add_handler(CommandHandler("ship", self._handle_ship))
        self._app.add_handler(CommandHandler("hold", self._handle_hold))

        # Phase 3 / 3.5 명령
        self._app.add_handler(CommandHandler("adopt",   self._handle_adopt))
        self._app.add_handler(CommandHandler("review",  self._handle_review))
        self._app.add_handler(CommandHandler("resume",  self._handle_resume))

        # Phase 4 명령
        self._app.add_handler(CommandHandler("diff", self._handle_diff))

        # Batch 5 명령
        self._app.add_handler(CommandHandler("doctor", self._handle_doctor))
        self._app.add_handler(CommandHandler("queue",  self._handle_queue))

        # Phase 0.5 — 멀티 프로젝트
        self._app.add_handler(CommandHandler("projects", self._handle_projects))
        self._app.add_handler(CommandHandler("project", self._handle_project))
        self._app.add_handler(CommandHandler("current", self._handle_current))

        # Phase 0.7 — Handoff
        self._app.add_handler(CommandHandler("handoff", self._handle_handoff))

        # Phase 2 — inline button callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))

        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("Telegram Bot polling 시작.")
        async with self._app:
            # Telegram 명령 메뉴 등록 (/ 눌렀을 때 자동완성)
            await self._app.bot.set_my_commands([
                BotCommand("help",     "도움말"),
                BotCommand("doctor",   "PM Bot 상태 점검 (health check)"),
                BotCommand("queue",    "전체 작업 대기열 요약"),
                BotCommand("running",  "현재 실행 중인 태스크 확인"),
                BotCommand("log",      "태스크 로그 출력  예: /log T-73"),
                BotCommand("ship",     "배포 승인  예: /ship T-73"),
                BotCommand("hold",     "배포 보류  예: /hold T-73"),
                BotCommand("adopt",    "외부 작업 편입 (ADOPTED)  예: /adopt T-91"),
                BotCommand("review",   "ADOPTED → Review Agent  예: /review T-91"),
                BotCommand("resume",   "Handoff 기반 재개  예: /resume T-91"),
                BotCommand("diff",     "Diff 요약  예: /diff T-73"),
                BotCommand("projects", "프로젝트 목록"),
                BotCommand("project",  "프로젝트 전환  예: /project ios_capture"),
                BotCommand("current",  "현재 프로젝트 확인"),
                BotCommand("handoff",  "Handoff 생성  예: /handoff T-91"),
                BotCommand("status",   "시스템 상태"),
                BotCommand("reload",   "태스크 큐 재스캔"),
                BotCommand("level",    "알림 레벨 변경"),
            ])
            await self._app.start()
            await self._app.updater.start_polling()
            try:
                await asyncio.Event().wait()
            finally:
                await self._app.updater.stop()
                await self._app.stop()

    async def send_message(self, text: str) -> None:
        """지정된 chat_id로 메시지 전송."""
        if self._app is None:
            logger.warning("send_message: Application이 초기화되지 않았습니다.")
            return
        try:
            await self._app.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as exc:
            logger.error("send_message 실패: %s", exc)

    async def send_failure_card(self, text: str, task_id: str, no_retry: bool = False) -> None:
        """Phase 3: 실패 카드를 inline keyboard와 함께 전송.

        no_retry=True: 구조적 실패 — 재시도 버튼 대신 안내 버튼 표시.
        """
        if self._app is None:
            logger.warning("send_failure_card: Application이 초기화되지 않았습니다.")
            return

        if no_retry:
            # 구조적 실패: 재시도 불가 — Resume / Adopt 우선
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("▶ 이어서",  callback_data=f"resume_task:{task_id}"),
                    InlineKeyboardButton("📥 편입",   callback_data=f"adopt_task:{task_id}"),
                ],
                [
                    InlineKeyboardButton("📌 브랜치 유지", callback_data=f"hold_branch:{task_id}"),
                    InlineKeyboardButton("📄 handoff",    callback_data=f"show_handoff:{task_id}"),
                ],
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔁 재시도", callback_data=f"retry_task:{task_id}"),
                    InlineKeyboardButton("▶ 이어서",  callback_data=f"resume_task:{task_id}"),
                ],
                [
                    InlineKeyboardButton("📥 편입",  callback_data=f"adopt_task:{task_id}"),
                    InlineKeyboardButton("📄 handoff", callback_data=f"show_handoff:{task_id}"),
                ],
            ])

        try:
            await self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error("send_failure_card 실패: %s", exc)
            try:
                await self._app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception:
                pass

    async def send_ready_to_ship_card(self, text: str, task_id: str) -> None:
        """Phase 2: READY_TO_SHIP 카드를 inline keyboard와 함께 전송."""
        if self._app is None:
            logger.warning("send_ready_to_ship_card: Application이 초기화되지 않았습니다.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ship 승인", callback_data=f"ship_task:{task_id}"),
                InlineKeyboardButton("🔍 Diff 보기", callback_data=f"show_diff:{task_id}"),
            ],
            [
                InlineKeyboardButton("📄 로그 보기", callback_data=f"show_log:{task_id}"),
                InlineKeyboardButton("⏸️ 보류", callback_data=f"hold_task:{task_id}"),
            ],
        ])

        try:
            await self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error("send_ready_to_ship_card 실패: %s", exc)
            try:
                await self._app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception:
                pass

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────

    def _should_notify(self, level: str) -> bool:
        order = {lvl: i for i, lvl in enumerate(NOTIFICATION_LEVELS)}
        return order.get(level.upper(), 0) >= order.get(self.notification_level, 0)

    async def _reply(self, update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(text)

    def _format_status(self) -> str:
        """orchestrator 상태를 읽기 좋은 문자열로 반환."""
        if self.orchestrator is None:
            return "Orchestrator가 초기화되지 않았습니다."

        try:
            status = self.orchestrator.get_status()
        except Exception as exc:
            return f"상태 조회 실패: {exc}"

        if isinstance(status, dict):
            lines = ["=== 시스템 상태 ==="]

            active = status.get("active_tasks", {})
            if active:
                lines.append(f"\n활성 태스크 ({len(active)}개):")
                for task_id, entry in active.items():
                    from datetime import datetime
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
                    st = entry.get("status", "unknown")
                    branch = entry.get("branch", "")
                    branch_str = f" | {branch}" if branch else ""
                    lines.append(f"  [{task_id}] {st}{branch_str}{elapsed_str}")
            else:
                lines.append("\n활성 태스크: 없음")

            queued = status.get("queued_tasks", [])
            lines.append(f"\n대기 태스크: {len(queued)}개")

            completed = status.get("completed_tasks", [])
            lines.append(f"완료 태스크: {len(completed)}개")

            lines.append(f"\n알림 레벨: {self.notification_level}")
            return "\n".join(lines)

        return str(status)

    # ── 명령 핸들러 ───────────────────────────────────────────────────

    async def _handle_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._process_approve(update)

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = self._format_status()
        await self._reply(update, text)

    async def _handle_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return
        try:
            if hasattr(self.orchestrator, "reload_task_queue"):
                result = self.orchestrator.reload_task_queue()
                await self._reply(update, f"태스크 큐 재스캔 완료.\n{result}")
            elif hasattr(self.orchestrator, "scan_task_queue"):
                result = self.orchestrator.scan_task_queue()
                await self._reply(update, f"태스크 큐 재스캔 완료.\n{result}")
            else:
                await self._reply(update, "reload 기능을 지원하지 않는 Orchestrator입니다.")
        except Exception as exc:
            await self._reply(update, f"재스캔 실패: {exc}")

    async def _handle_level(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await self._reply(
                update,
                f"현재 알림 레벨: {self.notification_level}\n"
                "변경: /level VERBOSE|NORMAL|QUIET",
            )
            return

        new_level = args[0].upper()
        if new_level not in NOTIFICATION_LEVELS:
            await self._reply(
                update,
                f"유효하지 않은 레벨: {args[0]}\n"
                f"사용 가능: {', '.join(NOTIFICATION_LEVELS)}",
            )
            return

        old_level = self.notification_level
        self.notification_level = new_level
        await self._reply(update, f"알림 레벨 변경: {old_level} → {new_level}")

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP_TEXT)

    # ── Phase 0 명령 핸들러 ───────────────────────────────────────────

    async def _handle_running(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/running — 현재 실행 중인 태스크 상태 표시."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            info = self.orchestrator.get_running_task()
        except Exception as exc:
            await self._reply(update, f"상태 조회 실패: {exc}")
            return

        if not info:
            await self._reply(update, "현재 실행 중인 task가 없습니다.")
            return

        from datetime import datetime
        task_id = info.get("task_id", "?")
        phase = info.get("phase", "RUNNING")
        started = info.get("started_at")
        branch = info.get("branch", "")
        log_dir = info.get("log_dir", "")

        elapsed_str = ""
        started_str = ""
        if isinstance(started, datetime):
            started_str = started.strftime("%H:%M")
            elapsed = int((datetime.now() - started).total_seconds())
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}분 {secs}초"
        elif started:
            started_str = str(started)

        lines = [f"⏳ 현재 실행 중\n"]
        lines.append(f"Task: {task_id}")
        lines.append(f"상태: {phase}")
        if started_str:
            lines.append(f"시작: {started_str}")
        if elapsed_str:
            lines.append(f"경과: {elapsed_str}")
        if branch:
            lines.append(f"branch: {branch}")
        lines.append(f"\n로그:\n- /log {task_id}")

        await self._reply(update, "\n".join(lines))

    async def _handle_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/log T-ID — 태스크 최근 로그 출력."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /log T-ID\n예: /log T-73")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /log T-73")
            return

        try:
            log_text = self.orchestrator.get_task_log(task_id)
        except Exception as exc:
            await self._reply(update, f"로그 조회 실패: {exc}")
            return

        if log_text is None:
            await self._reply(
                update,
                f"📄 {task_id} 로그 없음\n\n"
                f"로그 파일이 존재하지 않습니다.\n"
                f"태스크가 실행된 적이 없거나 ID가 올바르지 않습니다."
            )
            return

        if not log_text.strip():
            await self._reply(update, f"📄 {task_id} 로그\n\n(로그가 비어 있습니다)")
            return

        # Telegram 메시지 길이 제한 (4096자)
        header = f"📄 {task_id} 최근 로그\n\n"
        max_content = 4096 - len(header) - 10
        if len(log_text) > max_content:
            log_text = "...(앞 부분 생략)...\n" + log_text[-max_content:]

        await self._reply(update, header + log_text)

    # ── Phase 1 명령 핸들러 ───────────────────────────────────────────

    async def _handle_ship(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/ship T-ID — READY_TO_SHIP 태스크 배포 승인."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /ship T-ID\n예: /ship T-73")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /ship T-73")
            return

        await self._reply(update, f"🚢 {task_id} 배포 처리 중...")

        try:
            result = await self.orchestrator.ship_task(task_id)
            await self._reply(update, result)
        except Exception as exc:
            logger.error("ship_task 오류: %s", exc)
            await self._reply(update, f"❌ 배포 처리 중 오류 발생\n{exc}")

    # ── Phase 3 명령 핸들러 ──────────────────────────────────────────

    async def _handle_adopt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/adopt T-ID — 외부 작업을 PM Bot에 편입."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /adopt T-ID\n예: /adopt T-91")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /adopt T-91")
            return

        await self._reply(update, f"📥 {task_id} 편입 처리 중...")
        try:
            result = await self.orchestrator.adopt_task(task_id)
            await self._reply(update, result)
        except Exception as exc:
            logger.error("adopt_task 오류: %s", exc)
            await self._reply(update, f"❌ 편입 처리 중 오류 발생\n{exc}")

    async def _handle_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/review T-ID — ADOPTED 태스크 Review Agent 검토 (→ READY_TO_SHIP 또는 FAILED)."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /review T-ID\n예: /review T-91")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /review T-91")
            return

        await self._reply(update, f"🧪 {task_id} Review Agent 검토 시작...")
        try:
            result = await self.orchestrator.review_adopted_task(task_id)
            await self._reply(update, result)
        except Exception as exc:
            logger.error("review_adopted_task 오류: %s", exc)
            await self._reply(update, f"❌ 리뷰 처리 중 오류 발생\n{exc}")

    async def _handle_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/resume T-ID — Handoff 기반 태스크 재개."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /resume T-ID\n예: /resume T-91")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /resume T-91")
            return

        try:
            result = await self.orchestrator.resume_task(task_id)
            await self._reply(update, result)
        except Exception as exc:
            logger.error("resume_task 오류: %s", exc)
            await self._reply(update, f"❌ 재개 처리 중 오류 발생\n{exc}")

    async def _handle_hold(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/hold T-ID — READY_TO_SHIP 태스크 보류 (branch 유지, main 머지 없음)."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /hold T-ID\n예: /hold T-73")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /hold T-73")
            return

        try:
            result = self.orchestrator.hold_task(task_id)
            await self._reply(update, result)
        except Exception as exc:
            logger.error("hold_task 오류: %s", exc)
            await self._reply(update, f"❌ 보류 처리 중 오류 발생\n{exc}")

    # ── Phase 4 명령 핸들러 ───────────────────────────────────────────

    async def _handle_diff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/diff T-ID — 태스크 git diff 요약 출력."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /diff T-ID\n예: /diff T-73")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.\n예: /diff T-73")
            return

        try:
            summary = self.orchestrator.get_task_diff_summary(task_id)
        except Exception as exc:
            await self._reply(update, f"diff 조회 실패: {exc}")
            return

        # Telegram 4096자 제한
        if len(summary) > 4000:
            summary = summary[:4000] + "\n...(이하 생략)"

        await self._reply(update, summary)

    # ── Batch 5 명령 핸들러 ───────────────────────────────────────────

    async def _handle_doctor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/doctor — PM Bot 상태 점검 (health check)."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        await self._reply(update, "🩺 PM Bot 상태 점검 중...")

        try:
            info = await self.orchestrator.get_doctor_info()
        except Exception as exc:
            await self._reply(update, f"❌ 상태 점검 실패: {exc}")
            return

        ok = "✅"
        ng = "❌"
        warn = "⚠️"

        repo_icon = ok if info["repo_exists"] else ng
        queue_icon = ok if info["task_queue_exists"] else ng
        spec_icon = ok if info["spec_exists"] else warn

        running = info.get("running_task_id")
        running_str = f"✅ {running} ({info.get('running_phase', '?')})" if running else "— (없음)"

        rts = info.get("ready_to_ship_ids", [])
        adopted = info.get("adopted_ids", [])
        failed = info.get("failed_ids", [])

        rts_str = ", ".join(rts) if rts else "없음"
        adopted_str = ", ".join(adopted) if adopted else "없음"
        failed_str = ", ".join(failed) if failed else "없음"

        queued_md = info.get("queued_md", [])
        queued_str = f"{len(queued_md)}개" + (f" ({', '.join(queued_md[:3])})" if queued_md else "")

        git_clean = info.get("git_status_short", "").strip() in ("(clean)", "")
        git_icon = ok if git_clean else warn
        git_branch = info.get("git_branch", "?")
        git_status = info.get("git_status_short", "(조회 실패)")

        auto_ship = info.get("auto_ship_after_review", False)
        auto_ship_str = "true (자동 머지 활성화)" if auto_ship else "false (수동 /ship 필요)"

        lines = [
            "🩺 PM Bot 상태 점검 결과\n",
            f"프로젝트: {info.get('project_id', '?')}",
            f"{repo_icon} repo:  {info.get('repo_path', '?')}",
            f"{queue_icon} task_queue:  {info.get('task_queue', '?')}",
            f"   대기 파일: {queued_str}",
            f"{spec_icon} spec:  {info.get('spec_path', '?')}",
            "",
            f"실행 중: {running_str}",
            f"READY_TO_SHIP: {rts_str}",
            f"ADOPTED:       {adopted_str}",
            f"FAILED:        {failed_str}",
            "",
            f"{git_icon} git branch: {git_branch}",
            f"   status: {git_status[:200]}",
            "",
            f"AUTO_SHIP_AFTER_REVIEW: {auto_ship_str}",
            f"알림 레벨: {self.notification_level}",
        ]

        await self._reply(update, "\n".join(lines))

    async def _handle_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/queue — 전체 작업 대기열 요약."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            tasks = self.orchestrator.get_queue_summary()
        except Exception as exc:
            await self._reply(update, f"❌ 대기열 조회 실패: {exc}")
            return

        if not tasks:
            await self._reply(
                update,
                "📭 현재 대기 중인 작업 없음\n\n"
                "task_queue/ 디렉토리에 .md 파일을 추가하면 자동 처리됩니다."
            )
            return

        status_icon = {
            "RUNNING": "⏳",
            "REVIEWING": "🧪",
            "QUEUED": "📋",
            "READY_TO_SHIP": "✅",
            "ADOPTED": "📥",
        }

        lines = [f"📋 작업 대기열 ({len(tasks)}개)\n"]
        for t in tasks:
            st = t.get("status", "?")
            icon = status_icon.get(st.split(" ")[0], "🔴")
            tid = t.get("task_id", "?")
            branch = t.get("branch", "")
            nxt = t.get("next", "")
            branch_str = f"\n   branch: {branch}" if branch else ""
            lines.append(f"{icon} {tid} — {st}{branch_str}\n   다음: {nxt}")

        await self._reply(update, "\n\n".join(lines))

    # ── Phase 0.5: 멀티 프로젝트 명령 핸들러 ─────────────────────────

    async def _handle_projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/projects — 등록된 프로젝트 목록 표시."""
        if self.project_manager is None:
            await self._reply(update, "멀티 프로젝트 기능이 비활성화되어 있습니다.\n(projects.yaml 확인)")
            return
        text = self.project_manager.format_project_list()
        await self._reply(update, text)

    async def _handle_project(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/project PROJECT_ID — 프로젝트 전환."""
        if self.project_manager is None:
            await self._reply(update, "멀티 프로젝트 기능이 비활성화되어 있습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /project PROJECT_ID\n예: /project ios_capture")
            return

        project_id = args[0].strip()
        if not self.project_manager.switch(project_id):
            available = ", ".join(self.project_manager.list_projects())
            await self._reply(
                update,
                f"❌ 프로젝트 '{project_id}'를 찾을 수 없습니다.\n"
                f"등록된 프로젝트: {available}\n"
                f"(/projects 로 목록 확인)"
            )
            return

        paths = self.project_manager.current_paths
        if paths is None:
            await self._reply(update, f"❌ 프로젝트 경로 로드 실패: {project_id}")
            return

        # Orchestrator 에 전환 요청
        if self.orchestrator is not None and hasattr(self.orchestrator, "switch_project"):
            self.orchestrator.switch_project(paths)
            await self._reply(
                update,
                f"✅ 프로젝트 전환 요청\n\n"
                f"프로젝트: {project_id}\n"
                f"repo: {paths.repo_path}\n"
                f"task_queue: {paths.task_queue_dir}\n\n"
                f"현재 task 완료 후 적용됩니다."
            )
        else:
            await self._reply(
                update,
                f"✅ 프로젝트 선택: {project_id}\n"
                f"(Orchestrator 없음 — 경로 반영 불가)"
            )

    async def _handle_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/current — 현재 활성 프로젝트 표시."""
        if self.project_manager is None:
            # 단일 프로젝트 모드 — orchestrator에서 repo_path 가져오기
            if self.orchestrator is not None and hasattr(self.orchestrator, "config"):
                repo = getattr(self.orchestrator.config, "repo_path", "?")
                await self._reply(update, f"📌 현재 프로젝트: (단일)\nrepo: {repo}")
            else:
                await self._reply(update, "현재 프로젝트 정보 없음")
            return
        text = self.project_manager.format_current()
        await self._reply(update, text)

    # ── Phase 0.7: Handoff 명령 핸들러 ──────────────────────────────

    async def _handle_handoff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/handoff T-ID — 태스크 Handoff 파일 생성/업데이트."""
        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /handoff T-ID\n예: /handoff T-91")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.")
            return

        # handoff 경로 결정 (ProjectManager > Orchestrator > fallback)
        try:
            from project_manager import ProjectPaths, generate_handoff
        except ImportError:
            await self._reply(update, "❌ project_manager 모듈 로드 실패")
            return

        # 현재 프로젝트 paths 결정
        paths: ProjectPaths | None = None
        if self.project_manager is not None:
            paths = self.project_manager.current_paths
        elif self.orchestrator is not None:
            # Orchestrator에서 현재 경로 가져오기 (단일 프로젝트 모드)
            try:
                repo = getattr(self.orchestrator, "config", None)
                repo_path = getattr(repo, "repo_path", None)
                if repo_path:
                    from pathlib import Path as _Path
                    handoffs_dir = getattr(self.orchestrator, "handoffs_dir", None)
                    if handoffs_dir is None:
                        handoffs_dir = _Path(repo_path) / "pm_agent_system" / "handoffs"
                    # 임시 ProjectPaths 생성
                    paths = ProjectPaths(
                        project_id="default",
                        repo_path=_Path(repo_path),
                        spec_path=_Path(getattr(repo, "spec_path", repo_path)),
                        pmbot_dir=_Path(repo_path) / "pm_agent_system",
                    )
            except Exception:
                pass

        if paths is None:
            await self._reply(update, "❌ 현재 프로젝트 경로를 확인할 수 없습니다.")
            return

        # 태스크 파일에서 goal 추출
        goal = ""
        task_file = paths.task_queue_dir / f"{task_id}.md"
        if not task_file.exists():
            # 완료된 태스크 파일 탐색
            for candidate in paths.task_queue_dir.glob(f"*{task_id}*.md"):
                task_file = candidate
                break
        if task_file.exists():
            content = task_file.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped and len(stripped) > 5:
                    goal = stripped[:200]
                    break

        # 현재 실행 상태
        current_status = ""
        if self.orchestrator is not None and hasattr(self.orchestrator, "get_running_task"):
            running = self.orchestrator.get_running_task()
            if running and running.get("task_id") == task_id:
                phase = running.get("phase", "RUNNING")
                current_status = f"{phase} (실행 중)"

        # Handoff 생성
        try:
            content = generate_handoff(
                task_id=task_id,
                paths=paths,
                goal=goal,
                current_status=current_status,
            )
            handoff_path = paths.handoff_path(task_id)
            handoff_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            await self._reply(update, f"❌ Handoff 생성 실패\n{exc}")
            return

        # Phase 3-E: 생성 확인 + 요약 표시
        try:
            summary = self.orchestrator.get_handoff_summary(task_id) if self.orchestrator else ""
        except Exception:
            summary = ""

        confirm = f"📎 Handoff 생성/갱신 완료\n파일: {handoff_path}"
        await self._reply(update, confirm)
        if summary:
            await self._reply(update, summary)

    # ── Phase 2: Inline button callback ──────────────────────────────

    async def _handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """READY_TO_SHIP 카드 버튼 처리."""
        query = update.callback_query
        if query is None:
            return

        # Telegram 스피너 즉시 해제 (필수)
        try:
            await query.answer()
        except Exception:
            pass

        data = query.data or ""
        logger.info("callback_query: %s", data)

        if ":" not in data:
            await self._cb_reply(query, "알 수 없는 버튼입니다.")
            return

        action, task_id = data.split(":", 1)
        task_id = task_id.strip()

        # Batch 5: task_id 빈값 안전 처리
        if not task_id:
            await self._cb_reply(query, "❌ 버튼 데이터 오류: task_id가 비어 있습니다.")
            return

        if self.orchestrator is None:
            await self._cb_reply(query, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            if action == "ship_task":
                await self._cb_reply(query, f"🚢 {task_id} 배포 처리 중...")
                result = await self.orchestrator.ship_task(task_id)
                await self._cb_reply(query, result)

            elif action == "show_log":
                log_text = self.orchestrator.get_task_log(task_id)
                if log_text is None:
                    await self._cb_reply(
                        query,
                        f"📄 {task_id} 로그 없음\n\n로그 파일이 존재하지 않습니다."
                    )
                elif not log_text.strip():
                    await self._cb_reply(query, f"📄 {task_id} 로그\n\n(비어 있습니다)")
                else:
                    header = f"📄 {task_id} 최근 로그\n\n"
                    max_content = 4096 - len(header) - 10
                    if len(log_text) > max_content:
                        log_text = "...(앞 부분 생략)...\n" + log_text[-max_content:]
                    await self._cb_reply(query, header + log_text)

            elif action == "show_diff":
                # Phase 4: Diff 보기 버튼
                try:
                    summary = self.orchestrator.get_task_diff_summary(task_id)
                except Exception as exc:
                    summary = f"❌ diff 조회 실패\n{exc}"
                if len(summary) > 4000:
                    summary = summary[:4000] + "\n...(이하 생략)"
                await self._cb_reply(query, summary)

            elif action == "hold_task":
                result = self.orchestrator.hold_task(task_id)
                await self._cb_reply(query, result)
                # 원본 카드 버튼 비활성화
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            # ── Phase 3-A/B/E callbacks ────────────────────────────────
            elif action == "adopt_task":
                await self._cb_reply(query, f"📥 {task_id} 편입 처리 중...")
                result = await self.orchestrator.adopt_task(task_id)
                await self._cb_reply(query, result)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            elif action == "resume_task":
                result = await self.orchestrator.resume_task(task_id)
                await self._cb_reply(query, result)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            elif action == "show_handoff":
                summary = self.orchestrator.get_handoff_summary(task_id)
                if len(summary) > 4000:
                    summary = summary[:4000] + "\n...(이하 생략)"
                await self._cb_reply(query, summary)

            # ── Phase 3 callbacks ──────────────────────────────────────
            elif action == "retry_task":
                await self._cb_reply(query, f"🔁 {task_id} 재시도 처리 중...")
                result = await self.orchestrator.retry_task(task_id)
                await self._cb_reply(query, result)
                # 실패 카드 버튼 비활성화
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            elif action == "no_retry_info":
                await self._cb_reply(
                    query,
                    "🚫 이 실패 유형은 자동 재시도 대상이 아닙니다.\n\n"
                    "태스크 범위(allowed_files)를 수정하거나\n"
                    "새 태스크로 다시 요청해주세요."
                )

            elif action == "cancel_task":
                result = self.orchestrator.cancel_task(task_id)
                await self._cb_reply(query, result)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            elif action == "hold_branch":
                result = self.orchestrator.hold_branch(task_id)
                await self._cb_reply(query, result)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

            else:
                await self._cb_reply(query, f"알 수 없는 액션: {action}")

        except Exception as exc:
            logger.error("callback_query 처리 오류 [%s]: %s", data, exc)
            try:
                # 사용자 친화적 오류 메시지 (내부 traceback 노출 없음)
                err_msg = str(exc)
                if len(err_msg) > 300:
                    err_msg = err_msg[:300] + "..."
                await self._cb_reply(
                    query,
                    f"❌ 처리 중 오류 발생\n"
                    f"task: {task_id} / action: {action}\n"
                    f"{err_msg}\n\n"
                    f"/status 로 현재 상태를 확인하세요."
                )
            except Exception:
                pass

    async def _cb_reply(self, query: CallbackQuery, text: str) -> None:
        """CallbackQuery 컨텍스트에서 채팅방에 메시지 전송."""
        if query.message and query.message.chat_id:
            try:
                await self._app.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                )
                return
            except Exception:
                pass
        # fallback: 기본 chat_id
        await self.send_message(text)

    # ── 일반 메시지 핸들러 ────────────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """사용자 자유 메시지 → pm_agent.chat() 으로 전달."""
        if not update.effective_message:
            return

        text = update.effective_message.text or ""

        if text.strip() == "approve" or text.rstrip().endswith("\napprove"):
            await self._process_approve(update)
            return

        if self.pm_agent is None:
            await self._reply(update, "PM Agent가 초기화되지 않았습니다.")
            return

        try:
            if hasattr(self.pm_agent, "chat"):
                response = await self.pm_agent.chat(text)
            else:
                response = "PM Agent가 chat 인터페이스를 지원하지 않습니다."
        except Exception as exc:
            logger.error("pm_agent.chat 오류: %s", exc)
            await self._reply(update, f"처리 중 오류가 발생했습니다: {exc}")
            return

        if response is None:
            return

        response_str = str(response)

        if _is_raw_json(response_str):
            logger.warning("PM Agent 응답이 raw JSON — 사용자에게 전달하지 않음.")
            return

        await self._reply(update, response_str)

    async def _process_approve(self, update: Update) -> None:
        """approve 처리 공통 로직."""
        if self.orchestrator is None:
            await self._reply(update, "Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            if hasattr(self.orchestrator, "approve_pending"):
                result = await self.orchestrator.approve_pending()
                await self._reply(update, str(result))
            elif hasattr(self.orchestrator, "approve"):
                result = await self.orchestrator.approve()
                await self._reply(update, str(result))
            else:
                await self._reply(update, "approve 기능을 지원하지 않는 Orchestrator입니다.")
        except Exception as exc:
            logger.error("approve 처리 오류: %s", exc)
            await self._reply(update, f"approve 처리 실패: {exc}")
