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
    actual_diff: str = ""           # git diff HEAD (staged+unstaged)
    git_status: str = ""            # git status --short
    diff_stat: str = ""             # git diff --stat HEAD
    diff_numstat: str = ""          # git diff --numstat HEAD (per-file +/-)
    name_status: str = ""           # git diff --name-status HEAD (M/A/D + filename)
    review_target: str = "stdout"   # "actual_git_diff" | "stdout"


@dataclass
class Violation:
    rule: str
    description: str
    severity: str = "ERROR"


@dataclass
class ReviewVerdict:
    verdict: str
    task_id: str
    violations: list[Violation] = field(default_factory=list)
    notes: str = ""
