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

[실행 가시성]
  /running    — 현재 실행 중인 태스크 확인
  /log T-ID   — 태스크 최근 로그 출력 (최대 50줄)
  /diff T-ID  — 태스크 git diff 요약 출력

[배포 제어]
  /ship T-ID  — READY_TO_SHIP 태스크 main 배포 승인

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

        # Phase 4 명령
        self._app.add_handler(CommandHandler("diff", self._handle_diff))

        # Phase 2 — inline button callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))

        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("Telegram Bot polling 시작.")
        async with self._app:
            # Telegram 명령 메뉴 등록 (/ 눌렀을 때 자동완성)
            await self._app.bot.set_my_commands([
                BotCommand("help",    "도움말"),
                BotCommand("running", "현재 실행 중인 태스크 확인"),
                BotCommand("log",     "태스크 로그 출력  예: /log T-73"),
                BotCommand("ship",    "배포 승인  예: /ship T-73"),
                BotCommand("diff",    "Diff 요약  예: /diff T-73"),
                BotCommand("status",  "시스템 상태"),
                BotCommand("reload",  "태스크 큐 재스캔"),
                BotCommand("level",   "알림 레벨 변경"),
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

    async def send_failure_card(self, text: str, task_id: str) -> None:
        """Phase 3: 실패 카드를 inline keyboard와 함께 전송."""
        if self._app is None:
            logger.warning("send_failure_card: Application이 초기화되지 않았습니다.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 재시도", callback_data=f"retry_task:{task_id}"),
                InlineKeyboardButton("🛑 중단", callback_data=f"cancel_task:{task_id}"),
            ],
            [
                InlineKeyboardButton("📌 브랜치 유지", callback_data=f"hold_branch:{task_id}"),
                InlineKeyboardButton("📄 로그 보기", callback_data=f"show_log:{task_id}"),
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
                await self._cb_reply(query, f"❌ 처리 중 오류 발생\n{str(exc)[:200]}")
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
