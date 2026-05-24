"""
telegram_bot.py — Telegram Bot interface for PM Agent System

슬래시 명령:
  /approve  — 현재 대기중인 작업 승인
  /status   — 현재 시스템 상태
  /reload   — 태스크 큐 재스캔
  /level    — 알림 레벨 변경 (VERBOSE/NORMAL/QUIET)
  /help     — 도움말
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

NOTIFICATION_LEVELS = ("VERBOSE", "NORMAL", "QUIET")

HELP_TEXT = """\
사용 가능한 명령:
  /approve — 현재 대기중인 작업 승인
  /status  — 현재 시스템 상태 확인
  /reload  — 태스크 큐 재스캔
  /level VERBOSE|NORMAL|QUIET — 알림 레벨 변경
  /help    — 이 도움말

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
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.pm_agent = pm_agent
        self.orchestrator = orchestrator
        self.notification_level = notification_level.upper()
        if self.notification_level not in NOTIFICATION_LEVELS:
            self.notification_level = "NORMAL"

        self._app: Application | None = None

    # ── 공개 API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Application 빌드 후 polling 시작 (blocking)."""
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
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("Telegram Bot polling 시작.")
        await self._app.run_polling()

    async def send_message(self, text: str) -> None:
        """지정된 chat_id로 메시지 전송."""
        if self._app is None:
            logger.warning("send_message: Application이 초기화되지 않았습니다.")
            return
        try:
            await self._app.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as exc:
            logger.error("send_message 실패: %s", exc)

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────

    def _should_notify(self, level: str) -> bool:
        """현재 notification_level 기준으로 이 레벨의 알림을 보내야 하는지 판단."""
        order = {lvl: i for i, lvl in enumerate(NOTIFICATION_LEVELS)}
        return order.get(level.upper(), 0) >= order.get(self.notification_level, 0)

    async def _reply(self, update: Update, text: str) -> None:
        """update 객체로 답장 전송."""
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
                    lines.append(f"  [{task_id}] {st} | {branch}{elapsed_str}")
            else:
                lines.append("\n활성 태스크: 없음")

            queued = status.get("queued_tasks", [])
            lines.append(f"\n대기 태스크: {len(queued)}개")

            completed = status.get("completed_tasks", [])
            lines.append(f"완료 태스크: {len(completed)}개")

            nl = self.notification_level
            lines.append(f"\n알림 레벨: {nl}")

            return "\n".join(lines)

        return str(status)

    # ── 명령 핸들러 ───────────────────────────────────────────────────

    async def _handle_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/approve — 현재 대기중인 작업 승인."""
        await self._process_approve(update)

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/status — 현재 시스템 상태."""
        text = self._format_status()
        await self._reply(update, text)

    async def _handle_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reload — 태스크 큐 재스캔."""
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
        """/level VERBOSE|NORMAL|QUIET — 알림 레벨 변경."""
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
        """/help — 도움말."""
        await self._reply(update, HELP_TEXT)

    # ── 일반 메시지 핸들러 ────────────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """사용자 자유 메시지 → pm_agent.chat() 으로 전달."""
        if not update.effective_message:
            return

        text = update.effective_message.text or ""

        # approve 패턴 검사
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

        # 내부 JSON 노출 차단
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
