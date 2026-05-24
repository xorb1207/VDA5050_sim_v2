import json
import anthropic

from schemas import CompletedPacket, ReviewVerdict, Violation


_SYSTEM_PROMPT = """\
You are a strict code review agent. Given a spec context, a code diff, and test results,
evaluate whether the changes are correct, complete, and within scope.

Respond ONLY with a valid JSON object in this exact format:
{
  "verdict": "PASS" | "FAIL" | "NEEDS_REVISION",
  "violations": [
    {
      "rule": "<rule_id>",
      "description": "<what went wrong>",
      "severity": "ERROR" | "WARN"
    }
  ],
  "notes": "<overall review summary>"
}

Rules to check:
- correctness.logic: Does the implementation match the spec?
- correctness.tests: Do tests pass and adequately cover the changes?
- quality.code_style: Is the code clean and maintainable?
- scope.unrelated_changes: Are there changes unrelated to the task?
"""


class ReviewAgent:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)

    async def review(
        self,
        spec_context: str,
        completed_packet: CompletedPacket,
        allowed_files: list[str] | None = None,
    ) -> ReviewVerdict:
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
                        severity="ERROR"
                    )]
                )

        user_message = f"""\
## Spec Context
{spec_context}

## Task ID
{completed_packet.task_id}

## Files Changed
{json.dumps(completed_packet.files_changed, ensure_ascii=False, indent=2)}

## Code Diff
```diff
{completed_packet.code_diff}
```

## Test Result
{completed_packet.test_result}

## Agent Notes
{completed_packet.agent_notes}
"""

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                raw = response.content[0].text.strip()

                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start == -1 or end == 0:
                    raise ValueError("No JSON object found in response")
                data = json.loads(raw[start:end])

                verdict = data.get("verdict", "FAIL")
                notes = data.get("notes", "")
                violations = [
                    Violation(
                        rule=v.get("rule", "unknown"),
                        description=v.get("description", ""),
                        severity=v.get("severity", "ERROR"),
                    )
                    for v in data.get("violations", [])
                ]

                return ReviewVerdict(
                    verdict=verdict,
                    task_id=completed_packet.task_id,
                    violations=violations,
                    notes=notes,
                )

            except (json.JSONDecodeError, ValueError, KeyError) as e:
                last_error = e
                continue

        return ReviewVerdict(
            verdict="FAIL",
            task_id=completed_packet.task_id,
            violations=[Violation(
                rule="review.parse_error",
                description=f"Failed to parse LLM response after 3 attempts: {last_error}",
                severity="ERROR"
            )],
            notes="Review could not be completed due to repeated JSON parse failures.",
        )
