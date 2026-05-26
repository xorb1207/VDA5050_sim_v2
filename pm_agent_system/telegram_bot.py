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

[작업 등록 (V3)]
  /enqueue 제목\n본문  — 작업을 inbox에 등록 (승인 후 실행)
  /run 태스크ID        — inbox 태스크 승인 → 실행 대기열로 이동
  /run next            — inbox 중 가장 오래된 태스크 승인

[점검/조회]
  /doctor     — PM Bot 상태 점검 (health check)
  /queue      — 전체 작업 대기열 요약 (pending + queued 포함)
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

[기록/통계/관리]
  /archive T-ID — 태스크 archive에 보관
  /history      — 최근 완료/실패 이력
  /stats        — 작업 통계 (전체/프로젝트별)
  /stale        — 오래된 작업 감지 (HELD 7일+, RTS 3일+, ADOPTED 5일+)

[설정]
  /approve    — 현재 대기중인 작업 승인
  /status     — 현재 시스템 상태 확인
  /reload     — 태스크 큐 재스캔
  /level VERBOSE|NORMAL|QUIET — 알림 레벨 변경
  /help       — 이 도움말

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
        project_manager: Any = None,    # Phase 0.5: ProjectManager (선택)
        daily_report: bool = False,     # Batch 6: daily summary 활성화
        daily_report_hour: int = 9,     # Batch 6: 전송 시각 (0-23)
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.pm_agent = pm_agent
        self.orchestrator = orchestrator
        self.project_manager = project_manager  # Phase 0.5
        self.notification_level = notification_level.upper()
        if self.notification_level not in NOTIFICATION_LEVELS:
            self.notification_level = "NORMAL"
        # Batch 6: daily report
        self._daily_report = daily_report
        self._daily_report_hour = max(0, min(23, daily_report_hour))

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
        self._app.add_handler(CommandHandler("doctor",   self._handle_doctor))
        self._app.add_handler(CommandHandler("queue",    self._handle_queue))

        # Batch 6 명령
        self._app.add_handler(CommandHandler("archive",  self._handle_archive))
        self._app.add_handler(CommandHandler("history",  self._handle_history))
        self._app.add_handler(CommandHandler("stats",    self._handle_stats))
        self._app.add_handler(CommandHandler("stale",    self._handle_stale))

        # Phase 0.5 — 멀티 프로젝트
        self._app.add_handler(CommandHandler("projects", self._handle_projects))
        self._app.add_handler(CommandHandler("project", self._handle_project))
        self._app.add_handler(CommandHandler("current", self._handle_current))

        # Phase 0.7 — Handoff
        self._app.add_handler(CommandHandler("handoff", self._handle_handoff))

        # PM Bot V3 — task_inbox
        self._app.add_handler(CommandHandler("enqueue", self._handle_enqueue))
        self._app.add_handler(CommandHandler("run",     self._handle_run))

        # Phase 2 — inline button callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))

        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("Telegram Bot polling 시작.")
        async with self._app:
            # Telegram 명령 메뉴 등록 (/ 눌렀을 때 자동완성)
            await self._app.bot.set_my_commands([
                BotCommand("enqueue",  "작업 등록 (inbox 대기)  /enqueue 제목\\n본문"),
                BotCommand("run",      "inbox 작업 승인→실행  /run 태스크ID or next"),
                BotCommand("help",     "도움말"),
                BotCommand("doctor",   "PM Bot 상태 점검 (health check)"),
                BotCommand("queue",    "전체 작업 대기열 요약 (pending 포함)"),
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
                BotCommand("archive",  "태스크 archive  예: /archive T-91"),
                BotCommand("history",  "최근 완료/실패 이력"),
                BotCommand("stats",    "작업 통계"),
                BotCommand("stale",    "오래된 작업 감지"),
            ])
            await self._app.start()
            await self._app.updater.start_polling()
            try:
                # Batch 6: Daily summary background task
                _daily_task = None
                if self._daily_report:
                    _daily_task = asyncio.create_task(self._daily_summary_loop())
                    logger.info(f"Daily report 활성화: 매일 {self._daily_report_hour:02d}:00")

                await asyncio.Event().wait()
            finally:
                if _daily_task is not None:
                    _daily_task.cancel()
                    try:
                        await _daily_task
                    except asyncio.CancelledError:
                        pass
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
        """/queue — 전체 작업 대기열 요약 (pending inbox 포함)."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        # PM Bot V3: inbox pending 목록
        try:
            pending = self.orchestrator.get_inbox_summary()
        except Exception:
            pending = []

        try:
            tasks = self.orchestrator.get_queue_summary()
        except Exception as exc:
            await self._reply(update, f"❌ 대기열 조회 실패: {exc}")
            return

        if not pending and not tasks:
            await self._reply(
                update,
                "📭 현재 대기 중인 작업 없음\n\n"
                "/enqueue 로 작업을 등록하거나\n"
                "task_queue/ 에 .md 파일을 추가하면 처리됩니다."
            )
            return

        lines: list[str] = []

        # ── Pending (inbox) ──
        if pending:
            prio_icon = {"high": "🔴", "medium": "🟡", "low": "🔵"}
            lines.append(f"📥 Pending ({len(pending)}개) — 승인 대기 중")
            for i, t in enumerate(pending, 1):
                tid = t.get("task_id", "?")
                title = t.get("title", tid)
                prio = t.get("priority", "medium")
                icon = prio_icon.get(prio, "⚪")
                # title이 task_id와 같으면 task_id만 표시
                display = title if title != tid else tid
                lines.append(f"  {i}. {icon} {display}\n     /run {tid}")

        # ── Queued / Active ──
        if tasks:
            if lines:
                lines.append("")
            status_icon = {
                "RUNNING": "⏳",
                "REVIEWING": "🧪",
                "QUEUED": "📋",
                "READY_TO_SHIP": "✅",
                "ADOPTED": "📥",
                "HELD": "⏸",
            }
            lines.append(f"🔄 실행 대기열 ({len(tasks)}개)")
            for t in tasks:
                st = t.get("status", "?")
                icon = status_icon.get(st.split(" ")[0], "🔴")
                tid = t.get("task_id", "?")
                branch = t.get("branch", "")
                nxt = t.get("next", "")
                branch_str = f"\n     branch: {branch}" if branch else ""
                lines.append(f"  {icon} {tid} — {st}{branch_str}\n     다음: {nxt}")

        await self._reply(update, "\n".join(lines))

    # ── Batch 6 명령 핸들러 ───────────────────────────────────────────

    async def _handle_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/archive T-ID — 태스크를 archive에 보관."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(update, "사용법: /archive T-ID\n예: /archive T-91")
            return

        task_id = args[0].strip()
        if not task_id:
            await self._reply(update, "태스크 ID를 입력하세요.")
            return

        await self._reply(update, f"📦 {task_id} archive 처리 중...")
        try:
            result = await self.orchestrator.archive_task_manual(task_id)
            await self._reply(update, result)
        except Exception as exc:
            await self._reply(update, f"❌ archive 실패: {exc}")

    async def _handle_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/history — 최근 완료/실패 이력."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            history = self.orchestrator.get_history(limit=20)
        except Exception as exc:
            await self._reply(update, f"❌ 이력 조회 실패: {exc}")
            return

        if not history:
            await self._reply(
                update,
                "📚 이력 없음\n\n"
                "SHIPPED / FAILED 태스크가 아직 없거나\n"
                "archive에 저장된 항목이 없습니다."
            )
            return

        shipped = [h for h in history if h.get("status") == "SHIPPED"]
        failed  = [h for h in history if h.get("status") == "FAILED"]
        others  = [h for h in history if h.get("status") not in ("SHIPPED", "FAILED")]

        def _fmt(items: list[dict], n: int = 5) -> str:
            lines = []
            for h in items[:n]:
                tid = h.get("task_id", "?")
                arc_at = h.get("archived_at", "")[:10]
                lines.append(f"  - {tid}  ({arc_at})")
            return "\n".join(lines) if lines else "  없음"

        lines = ["📚 최근 이력\n"]
        lines.append(f"최근 완료 ({len(shipped)}개):\n{_fmt(shipped)}")
        if failed:
            lines.append(f"\n최근 실패 ({len(failed)}개):\n{_fmt(failed)}")
        if others:
            lines.append(f"\n기타 ({len(others)}개):\n{_fmt(others, 3)}")

        await self._reply(update, "\n".join(lines))

    async def _handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/stats — 작업 통계."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            summary = self.orchestrator.get_stats_summary()
        except Exception as exc:
            await self._reply(update, f"❌ 통계 조회 실패: {exc}")
            return

        g = summary.get("global", {})
        proj = summary.get("projects", {})

        total_shipped = g.get("shipped", 0)
        total_failed  = g.get("failed", 0)
        total_held    = g.get("held", 0)
        total_adopted = g.get("adopted", 0)

        avg_elapsed = summary.get("avg_elapsed_s", 0.0)
        elapsed_str = ""
        if avg_elapsed > 0:
            mins = int(avg_elapsed // 60)
            secs = int(avg_elapsed % 60)
            elapsed_str = f"{mins}분 {secs}초" if mins else f"{secs}초"

        lines = [
            "📊 작업 통계\n",
            "전체:",
            f"  SHIPPED: {total_shipped}",
            f"  FAILED:  {total_failed}",
            f"  HELD:    {total_held}",
            f"  ADOPTED: {total_adopted}",
        ]

        if total_shipped + total_failed > 0:
            lines.append(f"\n평균:")
            lines.append(f"  review 통과율: {summary.get('pass_rate', 0):.1f}%")
            if elapsed_str:
                lines.append(f"  평균 작업 시간: {elapsed_str}")
            lines.append(f"  평균 재시도: {summary.get('avg_retries', 0):.1f}회")

        if proj:
            lines.append("\n프로젝트별:")
            for pid, pcnt in sorted(proj.items()):
                shipped_n = pcnt.get("shipped", 0)
                failed_n  = pcnt.get("failed", 0)
                lines.append(f"  {pid}: SHIPPED {shipped_n} / FAILED {failed_n}")

        recent_shipped = summary.get("recent_shipped", [])
        recent_failed  = summary.get("recent_failed", [])
        if recent_shipped:
            lines.append(f"\n최근 완료:\n  " + "\n  ".join(f"- {t}" for t in recent_shipped))
        if recent_failed:
            lines.append(f"\n최근 실패:\n  " + "\n  ".join(f"- {t}" for t in recent_failed))

        await self._reply(update, "\n".join(lines))

    async def _handle_stale(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/stale — 오래된 작업 감지 (HELD 7일+, READY_TO_SHIP 3일+, ADOPTED 5일+)."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        try:
            stale = self.orchestrator.get_stale_tasks()
        except Exception as exc:
            await self._reply(update, f"❌ stale 감지 실패: {exc}")
            return

        if not stale:
            await self._reply(
                update,
                "✅ 오래된 작업 없음\n\n"
                "HELD 7일+ / READY_TO_SHIP 3일+ / ADOPTED 5일+ 기준으로 감지합니다."
            )
            return

        status_icon = {
            "HELD":          "⏸️",
            "READY_TO_SHIP": "✅",
            "ADOPTED":       "📥",
        }
        lines = [f"⚠️ 오래된 작업 ({len(stale)}개)\n"]
        for s in stale:
            tid = s.get("task_id", "?")
            st  = s.get("status", "?")
            days = s.get("days", 0)
            branch = s.get("branch", "")
            nxt = s.get("next", [])
            icon = status_icon.get(st, "🔴")
            branch_str = f"\n   branch: {branch}" if branch else ""
            nxt_str = "\n   ".join(nxt[:2])
            lines.append(
                f"{icon} {tid}\n"
                f"   {st} — {days:.0f}일 경과{branch_str}\n"
                f"   추천: {nxt_str}"
            )

        await self._reply(update, "\n\n".join(lines))

    # ── Batch 6: Daily Summary ────────────────────────────────────────

    async def _daily_summary_loop(self) -> None:
        """매일 지정 시각에 daily summary 전송하는 background loop."""
        from datetime import datetime, timedelta
        while True:
            try:
                now = datetime.now()
                target = now.replace(
                    hour=self._daily_report_hour, minute=0, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(days=1)
                sleep_secs = (target - now).total_seconds()
                logger.info(f"Daily report 대기: {sleep_secs/3600:.1f}시간 후")
                await asyncio.sleep(sleep_secs)
                await self._send_daily_summary()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("daily_summary_loop 오류: %s", exc)
                await asyncio.sleep(3600)  # 오류 시 1시간 후 재시도

    async def _send_daily_summary(self) -> None:
        """Daily summary 메시지 생성 및 전송."""
        from datetime import datetime
        if self.orchestrator is None:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        try:
            today_stats = self.orchestrator.get_today_stats()
            queue = self.orchestrator.get_queue_summary()
            stale = self.orchestrator.get_stale_tasks()
        except Exception as exc:
            logger.error("daily summary 생성 실패: %s", exc)
            return

        shipped_n = today_stats.get("shipped", 0)
        failed_n  = today_stats.get("failed", 0)
        running = self.orchestrator.get_running_task()
        running_str = f"{running.get('task_id')} ({running.get('phase')})" if running else "없음"

        rts   = [t for t in queue if t.get("status") == "READY_TO_SHIP"]
        held  = [t for t in queue if t.get("status") == "HELD"]

        lines = [
            f"📊 PM Bot Daily — {today}\n",
            "오늘:",
            f"  SHIPPED: {shipped_n}",
            f"  FAILED:  {failed_n}",
            f"  RUNNING: {running_str}",
        ]

        if rts:
            rts_ids = "\n".join(f"  - {t['task_id']}" for t in rts)
            lines.append(f"\nREADY_TO_SHIP:\n{rts_ids}")

        if held:
            held_ids = "\n".join(f"  - {t['task_id']}" for t in held)
            lines.append(f"\nHELD:\n{held_ids}")

        if stale:
            stale_ids = "\n".join(f"  - {s['task_id']} ({s['status']} {s['days']:.0f}일)" for s in stale[:5])
            lines.append(f"\n⚠️ stale:\n{stale_ids}")
        else:
            lines.append("\n✅ stale 없음")

        await self.send_message("\n".join(lines))

    # ── PM Bot V3: task_inbox 명령 핸들러 ────────────────────────────

    async def _handle_enqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/enqueue <제목>\\n<본문> — 작업을 inbox에 pending 등록."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        # 메시지 전체 텍스트 파싱 (/enqueue 이후)
        message = update.message
        if message is None:
            return

        raw_text = message.text or ""
        # '/enqueue' 커맨드 접두어 제거
        body_full = raw_text.split(None, 1)[1] if " " in raw_text or "\n" in raw_text else ""
        body_full = body_full.lstrip()

        if not body_full:
            await self._reply(
                update,
                "사용법:\n"
                "/enqueue 제목\n\n"
                "본문...\n\n"
                "예:\n"
                "/enqueue RMF YAML import\n\n"
                "## Goal\nRMF building_map YAML import/export 구현"
            )
            return

        # 첫 줄 = 제목, 나머지 = 본문
        lines = body_full.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

        if not title:
            await self._reply(update, "❌ 제목이 비어 있습니다. 첫 줄에 제목을 입력하세요.")
            return

        # 본문 없으면 최소 본문 자동 생성
        if not body:
            body = f"## Goal\n{title}\n\n(내용을 보강해 승인 후 실행하세요.)"

        try:
            result = self.orchestrator.enqueue_task(title=title, body=body)
        except Exception as exc:
            await self._reply(update, f"❌ 등록 실패: {exc}")
            return

        if result["errors"]:
            err_text = "\n".join(f"• {e}" for e in result["errors"])
            await self._reply(update, f"❌ 등록 불가\n\n{err_text}")
            return

        task_id = result["task_id"]
        filename = result["filename"]

        card = (
            f"📋 작업 등록 완료\n\n"
            f"작업명: {title}\n"
            f"상태: pending\n"
            f"저장 위치: task_inbox/{filename}\n\n"
            f"진행할까요?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶ 진행해",  callback_data=f"inbox:approve:{task_id}"),
                InlineKeyboardButton("✏ 수정",    callback_data=f"inbox:show:{task_id}"),
                InlineKeyboardButton("⏸ 보류",    callback_data=f"inbox:hold:{task_id}"),
            ]
        ])
        try:
            await message.reply_text(card, reply_markup=keyboard)
        except Exception as exc:
            await self._reply(update, f"{card}\n\n(버튼 전송 실패: {exc})\n승인: /run {task_id}")

    async def _handle_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/run <task_id> or /run next — inbox 태스크 승인 → task_queue 이동."""
        if self.orchestrator is None:
            await self._reply(update, "❌ Orchestrator가 초기화되지 않았습니다.")
            return

        args = context.args or []
        if not args:
            await self._reply(
                update,
                "사용법:\n"
                "  /run <task_id>  — 특정 태스크 승인\n"
                "  /run next       — inbox 중 가장 오래된 태스크 승인"
            )
            return

        target = args[0].strip()

        if target.lower() == "next":
            try:
                inbox = self.orchestrator.get_inbox_summary()
            except Exception as exc:
                await self._reply(update, f"❌ inbox 조회 실패: {exc}")
                return
            if not inbox:
                await self._reply(update, "📭 inbox에 대기 중인 작업이 없습니다.")
                return
            # priority 순 (high > medium > low) + created_at 순
            _prio_order = {"high": 0, "medium": 1, "low": 2}
            inbox.sort(key=lambda t: (
                _prio_order.get(t.get("priority", "medium"), 1),
                t.get("created_at", "")
            ))
            target = inbox[0]["task_id"]

        try:
            result = self.orchestrator.approve_inbox_task(target)
        except Exception as exc:
            await self._reply(update, f"❌ 승인 실패: {exc}")
            return

        if not result["ok"]:
            await self._reply(update, result["message"])
            return

        await self._reply(
            update,
            f"▶ 작업 실행 대기열 등록\n\n"
            f"작업 ID: {result['task_id']}\n"
            f"상태: queued\n\n"
            f"Claude가 곧 실행을 시작합니다. /running 으로 확인하세요."
        )

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

        # PM Bot V3: inbox 콜백은 "inbox:action:task_id" 형식
        if data.startswith("inbox:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                _, inbox_action, task_id = parts
                task_id = task_id.strip()
                if self.orchestrator is None:
                    await self._cb_reply(query, "❌ Orchestrator가 초기화되지 않았습니다.")
                    return
                await self._handle_inbox_callback(query, inbox_action, task_id)
            else:
                await self._cb_reply(query, "알 수 없는 inbox 버튼 형식입니다.")
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

    async def _handle_inbox_callback(
        self, query: "CallbackQuery", action: str, task_id: str
    ) -> None:
        """PM Bot V3: inbox 버튼 콜백 처리 (approve / show / hold)."""
        if action == "approve":
            try:
                result = self.orchestrator.approve_inbox_task(task_id)
            except Exception as exc:
                await self._cb_reply(query, f"❌ 승인 실패: {exc}")
                return
            if result["ok"]:
                await self._cb_reply(
                    query,
                    f"▶ 작업 실행 대기열 등록\n\n"
                    f"작업 ID: {result['task_id']}\n"
                    f"상태: queued\n\n"
                    f"Claude가 곧 실행을 시작합니다. /running 으로 확인하세요."
                )
                # 원본 카드 버튼 제거
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            else:
                await self._cb_reply(query, result["message"])

        elif action == "show":
            # inbox 파일 내용 미리보기
            try:
                inbox = self.orchestrator.get_inbox_summary()
                found = next((t for t in inbox if t["task_id"] == task_id), None)
                if found:
                    preview = (
                        f"📄 작업 미리보기\n\n"
                        f"제목: {found['title']}\n"
                        f"우선순위: {found['priority']}\n"
                        f"등록: {found['created_at']}\n\n"
                        f"승인: /run {task_id}\n"
                        f"취소(보류): inbox에 파일 유지 — 나중에 /run {task_id} 로 실행"
                    )
                else:
                    preview = f"❌ inbox에서 '{task_id}'를 찾을 수 없습니다."
                await self._cb_reply(query, preview)
            except Exception as exc:
                await self._cb_reply(query, f"❌ 미리보기 실패: {exc}")

        elif action == "hold":
            await self._cb_reply(
                query,
                f"⏸ 보류됨\n\n"
                f"'{task_id}' 은 inbox에 그대로 보관됩니다.\n"
                f"나중에 /run {task_id} 로 실행할 수 있습니다.\n"
                f"/queue 로 전체 목록 확인."
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

        else:
            await self._cb_reply(query, f"알 수 없는 inbox 액션: {action}")

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
