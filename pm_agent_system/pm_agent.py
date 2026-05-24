"""
pm_agent.py — PM Agent: 사용자와 대화하며 태스크를 설계하고 큐에 적재.

모델: PM_DIALOG_MODEL (claude-sonnet-4-6)
역할: 요구사항 → 태스크 .md 파일 생성 → task_queue/에 저장
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import anthropic

from config import Config
from schemas import TaskPacket

# 히스토리 압축 임계치 — 15턴
_HISTORY_COMPACT_THRESHOLD = 15
# spec 요약 최대 글자 수 (~2.6k 토큰)
_SPEC_SUMMARY_MAX_CHARS = 10_000
# ★ 히스토리 전송 시 오래된 메시지 잘라내기 (현재 메시지 제외)
# Telegram 최대 4096자이지만, 히스토리 누적 비용 방지를 위해 더 짧게 제한
_HISTORY_MSG_MAX_CHARS = 600

_SYSTEM_PROMPT = """\
당신은 FAB AMR 시뮬레이터 프로젝트(vda5050_sim_v2)의 PM입니다.

역할:
- 사용자(Teo)의 요청을 분석해 구체적인 코딩 태스크로 변환합니다.
- 태스크는 아래 JSON 형식으로 출력하면 자동으로 큐에 적재됩니다.
- 절대로 raw JSON만 단독 응답하지 마세요 — 반드시 설명 텍스트와 함께 출력하세요.
- 항상 한국어로 응답합니다.

태스크 JSON 형식:
```json
{
  "task_id": "T-N",
  "title": "한 줄 제목",
  "files": ["수정할/파일1.py", "수정할/파일2.py"],
  "description": "상세 구현 지침 (마크다운 가능)",
  "priority": 0
}
```

지침:
- task_id는 T-68처럼 이슈 번호를 포함하거나, T-AUTO-{숫자} 형식을 사용하세요.
- priority: 0=일반, 1=높음, 2=긴급
- 파일 목록은 실제 변경이 예상되는 파일만 포함하세요.
- description은 구현자가 추가 질문 없이 코딩할 수 있도록 충분히 상세하게 작성하세요.
- 태스크가 너무 크면 분할을 제안하세요.
- 이미 완료된 태스크와 중복되지 않도록 완료 이력을 참고하세요.
"""


class PMAgent:
    """Telegram 사용자와 대화하며 태스크를 설계하고 task_queue/에 .md 파일을 생성하는 PM."""

    def __init__(
        self,
        config: Config,
        repo_path: Path | None = None,
        spec_path: Path | None = None,
        task_queue_dir: Path | None = None,
        dialog_model: str | None = None,
        util_model: str | None = None,
        api_key: str | None = None,
        github_token: str | None = None,
        github_repo: str | None = None,
        # legacy: orchestrator/git_manager를 통한 생성도 지원
        git_manager: Any = None,
        orchestrator: Any = None,
    ) -> None:
        self._config = config
        self._git_manager = git_manager
        self._orchestrator = orchestrator

        # config 폴백
        _repo = Path(repo_path) if repo_path else Path(config.repo_path)
        _spec = Path(spec_path) if spec_path else Path(config.spec_path)
        _task_q = (
            Path(task_queue_dir)
            if task_queue_dir
            else _repo / config.task_queue_dir
        )
        _dialog_model = dialog_model or config.pm_dialog_model
        _util_model = util_model or config.anthropic_model
        _api_key = api_key or config.anthropic_api_key
        _gh_token = github_token or config.github_token or None
        _gh_repo = github_repo or config.github_repo or None

        self._repo_path = _repo
        self._spec_path = _spec
        self._task_queue_dir = _task_q
        self._dialog_model = _dialog_model
        self._util_model = _util_model
        self._github_token = _gh_token
        self._github_repo = _gh_repo

        # ★ api_key 버그 수정: 파라미터 api_key가 None일 수 있으므로 _api_key 사용
        self._client = anthropic.AsyncAnthropic(api_key=_api_key)
        self._history: list[dict] = []
        self._spec_summary: str = self._build_spec_summary()

        # task_queue 디렉토리 보장
        self._task_queue_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def chat(self, user_message: str) -> str:
        """사용자 메시지를 받아 응답을 반환한다. 태스크 JSON이 포함되면 .md 파일을 생성한다."""
        self._history.append({"role": "user", "content": user_message})

        # 히스토리 압축 (임계치 초과 시)
        await self._maybe_compact_history()

        # ★ Prompt caching: system prompt + spec_summary는 매 턴 동일 →
        #   cache_control 으로 캐싱. 5분 TTL 내 재호출 시 ~3,300 토큰 절약.
        system_text = f"{_SYSTEM_PROMPT}\n\n## 프로젝트 스펙 요약\n{self._spec_summary}"
        system_blocks = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # ★ 히스토리 전송 전 오래된 메시지 트리밍
        # - 현재 메시지(마지막)는 전문 전달 (이해 정확도 유지)
        # - 이전 메시지는 _HISTORY_MSG_MAX_CHARS로 잘라냄
        #   → 코드 붙여넣기 등 큰 메시지가 이후 모든 턴 비용을 올리는 구멍 차단
        api_messages = []
        for i, msg in enumerate(self._history):
            is_current = (i == len(self._history) - 1)
            content = msg["content"]
            if not is_current and len(content) > _HISTORY_MSG_MAX_CHARS:
                content = content[:_HISTORY_MSG_MAX_CHARS] + "…[이전 메시지 축약]"
            api_messages.append({"role": msg["role"], "content": content})

        response = await self._client.messages.create(
            model=self._dialog_model,
            max_tokens=2048,   # 4096 → 2048: 대화형 응답에 충분
            system=system_blocks,
            messages=api_messages,
        )

        assistant_text = ""
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text

        self._history.append({"role": "assistant", "content": assistant_text})

        # JSON 태스크 파싱 및 저장
        tasks = self._extract_tasks(assistant_text)
        saved_ids: list[str] = []
        for task in tasks:
            task_id = task.get("task_id", "T-AUTO")
            priority = int(task.get("priority", 0))
            content = self._render_task_markdown(task)

            # PR 충돌 검사
            files = task.get("files", [])
            if files:
                conflicts = await self._check_open_pr_conflicts(files)
                if conflicts:
                    conflict_note = f"\n\n> ⚠️ **PR 충돌 경고**: 다음 파일이 열린 PR과 겹칩니다: {', '.join(conflicts)}"
                    content += conflict_note

            self._save_task(task_id, content, priority)
            saved_ids.append(task_id)

        if saved_ids:
            assistant_text += f"\n\n✅ 태스크 큐에 저장됨: {', '.join(saved_ids)}"

        return assistant_text

    def inject_project_status(
        self, completed_tasks: list[dict], open_prs: list[str] | None = None
    ) -> None:
        """시작 시 호출 — 완료된 태스크 이력을 히스토리에 주입한다."""
        if not completed_tasks:
            return
        task_ids = [t.get("task_id", str(t)) for t in completed_tasks]
        summary = f"[시스템] 완료된 태스크: {task_ids}"
        if open_prs:
            summary += f"\n[시스템] 열린 PR: {open_prs}"
        self._history.append({"role": "assistant", "content": summary})

    # ------------------------------------------------------------------ #
    # Spec summary
    # ------------------------------------------------------------------ #

    def _build_spec_summary(self) -> str:
        """CLAUDE.md + specs/ 디렉토리 핵심 내용을 ~2.6k 토큰으로 압축."""
        parts: list[str] = []

        # CLAUDE.md 읽기
        claude_md = self._spec_path
        if claude_md.exists():
            text = claude_md.read_text(encoding="utf-8")
            # 핵심 우선순위 섹션만 추출 (첫 3000자)
            parts.append("### CLAUDE.md 요약\n" + text[:3000])

        # specs/ 디렉토리
        specs_dir = self._repo_path / "specs"
        if specs_dir.exists():
            spec_texts = self._gather_specs_from_dir(specs_dir)
            if spec_texts:
                parts.append("### specs/ 개요\n" + spec_texts)

        combined = "\n\n".join(parts)
        return combined[:_SPEC_SUMMARY_MAX_CHARS]

    def _gather_specs_from_dir(self, specs_dir: Path) -> str:
        """specs/ 디렉토리에서 제목과 첫 단락만 추출."""
        snippets: list[str] = []
        for md_file in sorted(specs_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
                # 제목 + 첫 200자
                lines = text.splitlines()
                title = lines[0] if lines else md_file.name
                preview = "\n".join(lines[:8])[:200]
                snippets.append(f"**{md_file.name}**: {title}\n{preview}")
            except Exception:
                pass
        return "\n\n".join(snippets)

    # ------------------------------------------------------------------ #
    # Task markdown rendering
    # ------------------------------------------------------------------ #

    def _render_task_markdown(self, task: dict) -> str:
        """task dict → .md 파일 내용 생성."""
        task_id = task.get("task_id", "T-AUTO")
        title = task.get("title", "(제목 없음)")
        description = task.get("description", "")
        files = task.get("files", [])
        priority = task.get("priority", 0)

        priority_label = {0: "일반", 1: "높음", 2: "긴급"}.get(int(priority), "일반")
        files_list = "\n".join(f"- `{f}`" for f in files) if files else "- (미정)"

        return f"""\
# Task {task_id}

⚠️ CRITICAL OUTPUT REQUIREMENT ⚠️
작업 완료 후 반드시 JSON 형식으로 결과를 출력하세요:
{{"task_id": "{task_id}", "files_changed": [...], "test_result": "...", "agent_notes": "..."}}

## 목표
{title}

## 우선순위
{priority_label}

## 수정 대상 파일
{files_list}

## 상세 구현 지침
{description}

## 완료 기준
- 위 파일들이 수정되었을 것
- `python tests/integration/test_simulation.py` 통과
- 새 기능이 있으면 기존 API 호환성 유지
"""

    # ------------------------------------------------------------------ #
    # Task extraction
    # ------------------------------------------------------------------ #

    def _extract_tasks(self, text: str) -> list[dict]:
        """응답 텍스트에서 태스크 JSON을 추출한다."""
        tasks: list[dict] = []

        # ```json ... ``` 블록 탐색
        pattern = r"```json\s*(\{[^`]+\})\s*```"
        for match in re.finditer(pattern, text, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                if "task_id" in data:
                    tasks.append(data)
            except json.JSONDecodeError:
                pass

        # 인라인 JSON (백틱 없이) 탐색 — fallback
        if not tasks:
            inline_pattern = r'(\{"task_id"[^}]*(?:\{[^}]*\}[^}]*)?\})'
            for match in re.finditer(inline_pattern, text, re.DOTALL):
                try:
                    data = json.loads(match.group(1))
                    if "task_id" in data and "title" in data:
                        tasks.append(data)
                except json.JSONDecodeError:
                    pass

        return tasks

    # ------------------------------------------------------------------ #
    # PR conflict check
    # ------------------------------------------------------------------ #

    async def _check_open_pr_conflicts(self, files: list[str]) -> list[str]:
        """GitHub API로 열린 feat/ 브랜치와 겹치는 파일 목록을 반환한다."""
        if not self._github_token or not self._github_repo:
            return []

        try:
            import urllib.request

            api_url = f"https://api.github.com/repos/{self._github_repo}/pulls?state=open&per_page=50"
            req = urllib.request.Request(
                api_url,
                headers={
                    "Authorization": f"token {self._github_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                prs = json.loads(resp.read())

            conflicting: list[str] = []
            for pr in prs:
                branch = pr.get("head", {}).get("ref", "")
                if not branch.startswith("feat/"):
                    continue
                # git diff로 변경 파일 확인
                try:
                    result = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(self._repo_path),
                            "diff",
                            "--name-only",
                            f"origin/main...origin/{branch}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    pr_files = set(result.stdout.strip().splitlines())
                    for f in files:
                        if f in pr_files and f not in conflicting:
                            conflicting.append(f)
                except Exception:
                    pass

            return conflicting

        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Save task
    # ------------------------------------------------------------------ #

    def _save_task(self, task_id: str, content: str, priority: int = 0) -> Path:
        """task_queue/{priority:02d}_{task_id}.md 형식으로 저장한다."""
        filename = f"{priority:02d}_{task_id}.md"
        path = self._task_queue_dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------ #
    # History compaction
    # ------------------------------------------------------------------ #

    async def _maybe_compact_history(self) -> None:
        """히스토리가 임계치를 초과하면 util_model로 요약 압축한다."""
        if len(self._history) <= _HISTORY_COMPACT_THRESHOLD:
            return

        # 최근 10턴은 보존, 나머지를 요약
        preserve_recent = 10
        to_summarize = self._history[:-preserve_recent]
        recent = self._history[-preserve_recent:]

        summary_prompt = (
            "다음은 PM Agent와 사용자 간의 대화 이력입니다. "
            "핵심 결정 사항, 생성된 태스크 ID, 중요 컨텍스트를 한국어로 간결하게 요약하세요.\n\n"
            + "\n".join(
                f"[{m['role']}]: {m['content'][:300]}"
                for m in to_summarize
            )
        )

        try:
            # util_model로 요약 — 단발 호출이라 캐싱 불필요
            resp = await self._client.messages.create(
                model=self._util_model,
                max_tokens=512,   # 요약은 짧게 충분
                messages=[{"role": "user", "content": summary_prompt}],
            )
            summary_text = ""
            for block in resp.content:
                if block.type == "text":
                    summary_text += block.text

            compressed = [
                {
                    "role": "assistant",
                    "content": f"[이전 대화 요약]\n{summary_text}",
                }
            ]
            self._history = compressed + recent
        except Exception:
            # 압축 실패 시 오래된 절반만 제거
            self._history = self._history[len(self._history) // 2 :]
