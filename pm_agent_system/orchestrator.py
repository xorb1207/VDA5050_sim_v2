"""
orchestrator.py — Claude Code CLI subprocess orchestrator

태스크 큐(.md 파일)를 감시하고, Claude Code CLI로 실행한 뒤
결과를 ReviewAgent로 검토하고 GitManager로 커밋/머지/푸시.

Phase 0: 태스크별 로그 파일 생성 + /running /log 지원
Phase 1: 상태 카드 알림 + AUTO_SHIP_AFTER_REVIEW 게이트
Phase 3: 실패 카드 + retry/cancel/hold_branch 버튼
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from schemas import TaskPacket, CompletedPacket, ReviewVerdict, FileFinding
from git_manager import GitManager
from review_agent import ReviewAgent
from config import Config
from archive_manager import ArchiveManager
from stats_tracker import StatsTracker
from task_helpers import (
    make_task_id, build_pending_content, preflight_check,
    update_task_frontmatter, parse_task_frontmatter,
    write_atomic, is_safe_queue_file, is_processed_queue_file,
    mark_queue_file_status,
)


CLI_TIMEOUT_S = 3600   # 1시간 (subscription 모드)
MAX_RETRIES = 3
_LOG_TAIL_LINES = 50   # /log 명령 기본 출력 줄 수

# TYPE_CHECKING 전용 import (런타임 순환 의존 방지)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from project_manager import ProjectPaths


class _TaskQueueHandler(FileSystemEventHandler):
    """task_queue 디렉토리 파일 변경 감지 → asyncio 큐에 푸시."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._queue = queue
        self._loop = loop

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            path = Path(event.src_path)
            if is_safe_queue_file(path):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            path = Path(event.dest_path)
            if is_safe_queue_file(path):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, path)


def _parse_task_id(path: Path) -> str:
    """파일 stem에서 task_id 추출.

    예: "01_T-60a" → "T-60a"
         "retry_T-73" → "T-73"   (Phase 3 재시도 파일)
    숫자 prefix 없으면 stem 그대로 반환.
    """
    stem = path.stem  # e.g. "01_T-60a"
    # Phase 3: retry_ / resume_ prefix 처리
    for _pfx in ("retry_", "resume_"):
        if stem.startswith(_pfx):
            return stem[len(_pfx):]
    parts = stem.split("_", 1)
    if len(parts) > 1 and parts[0].isdigit():
        return parts[1]
    return stem


def _parse_allowed_files(task_content: str) -> list[str]:
    """마크다운에서 allowed_files 섹션의 파일 목록 파싱.

    지원 형식:
      1) 헤딩 형식: ## allowed_files\n- file.py\n- file2.py
      2) 키-값 블록: allowed_files:\n- file.py
      3) 인라인:    allowed_files: file.py, file2.py
    """
    files: list[str] = []

    # 형식 1+2: "allowed_files" 뒤에 콜론이 있든 없든, 헤딩(##)이든 키-값이든
    # 모두 잡는 통합 패턴 — 이어지는 "- item" 줄들을 수집
    block_pattern = re.compile(
        r"(?:^|\n)(?:#+\s*)?allowed_files\s*:?\s*\n((?:[ \t]*-[ \t]+\S[^\n]*\n?)+)",
        re.IGNORECASE,
    )
    m = block_pattern.search(task_content)
    if m:
        for line in m.group(1).splitlines():
            stripped = re.sub(r"^\s*-\s*", "", line).strip()
            if stripped:
                files.append(stripped)
        return files

    # 형식 3: 인라인 콤마 구분
    inline_pattern = re.compile(
        r"(?:^|\n)(?:#+\s*)?allowed_files\s*:\s*([^\n]+)", re.IGNORECASE
    )
    m2 = inline_pattern.search(task_content)
    if m2:
        for item in m2.group(1).split(","):
            s = item.strip()
            if s:
                files.append(s)

    return files


def _parse_files_changed(stdout: str, fallback: list[str]) -> list[str]:
    """CLI stdout에서 변경된 파일 목록 파싱. 실패 시 fallback 사용."""
    block = re.search(
        r"Files changed\s*:\s*\n((?:[ \t]*-[ \t]+\S[^\n]*\n?)+)",
        stdout,
        re.IGNORECASE,
    )
    if block:
        files = []
        for line in block.group(1).splitlines():
            s = re.sub(r"^\s*-\s*", "", line).strip()
            if s:
                files.append(s)
        if files:
            return files

    json_match = re.search(r"\{[^{}]*\"files_changed\"\s*:\s*\[[^\]]*\][^{}]*\}", stdout)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            fc = data.get("files_changed", [])
            if isinstance(fc, list) and fc:
                return [str(f) for f in fc]
        except json.JSONDecodeError:
            pass

    return fallback


def _build_commit_message(task_id: str, task_content: str) -> str:
    """태스크 내용에서 첫 번째 제목 줄을 커밋 메시지로 사용."""
    for line in task_content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            title = stripped[:80]
            return f"feat({task_id}): {title}"
    return f"feat({task_id}): automated implementation"


def _extract_task_title(task_content: str) -> str:
    """태스크 내용에서 짧은 제목 추출 (알림 카드용)."""
    for line in task_content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith("```") and len(stripped) > 3:
            return stripped[:80]
    return "(제목 없음)"


def _short_reason(verdict: ReviewVerdict) -> str:
    """ReviewVerdict에서 짧은 실패 원인 문자열 생성."""
    if verdict.violations:
        first = verdict.violations[0]
        return f"{first.rule}: {first.description[:80]}"
    return verdict.notes[:100] if verdict.notes else "리뷰 실패"


def _format_file_findings(findings: list[FileFinding], max_items: int = 5) -> str:
    """FileFinding 목록을 Telegram 친화적 문자열로 변환."""
    if not findings:
        return ""
    icons = {"ERROR": "🔴", "WARN": "🟡", "INFO": "🔵"}
    lines = []
    for f in findings[:max_items]:
        icon = icons.get(f.severity, "⚪")
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"{icon} {loc}\n   {f.finding[:120]}")
    if len(findings) > max_items:
        lines.append(f"... 외 {len(findings) - max_items}개")
    return "\n".join(lines)


# Phase 4: Diff 유틸리티 ────────────────────────────────────────────────

def _smart_truncate_diff(diff_text: str, max_chars: int = 12000) -> str:
    """diff를 파일 섹션별로 균등하게 truncate.

    파일 헤더(diff --git a/... b/...) 기준으로 섹션 분리.
    각 파일 섹션에서 앞부분을 우선 유지.
    전체 max_chars 내에서 최대한 많은 파일을 포함한다.
    """
    if not diff_text or len(diff_text) <= max_chars:
        return diff_text

    # diff --git 헤더로 파일 섹션 분리
    sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    if len(sections) <= 1:
        return diff_text[:max_chars] + "\n... [truncated]"

    # 섹션 수에 따라 파일당 할당 크기 계산 (헤더 포함 최소 800)
    per_file_budget = max(800, max_chars // len(sections))
    result_parts: list[str] = []
    used = 0

    for sec in sections:
        if not sec.strip():
            continue
        remaining_budget = max_chars - used
        if remaining_budget <= 0:
            break
        budget = min(per_file_budget, remaining_budget)
        if len(sec) <= budget:
            result_parts.append(sec)
            used += len(sec)
        else:
            result_parts.append(sec[:budget] + "\n... [file diff truncated]\n")
            used += budget

    return "".join(result_parts)


_SENSITIVE_FILE_PATTERNS = frozenset([
    ".env", "secret", "credential", "token", "password", "private_key",
    "auth", ".pem", ".key", ".cert", ".p12", "id_rsa", "id_ed25519",
    "apikey", "api_key",
])


def _is_sensitive_file(fname: str) -> bool:
    """config/env/security 관련 파일 여부 판단."""
    name = fname.lower().replace("\\", "/").split("/")[-1]
    return any(pat in name for pat in _SENSITIVE_FILE_PATTERNS)


def _parse_numstat(numstat_raw: str) -> dict[str, tuple[int, int]]:
    """'git diff --numstat' 출력 파싱 → {filename: (insertions, deletions)}."""
    result: dict[str, tuple[int, int]] = {}
    for line in numstat_raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
                fname = parts[2].strip()
                if fname:
                    result[fname] = (ins, dels)
            except ValueError:
                pass
    return result


def _parse_name_status(ns_raw: str) -> dict[str, str]:
    """'git diff --name-status' 출력 파싱 → {filename: status_char}."""
    result: dict[str, str] = {}
    for line in ns_raw.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status = parts[0].strip()[:1]  # M/A/D/R/C
            fname = parts[1].strip()
            if fname:
                result[fname] = status
    return result


def _format_diff_summary(task_id: str, diff_info: dict) -> str:
    """Phase 4: 모바일 친화적 diff 요약 생성 (LLM 미사용)."""
    changed_files = diff_info.get("changed_files", [])
    numstat_raw = diff_info.get("diff_numstat", "")
    name_status_raw = diff_info.get("name_status_raw", "")
    diff_stat = diff_info.get("diff_stat", "")

    if not changed_files:
        return (
            f"🔍 {task_id} Diff Summary\n\n"
            f"변경 파일 없음\n\n"
            f"CLI가 파일을 변경하지 않았거나 변경 감지에 실패했습니다."
        )

    numstat = _parse_numstat(numstat_raw)
    name_status = _parse_name_status(name_status_raw)

    status_label = {"M": "수정", "A": "추가", "D": "삭제", "R": "이름변경", "?": "신규(untracked)"}

    lines = [f"🔍 {task_id} Diff Summary\n", "변경 파일:"]
    for i, fname in enumerate(changed_files, 1):
        st = name_status.get(fname, "?")
        label = status_label.get(st, "변경")
        ins, dels = numstat.get(fname, (0, 0))
        stat_str = f"+{ins} / -{dels}" if ins or dels else ""
        stat_part = f", {stat_str}" if stat_str else ""
        lines.append(f"{i}. {fname} ({label}{stat_part})")

    # 요약 줄 (마지막 통계 줄)
    for stat_line in reversed(diff_stat.splitlines()):
        if "changed" in stat_line:
            lines.append(f"\n요약: {stat_line.strip()}")
            break

    # 위험 요소
    risk: list[str] = []
    sensitive = [f for f in changed_files if _is_sensitive_file(f)]
    deleted = [f for f, st in name_status.items() if st == "D"]
    if sensitive:
        risk.append(f"⚠️ 보안/설정 파일 변경: {', '.join(sensitive)}")
    if deleted:
        risk.append(f"⚠️ 파일 삭제: {', '.join(deleted)}")

    lines.append("\n위험 요소:")
    if risk:
        lines.extend(f"- {r}" for r in risk)
    else:
        lines.append("- 없음")

    return "\n".join(lines)


# Phase 3: 실패 분류 및 추천 액션 ─────────────────────────────────────────

_FAILURE_CATEGORIES = (
    "CLI_ERROR", "TEST_FAILED", "SCOPE_VIOLATION",
    "REVIEW_FAILED", "REVIEW_TIMEOUT", "GIT_FAILED", "TIMEOUT", "UNKNOWN",
)


def _classify_failure(
    exit_code: int | None,
    verdict: ReviewVerdict | None,
) -> str:
    """실패 원인을 카테고리 문자열로 분류."""
    if exit_code == 124:
        return "TIMEOUT"
    if exit_code == -1:
        return "CLI_ERROR"
    if verdict is not None:
        if verdict.violations:
            for v in verdict.violations:
                rule_lower = getattr(v, "rule", "").lower()
                if "timeout" in rule_lower and "review" in rule_lower:
                    return "REVIEW_TIMEOUT"
                if "scope" in rule_lower or "file" in rule_lower:
                    return "SCOPE_VIOLATION"
            return "REVIEW_FAILED"
        if verdict.verdict in ("FAIL", "NEEDS_REVISION"):
            return "REVIEW_FAILED"
    if exit_code is not None and exit_code not in (0,):
        return "CLI_ERROR"
    return "UNKNOWN"


def _recommended_action(category: str) -> str:
    """실패 카테고리별 추천 액션 문자열 반환."""
    return {
        "CLI_ERROR":       "로그를 확인하고 재시도하세요.",
        "TEST_FAILED":     "테스트 실패 원인을 확인하고 재시도하세요.",
        "SCOPE_VIOLATION": "allowed_files 목록을 확인하고 범위 내에서 재시도하세요.",
        "REVIEW_FAILED":   "리뷰 결과를 확인하고 수정 후 재시도하세요.",
        "REVIEW_TIMEOUT":  "Review Agent가 응답하지 않았습니다. /resume 또는 /retry로 재시도하세요.",
        "GIT_FAILED":      "git 상태를 확인하고 branch를 정리하세요.",
        "TIMEOUT":         "태스크 복잡도를 줄이거나 분할하여 재시도하세요.",
        "UNKNOWN":         "로그를 확인하고 재시도하세요.",
    }.get(category, "로그를 확인하세요.")


# Phase 3: Handoff 유틸리티 ──────────────────────────────────────────────────

def _extract_handoff_section(content: str, section_name: str) -> str:
    """Handoff 마크다운에서 특정 섹션 내용 추출."""
    m = re.search(
        r"## " + re.escape(section_name) + r"\n(.*?)(?=\n## |\Z)",
        content,
        re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _replace_handoff_section(content: str, section_name: str, new_body: str) -> str:
    """Handoff 마크다운의 특정 섹션 내용 교체. 섹션 없으면 말미에 추가."""
    pattern = re.compile(
        r"(## " + re.escape(section_name) + r"\n)(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    replacement = f"## {section_name}\n{new_body.rstrip()}\n"
    result, n = pattern.subn(replacement, content)
    if n == 0:
        return content.rstrip("\n") + f"\n\n## {section_name}\n{new_body.rstrip()}\n"
    return result


_HANDOFF_PLACEHOLDERS = frozenset({
    "-",
    "(태스크 목표 — 태스크 파일에서 확인)",
    "(현재 상태 직접 기입)",
    "<!-- 완료된 항목을 여기에 기입 -->",
    "<!-- 남은 작업을 여기에 기입 -->",
    "<!-- 주의사항 / 미완성 부분 -->",
    "<!-- PM Bot 또는 Claude Code에 넘길 다음 구현 지시문 -->",
})


def _format_handoff_summary(task_id: str, content: str) -> str:
    """Handoff 파일을 Telegram 친화적 짧은 요약으로 변환."""
    def _sec(name: str) -> str:
        raw = _extract_handoff_section(content, name)
        lines = [l for l in raw.splitlines() if l.strip() and not l.strip().startswith("<!--")]
        text = "\n".join(lines).strip()
        return text if text and text not in _HANDOFF_PLACEHOLDERS else ""

    goal = _sec("Goal")
    current = _sec("Current Status")
    done = _sec("Done")
    remaining = _sec("Remaining")
    risks = _sec("Risks")
    next_p = _sec("Next Prompt")

    lines = [f"📄 {task_id} Handoff\n"]
    if goal:
        lines.append(f"Goal:\n{goal[:200]}")
    if current:
        lines.append(f"\n상태:\n{current[:200]}")
    if done:
        lines.append(f"\nDone:\n{done[:300]}")
    if remaining:
        lines.append(f"\nRemaining:\n{remaining[:300]}")
    if risks:
        lines.append(f"\nRisks:\n{risks[:200]}")
    if next_p:
        lines.append(f"\nNext:\n{next_p[:200]}")
    if len(lines) == 1:
        lines.append("(내용 없음 — /handoff 로 업데이트하세요)")
    return "\n".join(lines)


def _build_resume_prompt(task_id: str, handoff_content: str) -> str:
    """Handoff 마크다운에서 Claude Code CLI 재개 프롬프트 생성 (짧고 집중적)."""
    goal = _extract_handoff_section(handoff_content, "Goal")
    current = _extract_handoff_section(handoff_content, "Current Status")
    remaining = _extract_handoff_section(handoff_content, "Remaining")
    risks = _extract_handoff_section(handoff_content, "Risks")
    next_p = _extract_handoff_section(handoff_content, "Next Prompt")

    # Next Prompt 우선, 없으면 Remaining, 없으면 Goal
    instruction = next_p.strip() or remaining.strip() or goal.strip() or f"{task_id} 이어서 구현"

    return f"""# Resume: {task_id}

이전 작업을 이어서 진행합니다.
전체 repo 재스캔 없이 아래 지시에 집중하세요.

## 목표 (Goal)
{goal or '(handoff에서 확인)'}

## 현재 상태 (Current Status)
{current or '(확인 필요)'}

## 남은 작업 (Remaining)
{remaining or '(handoff에서 확인)'}

## 주의사항 (Risks)
{risks or '없음'}

## 구현 지시 (Next Prompt)
{instruction}
"""


class Orchestrator:
    """태스크 큐를 감시하고 Claude Code CLI로 태스크를 실행하는 오케스트레이터."""

    def __init__(
        self,
        config: Config,
        git_manager: GitManager | None = None,
        review_agent: ReviewAgent | None = None,
        notify_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        notify_card_fn: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.config = config
        self.git_manager = git_manager
        self.review_agent = review_agent
        self.notify_fn = notify_fn
        # Phase 2: READY_TO_SHIP 카드 (inline keyboard 포함) 전송용
        # signature: notify_card_fn(text: str, task_id: str) -> None
        self.notify_card_fn = notify_card_fn
        # Phase 3: 실패 카드 (inline keyboard 포함) 전송용
        # signature: notify_failure_card_fn(text: str, task_id: str) -> None
        self.notify_failure_card_fn: Callable[[str, str], Coroutine[Any, Any, None]] | None = None

        cfg = config
        base = Path(__file__).resolve().parent

        self.task_queue_dir = (
            Path(cfg.task_queue_dir)
            if Path(cfg.task_queue_dir).is_absolute()
            else base / cfg.task_queue_dir
        )
        self.completed_dir = (
            Path(cfg.completed_dir)
            if Path(cfg.completed_dir).is_absolute()
            else base / cfg.completed_dir
        )
        self.spec_path = Path(cfg.spec_path)

        # Phase 0: 로그 디렉토리
        self._log_dir: Path = (
            Path(cfg.logs_dir) / "tasks"
            if Path(cfg.logs_dir).is_absolute()
            else base / cfg.logs_dir / "tasks"
        )

        # PM Bot V3: task_inbox — 등록됐지만 미승인 태스크 대기 디렉토리
        self.task_inbox_dir: Path = base / "task_inbox"

        self.task_queue_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self.task_inbox_dir.mkdir(parents=True, exist_ok=True)

        self._processed_ids: set[str] = set()
        self._active_tasks: dict[str, Any] = {}

        # Phase 0: 현재 실행 중인 태스크 메타데이터
        self._running_task: dict = {}

        # Phase 1: READY_TO_SHIP 대기 중인 태스크
        self._ready_to_ship: dict[str, dict] = {}

        # Phase 3.5: ADOPTED 태스크 (Review 전 중간 상태)
        self._adopted_tasks: dict[str, dict] = {}

        # Phase 3: 실패 태스크 정보 (retry/hold_branch/cancel용)
        self._failed_tasks: dict[str, dict] = {}
        # Batch 6: HELD 태스크 추적 (stale 감지용)
        self._held_tasks: dict[str, dict] = {}
        # 현재 실행 중인 subprocess (cancel용)
        self._current_proc: asyncio.subprocess.Process | None = None  # type: ignore[type-arg]
        # cancel 요청된 task_id 집합
        self._cancel_requested: set[str] = set()
        # 수동 재시도 횟수 (task_id → count)
        self._manual_retry_counts: dict[str, int] = {}

        # Phase 4: 태스크별 git diff 정보 (/diff T-ID 및 리뷰용)
        self._task_diffs: dict[str, dict] = {}

        # Batch 6: 태스크 시작 시각 (통계용)
        self._task_started_at: dict[str, datetime] = {}

        # Phase 0.5: 프로젝트 전환 지원
        self._current_project_id: str = ""
        self._pending_paths: "ProjectPaths | None" = None
        self._switch_event: asyncio.Event | None = None
        # handoffs 디렉토리 (ProjectPaths 없으면 pm_agent_system/handoffs/)
        self._handoffs_dir: Path = (
            Path(cfg.logs_dir).parent / "handoffs"
            if Path(cfg.logs_dir).is_absolute()
            else base / "handoffs"
        )
        self._handoffs_dir.mkdir(parents=True, exist_ok=True)

        # Batch 6: Archive / Stats
        self._archive_dir: Path = base / "archive"
        self._archive_mgr = ArchiveManager(
            archive_dir=self._archive_dir,
            logs_dir=self._log_dir,
            handoffs_dir=self._handoffs_dir,
        )
        self._stats = StatsTracker(stats_path=base / "stats.json")

        # 시작 시 오래된 로그/archive 정리
        _log_ret = cfg.log_retention_days
        _arc_ret = cfg.archive_retention_days
        if _log_ret > 0:
            _removed_logs = self._archive_mgr.cleanup_old_logs(_log_ret)
            if _removed_logs:
                print(f"[orchestrator] 오래된 로그 {len(_removed_logs)}개 삭제 (>{_log_ret}일)")
        if _arc_ret > 0:
            _removed_arc = self._archive_mgr.cleanup_old_archives(_arc_ret)
            if _removed_arc:
                print(f"[orchestrator] 오래된 archive {len(_removed_arc)}개 삭제 (>{_arc_ret}일)")

        # Stale lock pre-populate — 중복 실행 방지
        for f in self.completed_dir.glob("*_completed.json"):
            stem = f.stem
            if stem.endswith("_completed"):
                self._processed_ids.add(stem[: -len("_completed")])

        if self.git_manager is not None:
            for t in self.git_manager.get_state().get("completed_tasks", []):
                tid = t.get("task_id")
                if tid:
                    self._processed_ids.add(tid)

        # 재시작 안전: task_queue/ 에 남은 .done.md / .cancelled.md → processed_ids 등록
        self._register_processed_queue_files()

    # ── Public interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        """watchdog으로 task_queue 감시 + 기존 파일 초기 스캔.

        Phase 0.5: switch_project() 호출 시 observer를 재시작해
        새 프로젝트의 task_queue 디렉토리를 감시.
        """
        while True:  # 프로젝트 전환 시 재시작
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[Path] = asyncio.Queue()
            self._switch_event = asyncio.Event()

            handler = _TaskQueueHandler(queue, loop)
            observer = Observer()
            observer.schedule(handler, str(self.task_queue_dir), recursive=False)
            observer.start()

            print(f"[orchestrator] task_queue 감시 시작: {self.task_queue_dir}"
                  + (f" (project: {self._current_project_id})" if self._current_project_id else ""))

            # 기존 .md 파일 초기 스캔 (안전 검사 통과한 파일만)
            for existing_md in sorted(self.task_queue_dir.glob("*.md")):
                if is_safe_queue_file(existing_md):
                    await queue.put(existing_md)

            try:
                while not self._switch_event.is_set():
                    try:
                        task_path = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    task_id = _parse_task_id(task_path)
                    if task_id in self._active_tasks or task_id in self._processed_ids:
                        continue

                    # ★ 직렬 실행
                    self._active_tasks[task_id] = True
                    try:
                        await self._process_task(task_path)
                    finally:
                        self._active_tasks.pop(task_id, None)
            finally:
                observer.stop()
                observer.join()

            # 전환 요청이 있으면 새 paths 적용 후 재시작
            if self._pending_paths is not None:
                self._apply_project_paths(self._pending_paths)
                self._pending_paths = None
            else:
                break  # 정상 종료

    # ── Phase 0.5: 프로젝트 전환 ─────────────────────────────────────────

    @property
    def current_project_id(self) -> str:
        return self._current_project_id

    @property
    def handoffs_dir(self) -> Path:
        return self._handoffs_dir

    def switch_project(self, paths: "ProjectPaths") -> None:
        """프로젝트 전환 요청.

        현재 task가 실행 중이면 완료 후 전환됨.
        start() 루프가 _switch_event를 감지해 observer를 재시작.
        """
        self._pending_paths = paths
        if self._switch_event is not None:
            self._switch_event.set()
        print(f"[orchestrator] 프로젝트 전환 요청: {paths.project_id}")

    def _apply_project_paths(self, paths: "ProjectPaths") -> None:
        """새 프로젝트 경로를 실제로 적용한다 (observer 재시작 전에 호출)."""
        self.task_queue_dir = paths.task_queue_dir
        self.completed_dir = paths.completed_dir
        self._log_dir = paths.task_logs_dir
        self.spec_path = paths.spec_path
        self._handoffs_dir = paths.handoffs_dir
        self._current_project_id = paths.project_id
        # PM Bot V3: 프로젝트별 inbox 디렉토리
        self.task_inbox_dir = paths.task_queue_dir.parent / "task_inbox"
        self.task_inbox_dir.mkdir(parents=True, exist_ok=True)

        # GitManager 경로도 업데이트
        if self.git_manager is not None:
            self.git_manager.repo_path = paths.repo_path  # type: ignore[assignment]

        # 디렉토리 생성
        paths.ensure_dirs()

        # 새 프로젝트의 completed/ 파일로 processed_ids 보강
        for f in self.completed_dir.glob("*_completed.json"):
            stem = f.stem
            if stem.endswith("_completed"):
                self._processed_ids.add(stem[: -len("_completed")])

        self._register_processed_queue_files()
        print(f"[orchestrator] 프로젝트 적용 완료: {paths.project_id} → {paths.task_queue_dir}")

    def _register_processed_queue_files(self) -> None:
        """task_queue/ 에 남은 .done.md / .failed.md / .cancelled.md 파일의
        task_id를 processed_ids에 등록.

        봇 재시작 시 이미 처리된 파일이 재실행되지 않도록 방지.
        """
        _status_strip = (".failed", ".done", ".cancelled")

        def _clean_tid(raw: str) -> str:
            """_parse_task_id 결과에서 상태 suffix 제거. 'T-OLD.failed' → 'T-OLD'"""
            for s in _status_strip:
                if raw.endswith(s):
                    return raw[: -len(s)]
            return raw

        count = 0
        for f in self.task_queue_dir.glob("*.md"):
            if is_processed_queue_file(f):
                tid = _clean_tid(_parse_task_id(f))
                if tid not in self._processed_ids:
                    self._processed_ids.add(tid)
                    count += 1
        if count:
            print(f"[orchestrator] 재시작 안전: 처리 완료 파일 {count}개 → processed_ids 등록")

    def reload_task_queue(self) -> dict:
        """task_queue/ 를 재스캔해 실행 가능한 파일을 asyncio 큐에 등록.

        watcher와 동일한 is_safe_queue_file() 필터를 사용하므로
        .done/.failed/.cancelled 파일은 절대 실행 후보에 포함되지 않음.

        Returns:
            {"queued": int, "skipped_processed": int, "skipped_unsafe": int}
        """
        queued = 0
        skipped_processed = 0
        skipped_unsafe = 0

        # 처리 완료 파일을 processed_ids에 먼저 등록 (재시작 안전 보강)
        self._register_processed_queue_files()

        for md in sorted(self.task_queue_dir.glob("*.md")):
            # 1. 처리 완료 파일 (is_processed_queue_file) → skip
            if is_processed_queue_file(md):
                skipped_processed += 1
                continue
            # 2. 안전하지 않은 파일 (숨김/tmp/너무 짧음) → skip
            if not is_safe_queue_file(md):
                skipped_unsafe += 1
                continue
            # 3. 이미 실행 중이거나 processed → skip
            tid = _parse_task_id(md)
            if tid in self._processed_ids or tid in self._active_tasks:
                skipped_processed += 1
                continue
            # 4. asyncio 큐에 직접 푸시 (start() 루프가 처리)
            # watchdog 루프와 공유하는 큐가 없으므로 직접 _process_task 스케줄
            # start() 루프에 접근할 수 없는 구조이므로 파일을 touch해 watchdog 트리거
            try:
                md.touch()
                queued += 1
                print(f"[orchestrator] reload: {md.name} → 실행 대기열 등록")
            except OSError:
                skipped_unsafe += 1

        return {
            "queued": queued,
            "skipped_processed": skipped_processed,
            "skipped_unsafe": skipped_unsafe,
        }

    def get_status(self) -> dict:
        """현재 시스템 상태 딕셔너리 반환."""
        active: dict[str, dict] = {}

        # 현재 실행 중인 태스크
        if self._running_task:
            tid = self._running_task.get("task_id", "")
            if tid:
                started = self._running_task.get("started_at")
                created_at = started.isoformat() if isinstance(started, datetime) else str(started or "")
                active[tid] = {
                    "status": self._running_task.get("phase", "RUNNING"),
                    "branch": self._running_task.get("branch", ""),
                    "created_at": created_at,
                }

        # ADOPTED 태스크 (Phase 3.5)
        for tid, info in self._adopted_tasks.items():
            active[tid] = {
                "status": "ADOPTED",
                "branch": info.get("branch", ""),
                "created_at": info.get("adopted_at", ""),
            }

        # READY_TO_SHIP 태스크
        for tid, info in self._ready_to_ship.items():
            active[tid] = {
                "status": "READY_TO_SHIP",
                "branch": info.get("branch", ""),
                "created_at": info.get("ready_at", ""),
            }

        # HELD 태스크 (Batch 6)
        held: dict[str, dict] = {}
        for tid, info in self._held_tasks.items():
            held[tid] = {
                "status": "HELD",
                "branch": info.get("branch", ""),
                "created_at": info.get("held_at", ""),
            }

        completed = []
        if self.git_manager is not None:
            completed = self.git_manager.get_state().get("completed_tasks", [])

        return {
            "active_tasks": active,
            "held_tasks": held,
            "queued_tasks": list(self._active_tasks.keys()),
            "completed_tasks": completed,
        }

    def get_running_task(self) -> dict | None:
        """Phase 0: 현재 실행 중인 태스크 정보 반환. 없으면 None."""
        if not self._running_task:
            return None
        return dict(self._running_task)

    # ── Batch 6 공개 메서드 ──────────────────────────────────────────

    async def archive_task_manual(self, task_id: str) -> str:
        """Phase 6-A: 수동 /archive T-ID — 태스크 파일을 archive에 저장.

        SHIPPED/FAILED/HELD 모든 상태에서 호출 가능.
        이미 archive된 경우 덮어쓰기.
        """
        # 현재 상태 판별
        if task_id in self._ready_to_ship:
            status = "READY_TO_SHIP"
        elif task_id in self._adopted_tasks:
            status = "ADOPTED"
        elif task_id in self._held_tasks:
            status = "HELD"
        elif task_id in self._failed_tasks:
            status = "FAILED"
        else:
            # git_manager completed 확인
            status = "UNKNOWN"
            if self.git_manager is not None:
                completed = self.git_manager.get_state().get("completed_tasks", [])
                if any(t.get("task_id") == task_id for t in completed):
                    status = "SHIPPED"

        label = self._task_label(task_id)
        try:
            arc_path = self._archive_mgr.archive_task(
                task_id, status,
                metadata={"project_id": self._current_project_id},
            )
            return (
                f"📦 {label} archive 완료\n\n"
                f"상태: {status}\n"
                f"경로: {arc_path}\n\n"
                f"/history 로 확인하세요."
            )
        except Exception as exc:
            return f"❌ {label} archive 실패\n{exc}"

    def get_history(self, limit: int = 20) -> list[dict]:
        """Phase 6-A: archive 목록 반환 (최신순)."""
        return self._archive_mgr.list_history(limit=limit)

    def get_stats_summary(self) -> dict:
        """Phase 6-B: 통계 집계 반환."""
        return self._stats.get_summary()

    def get_today_stats(self) -> dict:
        """Phase 6-D: 오늘 통계 반환."""
        return self._stats.get_today_summary()

    def get_stale_tasks(self) -> list[dict]:
        """Phase 6-C: 오래된 작업 감지.

        기준:
          HELD       → 7일 이상
          READY_TO_SHIP → 3일 이상
          ADOPTED    → 5일 이상
        """
        from datetime import timedelta

        stale: list[dict] = []
        now = datetime.now()

        thresholds = {
            "HELD":          timedelta(days=7),
            "READY_TO_SHIP": timedelta(days=3),
            "ADOPTED":       timedelta(days=5),
        }

        def _elapsed_days(iso_str: str) -> float:
            try:
                return (now - datetime.fromisoformat(iso_str)).total_seconds() / 86_400
            except Exception:
                return 0.0

        for tid, info in self._held_tasks.items():
            days = _elapsed_days(info.get("held_at", ""))
            if days >= thresholds["HELD"].days:
                stale.append({
                    "task_id": self._task_label(tid),
                    "status": "HELD",
                    "days": round(days, 1),
                    "branch": info.get("branch", ""),
                    "next": [f"/resume {tid}", f"/archive {tid}", f"/cancel {tid}"],
                })

        for tid, info in self._ready_to_ship.items():
            days = _elapsed_days(info.get("ready_at", ""))
            if days >= thresholds["READY_TO_SHIP"].days:
                stale.append({
                    "task_id": self._task_label(tid),
                    "status": "READY_TO_SHIP",
                    "days": round(days, 1),
                    "branch": info.get("branch", ""),
                    "next": [f"/ship {tid}", f"/hold {tid}"],
                })

        for tid, info in self._adopted_tasks.items():
            days = _elapsed_days(info.get("adopted_at", ""))
            if days >= thresholds["ADOPTED"].days:
                stale.append({
                    "task_id": self._task_label(tid),
                    "status": "ADOPTED",
                    "days": round(days, 1),
                    "branch": info.get("branch", ""),
                    "next": [f"/review {tid}", f"/resume {tid}", f"/archive {tid}"],
                })

        # 경과 일수 내림차순
        stale.sort(key=lambda x: x["days"], reverse=True)
        return stale

    async def get_doctor_info(self) -> dict:
        """Batch 5: PM Bot 상태 점검 데이터 반환."""
        repo = str(self.config.repo_path)
        from pathlib import Path as _Path

        repo_exists = _Path(repo).is_dir()
        task_queue_exists = self.task_queue_dir.is_dir()
        handoffs_exists = self._handoffs_dir.is_dir()

        # queued .md 파일 수 (ready/ 하위 제외)
        queued_md = []
        if task_queue_exists:
            queued_md = [
                f.name for f in self.task_queue_dir.glob("*.md")
                if not f.name.endswith(".cancelled.md")
            ]

        # git 상태
        git_branch = ""
        git_status_short = "(git 조회 실패)"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            git_branch = out.decode("utf-8", errors="replace").strip()

            proc2 = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "status", "--short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=10)
            git_status_short = out2.decode("utf-8", errors="replace").strip() or "(clean)"
        except Exception:
            pass

        return {
            "project_id": self._current_project_id or "(single)",
            "repo_path": repo,
            "repo_exists": repo_exists,
            "task_queue": str(self.task_queue_dir),
            "task_queue_exists": task_queue_exists,
            "queued_md": queued_md,
            "handoffs_dir": str(self._handoffs_dir),
            "handoffs_exists": handoffs_exists,
            "running_task_id": self._running_task.get("task_id") if self._running_task else None,
            "running_phase": self._running_task.get("phase") if self._running_task else None,
            "ready_to_ship_ids": list(self._ready_to_ship.keys()),
            "adopted_ids": list(self._adopted_tasks.keys()),
            "failed_ids": list(self._failed_tasks.keys()),
            "git_branch": git_branch,
            "git_status_short": git_status_short,
            "auto_ship_after_review": self.config.auto_ship_after_review,
            "spec_path": str(self.spec_path),
            "spec_exists": self.spec_path.exists(),
        }

    def get_queue_summary(self) -> list[dict]:
        """Batch 5: 모든 상태 태스크의 대기열 요약 반환."""
        tasks: list[dict] = []
        label_fn = self._task_label

        # RUNNING / REVIEWING / QUEUED (active)
        if self._running_task:
            tid = self._running_task.get("task_id", "")
            if tid:
                tasks.append({
                    "task_id": label_fn(tid),
                    "status": self._running_task.get("phase", "RUNNING"),
                    "branch": self._running_task.get("branch", ""),
                    "next": f"/log {tid}",
                })

        # ADOPTED
        for tid, info in self._adopted_tasks.items():
            tasks.append({
                "task_id": label_fn(tid),
                "status": "ADOPTED",
                "branch": info.get("branch", ""),
                "next": f"/review {tid}",
            })

        # READY_TO_SHIP
        for tid, info in self._ready_to_ship.items():
            tasks.append({
                "task_id": label_fn(tid),
                "status": "READY_TO_SHIP",
                "branch": info.get("branch", ""),
                "next": f"/ship {tid}",
            })

        # FAILED
        for tid, info in self._failed_tasks.items():
            cat = info.get("failure_category", "?")
            tasks.append({
                "task_id": label_fn(tid),
                "status": f"FAILED ({cat})",
                "branch": info.get("branch", ""),
                "next": f"/resume {tid}",
            })

        # HELD (Batch 6)
        for tid, info in self._held_tasks.items():
            tasks.append({
                "task_id": label_fn(tid),
                "status": "HELD",
                "branch": info.get("branch", ""),
                "next": f"/resume {tid}",
            })

        return tasks

    # ── PM Bot V3: task_inbox ─────────────────────────────────────────────

    def enqueue_task(
        self,
        title: str,
        body: str,
        priority: str = "medium",
        created_from: str = "telegram",
    ) -> dict:
        """제목 + 본문을 받아 task_inbox/ 에 pending 파일로 저장한다.

        Returns:
            {"task_id": str, "filename": str, "path": str, "errors": list[str]}
        """
        errors = preflight_check(title, body)
        if errors:
            return {"task_id": "", "filename": "", "path": "", "errors": errors}

        from datetime import datetime as _dt
        now = _dt.now()
        task_id = make_task_id(title, now)

        # task_id 중복 방지 (inbox + queue 동시 확인)
        if (self.task_inbox_dir / f"{task_id}.md").exists():
            task_id = f"{task_id}-{now.strftime('%S')}"
        if (self.task_queue_dir / f"{task_id}.md").exists():
            task_id = f"{task_id}-q"

        content = build_pending_content(
            task_id=task_id,
            title=title,
            body=body,
            created_from=created_from,
            priority=priority,
            now=now,
        )

        filename = f"{task_id}.md"
        dest = self.task_inbox_dir / filename
        write_atomic(dest, content)
        print(f"[orchestrator] 태스크 등록(pending): {filename}")
        return {"task_id": task_id, "filename": filename, "path": str(dest), "errors": []}

    def approve_inbox_task(self, task_id: str) -> dict:
        """inbox의 pending 태스크를 승인해 task_queue/ 로 이동.

        local drop 파일 (frontmatter 없음)도 지원:
        - title / task_id를 파싱해 frontmatter에 자동 추가
        - is_valid 체크 포함 (빈 파일, 파싱 실패, 중복 등)

        Returns:
            {"ok": bool, "task_id": str, "message": str, "title": str}
        """
        from task_helpers import parse_inbox_file, is_skip_inbox_file

        # ── 파일 탐색 (exact → glob) ────────────────────────────────────────
        inbox_file = self.task_inbox_dir / f"{task_id}.md"
        if not inbox_file.exists():
            matches = [
                f for f in self.task_inbox_dir.glob(f"*{task_id}*.md")
                if not is_skip_inbox_file(f)
            ]
            if len(matches) == 1:
                inbox_file = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m.name for m in matches)
                return {"ok": False, "task_id": task_id, "title": "",
                        "message": f"❌ 여러 파일이 매칭됩니다: {names}"}
            else:
                return {"ok": False, "task_id": task_id, "title": "",
                        "message": f"❌ inbox에서 '{task_id}' 파일을 찾을 수 없습니다."}

        # ── 파일 파싱 + 유효성 검사 ──────────────────────────────────────────
        info = parse_inbox_file(inbox_file)
        if not info["is_valid"]:
            return {"ok": False, "task_id": task_id, "title": info["title"],
                    "message": f"❌ 실행 불가: {info['invalid_reason']}"}

        # ── 이미 큐에 있는지 확인 ────────────────────────────────────────────
        resolved_id = info["task_id"]
        if self._is_already_queued(resolved_id):
            return {"ok": False, "task_id": resolved_id, "title": info["title"],
                    "message": f"❌ '{resolved_id}'는 이미 실행 대기열 또는 완료됨"}

        # ── frontmatter 보강 (local drop 포함) ───────────────────────────────
        from datetime import datetime as _dt
        updates: dict = {
            "id":          resolved_id,
            "title":       info["title"],
            "status":      "queued",
            "approved":    True,
            "approved_at": _dt.now().astimezone().isoformat(),
        }
        if not info["has_frontmatter"]:
            updates["created_from"] = "local_drop"
            updates["priority"]     = info["priority"]
        try:
            new_content = update_task_frontmatter(inbox_file, updates)
        except Exception as exc:
            return {"ok": False, "task_id": resolved_id, "title": info["title"],
                    "message": f"❌ frontmatter 업데이트 실패: {exc}"}

        # ── 재시도 시 이전 .failed.md 정리 ─────────────────────────────────
        for suffix in (".failed.md", ".cancelled.md"):
            old = self.task_queue_dir / (inbox_file.stem + suffix)
            if old.exists():
                try:
                    old.unlink()
                    print(f"[orchestrator] 이전 실패 파일 제거: {old.name}")
                except Exception:
                    pass
        # _processed_ids에서도 제거 (재실행 허용)
        self._processed_ids.discard(resolved_id)

        # ── task_queue/ 로 원자적 이동 ───────────────────────────────────────
        dest = self.task_queue_dir / inbox_file.name
        write_atomic(dest, new_content)
        try:
            inbox_file.unlink()
        except Exception:
            pass

        print(f"[orchestrator] 태스크 승인: {inbox_file.name} → task_queue/")
        return {"ok": True, "task_id": resolved_id, "title": info["title"],
                "message": f"▶ 실행 대기열 등록 완료: {inbox_file.name}"}

    def _is_already_queued(self, task_id: str) -> bool:
        """task_id가 현재 실행 대기 중이거나 실행 중인지 확인.

        실패(failed) / 완료(shipped) / 보류(held) 상태는 재시도 허용.
        막는 경우: RUNNING / 대기(.md) / ACTIVE 상태.
        """
        # 현재 실행 중
        if self._running_task and self._running_task.get("task_id") == task_id:
            return True
        # 인메모리 active (대기 중이지만 아직 실행 전)
        if task_id in getattr(self, "_active_tasks", {}):
            return True
        # task_queue에 .md (순수 대기 파일) 존재 — .failed.md/.done.md 제외
        plain_md = self.task_queue_dir / f"{task_id}.md"
        if plain_md.exists():
            return True
        return False

    def _is_retryable(self, task_id: str) -> bool:
        """실패/완료 이력이 있지만 재시도 가능한 task_id인지 확인."""
        # .failed.md 가 task_queue에 있으면 재시도 가능
        failed_md = self.task_queue_dir / f"{task_id}.failed.md"
        if failed_md.exists():
            return True
        # _failed_tasks 인메모리
        if task_id in getattr(self, "_failed_tasks", {}):
            return True
        return False

    def get_inbox_summary(self) -> list[dict]:
        """task_inbox/ 의 pending 태스크 목록 반환 (레거시 / 하위 호환).

        신규 코드는 get_inbox_files()를 사용 권장.
        """
        result: list[dict] = []
        for md in sorted(self.task_inbox_dir.glob("*.md")):
            if not md.name.endswith(".md") or md.name.startswith("."):
                continue
            fm = parse_task_frontmatter(md)
            result.append({
                "task_id":   fm.get("id", md.stem),
                "title":     fm.get("title", md.stem),
                "status":    fm.get("status", "pending"),
                "priority":  fm.get("priority", "medium"),
                "created_at": fm.get("created_at", ""),
                "filename":  md.name,
            })
        return result

    def get_inbox_files(self) -> list[dict]:
        """task_inbox/ 전체 스캔 + 파싱 (local drop 포함).

        기존 get_inbox_summary()와 달리:
        - frontmatter 없는 파일도 포함
        - is_valid / invalid_reason 필드 포함
        - 중복 task_id 감지
        - 이미 처리된 task_id 감지
        """
        from task_helpers import parse_inbox_file, is_skip_inbox_file

        result: list[dict] = []
        seen_ids: dict[str, str] = {}   # task_id → filename

        for md in sorted(self.task_inbox_dir.glob("*.md")):
            if is_skip_inbox_file(md):
                continue

            info = parse_inbox_file(md)
            tid = info["task_id"]

            # 중복 task_id 감지
            if info["is_valid"] and tid in seen_ids:
                info["is_valid"] = False
                info["invalid_reason"] = f"중복 task_id (기존 파일: {seen_ids[tid]})"
            else:
                seen_ids[tid] = md.name

            # 현재 실행/대기 중이면 차단, 실패 이력은 재시도 허용
            if info["is_valid"] and self._is_already_queued(tid):
                info["is_valid"] = False
                info["invalid_reason"] = "현재 실행 중 또는 대기열에 있음"
            elif info["is_valid"] and self._is_retryable(tid):
                # 실패 이력 있음 — 재시도 가능으로 표시 (is_valid=True 유지)
                info["retry"] = True
                info["retry_note"] = "이전 실행 실패 — 재시도 가능"

            result.append(info)

        return result

    def get_inbox_task_detail(self, task_id: str) -> dict | None:
        """inbox에서 특정 task_id의 상세 정보 반환. 없으면 None."""
        from task_helpers import parse_inbox_file, is_skip_inbox_file

        # exact match
        exact = self.task_inbox_dir / f"{task_id}.md"
        if exact.exists() and not is_skip_inbox_file(exact):
            info = parse_inbox_file(exact)
            try:
                info["body_full"] = exact.read_text(encoding="utf-8", errors="replace")
            except Exception:
                info["body_full"] = info.get("body_preview", "")
            return info

        # glob fallback (task_id를 파일명에 포함하는 파일 탐색)
        matches = [
            f for f in sorted(self.task_inbox_dir.glob("*.md"))
            if task_id in f.stem and not is_skip_inbox_file(f)
        ]
        if len(matches) == 1:
            info = parse_inbox_file(matches[0])
            try:
                info["body_full"] = matches[0].read_text(encoding="utf-8", errors="replace")
            except Exception:
                info["body_full"] = info.get("body_preview", "")
            return info

        return None

    def get_task_log(self, task_id: str, lines: int = _LOG_TAIL_LINES) -> str | None:
        """Phase 0: 태스크 combined 로그의 최근 N 줄 반환. 파일 없으면 None."""
        log_path = self._log_dir / f"{task_id}.combined.log"
        if not log_path.exists():
            return None
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = content.splitlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return "\n".join(tail)
        except OSError:
            return None

    def get_task_diff_summary(self, task_id: str) -> str:
        """Phase 4: /diff T-ID 명령용 모바일 친화적 diff 요약 반환.

        메타데이터(파일 목록/통계) + actual diff 앞부분 스니펫을 함께 표시.
        """
        diff_info = self._task_diffs.get(task_id)
        if diff_info is None:
            # 로그 디렉토리에서 저장된 diff 파일 시도
            diff_file = self._log_dir / f"{task_id}.diff"
            if diff_file.exists():
                try:
                    import json as _json
                    diff_info = _json.loads(diff_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

        if not diff_info:
            return (
                f"🔍 {task_id} Diff\n\n"
                f"diff 정보 없음\n"
                f"태스크가 실행된 적이 없거나 아직 CLI 실행 전입니다."
            )

        # 기본 요약 (파일 목록 + 통계 + 위험 요소)
        summary = _format_diff_summary(task_id, diff_info)

        # actual diff 스니펫 (앞 1500자) — .actual.diff 파일 우선, 메모리 fallback
        actual_diff = ""
        actual_file = self._log_dir / f"{task_id}.actual.diff"
        if actual_file.exists():
            try:
                actual_diff = actual_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        if not actual_diff:
            actual_diff = diff_info.get("actual_diff", "")

        if actual_diff.strip():
            snippet = _smart_truncate_diff(actual_diff, max_chars=1500)
            summary += f"\n\n--- diff snippet ---\n```diff\n{snippet}\n```"

        return summary

    def get_handoff_summary(self, task_id: str) -> str:
        """Phase 3-E: 태스크 Handoff 파일의 Telegram 친화적 요약 반환."""
        handoff_path = self._handoffs_dir / f"{task_id}.md"
        if not handoff_path.exists():
            return (
                f"📄 {task_id} handoff 없음\n\n"
                f"/handoff {task_id} 로 생성하거나\n"
                f"/adopt {task_id} 로 현재 작업을 편입하세요."
            )
        try:
            content = handoff_path.read_text(encoding="utf-8", errors="replace")
            return _format_handoff_summary(task_id, content)
        except OSError as e:
            return f"❌ handoff 읽기 실패\n{e}"

    async def adopt_task(self, task_id: str) -> str:
        """Phase 3.5: 외부 작업을 PM Bot에 편입 — ADOPTED 상태로 보관.

        git status / diff 수집 후 _adopted_tasks에 저장.
        READY_TO_SHIP 직접 진입 금지.
        후속: /review T-ID (Review Agent) 또는 /resume T-ID (CLI 재실행).
        """
        if task_id in self._active_tasks:
            return (
                f"❌ {task_id}이(가) 이미 실행 중입니다.\n"
                f"- /running 으로 상태 확인"
            )
        if task_id in self._adopted_tasks:
            return (
                f"ℹ️ {task_id}은(는) 이미 ADOPTED 상태입니다.\n"
                f"- /review {task_id} 로 Review Agent 검토\n"
                f"- /resume {task_id} 로 이어서 작업"
            )

        # 현재 branch + 미커밋/미머지 diff 수집
        branch = await self._get_current_branch()
        diff_info = await self._collect_git_diff(branch)
        vs_main_files = await self._get_files_vs_main()
        all_changed = list(dict.fromkeys(
            diff_info.get("changed_files", []) + vs_main_files
        ))
        diff_info["changed_files"] = all_changed

        # _adopted_tasks에 보관 (아직 READY_TO_SHIP 아님)
        task_content = f"# {task_id}\n\nAdopted from branch: {branch or 'main'}"
        self._adopted_tasks[task_id] = {
            "task_id": task_id,
            "branch": branch,
            "task_content": task_content,
            "diff_info": diff_info,
            "adopted_at": datetime.now().isoformat(),
        }
        self._task_diffs[task_id] = diff_info
        self._save_diff_info(task_id, diff_info)
        self._processed_ids.add(task_id)  # 정상 task_queue 경로로 중복 실행 방지

        # Handoff 자동 업데이트 (ADOPTED)
        self._update_handoff(
            task_id,
            current_status=f"ADOPTED — branch: {branch or 'main'}",
            changed_files=all_changed or None,
            next_prompt=(
                f"/review {task_id} 로 Review Agent 검토\n"
                f"또는 /resume {task_id} 로 이어서 작업"
            ),
        )

        label = self._task_label(task_id)
        files_text = (
            "\n".join(f"- {f}" for f in all_changed[:10])
            or "변경 파일 없음"
        )
        if len(all_changed) > 10:
            files_text += f"\n... 외 {len(all_changed) - 10}개"

        return (
            f"📥 {label} 편입 완료\n\n"
            f"branch:\n{branch or '(확인 불가)'}\n\n"
            f"변경 파일:\n{files_text}\n\n"
            f"상태:\nADOPTED\n\n"
            f"다음:\n"
            f"- /review {task_id}\n"
            f"- /resume {task_id}\n"
            f"- /handoff {task_id}"
        )

    async def review_adopted_task(self, task_id: str) -> str:
        """Phase 3.5: ADOPTED 태스크를 Review Agent로 검토.

        PASS → _ready_to_ship (READY_TO_SHIP)
        FAIL → FAILED 카드 전송

        ADOPTED → REVIEWING → READY_TO_SHIP 또는 FAILED
        """
        if task_id not in self._adopted_tasks:
            if task_id in self._ready_to_ship:
                return (
                    f"ℹ️ {task_id}은(는) 이미 READY_TO_SHIP 상태입니다.\n"
                    f"/ship {task_id} 로 배포 승인하세요."
                )
            return (
                f"❌ {task_id}은(는) ADOPTED 상태가 아닙니다.\n"
                f"먼저 /adopt {task_id} 를 실행하세요.\n"
                f"현재 ADOPTED: {list(self._adopted_tasks.keys()) or '없음'}"
            )

        info = self._adopted_tasks[task_id]
        branch = info.get("branch", "")
        task_content = info.get("task_content", f"# {task_id}")
        label = self._task_label(task_id)

        # REVIEWING 알림
        await self._notify(
            f"🧪 {label} 리뷰 중 (Adopted)\n\n"
            f"git diff 기반 Review Agent 검토 중...\n\n"
            f"branch: {branch or '(확인 중)'}"
        )
        self._update_handoff(task_id, current_status="REVIEWING — Adopted task Review Agent 검토 중")

        # 최신 diff 재수집 (adopt 시점과 달라졌을 수 있음)
        diff_info = await self._collect_git_diff(branch)
        vs_main = await self._get_files_vs_main()
        all_changed = list(dict.fromkeys(diff_info.get("changed_files", []) + vs_main))
        diff_info["changed_files"] = all_changed
        self._task_diffs[task_id] = diff_info

        # CompletedPacket 구성
        packet = CompletedPacket(
            task_id=task_id,
            agent_id="external-work",
            files_changed=all_changed,
            code_diff="",
            test_result="(Adopted task 리뷰 — git diff 기반)",
            agent_notes=f"branch: {branch or 'main'}",
            timestamp=datetime.now().isoformat(),
            actual_diff=_smart_truncate_diff(diff_info.get("actual_diff", ""), max_chars=12000),
            git_status=diff_info.get("git_status", ""),
            diff_stat=diff_info.get("diff_stat", ""),
            diff_numstat=diff_info.get("diff_numstat", ""),
            name_status=diff_info.get("name_status_raw", ""),
            review_target="actual_git_diff",
        )

        # Review Agent 실행
        if self.review_agent is not None:
            spec_context = self._load_spec_context()
            verdict = await self.review_agent.review(
                spec_context=spec_context,
                completed_packet=packet,
            )
        else:
            verdict = ReviewVerdict(
                verdict="PASS",
                task_id=task_id,
                notes="review_agent 없음 — 자동 PASS",
            )

        print(f"[orchestrator] {task_id} (adopted) — verdict: {verdict.verdict}")

        if verdict.verdict == "PASS":
            # ADOPTED → READY_TO_SHIP
            commit_message = _build_commit_message(task_id, task_content)
            stored_path = self.task_queue_dir / f"adopted_{task_id}.md"

            self._ready_to_ship[task_id] = {
                "task_id": task_id,
                "task_path": stored_path,
                "task_content": task_content,
                "packet": packet,
                "commit_message": commit_message,
                "branch": branch,
                "ready_at": datetime.now().isoformat(),
                "diff_info": diff_info,
                "adopted": True,
            }
            self._adopted_tasks.pop(task_id, None)

            self._update_handoff(
                task_id,
                current_status="READY_TO_SHIP — 배포 승인 대기 중",
                done_item="Adopted task Review PASS",
                next_prompt=f"/ship {task_id} 로 배포 승인",
                changed_files=all_changed or None,
            )

            card_text = (
                f"✅ {label} 리뷰 통과 (Adopted)\n\n"
                f"main에는 아직 반영하지 않았습니다.\n"
                f"Ship 승인이 필요합니다.\n\n"
                f"명령:\n"
                f"- /ship {task_id}\n"
                f"- /diff {task_id}\n"
                f"- /log {task_id}"
            )
            if self.notify_card_fn is not None:
                try:
                    await self.notify_card_fn(card_text, task_id)
                except Exception as e:
                    print(f"[orchestrator] notify_card_fn 실패, fallback: {e}")
                    await self._notify(card_text)
            else:
                await self._notify(card_text)

            return (
                f"✅ {label} 리뷰 통과 → READY_TO_SHIP\n\n"
                f"/ship {task_id} 로 배포 승인하세요."
            )

        else:
            # FAIL
            reason = _short_reason(verdict)
            category = _classify_failure(0, verdict)
            action = _recommended_action(category)

            self._update_handoff(
                task_id,
                current_status=f"REVIEW FAILED: {category}",
                risks=reason,
                next_prompt=f"/resume {task_id} 로 수정 후 재시도",
            )

            # Phase 4: file_findings 표시
            _ff_text_adopted = ""
            if verdict.file_findings:
                _ff_str = _format_file_findings(verdict.file_findings)
                if _ff_str:
                    _ff_text_adopted = f"\n\n파일별 피드백:\n{_ff_str}"
            card_text = (
                f"❌ {label} 리뷰 실패 (Adopted)\n\n"
                f"분류:\n- {category}\n\n"
                f"요약:\n- {reason}\n\n"
                f"추천:\n- {action}"
                f"{_ff_text_adopted}"
            )
            if self.notify_failure_card_fn is not None:
                try:
                    await self.notify_failure_card_fn(card_text, task_id, no_retry=False)
                except Exception as e:
                    await self._notify(card_text)
            else:
                await self._notify(card_text)

            return f"❌ {label} 리뷰 실패\n{reason}"

    async def resume_task(self, task_id: str) -> str:
        """Phase 3-B: Handoff 파일 기반 태스크 재개.

        resume_{task_id}.md 를 task_queue에 생성 → watchdog이 감지해 실행.
        """
        if task_id in self._active_tasks:
            return (
                f"❌ {task_id}이(가) 이미 실행 중입니다.\n"
                f"- /running 으로 상태 확인"
            )

        handoff_path = self._handoffs_dir / f"{task_id}.md"
        if not handoff_path.exists():
            return (
                f"❌ {task_id} handoff 없음\n\n"
                f"먼저 /handoff {task_id} 로 생성하거나\n"
                f"/adopt {task_id} 로 현재 작업을 편입하세요."
            )

        try:
            content = handoff_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"❌ handoff 읽기 실패\n{e}"

        resume_content = _build_resume_prompt(task_id, content)

        resume_path = self.task_queue_dir / f"resume_{task_id}.md"
        try:
            resume_path.write_text(resume_content, encoding="utf-8")
        except OSError as e:
            return f"❌ resume 파일 생성 실패\n{e}"

        # 재처리 허용
        self._processed_ids.discard(task_id)
        self._failed_tasks.pop(task_id, None)
        self._ready_to_ship.pop(task_id, None)
        self._held_tasks.pop(task_id, None)  # Batch 6: HELD 해제

        label = self._task_label(task_id)
        return (
            f"▶ {label} 재개 요청됨\n\n"
            f"handoff 기반으로 이어서 실행합니다.\n\n"
            f"진행 상황:\n"
            f"- /running\n"
            f"- /log {task_id}"
        )

    async def ship_task(self, task_id: str) -> str:
        """Phase 1: READY_TO_SHIP 태스크를 직접 머지/푸시.

        Returns: 결과 메시지 문자열
        """
        # Phase 3.5: ADOPTED 상태는 /review 먼저 필요
        if task_id in self._adopted_tasks:
            return (
                f"❌ {task_id}은(는) ADOPTED 상태입니다.\n\n"
                f"READY_TO_SHIP 직접 진입 금지.\n"
                f"먼저 Review를 통과해야 합니다:\n"
                f"- /review {task_id}\n"
                f"- /resume {task_id}"
            )

        # Batch 5: 이미 SHIPPED인 태스크 체크 (오래된 버튼 클릭 방지)
        if task_id not in self._ready_to_ship:
            if self.git_manager is not None:
                completed = self.git_manager.get_state().get("completed_tasks", [])
                if any(t.get("task_id") == task_id for t in completed):
                    return (
                        f"ℹ️ {task_id}은(는) 이미 배포 완료되었습니다.\n\n"
                        f"/status 로 확인하세요."
                    )
            rts_ids = list(self._ready_to_ship.keys()) or ["없음"]
            return (
                f"❌ {task_id}은(는) READY_TO_SHIP 상태가 아닙니다.\n"
                f"현재 READY_TO_SHIP: {rts_ids}"
            )

        info = self._ready_to_ship.pop(task_id)
        await self._execute_ship(
            task_id=task_id,
            task_path=info["task_path"],
            task_content=info["task_content"],
            packet=info["packet"],
            commit_message=info["commit_message"],
        )
        return f"🚀 {task_id} 배포 승인됨. 처리 중..."

    def hold_task(self, task_id: str) -> str:
        """Phase 2: 태스크를 HELD 상태로 전환. main 머지 없이 branch 유지.

        Returns: 결과 메시지 문자열
        """
        if task_id not in self._ready_to_ship:
            # Batch 5: 이미 배포 완료된 경우 안내
            if self.git_manager is not None:
                completed = self.git_manager.get_state().get("completed_tasks", [])
                if any(t.get("task_id") == task_id for t in completed):
                    return (
                        f"ℹ️ {task_id}은(는) 이미 배포 완료되어 보류 불가합니다.\n"
                        f"/status 로 확인하세요."
                    )
            return (
                f"❌ {task_id}은(는) READY_TO_SHIP 상태가 아닙니다.\n"
                f"현재 READY_TO_SHIP: {list(self._ready_to_ship.keys()) or '없음'}"
            )

        info = self._ready_to_ship.pop(task_id)
        branch = info.get("branch", "")

        # git 처리 락만 해제 (브랜치는 삭제하지 않음)
        if self.git_manager is not None:
            try:
                self.git_manager.release_lock(task_id)
            except Exception:
                pass

        # running_task 초기화
        if self._running_task.get("task_id") == task_id:
            self._running_task = {}

        # Batch 6: HELD 태스크 추적 (stale 감지용)
        self._held_tasks[task_id] = {
            "task_id": task_id,
            "branch": branch,
            "held_at": datetime.now().isoformat(),
        }
        # 통계 기록
        self._stats.record(task_id, "HELD", self._current_project_id)

        branch_str = f"\nbranch: {branch}" if branch else ""
        # Phase 3-C: Handoff 자동 업데이트 (HELD)
        self._update_handoff(
            task_id,
            current_status=f"HELD — branch 유지 중{branch_str}",
            next_prompt=f"/resume {task_id} 로 이어서 진행",
        )
        return (
            f"⏸️ {task_id} 보류 처리됨\n\n"
            f"branch는 유지됩니다.{branch_str}\n\n"
            f"나중에 다시 처리하려면:\n- /resume {task_id}"
        )

    # ── Phase 3 public methods ───────────────────────────────────────────

    async def retry_task(self, task_id: str) -> str:
        """Phase 3: 실패한 태스크를 실패 context 포함하여 재시도.

        task_queue/retry_{task_id}.md 파일을 생성해 watchdog이 감지하도록 한다.
        Returns: 결과 메시지 문자열
        """
        _MAX_MANUAL_RETRIES = 3

        if task_id not in self._failed_tasks:
            return (
                f"❌ {task_id} 실패 기록 없음\n"
                f"현재 실패 기록: {list(self._failed_tasks.keys()) or '없음'}"
            )

        retry_count = self._manual_retry_counts.get(task_id, 0) + 1
        if retry_count > _MAX_MANUAL_RETRIES:
            return (
                f"❌ {task_id} 수동 재시도 한도({_MAX_MANUAL_RETRIES}회) 초과\n"
                f"태스크 내용을 수정한 뒤 새 태스크로 등록해주세요."
            )

        info = self._failed_tasks[task_id]
        original_content = info.get("task_content", "")
        category = info.get("failure_category", "UNKNOWN")
        reason = info.get("short_reason", "알 수 없는 오류")

        # 최근 로그 20줄
        tail_log = self.get_task_log(task_id, lines=20) or "(로그 없음)"

        # 실패 context를 원본 태스크에 추가
        failure_context = (
            f"\n\n---\n"
            f"[이전 시도 실패 — retry {retry_count}/{_MAX_MANUAL_RETRIES}]\n\n"
            f"Failure category:\n{category}\n\n"
            f"Failure summary:\n{reason}\n\n"
            f"Recent logs:\n{tail_log}\n\n"
            f"Please fix the issue without expanding the task scope.\n"
        )
        modified_content = original_content + failure_context

        # retry_{task_id}.md 파일 생성 → watchdog이 감지
        retry_path = self.task_queue_dir / f"retry_{task_id}.md"
        try:
            retry_path.write_text(modified_content, encoding="utf-8")
        except OSError as e:
            return f"❌ 재시도 파일 생성 실패\n{e}"

        # processed_ids에서 제거 → 재처리 허용
        self._processed_ids.discard(task_id)
        self._manual_retry_counts[task_id] = retry_count
        # 실패 기록 제거 (새 시도에서 다시 기록됨)
        self._failed_tasks.pop(task_id, None)

        return (
            f"🔁 {task_id} 재시도 큐 등록됨 (수동 retry {retry_count}/{_MAX_MANUAL_RETRIES})\n\n"
            f"실패 context 포함하여 재실행합니다.\n"
            f"진행 상황:\n- /running\n- /log {task_id}"
        )

    def cancel_task(self, task_id: str) -> str:
        """Phase 3: 태스크 취소.

        QUEUED: .md 파일을 .cancelled.md로 이름 변경.
        RUNNING: cancel_requested 플래그 + subprocess terminate 시도.
        Returns: 결과 메시지 문자열
        """
        # QUEUED 상태 — task_queue/*.md 파일 존재 확인
        for md_file in self.task_queue_dir.glob("*.md"):
            if _parse_task_id(md_file) == task_id:
                try:
                    cancelled_path = md_file.with_suffix(".cancelled.md")
                    md_file.rename(cancelled_path)
                    self._processed_ids.add(task_id)  # 재처리 방지
                    return (
                        f"🛑 {task_id} 취소됨 (대기 → 취소)\n\n"
                        f"파일: {md_file.name} → {cancelled_path.name}"
                    )
                except OSError as e:
                    return f"❌ 파일 취소 실패\n{e}"

        # RUNNING 상태 — cancel 플래그 설정 + subprocess 종료 시도
        if self._running_task.get("task_id") == task_id:
            self._cancel_requested.add(task_id)
            proc_killed = False
            if self._current_proc is not None:
                try:
                    self._current_proc.terminate()
                    proc_killed = True
                except Exception:
                    pass
            proc_msg = "프로세스 종료 신호 전송됨" if proc_killed else "종료 신호 전송 불가 (이미 종료?)"
            return (
                f"🛑 {task_id} 중단 요청됨\n\n"
                f"{proc_msg}\n"
                f"현재 단계 완료 후 중단됩니다."
            )

        # READY_TO_SHIP 상태에서 취소 → hold_task와 동일 처리
        if task_id in self._ready_to_ship:
            return self.hold_task(task_id)

        return (
            f"❌ {task_id}은(는) 취소할 수 있는 상태가 아닙니다.\n"
            f"현재 실행 중: {self._running_task.get('task_id', '없음')}"
        )

    def hold_branch(self, task_id: str) -> str:
        """Phase 3: 실패한 태스크의 branch/log를 유지하고 HELD 처리.

        Returns: 결과 메시지 문자열
        """
        if task_id not in self._failed_tasks:
            return (
                f"❌ {task_id} 실패 기록 없음\n"
                f"현재 실패 기록: {list(self._failed_tasks.keys()) or '없음'}"
            )

        info = self._failed_tasks.pop(task_id)
        branch = info.get("branch", "")

        # processed_ids에 추가 — 자동 재실행 방지
        self._processed_ids.add(task_id)

        branch_str = f"\nbranch: {branch}" if branch else ""
        log_path = self._log_dir / f"{task_id}.combined.log"
        log_str = f"\n로그: {log_path}" if log_path.exists() else ""

        return (
            f"📌 {task_id} branch 유지됨\n\n"
            f"branch와 로그는 보존됩니다.{branch_str}{log_str}\n\n"
            f"나중에 재실행하려면 새 태스크로 등록해주세요."
        )

    # ── Task processing ──────────────────────────────────────────────────

    async def _process_task(self, task_path: Path) -> None:
        """단일 태스크 파일을 처리."""
        if not task_path.exists():
            return

        task_id = _parse_task_id(task_path)

        if task_id in self._processed_ids:
            print(f"[orchestrator] {task_id} — 이미 처리됨, skip")
            return

        self._processed_ids.add(task_id)

        print(f"[orchestrator] {task_id} 처리 시작: {task_path.name}")

        try:
            task_content = task_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[orchestrator] {task_id} — 파일 읽기 실패: {e}")
            self._processed_ids.discard(task_id)
            return

        allowed_files = _parse_allowed_files(task_content)
        task_title = _extract_task_title(task_content)

        # Phase 0: 실행 추적 초기화
        self._running_task = {
            "task_id": task_id,
            "started_at": datetime.now(),
            "branch": "",
            "phase": "QUEUED",
            "log_dir": str(self._log_dir),
            "task_title": task_title,   # Phase 3-C: handoff 자동 업데이트용
        }

        try:
            await self._process_task_inner(
                task_id, task_path, task_content, task_title, allowed_files
            )
        finally:
            # 완료 후 running_task 초기화 (READY_TO_SHIP은 별도 dict로 유지)
            if self._running_task.get("task_id") == task_id:
                phase = self._running_task.get("phase", "")
                if phase not in ("READY_TO_SHIP",):
                    self._running_task = {}

    async def _process_task_inner(
        self,
        task_id: str,
        task_path: Path,
        task_content: str,
        task_title: str,
        allowed_files: list[str],
    ) -> None:
        """실제 태스크 실행 로직."""
        # Batch 6: 시작 시각 기록 (통계용)
        self._task_started_at[task_id] = datetime.now()

        # Phase 1: QUEUED 알림
        label = self._task_label(task_id)
        await self._notify(
            f"📋 {label} 대기열 등록\n\n"
            f"목표:\n- {task_title}\n\n"
            f"로그:\n- /log {task_id}"
        )

        # CLI 실행 전 stale worktree 정리
        self._prune_worktrees()

        # 브랜치 생성
        branch = ""
        if self.git_manager is not None:
            try:
                branch = self.git_manager.create_branch(task_id, allowed_files)
                print(f"[orchestrator] {task_id} — 브랜치 생성: {branch}")
                self._running_task["branch"] = branch
            except RuntimeError as e:
                msg = f"[orchestrator] {task_id} — 브랜치 생성 실패: {e}"
                print(msg)
                await self._notify(f"❌ {task_id} 브랜치 생성 실패\n{e}")
                self._processed_ids.discard(task_id)
                self._running_task = {}
                return

        # Phase 1: RUNNING 알림
        self._running_task["phase"] = "RUNNING"
        label = self._task_label(task_id)
        await self._notify(
            f"⏳ {label} 진행 중\n\n"
            f"현재 단계:\n- Claude Code CLI 실행 중\n\n"
            f"로그:\n- /log {task_id}\n\n"
            f"상태:\n- /running"
        )
        # Phase 3-C: Handoff 자동 업데이트 (RUNNING)
        self._update_handoff(task_id, current_status="RUNNING — Claude Code CLI 실행 중")

        # Claude Code CLI 실행 (최대 MAX_RETRIES)
        last_verdict: ReviewVerdict | None = None
        last_exit_code: int | None = None
        succeeded = False

        for attempt in range(1, MAX_RETRIES + 1):
            # cancel 요청 확인
            if task_id in self._cancel_requested:
                self._cancel_requested.discard(task_id)
                await self._notify(f"🛑 {task_id} 중단됨 (cancel 요청)")
                return

            print(f"[orchestrator] {task_id} — CLI 실행 attempt {attempt}/{MAX_RETRIES}")

            stdout_text, exit_code = await self._run_cli(task_content, task_id)
            last_exit_code = exit_code

            if exit_code not in (0, None) and not stdout_text.strip():
                reason = f"CLI 종료 코드 {exit_code}, 출력 없음"
                print(f"[orchestrator] {task_id} attempt {attempt} — {reason}")
                if attempt < MAX_RETRIES:
                    await self._notify(
                        f"⚠️ {task_id} 실패 (attempt {attempt}/{MAX_RETRIES})\n"
                        f"원인: {reason}\n재시도 중..."
                    )
                    await asyncio.sleep(2)
                continue

            # Phase 4: CLI 실행 후 실제 git diff 수집
            branch = self._running_task.get("branch", "")
            diff_info = await self._collect_git_diff(branch)

            # git diff 기반 파일 목록 우선, fallback → stdout 파싱
            git_changed_files = diff_info.get("changed_files", [])
            files_changed = git_changed_files or _parse_files_changed(stdout_text, allowed_files)

            # diff 정보 저장 (/diff T-ID 용)
            self._task_diffs[task_id] = diff_info
            # 로그 디렉토리에도 영속화
            self._save_diff_info(task_id, diff_info)

            packet = CompletedPacket(
                task_id=task_id,
                agent_id="claude-code-cli",
                files_changed=files_changed,
                code_diff=stdout_text[:8000],
                test_result="(CLI stdout — 별도 테스트 없음)",
                agent_notes=stdout_text[-2000:] if len(stdout_text) > 2000 else "",
                timestamp=datetime.now().isoformat(),
                # Phase 4 필드 — smart truncate로 파일별 균등 보존
                actual_diff=_smart_truncate_diff(diff_info.get("actual_diff", ""), max_chars=12000),
                git_status=diff_info.get("git_status", ""),
                diff_stat=diff_info.get("diff_stat", ""),
                diff_numstat=diff_info.get("diff_numstat", ""),
                name_status=diff_info.get("name_status_raw", ""),
                review_target="actual_git_diff",
            )

            # Phase 1: REVIEWING 알림
            self._running_task["phase"] = "REVIEWING"
            label = self._task_label(task_id)
            await self._notify(
                f"🧪 {label} 리뷰 중\n\n"
                f"구현은 완료되었습니다.\n"
                f"Review Agent 검토를 진행합니다.\n\n"
                f"로그:\n- /log {task_id}"
            )
            # Phase 3-C: Handoff 자동 업데이트 (REVIEWING)
            changed_files = self._task_diffs.get(task_id, {}).get("changed_files", [])
            self._update_handoff(
                task_id,
                current_status="REVIEWING — Review Agent 검토 중",
                changed_files=changed_files or None,
            )

            # ReviewAgent 검토 (RC Hotfix 1: timeout 보호)
            _review_timeout = getattr(self.config, "review_timeout_seconds", 120)
            if self.review_agent is not None:
                spec_context = self._load_spec_context()
                try:
                    verdict = await asyncio.wait_for(
                        self.review_agent.review(
                            spec_context=spec_context,
                            completed_packet=packet,
                            allowed_files=allowed_files if allowed_files else None,
                        ),
                        timeout=float(_review_timeout),
                    )
                except asyncio.TimeoutError:
                    print(
                        f"[orchestrator] {task_id} — Review Agent {_review_timeout}초 타임아웃"
                    )
                    from schemas import ReviewVerdict as RV, Violation as V
                    verdict = RV(
                        verdict="FAIL",
                        task_id=task_id,
                        violations=[V(
                            rule="review.timeout",
                            description=(
                                f"Review Agent가 {_review_timeout}초 내 응답하지 않았습니다. "
                                "API 네트워크 상태를 확인하세요."
                            ),
                            severity="ERROR",
                        )],
                        notes=f"REVIEW_TIMEOUT after {_review_timeout}s",
                    )
            else:
                from schemas import ReviewVerdict as RV
                verdict = RV(verdict="PASS", task_id=task_id, notes="review_agent 없음 — 자동 PASS")

            last_verdict = verdict
            print(f"[orchestrator] {task_id} attempt {attempt} — verdict: {verdict.verdict}")

            if verdict.verdict == "PASS":
                succeeded = True
                break

            # 구조적 실패 또는 타임아웃 — 재시도하지 않음
            _NO_RETRY_RULES = frozenset({
                "scope.files_outside_task",
                "scope.sensitive_file_changed",
                "scope.file_deleted",
                "correctness.no_changes",
                "review.timeout",          # RC Hotfix 1: 타임아웃 재시도 금지
            })
            violated_rules = {getattr(v, "rule", "") for v in verdict.violations}
            if violated_rules & _NO_RETRY_RULES:
                print(f"[orchestrator] {task_id} — 재시도 불가 실패: {violated_rules & _NO_RETRY_RULES}")
                break

            # FAIL / NEEDS_REVISION — 일시적 실패, retry 가능
            reason = _short_reason(verdict)
            if attempt < MAX_RETRIES:
                self._running_task["phase"] = "RUNNING"
                await self._notify(
                    f"⚠️ {task_id} 실패 (attempt {attempt}/{MAX_RETRIES})\n"
                    f"원인: {reason}\n재시도 중..."
                )
                await asyncio.sleep(2)

        # 최종 처리
        if succeeded and last_verdict is not None:
            await self._on_pass(task_id, task_path, task_content, packet, verdict=last_verdict)
        else:
            await self._on_fail(
                task_id=task_id,
                task_path=task_path,
                verdict=last_verdict,
                task_content=task_content,
                allowed_files=allowed_files,
                last_exit_code=last_exit_code,
            )

    async def _run_cli(self, task_content: str, task_id: str) -> tuple[str, int | None]:
        """Phase 0: Claude Code CLI subprocess 실행 + 로그 파일 스트리밍.

        stdout → stdout.log + combined.log + in-memory buffer (Review Agent용)
        stderr → stderr.log + combined.log
        Returns: (stdout_text, exit_code)
        """
        cfg = self.config

        # frontmatter(--- ... ---) 를 제거한 순수 본문만 CLI에 전달.
        # "---" 로 시작하는 내용을 그대로 넘기면 claude CLI가 옵션 플래그로 해석해 crash.
        cli_prompt = task_content
        if cli_prompt.startswith("---"):
            end = cli_prompt.find("\n---", 3)
            if end != -1:
                cli_prompt = cli_prompt[end + 4:].lstrip()

        cmd = [
            "claude",
            "--print",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Bash",
            "--model", cfg.cli_model,
            cli_prompt,
        ]

        stdout_log_path = self._log_dir / f"{task_id}.stdout.log"
        stderr_log_path = self._log_dir / f"{task_id}.stderr.log"
        combined_log_path = self._log_dir / f"{task_id}.combined.log"

        stdout_buffer: list[str] = []

        # Claude Code CLI subprocess 환경: ANTHROPIC_API_KEY를 제거해
        # CLI가 API 과금 대신 OAuth(구독) 인증을 사용하도록 강제.
        # ReviewAgent/PMAgent는 별도 AsyncAnthropic 클라이언트로 API key 사용.
        import os as _os
        cli_env = {k: v for k, v in _os.environ.items() if k != "ANTHROPIC_API_KEY"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cfg.repo_path),
                env=cli_env,
            )
            self._current_proc = proc  # Phase 3: cancel용 참조 저장

            with (
                open(stdout_log_path, "w", encoding="utf-8") as f_out,
                open(stderr_log_path, "w", encoding="utf-8") as f_err,
                open(combined_log_path, "w", encoding="utf-8") as f_comb,
            ):
                async def _drain_stdout(stream: asyncio.StreamReader) -> None:
                    async for line in stream:
                        decoded = line.decode("utf-8", errors="replace")
                        stdout_buffer.append(decoded)
                        f_out.write(decoded)
                        f_out.flush()
                        f_comb.write(decoded)
                        f_comb.flush()

                async def _drain_stderr(stream: asyncio.StreamReader) -> None:
                    async for line in stream:
                        decoded = line.decode("utf-8", errors="replace")
                        f_err.write(decoded)
                        f_err.flush()
                        f_comb.write(decoded)
                        f_comb.flush()

                await asyncio.wait_for(
                    asyncio.gather(
                        _drain_stdout(proc.stdout),
                        _drain_stderr(proc.stderr),
                        proc.wait(),
                    ),
                    timeout=CLI_TIMEOUT_S,
                )

            stdout_text = "".join(stdout_buffer)
            self._current_proc = None
            return stdout_text, proc.returncode

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            print(f"[orchestrator] CLI 타임아웃 ({CLI_TIMEOUT_S}s)")
            # 타임아웃도 로그에 기록
            try:
                with open(combined_log_path, "a", encoding="utf-8") as f_comb:
                    f_comb.write(f"\n[TIMEOUT] CLI 타임아웃 ({CLI_TIMEOUT_S}s)\n")
            except Exception:
                pass
            self._current_proc = None
            return "".join(stdout_buffer), 124

        except FileNotFoundError:
            print("[orchestrator] 'claude' CLI를 찾을 수 없습니다. PATH를 확인하세요.")
            return "", -1

        except Exception as e:
            print(f"[orchestrator] CLI 실행 오류: {e}")
            self._current_proc = None
            return "", -1

    async def _on_pass(
        self,
        task_id: str,
        task_path: Path,
        task_content: str,
        packet: CompletedPacket,
        verdict: ReviewVerdict | None = None,
    ) -> None:
        """PASS 판정 처리."""
        commit_message = _build_commit_message(task_id, task_content)

        # Phase 1: AUTO_SHIP_AFTER_REVIEW 게이트
        if not self.config.auto_ship_after_review:
            branch = self._running_task.get("branch", "")
            if not branch and self.git_manager is not None:
                state = self.git_manager.get_state()
                branch = state.get("active_tasks", {}).get(task_id, {}).get("branch", "")

            # READY_TO_SHIP 상태 저장
            ready_dir = self.task_queue_dir / "ready"
            ready_dir.mkdir(exist_ok=True)
            try:
                ready_path = ready_dir / task_path.name
                task_path.rename(ready_path)
                stored_path = ready_path
            except OSError:
                stored_path = task_path

            self._ready_to_ship[task_id] = {
                "task_id": task_id,
                "task_path": stored_path,
                "task_content": task_content,
                "packet": packet,
                "commit_message": commit_message,
                "branch": branch,
                "ready_at": datetime.now().isoformat(),
                "diff_info": self._task_diffs.get(task_id, {}),  # Phase 4
            }
            self._running_task["phase"] = "READY_TO_SHIP"

            label = self._task_label(task_id)
            # Phase 4: PASS라도 INFO/WARN file_findings가 있으면 advisory로 표시
            _advisory = ""
            if verdict is not None and verdict.file_findings:
                _info_findings = [f for f in verdict.file_findings if f.severity in ("INFO", "WARN")]
                if _info_findings:
                    _ff_str = _format_file_findings(_info_findings, max_items=3)
                    _advisory = f"\n\n💬 리뷰 제안:\n{_ff_str}"
            card_text = (
                f"✅ {label} 리뷰 통과\n\n"
                f"main에는 아직 반영하지 않았습니다.\n"
                f"Ship 승인이 필요합니다."
                f"{_advisory}\n\n"
                f"명령:\n"
                f"- /ship {task_id}\n"
                f"- /diff {task_id}\n"
                f"- /log {task_id}"
            )
            # Phase 3-C: Handoff 자동 업데이트 (READY_TO_SHIP)
            _rts_files = self._task_diffs.get(task_id, {}).get("changed_files") or None
            self._update_handoff(
                task_id,
                current_status="READY_TO_SHIP — 배포 승인 대기 중",
                done_item="Review PASS",
                next_prompt=f"/ship {task_id} 로 배포 승인",
                changed_files=_rts_files,
            )

            if self.notify_card_fn is not None:
                try:
                    await self.notify_card_fn(card_text, task_id)
                except Exception as e:
                    print(f"[orchestrator] notify_card_fn 실패, fallback: {e}")
                    await self._notify(card_text)
            else:
                await self._notify(card_text)
            print(f"[orchestrator] {task_id} — READY_TO_SHIP (AUTO_SHIP=false)")
            return

        # AUTO_SHIP: 즉시 머지
        await self._execute_ship(task_id, task_path, task_content, packet, commit_message)

    async def _execute_ship(
        self,
        task_id: str,
        task_path: Path,
        task_content: str,
        packet: CompletedPacket,
        commit_message: str,
    ) -> None:
        """실제 main 머지 + 푸시 실행."""
        pr_url: str | None = None
        result: dict = {}

        if self.git_manager is not None:
            result = self.git_manager.commit_and_merge(task_id, commit_message)
            pr_url = result.get("pr_url")
            if not result.get("ok"):
                err = result.get("error", "알 수 없는 오류")
                print(f"[orchestrator] {task_id} — commit_and_merge 실패: {err}")
                await self._notify(f"❌ {task_id} 커밋/머지 실패\n{err}")
                return

        self._save_completed(task_id, packet, pr_url)

        try:
            done_path = task_path.with_suffix(".done.md")
            task_path.rename(done_path)
        except OSError:
            pass

        commit_sha = result.get("commit_sha", "") if self.git_manager else ""
        sha_line = f"\ncommit: {commit_sha}" if commit_sha else ""
        label = self._task_label(task_id)
        await self._notify(
            f"🚀 {label} 배포 완료\n\nmain merge/push 완료{sha_line}"
        )
        # Phase 3-C: Handoff 자동 업데이트 (SHIPPED)
        self._update_handoff(
            task_id,
            current_status=f"SHIPPED{sha_line}",
            done_item=f"main merge/push 완료{sha_line}",
            next_prompt="배포 완료 — 추가 작업 없음",
        )
        print(f"[orchestrator] {task_id} — 배포 완료.")

        # Batch 6: 통계 기록 + 자동 archive
        started_at = self._task_started_at.pop(task_id, None)
        elapsed_s = (
            (datetime.now() - started_at).total_seconds()
            if isinstance(started_at, datetime) else 0.0
        )
        retry_count = self._manual_retry_counts.get(task_id, 0)
        self._stats.record(
            task_id, "SHIPPED", self._current_project_id,
            elapsed_s=elapsed_s, retry_count=retry_count,
        )
        try:
            self._archive_mgr.archive_task(
                task_id, "SHIPPED",
                metadata={"commit_sha": commit_sha, "elapsed_s": elapsed_s},
            )
        except Exception as exc:
            print(f"[orchestrator] archive 실패 (무시): {exc}")

        # running_task 초기화
        if self._running_task.get("task_id") == task_id:
            self._running_task = {}

    async def _on_fail(
        self,
        task_id: str,
        task_path: Path,
        verdict: ReviewVerdict | None,
        task_content: str = "",
        allowed_files: list[str] | None = None,
        last_exit_code: int | None = None,
    ) -> None:
        """Phase 3: 최종 실패 처리 — 실패 카드 + inline 버튼 전송."""
        # git lock 해제 (branch는 유지)
        if self.git_manager is not None:
            try:
                self.git_manager.release_lock(task_id)
            except Exception:
                pass

        # 실패 분류
        category = _classify_failure(last_exit_code, verdict)
        reason = _short_reason(verdict) if verdict else (
            "CLI 타임아웃" if last_exit_code == 124
            else "CLI 실행 실패" if last_exit_code == -1
            else "알 수 없는 오류"
        )
        action = _recommended_action(category)
        branch = self._running_task.get("branch", "")

        print(f"[orchestrator] {task_id} — 최종 실패: [{category}] {reason}")

        # 실패 정보 저장 (retry/hold_branch용)
        self._failed_tasks[task_id] = {
            "task_id": task_id,
            "task_path": task_path,
            "task_content": task_content,
            "allowed_files": allowed_files or [],
            "branch": branch,
            "failure_category": category,
            "short_reason": reason,
            "failed_at": datetime.now().isoformat(),
        }

        # 재시작 안전: 실패한 .md 파일을 .failed.md 로 rename → 재시작 시 재실행 방지
        # _on_fail()은 모든 실패 카테고리(CLI_ERROR / TEST_FAILED / SCOPE_VIOLATION /
        # REVIEW_FAILED / REVIEW_TIMEOUT / GIT_FAILED / TIMEOUT / UNKNOWN)의 단일 종착점.
        if task_path and task_path.exists():
            _failed_path = mark_queue_file_status(task_path, "failed")
            self._failed_tasks[task_id]["task_path"] = _failed_path

        # 구조적 실패 여부 판단 (retry 불가 유형)
        _NO_RETRY_RULES = frozenset({
            "scope.files_outside_task",
            "scope.sensitive_file_changed",
            "scope.file_deleted",
            "correctness.no_changes",
        })
        violated_rules = {getattr(v, "rule", "") for v in (verdict.violations if verdict else [])}
        # REVIEW_TIMEOUT은 구조적이지 않음 — /resume 으로 재시도 가능
        is_structural = bool(violated_rules & _NO_RETRY_RULES)

        # 실패 카드 텍스트
        structural_note = (
            "\n⚠️ 구조적 차단 — 자동 재시도하지 않음\n태스크 범위를 수정해 새 태스크로 요청하세요."
            if is_structural else ""
        )
        label = self._task_label(task_id)
        # Phase 3-D: handoff 존재 여부 확인
        _hf_path = self._handoffs_dir / f"{task_id}.md"
        _hf_note = f"\n\nhandoff:\n{task_id}.md 존재" if _hf_path.exists() else ""
        # Phase 4: file_findings 표시
        _ff_text = ""
        if verdict is not None and verdict.file_findings:
            _ff_str = _format_file_findings(verdict.file_findings)
            if _ff_str:
                _ff_text = f"\n\n파일별 피드백:\n{_ff_str}"
        # RC Hotfix 1: REVIEW_TIMEOUT 전용 카드 형식
        _timeout_next = (
            f"\n\n다음:\n- /resume {task_id}\n- /log {task_id}\n- /handoff {task_id}"
            if category == "REVIEW_TIMEOUT" else ""
        )
        card_text = (
            f"❌ {label} 실패\n\n"
            f"분류:\n- {category}\n\n"
            f"요약:\n- {reason}\n\n"
            f"추천:\n- {action}"
            f"{_ff_text}"
            f"{_timeout_next}"
            f"{structural_note}"
            f"{_hf_note}"
        )

        # Phase 3-C: Handoff 자동 업데이트 (FAILED)
        self._update_handoff(
            task_id,
            current_status=f"FAILED: {category}",
            risks=reason,
            next_prompt=(
                f"/resume {task_id} 로 이어서 진행\n"
                f"또는 /adopt {task_id} 로 현재 상태 편입"
            ),
        )

        # Phase 3: notify_failure_card_fn 우선, fallback → _notify
        if self.notify_failure_card_fn is not None:
            try:
                await self.notify_failure_card_fn(card_text, task_id, no_retry=is_structural)
            except Exception as e:
                print(f"[orchestrator] notify_failure_card_fn 실패, fallback: {e}")
                await self._notify(card_text)
        else:
            await self._notify(card_text)

        # Batch 6: 통계 기록
        started_at = self._task_started_at.pop(task_id, None)
        elapsed_s = (
            (datetime.now() - started_at).total_seconds()
            if isinstance(started_at, datetime) else 0.0
        )
        retry_count = self._manual_retry_counts.get(task_id, 0)
        self._stats.record(
            task_id, "FAILED", self._current_project_id,
            elapsed_s=elapsed_s, retry_count=retry_count,
        )

        # processed_ids 제거 → 수동 수정 후 재처리 가능
        self._processed_ids.discard(task_id)

        if self._running_task.get("task_id") == task_id:
            self._running_task = {}

    # ── Phase 4: Git diff 수집 / 저장 / 포맷 ──────────────────────────

    async def _collect_git_diff(self, branch: str) -> dict:
        """Phase 4: Claude CLI 실행 후 실제 git diff 수집.

        수집 전략:
        1. git diff main..HEAD — 브랜치에서 커밋된 변경 (adopt/commit workflow)
        2. git diff HEAD — 미커밋 unstaged 변경
        3. git diff --cached HEAD — 미커밋 staged 변경
        세 가지를 합산하여 완전한 변경 그림을 제공.
        """
        repo = str(self.config.repo_path)

        async def _git(*args: str) -> str:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", repo, *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                return out.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[orchestrator] git {' '.join(args)} 실패: {exc}")
                return ""

        # 1. main..HEAD: 브랜치에서 커밋된 변경 (commit 기반 workflow 포착)
        diff_vs_main = await _git("diff", "main..HEAD")

        # 2. HEAD: 미커밋 unstaged 변경 (Claude Code CLI가 파일 수정만 한 경우)
        diff_unstaged = await _git("diff", "HEAD")

        # 3. --cached HEAD: 스테이지된 변경
        diff_staged = await _git("diff", "--cached", "HEAD")

        # 합산: main..HEAD 먼저 (커밋 기반 우선), 이후 uncommitted
        _parts = [p.strip() for p in [diff_vs_main, diff_staged, diff_unstaged] if p.strip()]
        actual_diff = "\n".join(_parts)

        # 통계
        stat_vs_main = await _git("diff", "--stat", "main..HEAD")
        stat_unstaged = await _git("diff", "--stat", "HEAD")
        stat_staged = await _git("diff", "--stat", "--cached", "HEAD")
        _stat_parts = [p.strip() for p in [stat_vs_main, stat_staged, stat_unstaged] if p.strip()]
        diff_stat = "\n".join(_stat_parts)

        # numstat — per-file +/-
        num_vs_main = await _git("diff", "--numstat", "main..HEAD")
        num_unstaged = await _git("diff", "--numstat", "HEAD")
        num_staged = await _git("diff", "--numstat", "--cached", "HEAD")
        _num_parts = [p.strip() for p in [num_vs_main, num_staged, num_unstaged] if p.strip()]
        diff_numstat = "\n".join(_num_parts)

        # name-status (M/A/D per file)
        ns_vs_main = await _git("diff", "--name-status", "main..HEAD")
        ns_unstaged = await _git("diff", "--name-status", "HEAD")
        ns_staged = await _git("diff", "--name-status", "--cached", "HEAD")
        _ns_parts = [p.strip() for p in [ns_vs_main, ns_staged, ns_unstaged] if p.strip()]
        name_status_raw = "\n".join(_ns_parts)

        # git status --short (untracked 포함 전체 상태)
        git_status = await _git("status", "--short")

        # changed_files 파싱 (tracked M/A/D + untracked ??)
        changed_files: list[str] = []
        seen: set[str] = set()

        for line in name_status_raw.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                fname = parts[1].strip()
                if fname and fname not in seen:
                    changed_files.append(fname)
                    seen.add(fname)

        for line in git_status.splitlines():
            if line.startswith("??"):
                fname = line[3:].strip().rstrip("/")
                if fname and fname not in seen:
                    changed_files.append(fname)
                    seen.add(fname)

        return {
            "actual_diff": actual_diff,
            "diff_stat": diff_stat,
            "diff_numstat": diff_numstat,
            "name_status_raw": name_status_raw,
            "changed_files": changed_files,
            "git_status": git_status,
            "branch": branch,
        }

    def _save_diff_info(self, task_id: str, diff_info: dict) -> None:
        """diff 정보를 logs/tasks/{task_id}.diff 에 영속화."""
        import json as _json
        diff_file = self._log_dir / f"{task_id}.diff"
        try:
            # actual_diff는 별도 .actual.diff 파일에 저장 (크기 절감)
            actual_diff = diff_info.get("actual_diff", "")
            actual_file = self._log_dir / f"{task_id}.actual.diff"
            if actual_diff:
                actual_file.write_text(actual_diff, encoding="utf-8")

            # JSON에는 actual_diff 제외
            saveable = {k: v for k, v in diff_info.items() if k != "actual_diff"}
            diff_file.write_text(
                _json.dumps(saveable, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[orchestrator] diff 저장 실패 {task_id}: {e}")

    def _prune_worktrees(self) -> None:
        """stale git worktrees 정리."""
        try:
            import subprocess as _sp
            _sp.run(
                ["git", "-C", str(self.config.repo_path), "worktree", "prune"],
                capture_output=True, check=False,
            )
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _get_current_branch(self) -> str:
        """현재 git branch 이름 반환."""
        repo = str(self.config.repo_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return out.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    async def _get_files_vs_main(self) -> list[str]:
        """main 대비 현재 branch의 커밋된 변경 파일 목록."""
        repo = str(self.config.repo_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "diff", "--name-only", "main..HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return [l.strip() for l in out.decode("utf-8", errors="replace").splitlines() if l.strip()]
        except Exception:
            return []

    def _update_handoff(
        self,
        task_id: str,
        *,
        current_status: str = "",
        done_item: str = "",        # Done 섹션에 추가할 단일 항목
        remaining: str = "",
        risks: str = "",
        next_prompt: str = "",
        changed_files: list[str] | None = None,
    ) -> None:
        """Phase 3-C: 의미 있는 단계에서 Handoff 파일 자동 업데이트.

        파일이 없으면 최소 포맷으로 새로 생성한다.
        짧고 가볍게 유지 — 전체 diff/로그 삽입 금지.
        """
        try:
            self._handoffs_dir.mkdir(parents=True, exist_ok=True)
            handoff_path = self._handoffs_dir / f"{task_id}.md"
            now = datetime.now().strftime("%Y-%m-%d %H:%M")

            if handoff_path.exists():
                content = handoff_path.read_text(encoding="utf-8", errors="replace")
            else:
                # 최소 포맷으로 신규 생성
                task_title = ""
                if self._running_task.get("task_id") == task_id:
                    task_title = self._running_task.get("task_title", "")
                branch = self._running_task.get("branch", "") if self._running_task.get("task_id") == task_id else ""
                content = (
                    f"# Handoff {task_id}\n\n"
                    f"> 자동 생성: {now} | branch: {branch} | project: {self._current_project_id}\n\n"
                    f"## Goal\n\n{task_title or task_id}\n\n"
                    f"## Current Status\n\n(확인 필요)\n\n"
                    f"## Changed Files\n\n변경 파일 추적 중\n\n"
                    f"## Done\n\n-\n\n"
                    f"## Remaining\n\n-\n\n"
                    f"## Risks\n\n-\n\n"
                    f"## Next Prompt\n\n-\n"
                )

            # Current Status 갱신
            if current_status:
                content = _replace_handoff_section(
                    content, "Current Status",
                    f"{current_status}\n\n(업데이트: {now})"
                )

            # Done 항목 추가 (append, 교체 아님) — 중복 방지
            if done_item:
                existing_done = _extract_handoff_section(content, "Done").strip()
                if done_item not in existing_done:  # dedup: 이미 있으면 추가 안 함
                    if existing_done in ("-", "", "<!-- 완료된 항목을 여기에 기입 -->"):
                        new_done = f"- {done_item}"
                    else:
                        new_done = f"{existing_done}\n- {done_item}"
                    content = _replace_handoff_section(content, "Done", new_done)

            # Remaining 교체
            if remaining:
                content = _replace_handoff_section(content, "Remaining", remaining)

            # Risks 교체
            if risks:
                content = _replace_handoff_section(content, "Risks", f"- {risks}")

            # Next Prompt 교체
            if next_prompt:
                content = _replace_handoff_section(content, "Next Prompt", next_prompt)

            # Changed Files 갱신
            if changed_files is not None:
                files_text = (
                    "\n".join(f"- {f}" for f in changed_files[:20])
                    or "변경 파일 없음"
                )
                content = _replace_handoff_section(content, "Changed Files", files_text)

            # Batch 5: 전체 handoff 최대 길이 제한 (8,000자 — LLM 컨텍스트 절약)
            _HANDOFF_MAX_CHARS = 8000
            if len(content) > _HANDOFF_MAX_CHARS:
                # 말미 초과분 제거 후 안내 주석 추가
                content = content[:_HANDOFF_MAX_CHARS].rstrip()
                content += "\n\n<!-- [자동 trim: handoff가 너무 길어 잘렸습니다] -->\n"

            handoff_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            print(f"[orchestrator] handoff 업데이트 오류 {task_id}: {exc}")

    def _task_label(self, task_id: str) -> str:
        """Telegram 알림용 '{project_id}:{task_id}' 레이블.

        project_id가 설정되어 있으면 접두사를 붙인다.
        단일 프로젝트 모드에서는 task_id 그대로 반환.
        """
        pid = self._current_project_id
        return f"{pid}:{task_id}" if pid else task_id

    async def _notify(self, text: str) -> None:
        """notify_fn이 있으면 Telegram 알림 전송."""
        if self.notify_fn is not None:
            try:
                await self.notify_fn(text)
            except Exception as e:
                print(f"[orchestrator] 알림 전송 실패: {e}")
        else:
            print(f"[notify] {text}")

    def _load_spec_context(self) -> str:
        """ReviewAgent에 전달할 spec 컨텍스트 로드."""
        if self.spec_path.is_file():
            try:
                return self.spec_path.read_text(encoding="utf-8")[:4000]
            except OSError:
                pass
        elif self.spec_path.is_dir():
            readme = self.spec_path / "README.md"
            if readme.exists():
                try:
                    return readme.read_text(encoding="utf-8")[:4000]
                except OSError:
                    pass
        return "(spec context 없음)"

    def _save_completed(
        self,
        task_id: str,
        packet: CompletedPacket,
        pr_url: str | None,
    ) -> None:
        """completed/<task_id>_completed.json 저장."""
        data = {
            "task_id": task_id,
            "agent_id": packet.agent_id,
            "files_changed": packet.files_changed,
            "pr_url": pr_url,
            "timestamp": packet.timestamp or datetime.now().isoformat(),
        }
        out = self.completed_dir / f"{task_id}_completed.json"
        try:
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[orchestrator] completed JSON 저장 실패: {e}")
