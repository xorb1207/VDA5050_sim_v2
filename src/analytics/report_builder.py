from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ReportBuilder:
    """
    KPI dict + 메타 정보 → 파일 저장.
    analytics 패키지의 최종 출력 담당.
    """

    @staticmethod
    def build_kpi_report(
        kpis: dict,
        scenario_name: str,
        scenario_meta: dict,
    ) -> dict:
        """KPI + 시나리오 메타를 하나의 리포트 dict로 조립."""
        return {
            "scenario_name":    scenario_name,
            "policy":           scenario_meta,
            "kpis":             kpis,
            "generated_at":     datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def save_kpi_json(report: dict, path: Path) -> None:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    @staticmethod
    def save_node_stats(
        scheduler,
        sim_time_s: float,
        path: Path,
    ) -> None:
        """노드별 점유 시간 + congestion score 저장."""
        stats = []
        for nid, occ in scheduler._node_occupancy_time.items():
            stats.append({
                "node_id":           nid,
                "occupancy_time_s":  round(occ, 2),
                "occupancy_rate":    round(occ / sim_time_s, 4) if sim_time_s else 0.0,
                "congestion_score":  scheduler.get_congestion_score(nid),
                "reservation_failures": scheduler._congestion_counts.get(nid, 0),
            })
        stats.sort(key=lambda x: x["occupancy_time_s"], reverse=True)
        path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    @staticmethod
    def save_edge_stats(
        agvs: dict,
        sim_time_s: float,
        path: Path,
    ) -> None:
        """에지별 점유 시간 저장."""
        edge_times: dict[str, float] = {}
        for a in agvs.values():
            for eid, t in a._edge_time.items():
                edge_times[eid] = edge_times.get(eid, 0.0) + t

        stats = [
            {
                "edge_id":          eid,
                "occupancy_time_s": round(t, 2),
                "occupancy_rate":   round(t / sim_time_s, 4) if sim_time_s else 0.0,
            }
            for eid, t in sorted(edge_times.items(), key=lambda x: x[1], reverse=True)
        ]
        path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    @staticmethod
    def save_run_metadata(
        scenario_name: str,
        random_seed: int,
        runtime_seconds: int,
        tick_hz: int,
        wall_time_s: float,
        path: Path,
    ) -> None:
        meta = {
            "scenario_name":    scenario_name,
            "random_seed":      random_seed,
            "runtime_seconds":  runtime_seconds,
            "tick_hz":          tick_hz,
            "wall_time_s":      round(wall_time_s, 2),
            "finished_at":      datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
