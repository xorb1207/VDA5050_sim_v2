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


# ── watcher 안전 검사 ──────────────────────────────────────────────────────

def is_safe_queue_file(path: Path, min_size: int = MIN_BODY_CHARS) -> bool:
    """watchdog이 실행해도 안전한 .md 파일인지 확인.

    조건:
    - suffix == ".md"
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
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    return size >= min_size
