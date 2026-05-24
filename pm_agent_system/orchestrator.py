"""
orchestrator.py — Claude Code CLI subprocess orchestrator

태스크 큐(.md 파일)를 감시하고, Claude Code CLI로 실행한 뒤
결과를 ReviewAgent로 검토하고 GitManager로 커밋/PR 생성.
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
        # 파일이 task_queue 안으로 이동된 경우도 처리
        if not event.is_directory and event.dest_path.endswith(".md"):
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, Path(event.dest_path)
            )


def _parse_task_id(path: Path) -> str:
    """파일 stem에서 task_id 추출.

    예: "01_T-60a" → "T-60a"
    숫자 prefix 없으면 stem 그대로 반환.
    """
    stem = path.stem  # e.g. "01_T-60a"
    parts = stem.split("_", 1)
    if len(parts) > 1 and parts[0].isdigit():
        return parts[1]
    return stem


def _parse_allowed_files(task_content: str) -> list[str]:
    """마크다운에서 'allowed_files:' 섹션의 파일 목록 파싱.

    지원 형식:
      allowed_files:
        - src/foo.py
        - src/bar.py

    또는 인라인:
      allowed_files: src/foo.py, src/bar.py
    """
    files: list[str] = []

    # 섹션 블록 탐색 (YAML-list 형식)
    block_pattern = re.compile(
        r"(?:^|\n)allowed_files\s*:\s*\n((?:[ \t]*-[ \t]+\S[^\n]*\n?)+)",
        re.IGNORECASE,
    )
    m = block_pattern.search(task_content)
    if m:
        for line in m.group(1).splitlines():
            stripped = re.sub(r"^\s*-\s*", "", line).strip()
            if stripped:
                files.append(stripped)
        return files

    # 인라인 형식: allowed_files: a.py, b.py
    inline_pattern = re.compile(
        r"(?:^|\n)allowed_files\s*:\s*([^\n]+)", re.IGNORECASE
    )
    m2 = inline_pattern.search(task_content)
    if m2:
        for item in m2.group(1).split(","):
            s = item.strip()
            if s:
                files.append(s)

    return files


def _parse_files_changed(stdout: str, fallback: list[str]) -> list[str]:
    """CLI stdout에서 변경된 파일 목록 파싱.

    "Files changed:" 또는 JSON 블록에서 시도.
    실패 시 fallback(allowed_files) 사용.
    """
    # "Files changed:\n  - path" 형식
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

    # JSON 블록 { "files_changed": [...] }
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


def _short_reason(verdict: ReviewVerdict) -> str:
    """ReviewVerdict에서 짧은 실패 원인 문자열 생성."""
    if verdict.violations:
        first = verdict.violations[0]
        return f"{first.rule}: {first.description[:80]}"
    return verdict.notes[:100] if verdict.notes else "리뷰 실패"


class Orchestrator:
    """태스크 큐를 감시하고 Claude Code CLI로 태스크를 실행하는 오케스트레이터."""

    def __init__(
        self,
        config: Config,
        git_manager: GitManager | None = None,
        review_agent: ReviewAgent | None = None,
        notify_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.config = config
        self.git_manager = git_manager
        self.review_agent = review_agent
        self.notify_fn = notify_fn

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

        self.task_queue_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)

        self._processed_ids: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task] = {}

        # Stale lock pre-populate — 중복 실행 방지
        # Source 1: completed/*.json 파일에서
        for f in self.completed_dir.glob("*_completed.json"):
            stem = f.stem
            if stem.endswith("_completed"):
                self._processed_ids.add(stem[: -len("_completed")])

        # Source 2: git_manager state (authoritative)
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

                # ★ 직렬 실행 — 태스크가 끝날 때까지 다음 태스크 대기
                # 이로써 각 태스크는 항상 최신 main 기반으로 시작되고
                # 파일 충돌이 원천 차단됩니다.
                self._active_tasks[task_id] = True  # type: ignore[assignment]
                try:
                    await self._process_task(task_path)
                finally:
                    self._active_tasks.pop(task_id, None)
        finally:
            observer.stop()
            observer.join()

    def get_status(self) -> str:
        """현재 active tasks, processed count 등 상태 문자열 반환."""
        lines = ["=== Orchestrator 상태 ==="]
        lines.append(f"처리 완료 태스크: {len(self._processed_ids)}개")

        if self._active_tasks:
            lines.append(f"\n현재 실행 중 ({len(self._active_tasks)}개):")
            for tid in self._active_tasks:
                lines.append(f"  - {tid}")
        else:
            lines.append("\n현재 실행 중인 태스크: 없음")

        lines.append(f"\n감시 디렉토리: {self.task_queue_dir}")
        return "\n".join(lines)

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

        # CLI 실행 전 stale worktree 정리
        self._prune_worktrees()

        # 브랜치 생성
        if self.git_manager is not None:
            try:
                branch = self.git_manager.create_branch(task_id, allowed_files)
                print(f"[orchestrator] {task_id} — 브랜치 생성: {branch}")
            except RuntimeError as e:
                msg = f"[orchestrator] {task_id} — 브랜치 생성 실패: {e}"
                print(msg)
                await self._notify(f"❌ {task_id} 브랜치 생성 실패\n{e}")
                self._processed_ids.discard(task_id)
                return

        # Claude Code CLI 실행 (최대 MAX_RETRIES)
        last_verdict: ReviewVerdict | None = None
        succeeded = False

        for attempt in range(1, MAX_RETRIES + 1):
            print(f"[orchestrator] {task_id} — CLI 실행 attempt {attempt}/{MAX_RETRIES}")

            stdout_text, exit_code = await self._run_cli(task_content)

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

            files_changed = _parse_files_changed(stdout_text, allowed_files)

            packet = CompletedPacket(
                task_id=task_id,
                agent_id="claude-code-cli",
                files_changed=files_changed,
                code_diff=stdout_text[:8000],  # 리뷰어에게 전달할 diff 발췌
                test_result="(CLI stdout — 별도 테스트 없음)",
                agent_notes=stdout_text[-2000:] if len(stdout_text) > 2000 else "",
                timestamp=datetime.now().isoformat(),
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
                # ReviewAgent 없으면 자동 PASS
                from schemas import ReviewVerdict as RV
                verdict = RV(verdict="PASS", task_id=task_id, notes="review_agent 없음 — 자동 PASS")

            last_verdict = verdict
            print(f"[orchestrator] {task_id} attempt {attempt} — verdict: {verdict.verdict}")

            if verdict.verdict == "PASS":
                succeeded = True
                break

            # FAIL / NEEDS_REVISION
            reason = _short_reason(verdict)
            if attempt < MAX_RETRIES:
                await self._notify(
                    f"⚠️ {task_id} 실패 (attempt {attempt}/{MAX_RETRIES})\n"
                    f"원인: {reason}\n재시도 중..."
                )
                await asyncio.sleep(2)

        # 최종 처리
        if succeeded and last_verdict is not None:
            await self._on_pass(task_id, task_path, task_content, packet)
        else:
            await self._on_fail(task_id, task_path, last_verdict)

    async def _run_cli(self, task_content: str) -> tuple[str, int | None]:
        """Claude Code CLI를 subprocess로 실행. (stdout, exit_code) 반환."""
        cfg = self.config
        cmd = [
            "claude",
            "--print",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Bash",
            "--model", cfg.cli_model,
            task_content,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,  # inherit — 실시간 출력
                cwd=str(cfg.repo_path),
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=CLI_TIMEOUT_S
            )
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            return stdout_text, proc.returncode

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            print(f"[orchestrator] CLI 타임아웃 ({CLI_TIMEOUT_S}s)")
            return "", 124

        except FileNotFoundError:
            print("[orchestrator] 'claude' CLI를 찾을 수 없습니다. PATH를 확인하세요.")
            return "", -1

        except Exception as e:
            print(f"[orchestrator] CLI 실행 오류: {e}")
            return "", -1

    async def _on_pass(
        self,
        task_id: str,
        task_path: Path,
        task_content: str,
        packet: CompletedPacket,
    ) -> None:
        """PASS 판정 처리: commit + 완료 기록 + 알림."""
        commit_message = _build_commit_message(task_id, task_content)

        pr_url: str | None = None
        if self.git_manager is not None:
            result = self.git_manager.commit_and_merge(task_id, commit_message)
            pr_url = result.get("pr_url")
            if not result.get("ok"):
                err = result.get("error", "알 수 없는 오류")
                print(f"[orchestrator] {task_id} — commit_and_merge 실패: {err}")
                await self._notify(f"❌ {task_id} 커밋/PR 생성 실패\n{err}")
                return

        # completed JSON 저장
        self._save_completed(task_id, packet, pr_url)

        # 원본 .md 파일 아카이브 (삭제 대신 이름 변경)
        try:
            done_path = task_path.with_suffix(".done.md")
            task_path.rename(done_path)
        except OSError:
            pass

        commit_sha = result.get("commit_sha", "") if self.git_manager else ""
        sha_line = f"\ncommit: {commit_sha}" if commit_sha else ""
        await self._notify(
            f"✅ {task_id} 완료! main 머지+push 됨{sha_line}\n{commit_message}"
        )
        print(f"[orchestrator] {task_id} — 완료.")

    async def _on_fail(
        self,
        task_id: str,
        task_path: Path,
        verdict: ReviewVerdict | None,
    ) -> None:
        """최종 실패 처리: lock 해제 + 알림."""
        if self.git_manager is not None:
            self.git_manager.release_lock(task_id)

        reason = _short_reason(verdict) if verdict else "알 수 없는 오류"
        await self._notify(
            f"❌ {task_id} 최종 실패 (MAX_RETRIES 소진)\n원인: {reason}"
        )
        print(f"[orchestrator] {task_id} — 최종 실패: {reason}")

        # processed_ids에서 제거 → 수동 수정 후 재처리 가능
        self._processed_ids.discard(task_id)

    def _prune_worktrees(self) -> None:
        """stale git worktrees 정리. 오류는 무시 (non-fatal)."""
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
            # specs/ 디렉토리면 README.md 우선
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
