"""
ExperimentRunner — 토폴로지 5종 × AGV 대수 매트릭스 실험

실행:
  python -m src.application.usecases.experiment_runner
  python -m src.application.usecases.experiment_runner --types A B C --agv 3 5 8 --duration 600
  python -m src.application.usecases.experiment_runner --experiment experiments/fab_topology.yaml

출력:
  outputs/experiments/{run_id}/
    summary.csv
    summary.json
    {type}_{n_agv}/
      kpi_summary.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.domain.map.graph import MapGraph, NodeRole
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler
from src.application.scenario.demand import DemandSet
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.adapters.bus.adapters import LocalMemoryBus
from src.domain.agv.agv import AGV


# ── 실험 파라미터 ──────────────────────────────────────────────
@dataclass
class ExperimentConfig:
    types: list[str]          = field(default_factory=lambda: ["A","B","C","D","E"])
    agv_counts: list[int]     = field(default_factory=lambda: [3, 5, 8, 12])
    duration_s: float         = 600.0
    task_interval_s: float    = 5.0
    random_seed: int          = 42
    random_seeds: list[int] | None = None
    demand_mode: str          = "generated"
    demand_count: Optional[int] = None
    output_dir: str           = "outputs/experiments"
    run_id: Optional[str]     = None

    def __post_init__(self):
        if self.run_id is None:
            self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if self.random_seeds is None:
            self.random_seeds = [self.random_seed]
        elif not self.random_seeds:
            raise ValueError("random_seeds must not be empty")


# ── 단일 실험 결과 ─────────────────────────────────────────────
@dataclass
class RunResult:
    topology_type: str
    n_agv: int
    random_seed: int = 42
    demand_mode: str = "generated"
    # KPI
    tasks_completed: int            = 0
    demands_completed: int          = 0
    demand_completion_rate: float   = 0.0
    demand_throughput_per_hour: float = 0.0
    throughput_tasks_per_hour: float = 0.0
    avg_task_completion_time_s: float = 0.0
    avg_wait_time_s: float          = 0.0
    total_wait_time_s: float        = 0.0
    reservation_failure_rate: float = 0.0
    reroute_count: int              = 0
    node_occupancy_rate: float      = 0.0
    edge_occupancy_rate: float      = 0.0
    agv_utilization: float          = 0.0
    total_travel_distance_m: float  = 0.0
    max_queue_length: int           = 0
    headon_total: int               = 0
    followon_total: int             = 0
    retry_total: int                = 0
    itinerary_success: int          = 0
    itinerary_failure: int          = 0
    avg_retry_per_headon: float     = 0.0
    top_bottleneck_node: str        = ""
    top_bottleneck_score: float     = 0.0
    top_headon_edge: str            = ""
    top_headon_edge_count: int      = 0
    diagnostics: dict               = field(default_factory=dict)
    sim_time_s: float               = 0.0
    wall_time_s: float              = 0.0
    error: str                      = ""


SUMMARY_COLUMNS = [
    "topology_type", "n_agv", "random_seed",
    "demand_mode",
    "tasks_completed", "demands_completed",
    "demand_completion_rate", "demand_throughput_per_hour",
    "throughput_tasks_per_hour",
    "avg_task_completion_time_s", "avg_wait_time_s", "total_wait_time_s",
    "reservation_failure_rate", "reroute_count",
    "node_occupancy_rate", "edge_occupancy_rate", "agv_utilization",
    "total_travel_distance_m", "max_queue_length",
    "headon_total", "followon_total", "retry_total",
    "itinerary_success", "itinerary_failure", "avg_retry_per_headon",
    "top_bottleneck_node", "top_bottleneck_score",
    "top_headon_edge", "top_headon_edge_count",
    "tasks_requested", "tasks_dispatched", "tasks_rejected_unreachable",
    "tasks_backlogged", "task_acceptance_rate", "completion_rate",
    "orders_published", "routeable_pair_count",
    "no_idle_agv", "not_enough_available_nodes", "no_routeable_available_pair",
    "no_routeable_current_pair",
    "no_path_to_pickup", "no_path_pickup_to_dropoff",
    "station_unreachable_from_start_count",
    "station_pair_unreachable_count", "station_min_access_edges",
    "siding_coverage_ratio",
    "sim_time_s", "wall_time_s", "error",
]


# ── 단일 시나리오 실행 ─────────────────────────────────────────
async def _run_single(
    topology_type: str,
    n_agv: int,
    duration_s: float,
    task_interval_s: float,
    random_seed: int = 42,
    demand_mode: str = "generated",
    demand_count: int | None = None,
) -> RunResult:
    result = RunResult(
        topology_type=topology_type,
        n_agv=n_agv,
        random_seed=random_seed,
        demand_mode=demand_mode,
    )
    t0 = time.perf_counter()

    try:
        random.seed(random_seed)
        gen_map = MapTopologyGenerator()
        graph   = gen_map.generate(topology_type)
        bus     = LocalMemoryBus()
        sched   = TimeWindowScheduler()
        demand_set = _build_demand_set(
            graph=graph,
            mode=demand_mode,
            duration_s=duration_s,
            interval_s=task_interval_s,
            random_seed=random_seed,
            demand_count=demand_count,
        )
        task_gen = TaskGenerator(
            graph,
            bus,
            task_interval_s=task_interval_s,
            demand_set=demand_set,
        )

        # Type E: graph._lane_mode 태그로 creep policy 자동 주입
        # (agv._get_effective_speed()가 graph._lane_mode를 직접 읽음)
        engine  = SimulationEngine(graph, sched, task_generator=task_gen)

        # AGV 초기 배치 — 충전소 우선, 부족하면 웨이포인트로 채움
        charger_nodes = [n.node_id for n in graph.get_chargers()]
        wp_nodes = [
            nid for nid, n in graph.nodes.items()
            if n.role == NodeRole.STANDARD and nid.startswith("WP_")
        ]
        start_pool = charger_nodes + wp_nodes
        if not start_pool:
            raise ValueError("no start nodes available")
        start_nodes = [start_pool[i % len(start_pool)] for i in range(n_agv)]

        for i, start_node in enumerate(start_nodes):
            agv = AGV(f"AGV_{i+1:03d}", bus, graph, sched)
            agv.current_node_id = start_node
            node = graph.nodes[start_node]
            agv.physics.x = node.x
            agv.physics.y = node.y
            engine.register_agv(agv)

        kpis = await engine.run(duration_s=duration_s)

        # KPI 매핑
        result.tasks_completed             = kpis.get("tasks_completed", 0)
        result.throughput_tasks_per_hour   = kpis.get("throughput_tasks_per_hour", 0.0)
        result.avg_task_completion_time_s  = kpis.get("avg_task_completion_time_s", 0.0)
        result.avg_wait_time_s             = kpis.get("avg_wait_time_s", 0.0)
        result.total_wait_time_s           = kpis.get("total_wait_time_s", 0.0)
        result.reservation_failure_rate    = kpis.get("reservation_failure_rate", 0.0)
        result.reroute_count               = kpis.get("reroute_count", 0)
        result.node_occupancy_rate         = kpis.get("node_occupancy_rate", 0.0)
        result.edge_occupancy_rate         = kpis.get("edge_occupancy_rate", 0.0)
        result.agv_utilization             = kpis.get("agv_utilization", 0.0)
        result.total_travel_distance_m     = kpis.get("total_travel_distance_m", 0.0)
        result.max_queue_length            = kpis.get("max_queue_length", 0)
        result.sim_time_s                  = kpis.get("sim_time_s", 0.0)

        bn = kpis.get("bottleneck_nodes", [])
        if bn:
            result.top_bottleneck_node  = bn[0].get("node_id", "")
            result.top_bottleneck_score = bn[0].get("congestion_score", 0.0)

        # head-on 분석
        result.headon_total          = kpis.get("headon_total", 0)
        result.followon_total        = kpis.get("followon_total", 0)
        result.retry_total           = kpis.get("retry_total", 0)
        result.itinerary_success     = kpis.get("itinerary_success", 0)
        result.itinerary_failure     = kpis.get("itinerary_failure", 0)
        result.avg_retry_per_headon  = kpis.get("avg_retry_per_headon", 0.0)
        top_headon_edges = kpis.get("top_headon_edges", [])
        if top_headon_edges:
            result.top_headon_edge       = top_headon_edges[0]["edge"]
            result.top_headon_edge_count = top_headon_edges[0]["count"]

        result.diagnostics = {
            "registered_agvs": len(engine.agvs),
            "requested_agvs": n_agv,
            "demand_mode": demand_mode,
            "demand_set": demand_set.to_dict() if demand_set else None,
            "task_generation": task_gen.diagnostics,
            "station_access": _build_station_access_diagnostics(graph),
            "siding_coverage": _build_siding_coverage_diagnostics(graph),
        }
        task_diag = result.diagnostics["task_generation"]
        result.demands_completed = task_diag.get("demands_completed", 0)
        requested = task_diag.get("tasks_requested", 0)
        result.demand_completion_rate = (
            round(result.demands_completed / requested, 4)
            if requested else 0.0
        )
        result.demand_throughput_per_hour = (
            round(result.demands_completed / duration_s * 3600.0, 3)
            if duration_s > 0 else 0.0
        )

    except Exception as e:
        result.error = str(e)

    result.wall_time_s = round(time.perf_counter() - t0, 2)
    return result


def _build_demand_set(
    graph: MapGraph,
    mode: str,
    duration_s: float,
    interval_s: float,
    random_seed: int,
    demand_count: int | None,
) -> DemandSet | None:
    if mode == "generated":
        return None

    count = demand_count
    if count is None:
        count = int(duration_s // max(interval_s, 0.001)) + 1

    if mode == "common_demand":
        return DemandSet.common_from_graph(
            graph,
            count=count,
            interval_s=interval_s,
            random_seed=random_seed,
        )
    if mode == "capability":
        return DemandSet.capability_from_graph(
            graph,
            count=count,
            interval_s=interval_s,
            random_seed=random_seed,
        )
    raise ValueError(f"unknown demand_mode: {mode}")


def _build_station_access_diagnostics(graph: MapGraph) -> dict:
    stations = graph.get_stations()
    unreachable: list[str] = []
    access_edge_counts: dict[str, int] = {}
    station_details: list[dict] = []

    charger_nodes = [n.node_id for n in graph.get_chargers()]
    fallback_start = next(iter(graph.nodes), "")
    start = charger_nodes[0] if charger_nodes else fallback_start

    for station in stations:
        inbound = [
            e for e in graph.edges.values()
            if e.end_node_id == station.node_id
        ]
        outbound = [
            e for e in graph.edges.values()
            if e.start_node_id == station.node_id
        ]
        access_edge_counts[station.node_id] = len(inbound) + len(outbound)
        if start and not graph.get_path(start, station.node_id):
            unreachable.append(station.node_id)
        station_details.append({
            "station_id": station.node_id,
            "inbound_edges": len(inbound),
            "outbound_edges": len(outbound),
            "access_edges": len(inbound) + len(outbound),
        })

    pair_failures: list[dict] = []
    for src in stations:
        for dst in stations:
            if src.node_id == dst.node_id:
                continue
            if not graph.get_path(src.node_id, dst.node_id):
                pair_failures.append({
                    "src": src.node_id,
                    "dst": dst.node_id,
                })

    return {
        "station_count": len(stations),
        "reachable_from_first_charger": len(stations) - len(unreachable),
        "unreachable_from_first_charger_count": len(unreachable),
        "unreachable_from_first_charger": unreachable[:10],
        "station_pair_unreachable_count": len(pair_failures),
        "station_pair_unreachable_samples": pair_failures[:10],
        "min_access_edges": min(access_edge_counts.values(), default=0),
        "station_details": sorted(
            station_details,
            key=lambda x: (x["access_edges"], x["station_id"]),
        ),
    }


def _build_siding_coverage_diagnostics(graph: MapGraph) -> dict:
    siding_nodes = [
        nid for nid, node in graph.nodes.items()
        if node.role == NodeRole.SIDING
    ]
    main_nodes = [
        nid for nid, node in graph.nodes.items()
        if node.role in (NodeRole.STANDARD, NodeRole.APPROACH)
        and nid.startswith("WP_")
    ]
    covered = [
        nid for nid in main_nodes
        if any(nb.role == NodeRole.SIDING for nb in graph.get_neighbors(nid))
    ]
    coverage_ratio = round(len(covered) / len(main_nodes), 4) if main_nodes else 0.0
    return {
        "siding_count": len(siding_nodes),
        "main_node_count": len(main_nodes),
        "main_nodes_with_adjacent_siding": len(covered),
        "coverage_ratio": coverage_ratio,
    }


def _flatten_summary_row(result: RunResult) -> dict:
    row = asdict(result)
    diagnostics = row.get("diagnostics", {})
    task = diagnostics.get("task_generation", {})
    station = diagnostics.get("station_access", {})
    siding = diagnostics.get("siding_coverage", {})

    tasks_requested = task.get("tasks_requested", 0)
    tasks_dispatched = task.get("tasks_dispatched", 0)
    tasks_rejected = task.get("tasks_rejected_unreachable", 0)
    demands_completed = task.get("demands_completed", row.get("demands_completed", 0))
    completion_rate = (
        round(demands_completed / tasks_requested, 4)
        if tasks_requested else 0.0
    )
    task_acceptance_rate = (
        round(tasks_dispatched / tasks_requested, 4)
        if tasks_requested else 0.0
    )

    row.update({
        "tasks_requested": tasks_requested,
        "tasks_dispatched": tasks_dispatched,
        "tasks_rejected_unreachable": tasks_rejected,
        "tasks_backlogged": task.get("tasks_backlogged", 0),
        "demands_completed": demands_completed,
        "demand_completion_rate": completion_rate,
        "demand_throughput_per_hour": (
            round(demands_completed / row.get("sim_time_s", 0.0) * 3600.0, 3)
            if row.get("sim_time_s", 0.0) else 0.0
        ),
        "task_acceptance_rate": task_acceptance_rate,
        "completion_rate": completion_rate,
        "orders_published": task.get("orders_published", 0),
        "routeable_pair_count": task.get("routeable_pair_count", 0),
        "no_idle_agv": task.get("no_idle_agv", 0),
        "not_enough_available_nodes": task.get("not_enough_available_nodes", 0),
        "no_routeable_available_pair": task.get("no_routeable_available_pair", 0),
        "no_routeable_current_pair": task.get("no_routeable_current_pair", 0),
        "no_path_to_pickup": task.get("no_path_to_pickup", 0),
        "no_path_pickup_to_dropoff": task.get("no_path_pickup_to_dropoff", 0),
        "station_unreachable_from_start_count": (
            station.get("unreachable_from_first_charger_count", 0)
        ),
        "station_pair_unreachable_count": (
            station.get("station_pair_unreachable_count", 0)
        ),
        "station_min_access_edges": station.get("min_access_edges", 0),
        "siding_coverage_ratio": siding.get("coverage_ratio", 0.0),
    })
    return row


def _ranking_sort_key(row: dict) -> tuple:
    if row.get("error"):
        return (1, 0.0, 0.0, 0.0, float("inf"), float("inf"), float("inf"))
    return (
        0,
        -float(row.get("completion_rate", 0.0)),
        -float(row.get("task_acceptance_rate", 0.0)),
        -float(row.get("demand_throughput_per_hour", 0.0)),
        float(row.get("total_wait_time_s", 0.0)),
        float(row.get("followon_total", 0.0)),
        float(row.get("headon_total", 0.0)),
        float(row.get("retry_total", 0.0)),
    )


def _build_ranking_rows(results: list[RunResult]) -> list[dict]:
    rows = [_flatten_summary_row(r) for r in results]
    grouped: dict[tuple[int, int, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(
            int(row.get("n_agv", 0)),
            int(row.get("random_seed", 0)),
            str(row.get("demand_mode", "")),
        )].append(row)

    ranking_rows: list[dict] = []
    for (n_agv, seed, demand_mode), group in sorted(grouped.items()):
        ranked = sorted(group, key=_ranking_sort_key)
        previous_key = None
        current_rank = 0
        for index, row in enumerate(ranked, start=1):
            row_key = _ranking_sort_key(row)
            if row_key != previous_key:
                current_rank = index
                previous_key = row_key
            ranking_rows.append({
                "n_agv": n_agv,
                "random_seed": seed,
                "demand_mode": demand_mode,
                "rank": current_rank,
                "winner": current_rank == 1 and not row.get("error"),
                "topology_type": row.get("topology_type", ""),
                "completion_rate": row.get("completion_rate", 0.0),
                "task_acceptance_rate": row.get("task_acceptance_rate", 0.0),
                "demands_completed": row.get("demands_completed", 0),
                "demand_throughput_per_hour": row.get("demand_throughput_per_hour", 0.0),
                "total_wait_time_s": row.get("total_wait_time_s", 0.0),
                "followon_total": row.get("followon_total", 0),
                "headon_total": row.get("headon_total", 0),
                "retry_total": row.get("retry_total", 0),
                "error": row.get("error", ""),
            })
    return ranking_rows


def _build_ranking_aggregate(ranking_rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in ranking_rows:
        grouped[str(row["topology_type"])].append(row)

    aggregate: list[dict] = []
    for topology_type, rows in sorted(grouped.items()):
        valid = [r for r in rows if not r.get("error")]
        if not valid:
            aggregate.append({
                "topology_type": topology_type,
                "evaluated_groups": 0,
                "first_place_wins": 0,
                "avg_rank": 0.0,
                "avg_completion_rate": 0.0,
                "avg_demand_throughput_per_hour": 0.0,
                "avg_total_wait_time_s": 0.0,
                "avg_headon_total": 0.0,
                "avg_followon_total": 0.0,
            })
            continue

        count = len(valid)
        aggregate.append({
            "topology_type": topology_type,
            "evaluated_groups": count,
            "first_place_wins": sum(1 for r in valid if r["winner"]),
            "avg_rank": round(sum(r["rank"] for r in valid) / count, 4),
            "avg_completion_rate": round(
                sum(float(r["completion_rate"]) for r in valid) / count, 4
            ),
            "avg_demand_throughput_per_hour": round(
                sum(float(r["demand_throughput_per_hour"]) for r in valid) / count, 3
            ),
            "avg_total_wait_time_s": round(
                sum(float(r["total_wait_time_s"]) for r in valid) / count, 3
            ),
            "avg_headon_total": round(
                sum(float(r["headon_total"]) for r in valid) / count, 3
            ),
            "avg_followon_total": round(
                sum(float(r["followon_total"]) for r in valid) / count, 3
            ),
        })

    return sorted(
        aggregate,
        key=lambda r: (
            -r["first_place_wins"],
            r["avg_rank"] if r["avg_rank"] else float("inf"),
            -r["avg_completion_rate"],
            -r["avg_demand_throughput_per_hour"],
            r["avg_total_wait_time_s"],
            r["avg_followon_total"],
            r["avg_headon_total"],
        ),
    )


# ── 배치 실험 러너 ─────────────────────────────────────────────
class ExperimentRunner:
    def __init__(self, config: ExperimentConfig) -> None:
        self.config = config
        self.out_dir = Path(config.output_dir) / config.run_id
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> list[RunResult]:
        cfg = self.config
        seeds = cfg.random_seeds or [cfg.random_seed]
        total = len(cfg.types) * len(cfg.agv_counts) * len(seeds)
        print(f"\n{'='*56}")
        print(f"FAB Topology Experiment  run_id={cfg.run_id}")
        print(f"Types: {cfg.types}  AGV counts: {cfg.agv_counts}")
        print(f"Seeds: {seeds}")
        print(f"Duration: {cfg.duration_s}s  Total runs: {total}")
        print(f"{'='*56}\n")

        results: list[RunResult] = []
        idx = 0
        for seed in seeds:
            for t in cfg.types:
                for n in cfg.agv_counts:
                    idx += 1
                    print(
                        f"[{idx:2d}/{total}] seed={seed}  Type-{t}  AGV={n:2d}  ...",
                        end=" ",
                        flush=True,
                    )
                    result = asyncio.run(
                        _run_single(
                            t,
                            n,
                            cfg.duration_s,
                            cfg.task_interval_s,
                            seed,
                            cfg.demand_mode,
                            cfg.demand_count,
                        )
                    )
                    results.append(result)
                    if result.error:
                        print(f"ERROR: {result.error}")
                    else:
                        print(
                            f"demand_done={result.demands_completed:3d}  "
                            f"demand_tph={result.demand_throughput_per_hour:6.1f}  "
                            f"completion={result.demand_completion_rate:.3f}  "
                            f"wait={result.total_wait_time_s:6.1f}s  "
                            f"wall={result.wall_time_s:.1f}s"
                        )
                    self._save_single(result)

        self._save_summary(results)
        self._print_matrix(results)
        return results

    def _save_single(self, r: RunResult) -> None:
        d = self.out_dir / f"{r.topology_type}_{r.n_agv:02d}agv_seed{r.random_seed}"
        d.mkdir(exist_ok=True)
        (d / "kpi_summary.json").write_text(
            json.dumps(asdict(r), ensure_ascii=False, indent=2)
        )

    def _save_summary(self, results: list[RunResult]) -> None:
        rows = [_flatten_summary_row(r) for r in results]

        # CSV
        csv_path = self.out_dir / "summary.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        # JSON
        json_path = self.out_dir / "summary.json"
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))

        ranking_rows = _build_ranking_rows(results)
        ranking_path = self.out_dir / "ranking.csv"
        ranking_columns = [
            "n_agv", "random_seed", "demand_mode", "rank", "winner",
            "topology_type", "completion_rate", "task_acceptance_rate",
            "demands_completed", "demand_throughput_per_hour",
            "total_wait_time_s", "followon_total", "headon_total",
            "retry_total", "error",
        ]
        with open(ranking_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ranking_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(ranking_rows)

        ranking_json_path = self.out_dir / "ranking.json"
        ranking_json_path.write_text(
            json.dumps(ranking_rows, ensure_ascii=False, indent=2)
        )

        aggregate = _build_ranking_aggregate(ranking_rows)
        aggregate_path = self.out_dir / "ranking_aggregate.json"
        aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))

        print(f"\n결과 저장: {self.out_dir}/")
        print(f"  summary.csv  ({len(results)}행)")
        print(f"  summary.json")
        print(f"  ranking.csv")
        print(f"  ranking.json")
        print(f"  ranking_aggregate.json")

    def _print_matrix(self, results: list[RunResult]) -> None:
        """처리량 매트릭스 콘솔 출력."""
        cfg = self.config
        col_w = 10

        def avg_for(topology_type: str, n_agv: int, attr: str) -> float | None:
            values = [
                float(getattr(r, attr))
                for r in results
                if r.topology_type == topology_type and r.n_agv == n_agv and not r.error
            ]
            if not values:
                return None
            return sum(values) / len(values)

        print(f"\n{'─'*56}")
        print("실제 demand 처리량 (demands/hour) 매트릭스")
        print(f"{'Type':6s}", end="")
        for n in cfg.agv_counts:
            print(f"{'AGV='+str(n):>{col_w}}", end="")
        print()
        print("─" * (6 + col_w * len(cfg.agv_counts)))

        for t in cfg.types:
            print(f"{'Type '+t:6s}", end="")
            for n in cfg.agv_counts:
                value = avg_for(t, n, "demand_throughput_per_hour")
                if value is not None:
                    print(f"{value:>{col_w}.1f}", end="")
                else:
                    print(f"{'ERR':>{col_w}}", end="")
            print()

        print(f"\n실제 demand 완료율 매트릭스")
        print(f"{'Type':6s}", end="")
        for n in cfg.agv_counts:
            print(f"{'AGV='+str(n):>{col_w}}", end="")
        print()
        print("─" * (6 + col_w * len(cfg.agv_counts)))

        for t in cfg.types:
            print(f"{'Type '+t:6s}", end="")
            for n in cfg.agv_counts:
                value = avg_for(t, n, "demand_completion_rate")
                if value is not None:
                    print(f"{value:>{col_w}.4f}", end="")
                else:
                    print(f"{'ERR':>{col_w}}", end="")
            print()

        print(f"\n가동률 (agv_utilization) 매트릭스")
        print(f"{'Type':6s}", end="")
        for n in cfg.agv_counts:
            print(f"{'AGV='+str(n):>{col_w}}", end="")
        print()
        print("─" * (6 + col_w * len(cfg.agv_counts)))

        for t in cfg.types:
            print(f"{'Type '+t:6s}", end="")
            for n in cfg.agv_counts:
                value = avg_for(t, n, "agv_utilization")
                if value is not None:
                    print(f"{value:>{col_w}.4f}", end="")
                else:
                    print(f"{'ERR':>{col_w}}", end="")
            print()

        print(f"\nHead-on 충돌 횟수 매트릭스")
        print(f"{'Type':6s}", end="")
        for n in cfg.agv_counts:
            print(f"{'AGV='+str(n):>{col_w}}", end="")
        print()
        print("─" * (6 + col_w * len(cfg.agv_counts)))

        for t in cfg.types:
            print(f"{'Type '+t:6s}", end="")
            for n in cfg.agv_counts:
                value = avg_for(t, n, "headon_total")
                if value is not None:
                    print(f"{value:>{col_w}.1f}", end="")
                else:
                    print(f"{'ERR':>{col_w}}", end="")
            print()

        print(f"\nSame-direction follow-on 차단 횟수 매트릭스")
        print(f"{'Type':6s}", end="")
        for n in cfg.agv_counts:
            print(f"{'AGV='+str(n):>{col_w}}", end="")
        print()
        print("─" * (6 + col_w * len(cfg.agv_counts)))

        for t in cfg.types:
            print(f"{'Type '+t:6s}", end="")
            for n in cfg.agv_counts:
                value = avg_for(t, n, "followon_total")
                if value is not None:
                    print(f"{value:>{col_w}.1f}", end="")
                else:
                    print(f"{'ERR':>{col_w}}", end="")
            print()
        print(f"{'─'*56}\n")

        aggregate = _build_ranking_aggregate(_build_ranking_rows(results))
        if aggregate:
            print("Topology ranking summary")
            for row in aggregate:
                print(
                    f"Type {row['topology_type']}: "
                    f"wins={row['first_place_wins']}/{row['evaluated_groups']}  "
                    f"avg_rank={row['avg_rank']:.2f}  "
                    f"avg_completion={row['avg_completion_rate']:.4f}  "
                    f"avg_demand_tph={row['avg_demand_throughput_per_hour']:.1f}"
                )
            print()


# ── YAML 기반 실험 설정 ────────────────────────────────────────
def load_from_yaml(path: str) -> ExperimentConfig:
    import yaml
    with open(path) as f:
        raw = yaml.safe_load(f)
    random_seeds = raw.get("random_seeds")
    return ExperimentConfig(
        types          = raw.get("types", ["A","B","C","D","E"]),
        agv_counts     = raw.get("agv_counts", [3, 5, 8, 12]),
        duration_s     = float(raw.get("duration_s", 600.0)),
        task_interval_s= float(raw.get("task_interval_s", 5.0)),
        random_seed    = int(raw.get("random_seed", 42)),
        random_seeds   = (
            [int(seed) for seed in random_seeds]
            if random_seeds is not None else None
        ),
        demand_mode    = raw.get("demand_mode", "generated"),
        demand_count   = (
            int(raw["demand_count"]) if raw.get("demand_count") is not None else None
        ),
        output_dir     = raw.get("output_dir", "outputs/experiments"),
        run_id         = raw.get("run_id", None),
    )


# ── CLI ───────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="FAB Topology Experiment Runner")
    parser.add_argument("--experiment", help="실험 설정 YAML 파일")
    parser.add_argument("--types",    nargs="+", default=["A","B","C","D","E"],
                        help="토폴로지 타입 (기본: A B C D E)")
    parser.add_argument("--agv",      nargs="+", type=int, default=[3, 5, 8, 12],
                        help="AGV 대수 목록 (기본: 3 5 8 12)")
    parser.add_argument("--duration", type=float, default=600.0,
                        help="시뮬 시간 초 (기본: 600)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="태스크 발행 간격 초 (기본: 5)")
    parser.add_argument("--seed",     type=int, default=42,
                        help="난수 시드 (기본: 42)")
    parser.add_argument("--seeds", nargs="+", type=int, default=None,
                        help="반복 실험용 난수 시드 목록")
    parser.add_argument("--demand-mode", default="generated",
                        choices=["generated", "common_demand", "capability"],
                        help="태스크 수요 생성 모드")
    parser.add_argument("--demand-count", type=int, default=None,
                        help="DemandSet 태스크 개수")
    parser.add_argument("--output",   default="outputs/experiments",
                        help="결과 저장 디렉토리")
    args = parser.parse_args()

    if args.experiment:
        config = load_from_yaml(args.experiment)
    else:
        config = ExperimentConfig(
            types          = args.types,
            agv_counts     = args.agv,
            duration_s     = args.duration,
            task_interval_s= args.interval,
            random_seed    = args.seed,
            random_seeds   = args.seeds,
            demand_mode    = args.demand_mode,
            demand_count   = args.demand_count,
            output_dir     = args.output,
        )

    runner = ExperimentRunner(config)
    runner.run()


if __name__ == "__main__":
    main()
