"""
review_agent.py — 코드 리뷰 에이전트

Phase 4 업데이트:
  - Pre-LLM scope check 강화 (6가지 차단 조건)
  - actual_diff (git diff HEAD) 를 primary 리뷰 기준으로 사용
  - CLI stdout → fallback only

비용 최적화:
  - AsyncAnthropic 사용 (이벤트루프 블로킹 해소)
  - system prompt에 cache_control: ephemeral 적용
  - Pre-LLM scope check: API 호출 없이 즉시 FAIL
  - actual_diff: 10,000자 제한
  - agent_notes: 1,000자 제한
"""
from __future__ import annotations

import json
import re

import anthropic

from schemas import CompletedPacket, ReviewVerdict, Violation


# ── Sensitive file patterns ───────────────────────────────────────────────
_SENSITIVE_PATTERNS = frozenset([
    ".env", "secret", "credential", "token", "password", "private_key",
    "auth", ".pem", ".key", ".cert", ".p12", "id_rsa", "id_ed25519",
    "apikey", "api_key",
])


def _is_sensitive_file(fname: str) -> bool:
    """config/env/security 관련 파일 여부 판단."""
    name = fname.lower().replace("\\", "/").split("/")[-1]
    return any(pat in name for pat in _SENSITIVE_PATTERNS)


def _get_deleted_files(git_status: str, name_status: str) -> list[str]:
    """git status --short / --name-status 에서 삭제된 파일 목록 추출."""
    deleted: set[str] = set()

    # git status --short: 'D ' or ' D' prefix
    for line in git_status.splitlines():
        if len(line) >= 3:
            xy = line[:2]
            fname = line[3:].strip()
            if "D" in xy and fname:
                deleted.add(fname)

    # git diff --name-status: 'D\tfilename'
    for line in name_status.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().startswith("D"):
            fname = parts[1].strip()
            if fname:
                deleted.add(fname)

    return sorted(deleted)


def _detect_test_failure(test_result: str) -> bool:
    """테스트 실패 패턴 감지. CLI stdout에 명확한 실패 패턴이 있을 때만 True."""
    import re as _re
    if not test_result or "CLI stdout" in test_result:
        return False  # 별도 테스트 없음 → 판단 불가
    result_lower = test_result.lower()
    # "3 failed" 같은 숫자+failed 패턴 → 숫자 > 0 이면 실패 (0 failed 제외)
    for m in _re.finditer(r"(\d+)\s+failed", result_lower):
        if int(m.group(1)) > 0:
            return True
    if "assertion error" in result_lower or "test_failed" in result_lower:
        return True
    # 그 외 fail 패턴은 pass 패턴 없을 때만
    fail_patterns = ["error:", "tests failed"]
    pass_patterns = ["passed", "ok", "all tests", "success"]
    has_fail = any(p in result_lower for p in fail_patterns)
    has_pass = any(p in result_lower for p in pass_patterns)
    return has_fail and not has_pass


# ★ cache_control 적용: system prompt는 매 리뷰 동일 → 캐시 히트 시 ~300 토큰 절약
_SYSTEM_BLOCKS = [
    {
        "type": "text",
        "text": """\
You are a strict code review agent. Given a task spec and the ACTUAL git diff,
evaluate whether the changes are correct, complete, and within scope.

IMPORTANT: Base your verdict primarily on the actual_diff (real git changes),
NOT on stdout summaries or agent notes.

Respond ONLY with a valid JSON object:
{
  "verdict": "PASS" | "FAIL" | "NEEDS_REVISION",
  "violations": [
    {"rule": "<rule_id>", "description": "<what went wrong>", "severity": "ERROR" | "WARN"}
  ],
  "notes": "<one-line summary>"
}

Rules:
- correctness.logic: Does the actual diff match the task spec?
- correctness.tests: Do tests cover the changes?
- scope.unrelated_changes: Are there changes unrelated to the task?
- scope.files_outside_task: Are changed files within the expected set?
""",
        "cache_control": {"type": "ephemeral"},
    }
]

_DIFF_MAX_CHARS = 10_000   # actual_diff 상한
_NOTES_MAX_CHARS = 1_000


class ReviewAgent:
    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        # ★ AsyncAnthropic: async def review() 내에서 블로킹 없이 호출
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def review(
        self,
        spec_context: str,
        completed_packet: CompletedPacket,
        allowed_files: list[str] | None = None,
    ) -> ReviewVerdict:
        """태스크 결과를 검토하고 ReviewVerdict 반환.

        Phase 4:
        1단계: Pre-LLM scope check (API 호출 없음, 즉시 차단)
        2단계: LLM 리뷰 (actual_diff 우선)
        """
        packet = completed_packet
        task_id = packet.task_id

        # ── 1단계: Pre-LLM scope checks (zero cost) ──────────────────────

        # 1a. 허용 파일 범위 초과
        if allowed_files:
            allowed_set = {f.strip() for f in allowed_files}
            out_of_scope = [
                f for f in packet.files_changed
                if f.strip() not in allowed_set
            ]
            if out_of_scope:
                return ReviewVerdict(
                    verdict="FAIL",
                    task_id=task_id,
                    violations=[Violation(
                        rule="scope.files_outside_task",
                        description=f"Files changed outside task scope: {out_of_scope}",
                        severity="ERROR",
                    )],
                    notes="Scope violation — files outside allowed list",
                )

        # 1b. 민감 파일 (config/env/security) 변경
        sensitive = [f for f in packet.files_changed if _is_sensitive_file(f)]
        if sensitive:
            return ReviewVerdict(
                verdict="FAIL",
                task_id=task_id,
                violations=[Violation(
                    rule="scope.sensitive_file_changed",
                    description=f"Config/security files modified: {sensitive}",
                    severity="ERROR",
                )],
                notes="Sensitive file change detected — ship blocked",
            )

        # 1c. 파일 삭제 감지
        deleted = _get_deleted_files(packet.git_status, packet.name_status)
        if deleted:
            return ReviewVerdict(
                verdict="FAIL",
                task_id=task_id,
                violations=[Violation(
                    rule="scope.file_deleted",
                    description=f"Files deleted unexpectedly: {deleted}",
                    severity="ERROR",
                )],
                notes="Unexpected file deletion — ship blocked",
            )

        # 1d. diff가 비어 있음 (변경 없음)
        # actual_diff 기준으로만 판단 — files_changed는 fallback으로 allowed_files가
        # 들어올 수 있어서 신뢰하지 않음
        has_actual_changes = bool(packet.actual_diff.strip())
        if not has_actual_changes:
            return ReviewVerdict(
                verdict="NEEDS_REVISION",
                task_id=task_id,
                violations=[Violation(
                    rule="correctness.no_changes",
                    description="No actual file changes detected in git diff",
                    severity="ERROR",
                )],
                notes="No changes detected — task may not have executed properly",
            )

        # 1e. 테스트 실패 패턴
        if _detect_test_failure(packet.test_result):
            return ReviewVerdict(
                verdict="FAIL",
                task_id=task_id,
                violations=[Violation(
                    rule="correctness.tests",
                    description=f"Test failure detected: {packet.test_result[:200]}",
                    severity="ERROR",
                )],
                notes="Test failure detected",
            )

        # ── 2단계: LLM 리뷰 ──────────────────────────────────────────────
        # actual_diff 우선, fallback → code_diff (stdout)
        if packet.actual_diff.strip():
            diff_text = packet.actual_diff[:_DIFF_MAX_CHARS]
            diff_source = "actual git diff"
        else:
            diff_text = packet.code_diff[:_DIFF_MAX_CHARS]
            diff_source = "CLI stdout (actual diff unavailable)"

        notes_text = packet.agent_notes[:_NOTES_MAX_CHARS]

        git_status_section = ""
        if packet.git_status.strip():
            git_status_section = f"\n## Git Status\n```\n{packet.git_status[:500]}\n```\n"

        diff_stat_section = ""
        if packet.diff_stat.strip():
            diff_stat_section = f"\n## Diff Stats\n```\n{packet.diff_stat[:300]}\n```\n"

        user_message = (
            f"## Task ID\n{task_id}\n\n"
            f"## Task Spec (excerpt)\n{spec_context[:2000]}\n\n"
            f"## Files Changed (from git)\n{json.dumps(packet.files_changed)}\n\n"
            f"## Test Result\n{packet.test_result}\n\n"
            f"## Actual Diff [{diff_source}]\n```diff\n{diff_text}\n```\n"
            f"{git_status_section}"
            f"{diff_stat_section}"
            f"## Agent Notes\n{notes_text}\n\n"
            f"## Review Target\n{packet.review_target}"
        )

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    system=_SYSTEM_BLOCKS,
                    messages=[{"role": "user", "content": user_message}],
                )
                raw = response.content[0].text.strip()

                # JSON 파싱
                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start == -1 or end == 0:
                    raise ValueError("No JSON object in response")
                data = json.loads(raw[start:end])

                return ReviewVerdict(
                    verdict=data.get("verdict", "FAIL"),
                    task_id=task_id,
                    violations=[
                        Violation(
                            rule=v.get("rule", "unknown"),
                            description=v.get("description", ""),
                            severity=v.get("severity", "ERROR"),
                        )
                        for v in data.get("violations", [])
                    ],
                    notes=data.get("notes", ""),
                )

            except (json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
                last_error = e
                continue

        # 3회 모두 실패
        return ReviewVerdict(
            verdict="FAIL",
            task_id=task_id,
            violations=[Violation(
                rule="review.parse_error",
                description=f"JSON parse failed after 3 attempts: {last_error}",
                severity="ERROR",
            )],
            notes="Review could not be completed.",
        )
