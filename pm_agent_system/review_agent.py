"""
review_agent.py — 코드 리뷰 에이전트

비용 최적화:
  - AsyncAnthropic 사용 (이벤트루프 블로킹 해소)
  - system prompt에 cache_control: ephemeral 적용 (매 리뷰 ~300 토큰 절약)
  - Pre-LLM scope check: 파일 범위 위반은 API 호출 없이 즉시 FAIL
  - code_diff: 4,000자 제한 (CLI stdout 노이즈 제거)
  - agent_notes: 1,000자 제한
"""
from __future__ import annotations

import json

import anthropic

from schemas import CompletedPacket, ReviewVerdict, Violation


# ★ cache_control 적용: system prompt는 매 리뷰 동일 → 캐시 히트 시 ~300 토큰 절약
_SYSTEM_BLOCKS = [
    {
        "type": "text",
        "text": """\
You are a strict code review agent. Given a task spec, code diff, and test results,
evaluate whether the changes are correct, complete, and within scope.

Respond ONLY with a valid JSON object:
{
  "verdict": "PASS" | "FAIL" | "NEEDS_REVISION",
  "violations": [
    {"rule": "<rule_id>", "description": "<what went wrong>", "severity": "ERROR" | "WARN"}
  ],
  "notes": "<one-line summary>"
}

Rules:
- correctness.logic: Does the implementation match the task spec?
- correctness.tests: Do tests pass?
- scope.unrelated_changes: Are there changes unrelated to the task?
""",
        "cache_control": {"type": "ephemeral"},
    }
]

# code_diff / agent_notes 크기 상한 (CLI stdout 노이즈 제거)
_DIFF_MAX_CHARS = 4_000
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

        1단계: Pre-LLM scope check (API 호출 없음, 즉시 FAIL)
        2단계: LLM 리뷰 (최대 3회 재시도)
        """
        # ── 1단계: scope check (zero cost) ───────────────────────────
        if allowed_files:
            allowed_set = {f.strip() for f in allowed_files}
            scope_violations = [
                f for f in completed_packet.files_changed
                if f.strip() not in allowed_set
            ]
            if scope_violations:
                return ReviewVerdict(
                    verdict="FAIL",
                    task_id=completed_packet.task_id,
                    violations=[Violation(
                        rule="scope.files_outside_task",
                        description=f"Files changed outside task scope: {scope_violations}",
                        severity="ERROR",
                    )],
                )

        # ── 2단계: LLM 리뷰 ──────────────────────────────────────────
        # code_diff / agent_notes 크기 제한 (CLI stdout 노이즈 방지)
        diff_text = completed_packet.code_diff[:_DIFF_MAX_CHARS]
        notes_text = completed_packet.agent_notes[:_NOTES_MAX_CHARS]

        user_message = (
            f"## Task ID\n{completed_packet.task_id}\n\n"
            f"## Task Spec (excerpt)\n{spec_context[:2000]}\n\n"
            f"## Files Changed\n{json.dumps(completed_packet.files_changed)}\n\n"
            f"## Test Result\n{completed_packet.test_result}\n\n"
            f"## Code Diff\n```diff\n{diff_text}\n```\n\n"
            f"## Agent Notes\n{notes_text}"
        )

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=512,   # 리뷰 응답은 JSON만 → 512 충분
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
                    task_id=completed_packet.task_id,
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
            task_id=completed_packet.task_id,
            violations=[Violation(
                rule="review.parse_error",
                description=f"JSON parse failed after 3 attempts: {last_error}",
                severity="ERROR",
            )],
            notes="Review could not be completed.",
        )
