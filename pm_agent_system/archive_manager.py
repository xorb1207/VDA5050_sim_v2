"""
archive_manager.py — 완료/실패 태스크 Archive 관리

Archive 구조:
  pm_agent_system/archive/
    {task_id}/
      meta.json          # 태스크 메타데이터 + 상태
      handoff.md         # Handoff 파일 사본
      combined.log.gz    # 로그 압축본 (gzip)
      stdout.log.gz
      *.diff             # git diff 파일

운영:
  - /archive T-ID  → 수동 archive
  - auto-archive   → SHIPPED 시 자동 호출 (orchestrator)
  - cleanup        → 시작 시 retention_days 초과분 삭제
"""
from __future__ import annotations

import gzip
import json
import shutil
import time
from datetime import datetime
from pathlib import Path


class ArchiveManager:
    def __init__(
        self,
        archive_dir: Path,
        logs_dir: Path,
        handoffs_dir: Path,
    ) -> None:
        self.archive_dir = archive_dir
        self.logs_dir = logs_dir
        self.handoffs_dir = handoffs_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    # ── 공개 API ──────────────────────────────────────────────────────

    def archive_task(
        self,
        task_id: str,
        status: str,
        metadata: dict | None = None,
    ) -> Path:
        """태스크 파일들을 archive/{task_id}/ 에 복사·압축.

        기존 archive가 있으면 meta.json만 갱신.
        Returns: archive 디렉토리 경로
        """
        task_archive = self.archive_dir / task_id
        task_archive.mkdir(parents=True, exist_ok=True)

        # meta.json 저장 (overwrite)
        meta: dict = {
            "task_id": task_id,
            "status": status,
            "archived_at": datetime.now().isoformat(),
        }
        if metadata:
            meta.update(metadata)
        (task_archive / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 로그 압축 복사
        for suffix in ("stdout.log", "stderr.log", "combined.log"):
            log_file = self.logs_dir / f"{task_id}.{suffix}"
            if log_file.exists():
                self._compress(log_file, task_archive / f"{suffix}.gz")

        # diff 파일 복사 (압축 없이)
        for ext in (".diff", ".actual.diff"):
            df = self.logs_dir / f"{task_id}{ext}"
            if df.exists():
                try:
                    shutil.copy2(df, task_archive / df.name)
                except OSError:
                    pass

        # handoff 복사
        handoff = self.handoffs_dir / f"{task_id}.md"
        if handoff.exists():
            try:
                shutil.copy2(handoff, task_archive / "handoff.md")
            except OSError:
                pass

        return task_archive

    def is_archived(self, task_id: str) -> bool:
        """해당 task_id가 이미 archive에 있는지 확인."""
        return (self.archive_dir / task_id / "meta.json").exists()

    def get_meta(self, task_id: str) -> dict | None:
        """archive된 task의 meta 반환. 없으면 None."""
        meta_file = self.archive_dir / task_id / "meta.json"
        if not meta_file.exists():
            return None
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_history(self, limit: int = 30, status_filter: str | None = None) -> list[dict]:
        """archive 목록 반환 (최신순).

        status_filter: "SHIPPED" / "FAILED" / None(전체)
        """
        results: list[dict] = []
        for meta_file in sorted(
            self.archive_dir.glob("*/meta.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                if status_filter is None or data.get("status") == status_filter:
                    results.append(data)
            except Exception:
                pass
        return results[:limit]

    def cleanup_old_archives(self, retention_days: int) -> list[str]:
        """retention_days보다 오래된 archive 삭제."""
        cutoff = time.time() - (retention_days * 86_400)
        removed: list[str] = []
        for task_dir in self.archive_dir.iterdir():
            if not task_dir.is_dir():
                continue
            try:
                if task_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(task_dir)
                    removed.append(task_dir.name)
            except OSError:
                pass
        return removed

    def cleanup_old_logs(self, retention_days: int) -> list[str]:
        """logs_dir에서 retention_days 초과 로그 파일 삭제."""
        cutoff = time.time() - (retention_days * 86_400)
        removed: list[str] = []
        for log_file in self.logs_dir.glob("*.log"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    removed.append(log_file.name)
            except OSError:
                pass
        # .diff 파일도 동일 정책
        for diff_file in self.logs_dir.glob("*.diff"):
            try:
                if diff_file.stat().st_mtime < cutoff:
                    diff_file.unlink()
                    removed.append(diff_file.name)
            except OSError:
                pass
        return removed

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────

    @staticmethod
    def _compress(src: Path, dst: Path) -> None:
        """src를 gzip 압축하여 dst에 저장."""
        try:
            with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        except Exception:
            pass
