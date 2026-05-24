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
