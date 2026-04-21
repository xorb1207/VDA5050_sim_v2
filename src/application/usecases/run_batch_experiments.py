from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from src.application.scenario.experiment_matrix import ExperimentMatrix
from src.application.scenario.scenario import Scenario
from src.application.scenario.scenario_runner import ScenarioRunner


# summary.csv 컬럼 순서
SUMMARY_COLUMNS = [
    "scenario_id",
    "map_file",
    "fleet_size",
    "lane_mode",
    "lane_count",
    "reservation_mode",
    "lookahead_depth",
    "random_seed",
    "throughput_tasks_per_hour",
    "tasks_completed",
    "avg_task_completion_time_s",
    "avg_wait_time_s",
    "total_wait_time_s",
    "reservation_failure_rate",
    "node_occupancy_rate",
    "edge_occupancy_rate",
    "agv_utilization",
    "reroute_count",
    "deadlock_or_stall_count",
    "max_queue_length",
    "total_travel_distance_m",
    "top_bottleneck_node",
    "top_bottleneck_edge",
    "sim_time_s",
]


class BatchRunner:
    """
    ExperimentMatrix → 전체 시나리오 순차 실행 → 결과 저장.
    각 시나리오: outputs/{dir}/scenario_NNN/kpi_summary.json
    전체 요약:   outputs/{dir}/summary.csv + summary.json
    """

    def __init__(self, experiment_yaml: str) -> None:
        self.matrix     = ExperimentMatrix.from_yaml(experiment_yaml)
        self.output_dir = Path(self.matrix.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def run(self) -> Path:
        """전체 실행. summary.csv 경로 반환."""
        scenarios = self.matrix.generate_scenarios()
        total     = len(scenarios)

        print(f"\n[Batch] {self.matrix.name}")
        print(f"[Batch] 총 {total}개 시나리오 실행 시작\n")

        all_results = []

        for idx, scenario in enumerate(scenarios, 1):
            scenario_id = f"scenario_{idx:03d}"
            print(f"  [{idx:>3}/{total}] {scenario_id} — {scenario.description}")

            t0 = time.perf_counter()
            result = await ScenarioRunner.run(scenario)
            elapsed = time.perf_counter() - t0

            result["scenario_id"] = scenario_id
            result["wall_time_s"] = round(elapsed, 2)

            self._save_scenario(scenario_id, scenario, result)
            all_results.append(result)

            print(
                f"         done in {elapsed:.1f}s — "
                f"throughput={result['throughput_tasks_per_hour']} tasks/hr  "
                f"wait={result['total_wait_time_s']}s"
            )

        summary_csv  = self._save_summary_csv(all_results)
        summary_json = self._save_summary_json(all_results)

        print(f"\n[Batch] 완료")
        print(f"  summary.csv  → {summary_csv}")
        print(f"  summary.json → {summary_json}")
        return summary_csv

    # ── 저장 ──────────────────────────────────────────────────────

    def _save_scenario(
        self, scenario_id: str, scenario: Scenario, result: dict
    ) -> None:
        """시나리오별 디렉토리에 kpi_summary.json + run_metadata.json 저장."""
        sdir = self.output_dir / scenario_id
        sdir.mkdir(exist_ok=True)

        from src.analytics.report_builder import ReportBuilder

        # kpi_summary.json
        policy_meta = {
            "fleet_size":       scenario.fleet_size,
            "lane_mode":        scenario.traffic_policy.lane_mode,
            "lane_count":       scenario.traffic_policy.lane_count,
            "reservation_mode": scenario.traffic_policy.reservation_mode,
            "lookahead_depth":  scenario.traffic_policy.lookahead_depth,
        }
        report = ReportBuilder.build_kpi_report(result, scenario_id, policy_meta)
        ReportBuilder.save_kpi_json(report, sdir / "kpi_summary.json")

        # run_metadata.json
        ReportBuilder.save_run_metadata(
            scenario_name=scenario_id,
            random_seed=scenario.random_seed,
            runtime_seconds=scenario.runtime_seconds,
            tick_hz=scenario.tick_hz,
            wall_time_s=result.get("wall_time_s", 0.0),
            path=sdir / "run_metadata.json",
        )

    def _save_summary_csv(self, results: list[dict]) -> Path:
        path = self.output_dir / "summary.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for r in results:
                bn = r.get("bottleneck_nodes", [])
                be = r.get("bottleneck_edges", [])
                row = dict(r)
                row["map_file"]            = self.matrix.base_map_file
                row["top_bottleneck_node"] = bn[0]["node_id"] if bn else ""
                row["top_bottleneck_edge"] = be[0]["edge_id"] if be else ""
                writer.writerow(row)
        return path

    def _save_summary_json(self, results: list[dict]) -> Path:
        path = self.output_dir / "summary.json"
        path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        return path


async def run_batch(experiment_yaml: str) -> None:
    runner = BatchRunner(experiment_yaml)
    await runner.run()
