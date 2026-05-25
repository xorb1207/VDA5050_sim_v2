from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskPacket:
    task_id: str
    title: str
    description: str
    files: list[str]
    priority: int = 0
    raw_md: str = ""


@dataclass
class CompletedPacket:
    task_id: str
    agent_id: str
    files_changed: list[str]
    code_diff: str
    test_result: str
    agent_notes: str = ""
    timestamp: str = ""
    # Phase 4: actual git diff 기반 리뷰
    actual_diff: str = ""           # git diff main..HEAD + HEAD (staged+unstaged)
    git_status: str = ""            # git status --short
    diff_stat: str = ""             # git diff --stat
    diff_numstat: str = ""          # git diff --numstat (per-file +/-)
    name_status: str = ""           # git diff --name-status (M/A/D + filename)
    review_target: str = "stdout"   # "actual_git_diff" | "stdout"


@dataclass
class Violation:
    rule: str
    description: str
    severity: str = "ERROR"


@dataclass
class FileFinding:
    """ReviewAgent의 파일/라인 수준 피드백."""
    file: str
    finding: str
    severity: str = "WARN"   # "ERROR" | "WARN" | "INFO"
    line: int = 0             # 0 = 라인 특정 안 됨


@dataclass
class ReviewVerdict:
    verdict: str
    task_id: str
    violations: list[Violation] = field(default_factory=list)
    notes: str = ""
    file_findings: list[FileFinding] = field(default_factory=list)
