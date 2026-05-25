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

from schemas import TaskPacket, CompletedPacket, ReviewVerdict
from git_manager import GitManager
from review_agent import ReviewAgent
from config import Config


CLI_TIMEOUT_S = 3600   # 1시간 (subscription 모드)
MAX_RETRIES = 3
_LOG_TAIL_LINES = 50   # /log 명령 기본 출력 줄 수


class _TaskQueueHandler(FileSystemEventHandler):
    """task_queue 디렉토리 파일 변경 감지 → asyncio 큐에 푸시."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._queue = queue
        self._loop = loop

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory and event.src_path.endswith(".md"):
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, Path(event.src_path)
            )

    def on_moved(self, event) -> None:  # type: ignore[override]
        if not event.is_directory and event.dest_path.endswith(".md"):
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, Path(event.dest_path)
            )


def _parse_task_id(path: Path) -> str:
    """파일 stem에서 task_id 추출.

    예: "01_T-60a" → "T-60a"
         "retry_T-73" → "T-73"   (Phase 3 재시도 파일)
    숫자 prefix 없으면 stem 그대로 반환.
    """
    stem = path.stem  # e.g. "01_T-60a"
    # Phase 3: retry_ prefix 처리
    if stem.startswith("retry_"):
        return stem[len("retry_"):]
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


# Phase 4: Diff 유틸리티 ────────────────────────────────────────────────

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
    "REVIEW_FAILED", "GIT_FAILED", "TIMEOUT", "UNKNOWN",
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
        "GIT_FAILED":      "git 상태를 확인하고 branch를 정리하세요.",
        "TIMEOUT":         "태스크 복잡도를 줄이거나 분할하여 재시도하세요.",
        "UNKNOWN":         "로그를 확인하고 재시도하세요.",
    }.get(category, "로그를 확인하세요.")


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

        self.task_queue_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._processed_ids: set[str] = set()
        self._active_tasks: dict[str, Any] = {}

        # Phase 0: 현재 실행 중인 태스크 메타데이터
        self._running_task: dict = {}

        # Phase 1: READY_TO_SHIP 대기 중인 태스크
        self._ready_to_ship: dict[str, dict] = {}

        # Phase 3: 실패 태스크 정보 (retry/hold_branch/cancel용)
        self._failed_tasks: dict[str, dict] = {}
        # 현재 실행 중인 subprocess (cancel용)
        self._current_proc: asyncio.subprocess.Process | None = None  # type: ignore[type-arg]
        # cancel 요청된 task_id 집합
        self._cancel_requested: set[str] = set()
        # 수동 재시도 횟수 (task_id → count)
        self._manual_retry_counts: dict[str, int] = {}

        # Phase 4: 태스크별 git diff 정보 (/diff T-ID 및 리뷰용)
        self._task_diffs: dict[str, dict] = {}

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

    # ── Public interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        """watchdog으로 task_queue 감시 + 기존 파일 초기 스캔."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Path] = asyncio.Queue()

        handler = _TaskQueueHandler(queue, loop)
        observer = Observer()
        observer.schedule(handler, str(self.task_queue_dir), recursive=False)
        observer.start()

        print(f"[orchestrator] task_queue 감시 시작: {self.task_queue_dir}")

        # 기존 .md 파일 초기 스캔
        for existing_md in sorted(self.task_queue_dir.glob("*.md")):
            await queue.put(existing_md)

        try:
            while True:
                try:
                    task_path = await asyncio.wait_for(queue.get(), timeout=5.0)
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

        # READY_TO_SHIP 태스크
        for tid, info in self._ready_to_ship.items():
            active[tid] = {
                "status": "READY_TO_SHIP",
                "branch": info.get("branch", ""),
                "created_at": info.get("ready_at", ""),
            }

        completed = []
        if self.git_manager is not None:
            completed = self.git_manager.get_state().get("completed_tasks", [])

        return {
            "active_tasks": active,
            "queued_tasks": list(self._active_tasks.keys()),
            "completed_tasks": completed,
        }

    def get_running_task(self) -> dict | None:
        """Phase 0: 현재 실행 중인 태스크 정보 반환. 없으면 None."""
        if not self._running_task:
            return None
        return dict(self._running_task)

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
        """Phase 4: /diff T-ID 명령용 모바일 친화적 diff 요약 반환."""
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

        return _format_diff_summary(task_id, diff_info)

    async def ship_task(self, task_id: str) -> str:
        """Phase 1: READY_TO_SHIP 태스크를 직접 머지/푸시.

        Returns: 결과 메시지 문자열
        """
        if task_id not in self._ready_to_ship:
            return f"❌ {task_id}은(는) READY_TO_SHIP 상태가 아닙니다.\n현재 READY_TO_SHIP: {list(self._ready_to_ship.keys()) or '없음'}"

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

        branch_str = f"\nbranch: {branch}" if branch else ""
        return (
            f"⏸️ {task_id} 보류 처리됨\n\n"
            f"branch는 유지됩니다.{branch_str}\n\n"
            f"나중에 다시 처리하려면 태스크를 재실행해주세요."
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
        # Phase 1: QUEUED 알림
        await self._notify(
            f"📋 {task_id} 대기열 등록\n\n"
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
        await self._notify(
            f"⏳ {task_id} 진행 중\n\n"
            f"현재 단계:\n- Claude Code CLI 실행 중\n\n"
            f"로그:\n- /log {task_id}\n\n"
            f"상태:\n- /running"
        )

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
                # Phase 4 필드
                actual_diff=diff_info.get("actual_diff", "")[:12000],
                git_status=diff_info.get("git_status", ""),
                diff_stat=diff_info.get("diff_stat", ""),
                diff_numstat=diff_info.get("diff_numstat", ""),
                name_status=diff_info.get("name_status_raw", ""),
                review_target="actual_git_diff",
            )

            # Phase 1: REVIEWING 알림
            self._running_task["phase"] = "REVIEWING"
            await self._notify(
                f"🧪 {task_id} 리뷰 중\n\n"
                f"구현은 완료되었습니다.\n"
                f"Review Agent 검토를 진행합니다.\n\n"
                f"로그:\n- /log {task_id}"
            )

            # ReviewAgent 검토
            if self.review_agent is not None:
                spec_context = self._load_spec_context()
                verdict = await self.review_agent.review(
                    spec_context=spec_context,
                    completed_packet=packet,
                    allowed_files=allowed_files if allowed_files else None,
                )
            else:
                from schemas import ReviewVerdict as RV
                verdict = RV(verdict="PASS", task_id=task_id, notes="review_agent 없음 — 자동 PASS")

            last_verdict = verdict
            print(f"[orchestrator] {task_id} attempt {attempt} — verdict: {verdict.verdict}")

            if verdict.verdict == "PASS":
                succeeded = True
                break

            # 구조적 실패 — 재시도해도 통과 불가, 즉시 중단
            _NO_RETRY_RULES = frozenset({
                "scope.files_outside_task",
                "scope.sensitive_file_changed",
                "scope.file_deleted",
                "correctness.no_changes",
            })
            violated_rules = {getattr(v, "rule", "") for v in verdict.violations}
            if violated_rules & _NO_RETRY_RULES:
                print(f"[orchestrator] {task_id} — 구조적 실패 감지, retry 생략: {violated_rules & _NO_RETRY_RULES}")
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
            await self._on_pass(task_id, task_path, task_content, packet)
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
        cmd = [
            "claude",
            "--print",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Bash",
            "--model", cfg.cli_model,
            task_content,
        ]

        stdout_log_path = self._log_dir / f"{task_id}.stdout.log"
        stderr_log_path = self._log_dir / f"{task_id}.stderr.log"
        combined_log_path = self._log_dir / f"{task_id}.combined.log"

        stdout_buffer: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cfg.repo_path),
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

            card_text = (
                f"✅ {task_id} 리뷰 통과\n\n"
                f"main에는 아직 반영하지 않았습니다.\n"
                f"Ship 승인이 필요합니다.\n\n"
                f"명령:\n"
                f"- /ship {task_id}\n"
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
        await self._notify(
            f"🚀 {task_id} 배포 완료\n\nmain merge/push 완료{sha_line}"
        )
        print(f"[orchestrator] {task_id} — 배포 완료.")

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

        # 구조적 실패 여부 판단 (retry 불가 유형)
        _NO_RETRY_RULES = frozenset({
            "scope.files_outside_task",
            "scope.sensitive_file_changed",
            "scope.file_deleted",
            "correctness.no_changes",
        })
        violated_rules = {getattr(v, "rule", "") for v in (verdict.violations if verdict else [])}
        is_structural = bool(violated_rules & _NO_RETRY_RULES)

        # 실패 카드 텍스트
        structural_note = (
            "\n⚠️ 구조적 차단 — 자동 재시도하지 않음\n태스크 범위를 수정해 새 태스크로 요청하세요."
            if is_structural else ""
        )
        card_text = (
            f"❌ {task_id} 실패\n\n"
            f"분류:\n- {category}\n\n"
            f"요약:\n- {reason}\n\n"
            f"추천:\n- {action}"
            f"{structural_note}"
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

        # processed_ids 제거 → 수동 수정 후 재처리 가능
        self._processed_ids.discard(task_id)

        if self._running_task.get("task_id") == task_id:
            self._running_task = {}

    # ── Phase 4: Git diff 수집 / 저장 / 포맷 ──────────────────────────

    async def _collect_git_diff(self, branch: str) -> dict:
        """Phase 4: Claude CLI 실행 후 실제 git diff 수집.

        Review 시점에는 변경이 아직 unstaged 상태이므로
        'git diff HEAD' (unstaged) + 'git diff --cached HEAD' (staged) 조합 사용.
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

        # unstaged + staged 변경 모두 수집
        diff_unstaged = await _git("diff", "HEAD")
        diff_staged = await _git("diff", "--cached", "HEAD")
        actual_diff = (diff_staged + diff_unstaged).strip()

        # 통계 (staged+unstaged 합산)
        stat_unstaged = await _git("diff", "--stat", "HEAD")
        stat_staged = await _git("diff", "--stat", "--cached", "HEAD")
        diff_stat = (stat_staged + stat_unstaged).strip()

        # numstat — per-file +/-
        num_unstaged = await _git("diff", "--numstat", "HEAD")
        num_staged = await _git("diff", "--numstat", "--cached", "HEAD")
        diff_numstat = (num_staged + num_unstaged).strip()

        # name-status (M/A/D per file)
        ns_unstaged = await _git("diff", "--name-status", "HEAD")
        ns_staged = await _git("diff", "--name-status", "--cached", "HEAD")
        name_status_raw = (ns_staged + ns_unstaged).strip()

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
