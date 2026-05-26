import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)


@dataclass
class Config:
    anthropic_api_key: str
    repo_path: str
    spec_path: str
    github_token: str = ""
    github_repo: str = ""
    dry_run: bool = False
    no_auto_pr: bool = False
    task_queue_dir: str = "task_queue"
    completed_dir: str = "completed"
    state_path: str = "state/git_state.json"
    logs_dir: str = "logs"
    notification_level: str = "NORMAL"
    auto_ship_after_review: bool = False
    anthropic_model: str = "claude-haiku-4-5-20251001"
    pm_dialog_model: str = "claude-sonnet-4-6"
    cli_model: str = "claude-opus-4-7"
    # Batch 6: 보존 정책
    log_retention_days: int = 30
    archive_retention_days: int = 90
    # Batch 6: Daily Report
    daily_report: bool = False
    daily_report_hour: int = 9   # 0-23
    # RC Hotfix 1: ReviewAgent 타임아웃
    review_timeout_seconds: int = 120


def load_config() -> Config:
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key:
        raise RuntimeError("Required environment variable ANTHROPIC_API_KEY is not set")

    repo_path = os.environ.get("REPO_PATH", "")
    if not repo_path:
        raise RuntimeError("Required environment variable REPO_PATH is not set")

    spec_path = os.environ.get("SPEC_PATH", "")
    if not spec_path:
        raise RuntimeError("Required environment variable SPEC_PATH is not set")

    def parse_bool(value: str, default: bool = False) -> bool:
        if not value:
            return default
        return value.strip().lower() in ("1", "true", "yes")

    def parse_int(value: str, default: int) -> int:
        try:
            return int(value.strip()) if value.strip() else default
        except ValueError:
            return default

    return Config(
        anthropic_api_key=anthropic_api_key,
        repo_path=repo_path,
        spec_path=spec_path,
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        github_repo=os.environ.get("GITHUB_REPO", ""),
        dry_run=parse_bool(os.environ.get("DRY_RUN", ""), default=False),
        no_auto_pr=parse_bool(os.environ.get("NO_AUTO_PR", ""), default=False),
        task_queue_dir=os.environ.get("TASK_QUEUE_DIR", "task_queue"),
        completed_dir=os.environ.get("COMPLETED_DIR", "completed"),
        state_path=os.environ.get("STATE_PATH", "state/git_state.json"),
        logs_dir=os.environ.get("LOGS_DIR", "logs"),
        auto_ship_after_review=parse_bool(os.environ.get("AUTO_SHIP_AFTER_REVIEW", ""), default=False),
        notification_level=os.environ.get("NOTIFICATION_LEVEL", "NORMAL"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        pm_dialog_model=os.environ.get("PM_DIALOG_MODEL", "claude-sonnet-4-6"),
        cli_model=os.environ.get("CLI_MODEL", "claude-opus-4-7"),
        log_retention_days=parse_int(os.environ.get("LOG_RETENTION_DAYS", ""), default=30),
        archive_retention_days=parse_int(os.environ.get("ARCHIVE_RETENTION_DAYS", ""), default=90),
        daily_report=parse_bool(os.environ.get("DAILY_REPORT", ""), default=False),
        daily_report_hour=parse_int(os.environ.get("DAILY_REPORT_HOUR", ""), default=9),
        review_timeout_seconds=parse_int(os.environ.get("REVIEW_TIMEOUT_SECONDS", ""), default=120),
    )
