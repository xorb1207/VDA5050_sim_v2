"""
stats_tracker.py — PM Bot 작업 통계 추적

stats.json 구조:
  {
    "global": {"shipped": N, "failed": N, "held": N, "adopted": N},
    "projects": {"vda5050": {"shipped": N, "failed": N}, ...},
    "tasks": [ {task_id, project_id, status, elapsed_s, retry_count, timestamp}, ... ]
  }

tasks 배열은 최근 500개만 유지.
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from pathlib import Path


_TASK_LIMIT = 500
_TRACKED_STATUSES = frozenset({"SHIPPED", "FAILED", "HELD", "ADOPTED"})


class StatsTracker:
    def __init__(self, stats_path: Path) -> None:
        self.stats_path = stats_path
        self._data = self._load()

    # ── 공개 API ──────────────────────────────────────────────────────

    def record(
        self,
        task_id: str,
        status: str,
        project_id: str = "",
        elapsed_s: float = 0.0,
        retry_count: int = 0,
    ) -> None:
        """태스크 결과 기록."""
        if status not in _TRACKED_STATUSES:
            return

        d = self._data
        key = status.lower()

        # 글로벌 카운터
        g = d.setdefault("global", {})
        g[key] = g.get(key, 0) + 1

        # 프로젝트별 카운터
        if project_id:
            proj = d.setdefault("projects", {}).setdefault(project_id, {})
            proj[key] = proj.get(key, 0) + 1

        # task 레코드 (최근 _TASK_LIMIT 개 유지)
        tasks: list[dict] = d.setdefault("tasks", [])
        tasks.append({
            "task_id": task_id,
            "project_id": project_id,
            "status": status,
            "elapsed_s": round(elapsed_s, 1),
            "retry_count": retry_count,
            "timestamp": datetime.now().isoformat(),
        })
        if len(tasks) > _TASK_LIMIT:
            d["tasks"] = tasks[-_TASK_LIMIT:]

        self._save()

    def get_summary(self) -> dict:
        """전체 기간 통계 집계."""
        d = self._data
        tasks: list[dict] = d.get("tasks", [])

        shipped = [t for t in tasks if t.get("status") == "SHIPPED"]
        failed  = [t for t in tasks if t.get("status") == "FAILED"]
        total_reviewed = len(shipped) + len(failed)

        avg_elapsed = (
            sum(t.get("elapsed_s", 0) for t in shipped) / len(shipped)
            if shipped else 0.0
        )
        pass_rate = (len(shipped) / total_reviewed * 100) if total_reviewed else 0.0
        avg_retries = (
            sum(t.get("retry_count", 0) for t in shipped) / len(shipped)
            if shipped else 0.0
        )

        def _recent(lst: list[dict], n: int = 5) -> list[str]:
            return [
                t["task_id"]
                for t in sorted(lst, key=lambda x: x.get("timestamp", ""), reverse=True)[:n]
            ]

        return {
            "global":          d.get("global", {}),
            "projects":        d.get("projects", {}),
            "avg_elapsed_s":   round(avg_elapsed, 1),
            "pass_rate":       round(pass_rate, 1),
            "avg_retries":     round(avg_retries, 2),
            "recent_shipped":  _recent(shipped),
            "recent_failed":   _recent(failed),
            "total_recorded":  len(tasks),
        }

    def get_today_summary(self) -> dict:
        """오늘(local date) 통계."""
        today = date.today().isoformat()
        tasks = self._data.get("tasks", [])
        today_tasks = [t for t in tasks if t.get("timestamp", "").startswith(today)]
        shipped = [t for t in today_tasks if t.get("status") == "SHIPPED"]
        failed  = [t for t in today_tasks if t.get("status") == "FAILED"]
        return {
            "shipped":     len(shipped),
            "failed":      len(failed),
            "shipped_ids": [t["task_id"] for t in shipped],
            "failed_ids":  [t["task_id"] for t in failed],
            "date":        today,
        }

    def get_period_summary(self, days: int = 7) -> dict:
        """최근 N일 통계."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        tasks = self._data.get("tasks", [])
        period_tasks = [t for t in tasks if t.get("timestamp", "") >= cutoff]
        shipped = [t for t in period_tasks if t.get("status") == "SHIPPED"]
        failed  = [t for t in period_tasks if t.get("status") == "FAILED"]
        return {
            "period_days": days,
            "shipped":     len(shipped),
            "failed":      len(failed),
        }

    # ── 내부 ─────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.stats_path.exists():
            try:
                return json.loads(self.stats_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"global": {}, "projects": {}, "tasks": []}

    def _save(self) -> None:
        try:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            self.stats_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[stats] 저장 실패: {e}")
