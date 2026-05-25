"""
project_manager.py — 멀티 프로젝트 지원 (Phase 0.5)

projects.yaml 기반 프로젝트 정의 로드 + 현재 프로젝트 상태 관리.
Orchestrator와 TelegramBot 이 공유하는 단일 인스턴스.

핵심 개념:
  ProjectPaths — 한 프로젝트의 모든 경로 모음 (task_queue, logs, handoffs 등)
  ProjectManager — projects.yaml 로드 + 현재 프로젝트 전환 + 경로 제공
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False


# ── ProjectPaths ─────────────────────────────────────────────────────────────

@dataclass
class ProjectPaths:
    """한 프로젝트의 경로 묶음."""
    project_id: str
    repo_path: Path
    spec_path: Path
    pmbot_dir: Path          # .pmbot/ 또는 pm_agent_system/ (하위호환)

    # ── 파생 경로 (pmbot_dir 기준) ───────────────────────────────────────

    @property
    def task_queue_dir(self) -> Path:
        return self.pmbot_dir / "task_queue"

    @property
    def logs_dir(self) -> Path:
        return self.pmbot_dir / "logs"

    @property
    def task_logs_dir(self) -> Path:
        return self.pmbot_dir / "logs" / "tasks"

    @property
    def completed_dir(self) -> Path:
        return self.pmbot_dir / "completed"

    @property
    def state_path(self) -> Path:
        return self.pmbot_dir / "state" / "git_state.json"

    @property
    def handoffs_dir(self) -> Path:
        return self.pmbot_dir / "handoffs"

    def ensure_dirs(self) -> None:
        """필요한 디렉토리를 모두 생성한다."""
        for d in [
            self.task_queue_dir,
            self.task_logs_dir,
            self.completed_dir,
            self.state_path.parent,
            self.handoffs_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    def task_log_path(self, task_id: str, suffix: str = "combined") -> Path:
        """태스크 로그 파일 경로. suffix: stdout / stderr / combined."""
        return self.task_logs_dir / f"{task_id}.{suffix}.log"

    def handoff_path(self, task_id: str) -> Path:
        return self.handoffs_dir / f"{task_id}.md"


# ── Handoff 생성 헬퍼 ────────────────────────────────────────────────────────

def generate_handoff(
    task_id: str,
    paths: ProjectPaths,
    goal: str = "",
    current_status: str = "",
) -> str:
    """git 상태 + 태스크 정보 기반 Handoff 마크다운 생성.

    LLM 미사용 — git status/diff 결과를 그대로 삽입.
    Done / Remaining / Risks / Next Prompt 는 사용자가 채우는 placeholder.
    """
    repo = str(paths.repo_path)

    # git changed files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        )
        changed_files_unstaged = [l.strip() for l in result.stdout.splitlines() if l.strip()]

        result2 = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        )
        changed_files_staged = [l.strip() for l in result2.stdout.splitlines() if l.strip()]

        result3 = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        )
        git_status_short = result3.stdout.strip()

        result4 = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        )
        current_branch = result4.stdout.strip()
    except Exception as exc:
        changed_files_unstaged = []
        changed_files_staged = []
        git_status_short = f"(git 조회 실패: {exc})"
        current_branch = "unknown"

    all_changed = sorted(set(changed_files_unstaged + changed_files_staged))
    files_section = "\n".join(f"- {f}" for f in all_changed) if all_changed else "변경 파일 없음"

    # 최근 로그 미리보기 (마지막 20줄)
    log_path = paths.task_log_path(task_id)
    log_preview = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-20:] if len(lines) > 20 else lines
        log_preview = "\n".join(tail)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""# Handoff {task_id}

> 생성: {now} | branch: {current_branch} | project: {paths.project_id}

## Goal

{goal or '(태스크 목표 — 태스크 파일에서 확인)'}

## Current Status

{current_status or '(현재 상태 직접 기입)'}

## Changed Files

{files_section}

```
{git_status_short or '(변경 없음)'}
```

## Done

<!-- 완료된 항목을 여기에 기입 -->
-

## Remaining

<!-- 남은 작업을 여기에 기입 -->
-

## Risks

<!-- 주의사항 / 미완성 부분 -->
-

## Next Prompt

<!-- PM Bot 또는 Claude Code에 넘길 다음 구현 지시문 -->

## Log Preview (최근 20줄)

```
{log_preview or '(로그 없음)'}
```
"""


# ── ProjectManager ──────────────────────────────────────────────────────────

class ProjectManager:
    """projects.yaml 기반 프로젝트 관리자.

    - projects.yaml 로드 (없으면 단일 프로젝트 모드 유지)
    - /project PROJECT_ID 전환
    - current_paths: 현재 활성 프로젝트의 ProjectPaths 반환
    """

    def __init__(self, yaml_path: Path) -> None:
        self._yaml_path = yaml_path
        self._raw: dict = {}
        self._default_id: str = ""
        self._current_id: str = ""
        self._load()

    # ── 로드 ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._yaml_path.exists():
            return
        if not _YAML_OK:
            print("[project_manager] PyYAML 미설치 — projects.yaml 무시")
            return
        try:
            data = yaml.safe_load(self._yaml_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self._raw = data.get("projects", {})
            self._default_id = str(data.get("default_project", ""))
            self._current_id = self._default_id
        except Exception as exc:
            print(f"[project_manager] projects.yaml 로드 실패: {exc}")

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def current_project_id(self) -> str:
        return self._current_id or self._default_id

    @property
    def current_paths(self) -> Optional[ProjectPaths]:
        return self.get_paths(self.current_project_id)

    def list_projects(self) -> list[str]:
        return list(self._raw.keys())

    def switch(self, project_id: str) -> bool:
        """프로젝트 전환. 존재하지 않으면 False 반환."""
        if project_id not in self._raw:
            return False
        self._current_id = project_id
        return True

    def get_paths(self, project_id: str) -> Optional[ProjectPaths]:
        """project_id에 해당하는 ProjectPaths 반환. 없으면 None."""
        cfg = self._raw.get(project_id)
        if not cfg:
            return None

        repo_path = Path(cfg["repo_path"])
        spec_path = Path(cfg.get("spec_path", repo_path / "CLAUDE.md"))

        # pmbot_dir 결정: 명시 지정 > repo/.pmbot/ 기본값
        pmbot_dir_raw = cfg.get("pmbot_dir", "")
        pmbot_dir = Path(pmbot_dir_raw) if pmbot_dir_raw else repo_path / ".pmbot"

        paths = ProjectPaths(
            project_id=project_id,
            repo_path=repo_path,
            spec_path=spec_path,
            pmbot_dir=pmbot_dir,
        )
        paths.ensure_dirs()
        return paths

    def format_project_list(self) -> str:
        """Telegram 출력용 프로젝트 목록 문자열."""
        if not self._raw:
            return "등록된 프로젝트 없음 (projects.yaml 확인)"
        lines = ["📂 프로젝트 목록\n"]
        for pid in self._raw:
            marker = "▶ " if pid == self.current_project_id else "  "
            cfg = self._raw[pid]
            repo = cfg.get("repo_path", "?")
            lines.append(f"{marker}{pid}\n    {repo}")
        return "\n".join(lines)

    def format_current(self) -> str:
        """Telegram 출력용 현재 프로젝트 상태 문자열."""
        pid = self.current_project_id
        if not pid:
            return "현재 프로젝트 없음"
        cfg = self._raw.get(pid, {})
        repo = cfg.get("repo_path", "?")
        spec = cfg.get("spec_path", "?")
        return (
            f"📌 현재 프로젝트: {pid}\n\n"
            f"repo: {repo}\n"
            f"spec: {spec}"
        )
