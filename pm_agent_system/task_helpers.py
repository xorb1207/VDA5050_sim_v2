"""PM Bot V3 — task_inbox 헬퍼 함수.

slugify / task_id 생성 / atomic write / frontmatter 파싱·업데이트.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # PyYAML (python-telegram-bot 의존성으로 이미 설치됨)

# ── slugify ────────────────────────────────────────────────────────────────

def slugify_title(title: str, max_len: int = 60) -> str:
    """제목을 URL-safe 슬러그로 변환.

    예: "RMF building_map YAML import/export" → "rmf-building-map-yaml-import-export"
    """
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)       # 특수문자 제거 (하이픈·언더스코어 유지)
    slug = re.sub(r"[\s_]+", "-", slug)         # 공백·언더스코어 → 하이픈
    slug = re.sub(r"-+", "-", slug)             # 연속 하이픈 정리
    slug = slug.strip("-")
    return slug[:max_len]


def make_task_id(title: str, now: datetime | None = None) -> str:
    """타임스탬프 + 슬러그 조합의 task_id 생성.

    예: "2026-05-26_2001_rmf-building-map-yaml-import-export"
    """
    if now is None:
        now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H%M")
    slug = slugify_title(title)
    return f"{ts}_{slug}"


# ── atomic write ───────────────────────────────────────────────────────────

def write_atomic(path: Path, content: str, encoding: str = "utf-8") -> None:
    """content를 path에 원자적으로 기록한다.

    임시 파일(.tmp)에 먼저 쓴 뒤 rename — watchdog이 부분 기록된 파일을 읽지 않도록 방지.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding=encoding)
        tmp_path.rename(path)
    except Exception:
        # 실패 시 tmp 파일 정리
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── frontmatter 파싱 / 업데이트 ────────────────────────────────────────────

_FM_DELIMITER = "---"


def parse_task_frontmatter(path: Path) -> dict[str, Any]:
    """마크다운 파일에서 YAML frontmatter를 파싱해 딕셔너리로 반환.

    frontmatter 없으면 빈 딕셔너리 반환.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    if not text.startswith("---"):
        return {}

    # 두 번째 '---' 구분자 찾기
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}

    fm_text = text[3:end_idx].strip()
    try:
        return yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}


def update_task_frontmatter(path: Path, updates: dict[str, Any]) -> str:
    """파일의 frontmatter를 updates로 병합·갱신하고 전체 문자열 반환.

    반환값: 갱신된 파일 전체 내용 (파일은 직접 쓰지 않음 — 호출자가 write_atomic 사용).
    """
    text = path.read_text(encoding="utf-8")

    if text.startswith("---"):
        end_idx = text.find("\n---", 3)
        if end_idx != -1:
            fm_text = text[3:end_idx].strip()
            body = text[end_idx + 4:]  # '\n---' 이후
            try:
                fm = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                fm = {}
        else:
            fm = {}
            body = text
    else:
        fm = {}
        body = text

    fm.update(updates)
    fm_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip()
    return f"---\n{fm_yaml}\n---\n{body.lstrip()}"


# ── pending 태스크 파일 생성 ────────────────────────────────────────────────

_BANNED_KEYWORDS = frozenset({
    ".env", "credentials", "secrets", "api_key", "password", "token",
    "private_key", "secret_key",
})

MIN_BODY_CHARS = 100


def build_pending_content(
    task_id: str,
    title: str,
    body: str,
    created_from: str = "telegram",
    priority: str = "medium",
    now: datetime | None = None,
) -> str:
    """pending 상태의 마크다운 파일 내용을 생성한다.

    frontmatter + body 구조로 반환.
    """
    if now is None:
        now = datetime.now().astimezone()

    fm: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "status": "pending",
        "priority": priority,
        "created_from": created_from,
        "approved": False,
        "created_at": now.isoformat(),
    }
    fm_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip()

    # body에 이미 '# Task:' 헤딩이 있으면 그대로, 없으면 자동 추가
    stripped_body = body.strip()
    if not stripped_body.startswith("#"):
        stripped_body = f"# Task: {title}\n\n{stripped_body}"

    return f"---\n{fm_yaml}\n---\n\n{stripped_body}\n"


def preflight_check(title: str, body: str) -> list[str]:
    """Claude 실행 전 안전 검사. 문제 항목 메시지 목록 반환 (비면 OK).

    검사 항목:
    1. title 존재
    2. body 최소 길이 (MIN_BODY_CHARS)
    3. 금지 파일/키워드 패턴
    """
    errors: list[str] = []

    if not title.strip():
        errors.append("제목(title)이 비어 있습니다.")

    if len(body.strip()) < MIN_BODY_CHARS:
        errors.append(
            f"작업 내용이 너무 짧습니다 (최소 {MIN_BODY_CHARS}자, 현재 {len(body.strip())}자)."
        )

    combined = (title + " " + body).lower()
    for kw in _BANNED_KEYWORDS:
        if kw in combined:
            errors.append(f"금지 키워드 포함: '{kw}' — 민감 정보가 노출될 수 있습니다.")
            break  # 첫 번째만 보고

    return errors


# ── inbox 파일 파싱 (local drop 지원) ─────────────────────────────────────

# inbox에서 제외할 파일 패턴 (.tmp / backup / swap 등)
_INBOX_SKIP_RE = re.compile(r"\.(tmp|bak|swp|orig|back)$|~$", re.IGNORECASE)


def is_skip_inbox_file(path: Path) -> bool:
    """inbox에서 무시해야 할 파일 (숨김 / tmp / backup)."""
    name = path.name
    if name.startswith("."):
        return True
    return bool(_INBOX_SKIP_RE.search(name))


def extract_title_from_body(text: str, fallback: str = "") -> str:
    """마크다운 본문에서 첫 번째 # heading 추출. 없으면 fallback 반환."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return fallback


def slug_to_title(name: str) -> str:
    """파일명 슬러그 → 사람이 읽을 수 있는 제목.

    예: "2026-05-30_001_rmf-yaml-import.md" → "rmf yaml import"
        "my-feature-task.md"               → "my feature task"
    """
    stem = Path(name).stem if name.endswith(".md") else name
    # 날짜·숫자 접두어 제거 (예: 2026-05-30_001_ / 01_)
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}_\d+_?", "", stem)
    stem = re.sub(r"^\d+_?", "", stem)
    title = stem.replace("-", " ").replace("_", " ").strip()
    return title or stem


def parse_inbox_file(path: Path) -> dict[str, Any]:
    """task_inbox 파일 파싱. frontmatter 없어도 OK.

    Returns dict keys:
        task_id, title, status, priority, source, created_at, filename,
        path, has_frontmatter, is_valid, invalid_reason, body_preview
    """
    result: dict[str, Any] = {
        "task_id":        path.stem,
        "title":          slug_to_title(path.name),
        "status":         "pending",
        "priority":       "medium",
        "source":         "local_drop",
        "created_at":     "",
        "filename":       path.name,
        "path":           path,
        "has_frontmatter": False,
        "is_valid":       True,
        "invalid_reason": None,
        "body_preview":   "",
    }

    # 파일 읽기
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        result["is_valid"] = False
        result["invalid_reason"] = f"파일 읽기 실패: {exc}"
        return result

    if not text.strip():
        result["is_valid"] = False
        result["invalid_reason"] = "빈 파일"
        return result

    if len(text.strip()) < MIN_BODY_CHARS:
        result["is_valid"] = False
        result["invalid_reason"] = (
            f"내용이 너무 짧음 ({len(text.strip())}자 < {MIN_BODY_CHARS}자 최소)"
        )

    body = text

    # frontmatter 파싱 시도
    if text.startswith("---"):
        end_idx = text.find("\n---", 3)
        if end_idx != -1:
            fm_text = text[3:end_idx].strip()
            try:
                fm = yaml.safe_load(fm_text) or {}
                result["has_frontmatter"] = True
                # id / task_id 두 키 모두 지원
                tid = fm.get("id") or fm.get("task_id") or ""
                if tid:
                    result["task_id"] = str(tid)
                result["title"]      = fm.get("title",        result["title"])
                result["status"]     = fm.get("status",       "pending")
                result["priority"]   = fm.get("priority",     "medium")
                result["source"]     = (
                    fm.get("created_from") or fm.get("source") or "local_drop"
                )
                result["created_at"] = str(fm.get("created_at", ""))
                body = text[end_idx + 4:]
            except yaml.YAMLError:
                result["has_frontmatter"] = False
                result["is_valid"] = False
                result["invalid_reason"] = "YAML frontmatter 파싱 실패"

    # title 보강: frontmatter에 없거나 local_drop이면 heading에서 추출
    if not result["has_frontmatter"] or result["source"] == "local_drop":
        heading = extract_title_from_body(body)
        if heading:
            result["title"] = heading

    # body preview (앞 500자)
    result["body_preview"] = body.strip()[:500]

    return result


# ── watcher 안전 검사 ──────────────────────────────────────────────────────

# 이미 처리된 파일임을 나타내는 중간 확장자 패턴
_PROCESSED_SUFFIXES = (".cancelled.md", ".done.md", ".failed.md", ".done", ".cancelled", ".failed")


def is_processed_queue_file(path: Path) -> bool:
    """이미 처리 완료/취소된 task_queue 파일 여부."""
    name = path.name
    return any(name.endswith(s) for s in _PROCESSED_SUFFIXES)


def is_safe_queue_file(path: Path, min_size: int = MIN_BODY_CHARS) -> bool:
    """watchdog이 실행해도 안전한 .md 파일인지 확인.

    조건:
    - suffix == ".md" (단 .cancelled.md / .done.md 제외)
    - 숨김 파일(.)로 시작하지 않음
    - .tmp 포함 아님
    - 파일 크기 >= min_size
    """
    name = path.name
    if not name.endswith(".md"):
        return False
    if name.startswith("."):
        return False
    if ".tmp" in name:
        return False
    # 이미 처리된 파일 제외 (.cancelled.md, .done.md)
    if is_processed_queue_file(path):
        return False
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    return size >= min_size


# ── 처리 상태 rename 헬퍼 ──────────────────────────────────────────────────

_VALID_STATUSES = frozenset({"done", "failed", "cancelled"})

# .md 파일에서 상태 suffix를 추출하는 패턴: "foo.done.md" → "done"
_STATUS_SUFFIX_RE = re.compile(
    r"\.(" + "|".join(_VALID_STATUSES) + r")\.md$"
)


def mark_queue_file_status(path: Path, status: str) -> Path:
    """task_queue 파일에 상태 suffix를 붙여 rename하고 새 경로를 반환.

    Examples:
        01_T-RC1.md          → 01_T-RC1.failed.md
        resume_T-91.md       → resume_T-91.failed.md
        01_T-91.failed.md    → 그대로 (이미 .failed.md)
        01_T-91.done.md      → 01_T-91.failed.md  (status 교체)

    Args:
        path:   현재 파일 경로 (존재해야 함)
        status: "done" | "failed" | "cancelled"

    Returns:
        rename 후 새 경로. rename 실패 시 원래 path 반환.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {_VALID_STATUSES}, got {status!r}")

    name = path.name

    # 이미 같은 status suffix면 그대로
    if name.endswith(f".{status}.md"):
        return path

    # base 추출: 처리 상태 suffix를 제거한 순수 stem
    # 우선 "foo.done.md" / "foo.cancelled.md" / "foo.failed.md" 패턴 처리
    if _STATUS_SUFFIX_RE.search(name):
        base = _STATUS_SUFFIX_RE.sub("", name)    # "foo.done.md" → "foo"
    elif name.endswith(".md"):
        base = name[:-3]                           # "01_T-RC1.md" → "01_T-RC1"
    else:
        base = name                                # fallback

    new_name = f"{base}.{status}.md"
    new_path = path.with_name(new_name)

    try:
        path.rename(new_path)
        return new_path
    except OSError:
        return path
