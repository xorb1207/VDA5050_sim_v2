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
import statistics
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
from src.analytics.playback_trace import PlaybackTraceRecorder, build_playback_html


def _interleave_groups(groups: list[list[str]]) -> list[str]:
    merged: list[str] = []
    max_len = max((len(group) for group in groups), default=0)
    for idx in range(max_len):
        for group in groups:
            if idx < len(group):
                merged.append(group[idx])
    return merged


def _build_start_pool(graph: MapGraph, random_seed: int | None = None) -> list[str]:
    chargers = sorted([n.node_id for n in graph.get_chargers()])
    holding_points = sorted([
        n.node_id
        for n in graph.get_holding_points()
        if not n.is_charger
    ])
    charger_groups = [
        [nid for nid in chargers if nid in ("CH_01", "CH_02", "CH_03")],
        [nid for nid in chargers if nid in ("CH_04", "CH_05")],
        [nid for nid in chargers if nid in ("CH_06", "CH_07", "CH_08")],
    ]
    holding_groups = [
        sorted(
            nid for nid in holding_points if nid.startswith("HP_N_")
        ),
        sorted(
            nid for nid in holding_points if nid.startswith("HP_C_")
        ),
        sorted(
            nid for nid in holding_points if nid.startswith("HP_S_")
        ),
    ]
    pool = _interleave_groups(charger_groups) + _interleave_groups(holding_groups)
    seen: set[str] = set()
    ordered_unique: list[str] = []
    for node_id in pool:
        if node_id in graph.nodes and node_id not in seen:
            seen.add(node_id)
            ordered_unique.append(node_id)
    if random_seed is not None:
        rng = random.Random(random_seed)
        rng.shuffle(ordered_unique)
    return ordered_unique


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
    # Type B siding placement sweep: ["base"], ["base","mid","dense"] 등
    # types에 "B"가 포함될 때만 유효. 다른 타입에는 "base" 적용
    siding_placements: list[str] = field(default_factory=lambda: ["base"])
    # Type B siding policy preset: ["adjacent"], ["reachable"] 등
    type_b_siding_policies: list[str] = field(default_factory=lambda: ["reachable"])
    # Type B custom variants: placement/policy cross product 대신 이 목록만 실행
    type_b_variants: list[dict[str, str]] | None = None
    battery_enabled: bool = False
    battery_initial_pct: float = 100.0
    battery_charge_band_entry_pct: float = 40.0
    battery_charge_assign_pct: float = 30.0
    battery_charge_target_pct: float = 90.0
    battery_charge_rate_pct_per_s: float = 1.0
    battery_base_drain_pct_per_hour: float = 8.0
    battery_loaded_drain_pct_per_hour: float = 12.0
    report_language: str = "ko"
    playback_trace_enabled: bool = False
    playback_sample_interval_s: float = 0.5

    def __post_init__(self):
        if self.run_id is None:
            self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if self.random_seeds is None:
            self.random_seeds = [self.random_seed]
        elif not self.random_seeds:
            raise ValueError("random_seeds must not be empty")
        if self.report_language not in {"ko", "en"}:
            raise ValueError("report_language must be 'ko' or 'en'")


# ── 단일 실험 결과 ─────────────────────────────────────────────
@dataclass
class RunResult:
    topology_type: str
    n_agv: int
    random_seed: int = 42
    demand_mode: str = "generated"
    siding_placement: str = "base"  # Type B 사이딩 배치 프리셋
    siding_policy: str = "reachable"
    type_b_variant: str = ""
    battery_enabled: bool = False
    # KPI
    tasks_completed: int            = 0
    demands_completed: int          = 0
    demand_completion_rate: float   = 0.0
    demand_throughput_per_hour: float = 0.0
    throughput_tasks_per_hour: float = 0.0
    avg_task_completion_time_s: float = 0.0
    avg_wait_time_s: float          = 0.0
    total_wait_time_s: float        = 0.0
    total_restart_delay_s: float    = 0.0
    charging_sessions: int          = 0
    total_charging_time_s: float    = 0.0
    low_battery_charge_requests: int = 0
    avg_battery_pct: float          = 0.0
    min_battery_pct: float          = 0.0
    reservation_failure_rate: float = 0.0
    reroute_count: int              = 0
    node_occupancy_rate: float      = 0.0
    edge_occupancy_rate: float      = 0.0
    agv_utilization: float          = 0.0
    total_travel_distance_m: float  = 0.0
    max_queue_length: int           = 0
    headon_total: int               = 0
    followon_total: int             = 0
    section_conflict_total: int     = 0
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
    playback_trace_file: str        = ""
    playback_html_file: str         = ""


SUMMARY_COLUMNS = [
    "topology_type", "n_agv", "random_seed",
    "demand_mode", "siding_placement", "siding_policy", "type_b_variant",
    "battery_enabled",
    "tasks_completed", "demands_completed",
    "demand_completion_rate", "demand_throughput_per_hour",
    "throughput_tasks_per_hour",
    "avg_task_completion_time_s", "avg_wait_time_s", "total_wait_time_s",
    "total_restart_delay_s",
    "charging_sessions", "total_charging_time_s",
    "low_battery_charge_requests", "avg_battery_pct", "min_battery_pct",
    "reservation_failure_rate", "reroute_count",
    "node_occupancy_rate", "edge_occupancy_rate", "agv_utilization",
    "total_travel_distance_m", "max_queue_length",
    "headon_total", "followon_total", "section_conflict_total", "retry_total",
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
    siding_placement: str = "base",
    siding_policy: str = "reachable",
    type_b_variant: str = "",
    battery_enabled: bool = False,
    battery_initial_pct: float = 100.0,
    battery_charge_band_entry_pct: float = 40.0,
    battery_charge_assign_pct: float = 30.0,
    battery_charge_target_pct: float = 90.0,
    battery_charge_rate_pct_per_s: float = 1.0,
    battery_base_drain_pct_per_hour: float = 8.0,
    battery_loaded_drain_pct_per_hour: float = 12.0,
    playback_trace_enabled: bool = False,
    playback_sample_interval_s: float = 0.5,
) -> RunResult:
    result = RunResult(
        topology_type=topology_type,
        n_agv=n_agv,
        random_seed=random_seed,
        demand_mode=demand_mode,
        siding_placement=siding_placement,
        siding_policy=siding_policy,
        type_b_variant=type_b_variant,
        battery_enabled=battery_enabled,
    )
    t0 = time.perf_counter()

    try:
        random.seed(random_seed)
        gen_map = MapTopologyGenerator()
        graph   = gen_map.generate(topology_type, siding_placement=siding_placement)
        graph._type_b_siding_policy = siding_policy
        graph._battery_enabled = battery_enabled
        graph._battery_initial_pct = battery_initial_pct
        graph._battery_charge_band_entry_pct = battery_charge_band_entry_pct
        graph._battery_charge_assign_pct = battery_charge_assign_pct
        graph._battery_charge_target_pct = battery_charge_target_pct
        graph._battery_charge_rate_pct_per_s = battery_charge_rate_pct_per_s
        graph._battery_base_drain_pct_per_hour = battery_base_drain_pct_per_hour
        graph._battery_loaded_drain_pct_per_hour = battery_loaded_drain_pct_per_hour
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
        trace_recorder = (
            PlaybackTraceRecorder(graph, sample_interval_s=playback_sample_interval_s)
            if playback_trace_enabled else None
        )

        # Type E: graph._lane_mode 태그로 creep policy 자동 주입
        # (agv._get_effective_speed()가 graph._lane_mode를 직접 읽음)
        engine  = SimulationEngine(
            graph,
            sched,
            task_generator=task_gen,
            trace_recorder=trace_recorder,
        )

        # AGV 초기 배치 — 북/중/남 corridor를 섞어서 분산 배치
        start_pool = _build_start_pool(graph, random_seed=random_seed)
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
        result.total_restart_delay_s       = kpis.get("total_restart_delay_s", 0.0)
        result.charging_sessions           = kpis.get("charging_sessions", 0)
        result.total_charging_time_s       = kpis.get("total_charging_time_s", 0.0)
        result.low_battery_charge_requests = kpis.get("low_battery_charge_requests", 0)
        result.avg_battery_pct             = kpis.get("avg_battery_pct", 0.0)
        result.min_battery_pct             = kpis.get("min_battery_pct", 0.0)
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
        result.section_conflict_total = kpis.get("section_conflict_total", 0)
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
        if trace_recorder is not None:
            extra_meta = {
                "topology_type": result.topology_type,
                "topology_variant": _topology_variant_label(asdict(result)),
                "n_agv": result.n_agv,
                "random_seed": result.random_seed,
                "siding_placement": result.siding_placement,
                "siding_policy": result.siding_policy,
                "demand_mode": result.demand_mode,
                "demand_count": demand_count if demand_count is not None else 0,
                "description": _topology_description(result.topology_type, "ko"),
            }
            result.diagnostics["playback_trace"] = trace_recorder.build_trace(
                duration_s, extra_meta=extra_meta
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
        access_neighbors = {
            e.start_node_id if e.end_node_id == station.node_id else e.end_node_id
            for e in graph.edges.values()
            if e.access_type == "station_access"
            and station.node_id in (e.start_node_id, e.end_node_id)
        }
        cluster_node_ids = {station.node_id, *access_neighbors}
        inbound = [
            e for e in graph.edges.values()
            if e.end_node_id == station.node_id
        ]
        outbound = [
            e for e in graph.edges.values()
            if e.start_node_id == station.node_id
        ]
        cluster_access_edges = [
            e for e in graph.edges.values()
            if e.access_type == "station_access"
            and (e.start_node_id in cluster_node_ids or e.end_node_id in cluster_node_ids)
        ]
        access_edge_counts[station.node_id] = len(cluster_access_edges)
        if start and not graph.get_path(start, station.node_id):
            unreachable.append(station.node_id)
        station_details.append({
            "station_id": station.node_id,
            "inbound_edges": len(inbound),
            "outbound_edges": len(outbound),
            "access_edges": len(cluster_access_edges),
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
    covered_set = set(covered)
    coverage_ratio = round(len(covered) / len(main_nodes), 4) if main_nodes else 0.0

    corridor_groups: dict[str, list[tuple[int, str]]] = {}
    for nid in main_nodes:
        parts = nid.split("_")
        if len(parts) < 3:
            continue
        corridor_tag = parts[1]
        try:
            x_pos = int(parts[2])
        except ValueError:
            continue
        corridor_groups.setdefault(corridor_tag, []).append((x_pos, nid))

    corridor_coverage: list[dict] = []
    longest_gap_nodes = 0
    longest_gap_m = 0.0
    longest_gap_corridor = ""
    uncovered_samples: list[str] = []

    for corridor_tag, entries in sorted(corridor_groups.items()):
        ordered = sorted(entries)
        uncovered_in_corridor = [nid for _, nid in ordered if nid not in covered_set]
        if not uncovered_samples:
            uncovered_samples = uncovered_in_corridor[:5]

        best_run_nodes = 0
        best_run_m = 0.0
        current_run: list[int] = []

        for x_pos, nid in ordered:
            if nid in covered_set:
                if current_run:
                    run_nodes = len(current_run)
                    run_m = float(current_run[-1] - current_run[0])
                    if run_nodes > best_run_nodes or (
                        run_nodes == best_run_nodes and run_m > best_run_m
                    ):
                        best_run_nodes = run_nodes
                        best_run_m = run_m
                    current_run = []
                continue
            current_run.append(x_pos)

        if current_run:
            run_nodes = len(current_run)
            run_m = float(current_run[-1] - current_run[0])
            if run_nodes > best_run_nodes or (
                run_nodes == best_run_nodes and run_m > best_run_m
            ):
                best_run_nodes = run_nodes
                best_run_m = run_m

        if best_run_nodes > longest_gap_nodes or (
            best_run_nodes == longest_gap_nodes and best_run_m > longest_gap_m
        ):
            longest_gap_nodes = best_run_nodes
            longest_gap_m = best_run_m
            longest_gap_corridor = corridor_tag

        corridor_coverage.append({
            "corridor": corridor_tag,
            "main_node_count": len(ordered),
            "covered_count": sum(1 for _, nid in ordered if nid in covered_set),
            "coverage_ratio": round(
                sum(1 for _, nid in ordered if nid in covered_set) / len(ordered),
                4,
            ) if ordered else 0.0,
            "longest_uncovered_run_nodes": best_run_nodes,
            "longest_uncovered_run_m": round(best_run_m, 3),
        })

    return {
        "siding_count": len(siding_nodes),
        "main_node_count": len(main_nodes),
        "main_nodes_with_adjacent_siding": len(covered),
        "coverage_ratio": coverage_ratio,
        "longest_uncovered_run_nodes": longest_gap_nodes,
        "longest_uncovered_run_m": round(longest_gap_m, 3),
        "longest_uncovered_corridor": longest_gap_corridor,
        "corridor_coverage": corridor_coverage,
        "uncovered_main_node_samples": uncovered_samples,
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
        return (
            1,
            0.0,
            0.0,
            0.0,
            float("inf"),
            float("inf"),
            float("inf"),
            float("inf"),
        )
    return (
        0,
        -float(row.get("completion_rate", 0.0)),
        -float(row.get("task_acceptance_rate", 0.0)),
        -float(row.get("demand_throughput_per_hour", 0.0)),
        float(row.get("total_wait_time_s", 0.0)),
        float(row.get("section_conflict_total", 0.0)),
        float(row.get("followon_total", 0.0)),
        float(row.get("headon_total", 0.0)),
        float(row.get("retry_total", 0.0)),
    )


def _topology_variant_label(row: dict) -> str:
    topology_type = str(row.get("topology_type", ""))
    siding_placement = str(row.get("siding_placement", "base") or "base")
    siding_policy = str(row.get("siding_policy", "reachable") or "reachable")
    if topology_type == "B":
        return f"B/{siding_placement}/{siding_policy}"
    return topology_type


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
                "siding_placement": row.get("siding_placement", "base"),
                "siding_policy": row.get("siding_policy", "reachable"),
                "type_b_variant": row.get("type_b_variant", ""),
                "rank": current_rank,
                "winner": current_rank == 1 and not row.get("error"),
                "topology_type": row.get("topology_type", ""),
                "topology_variant": _topology_variant_label(row),
                "completion_rate": row.get("completion_rate", 0.0),
                "task_acceptance_rate": row.get("task_acceptance_rate", 0.0),
                "demands_completed": row.get("demands_completed", 0),
                "demand_throughput_per_hour": row.get("demand_throughput_per_hour", 0.0),
                "total_wait_time_s": row.get("total_wait_time_s", 0.0),
                "section_conflict_total": row.get("section_conflict_total", 0),
                "followon_total": row.get("followon_total", 0),
                "headon_total": row.get("headon_total", 0),
                "retry_total": row.get("retry_total", 0),
                "error": row.get("error", ""),
            })
    return ranking_rows


def _build_ranking_aggregate(ranking_rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in ranking_rows:
        grouped[str(row["topology_variant"])].append(row)

    aggregate: list[dict] = []
    for topology_variant, rows in sorted(grouped.items()):
        valid = [r for r in rows if not r.get("error")]
        topology_type = str(valid[0]["topology_type"] if valid else rows[0]["topology_type"])
        siding_placement = str(
            valid[0].get("siding_placement", "base")
            if valid else rows[0].get("siding_placement", "base")
        )
        siding_policy = str(
            valid[0].get("siding_policy", "reachable")
            if valid else rows[0].get("siding_policy", "reachable")
        )
        if not valid:
            aggregate.append({
                "topology_type": topology_type,
                "topology_variant": topology_variant,
                "siding_placement": siding_placement,
                "siding_policy": siding_policy,
                "evaluated_groups": 0,
                "first_place_wins": 0,
                "avg_rank": 0.0,
                "avg_completion_rate": 0.0,
                "avg_demand_throughput_per_hour": 0.0,
                "avg_total_wait_time_s": 0.0,
                "avg_headon_total": 0.0,
                "avg_followon_total": 0.0,
                "avg_section_conflict_total": 0.0,
            })
            continue

        count = len(valid)
        aggregate.append({
            "topology_type": topology_type,
            "topology_variant": topology_variant,
            "siding_placement": siding_placement,
            "siding_policy": siding_policy,
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
            "avg_section_conflict_total": round(
                sum(float(r["section_conflict_total"]) for r in valid) / count, 3
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
            r["avg_section_conflict_total"],
            r["avg_followon_total"],
            r["avg_headon_total"],
        ),
    )


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return round(statistics.pstdev(values), 4)


def _ranking_policy() -> dict:
    return {
        "primary_metrics": [
            "completion_rate",
            "task_acceptance_rate",
            "demand_throughput_per_hour",
        ],
        "secondary_metrics": [
            "total_wait_time_s",
            "section_conflict_total",
            "followon_total",
            "headon_total",
            "retry_total",
        ],
        "sort_direction": {
            "completion_rate": "desc",
            "task_acceptance_rate": "desc",
            "demand_throughput_per_hour": "desc",
            "total_wait_time_s": "asc",
            "section_conflict_total": "asc",
            "followon_total": "asc",
            "headon_total": "asc",
            "retry_total": "asc",
        },
    }


def _tr(locale: str, key: str, **kwargs) -> str:
    templates = {
        "ko": {
            "highest_completion": "완료율 우세",
            "highest_throughput": "처리량 우세",
            "lowest_wait": "대기시간 우세",
            "lowest_headon": "정면 교행 충돌 최소",
            "lowest_section_conflict": "구간 충돌 최소",
            "lowest_completion": "완료율 열세",
            "lowest_throughput": "처리량 열세",
            "highest_wait": "대기시간 부담 큼",
            "highest_headon": "정면 교행 충돌 부담 큼",
            "highest_section_conflict": "구간 충돌 부담 큼",
            "consistently_top_ranked": "상위권 순위 일관",
            "early_saturation_risk": "조기 포화 위험",
            "throughput_constrained": "처리량 제한 뚜렷",
            "high_density_use_case": "고밀도 처리량 중심 운영",
            "stability_use_case": "안정성 중심 운영",
            "limited_use_case": "저밀도/제한 수요 운영에 적합",
            "moderate_use_case": "중간 밀도 일반 운영에 적합",
            "tradeoff_high_throughput": "처리량은 높지만 대기 비용이 크고, 지배 병목은 {bottleneck}입니다.",
            "tradeoff_balanced_stable": "처리량과 안정성의 균형이 좋고, 지배 병목은 {bottleneck}입니다.",
            "tradeoff_balanced_low_congestion": "혼잡 부담이 비교적 낮은 균형형이며, 지배 병목은 {bottleneck}입니다.",
            "tradeoff_low_completion": "완료율이 낮아 혼잡 지표가 과소평가될 수 있으며, 지배 병목은 {bottleneck}입니다.",
            "tradeoff_constrained": "처리량이 충분히 올라가기 전에 제한이 걸리며, 지배 병목은 {bottleneck}입니다.",
            "against": "{baseline} 대비 {target}는 {phrases}.",
            "flat_perf": "완료율과 처리량이 거의 동일합니다",
            "improved_completion": "완료율이 좋아졌습니다 ({value:+.4f})",
            "worsened_completion": "완료율이 나빠졌습니다 ({value:+.4f})",
            "improved_throughput": "처리량이 좋아졌습니다 ({value:+.3f}/h)",
            "worsened_throughput": "처리량이 나빠졌습니다 ({value:+.3f}/h)",
            "improved_wait": "대기시간이 줄었습니다 ({value:+.3f}s)",
            "worsened_wait": "대기시간이 늘었습니다 ({value:+.3f}s)",
            "improved_section": "구간 충돌이 줄었습니다 ({value:+.3f})",
            "worsened_section": "구간 충돌이 늘었습니다 ({value:+.3f})",
            "improved_headon": "정면 교행 충돌이 줄었습니다 ({value:+.3f})",
            "worsened_headon": "정면 교행 충돌이 늘었습니다 ({value:+.3f})",
            "winner_summary": "{winner}가 전체 1위입니다. 완료율 {completion:.4f}, 처리량 {throughput:.1f}/h를 유지하면서 처리량 1위 대비 충돌 비용을 더 낮게 억제했습니다.",
            "runner_up_summary": "차상위는 {runner_up}이며 평균 순위 {avg_rank:.2f}를 기록했습니다.",
            "throughput_leader_summary": "{leader}는 최고 처리량({throughput:.1f}/h)을 냈지만 대기/충돌 비용이 더 컸습니다.",
            "ranking_policy_summary": "랭킹은 완료율과 처리량을 우선하고, 그 다음 대기와 충돌 비용을 패널티로 반영합니다.",
            "topology_line": "{variant}: 완료율 {completion:.4f}, 처리량 {throughput:.1f}/h, 대기 {wait:.1f}s, 지배 병목 {bottleneck}.",
        },
        "en": {
            "highest_completion": "highest completion",
            "highest_throughput": "highest throughput",
            "lowest_wait": "lowest wait",
            "lowest_headon": "lowest headon",
            "lowest_section_conflict": "lowest section conflict",
            "lowest_completion": "lowest completion",
            "lowest_throughput": "lowest throughput",
            "highest_wait": "highest wait",
            "highest_headon": "highest headon",
            "highest_section_conflict": "highest section conflict",
            "consistently_top_ranked": "consistently top-ranked",
            "early_saturation_risk": "early saturation risk",
            "throughput_constrained": "throughput-constrained",
            "high_density_use_case": "High-density throughput-oriented operation",
            "stability_use_case": "Stability-oriented operation with lower conflict tolerance",
            "limited_use_case": "Low-density or limited-demand operation only",
            "moderate_use_case": "Moderate-density operation where simpler routing is preferred",
            "tradeoff_high_throughput": "High throughput with a noticeable wait cost; dominant bottleneck is {bottleneck}.",
            "tradeoff_balanced_stable": "Balanced throughput and stability; dominant bottleneck is {bottleneck}.",
            "tradeoff_balanced_low_congestion": "Balanced profile with lower congestion pressure; dominant bottleneck is {bottleneck}.",
            "tradeoff_low_completion": "Low completion keeps congestion metrics deceptively low; dominant bottleneck is {bottleneck}.",
            "tradeoff_constrained": "Throughput is constrained before full demand recovery; dominant bottleneck is {bottleneck}.",
            "against": "Against {baseline}, {target} {phrases}.",
            "flat_perf": "kept completion and throughput essentially flat",
            "improved_completion": "improved completion ({value:+.4f})",
            "worsened_completion": "worsened completion ({value:+.4f})",
            "improved_throughput": "improved throughput ({value:+.3f}/h)",
            "worsened_throughput": "worsened throughput ({value:+.3f}/h)",
            "improved_wait": "improved wait ({value:+.3f}s)",
            "worsened_wait": "worsened wait ({value:+.3f}s)",
            "improved_section": "improved section conflict ({value:+.3f})",
            "worsened_section": "worsened section conflict ({value:+.3f})",
            "improved_headon": "improved headon ({value:+.3f})",
            "worsened_headon": "worsened headon ({value:+.3f})",
            "winner_summary": "{winner} ranked first overall because it balanced completion {completion:.4f}, throughput {throughput:.1f}/h, and lower conflict cost than the throughput leader.",
            "runner_up_summary": "Runner-up {runner_up} posted avg rank {avg_rank:.2f}.",
            "throughput_leader_summary": "{leader} delivered the highest throughput ({throughput:.1f}/h) but paid more in wait/conflict.",
            "ranking_policy_summary": "Ranking prioritizes completion and throughput first, then penalizes wait and conflict costs.",
            "topology_line": "{variant}: completion {completion:.4f}, throughput {throughput:.1f}/h, wait {wait:.1f}s, dominant bottleneck {bottleneck}.",
        },
    }
    return templates[locale][key].format(**kwargs)


def _build_input_parameters(config: ExperimentConfig, results: list[RunResult]) -> dict:
    type_variants: dict[str, list[str]] = defaultdict(list)
    for result in results:
        label = _topology_variant_label(asdict(result))
        if label not in type_variants[result.topology_type]:
            type_variants[result.topology_type].append(label)
    return {
        "types": config.types,
        "type_variants": dict(type_variants),
        "mixed_topology": None,
        "agv_counts": config.agv_counts,
        "duration_s": config.duration_s,
        "task_interval_s": config.task_interval_s,
        "demand_mode": config.demand_mode,
        "demand_count": config.demand_count,
        "random_seeds": config.random_seeds or [config.random_seed],
        "battery": {
            "enabled": config.battery_enabled,
            "initial_pct": config.battery_initial_pct,
            "charge_band_entry_pct": config.battery_charge_band_entry_pct,
            "charge_assign_pct": config.battery_charge_assign_pct,
            "charge_target_pct": config.battery_charge_target_pct,
            "charge_rate_pct_per_s": config.battery_charge_rate_pct_per_s,
            "base_drain_pct_per_hour": config.battery_base_drain_pct_per_hour,
            "loaded_drain_pct_per_hour": config.battery_loaded_drain_pct_per_hour,
        },
        "map_resolution": {
            "wp_step_m": getattr(MapTopologyGenerator, "WP_STEP", None),
        },
        "report_language": config.report_language,
    }


def _series_points(
    rows: list[dict],
    topology_variant: str,
    metric: str,
) -> list[dict]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if row.get("error"):
            continue
        if _topology_variant_label(row) != topology_variant:
            continue
        grouped[int(row.get("n_agv", 0))].append(float(row.get(metric, 0.0)))
    points: list[dict] = []
    for agv_count in sorted(grouped):
        values = grouped[agv_count]
        points.append({
            "agv_count": agv_count,
            "mean": _avg(values),
            "stddev": _stddev(values),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "sample_count": len(values),
        })
    return points


def _build_chart_series(rows: list[dict], aggregate: list[dict]) -> dict:
    metrics = [
        ("completion_by_agv", "completion_rate"),
        ("throughput_by_agv", "demand_throughput_per_hour"),
        ("wait_by_agv", "total_wait_time_s"),
        ("headon_by_agv", "headon_total"),
        ("followon_by_agv", "followon_total"),
        ("section_conflict_by_agv", "section_conflict_total"),
        ("battery_min_pct_by_agv", "min_battery_pct"),
    ]
    chart_series: dict[str, list[dict]] = {}
    for key, metric in metrics:
        chart_series[key] = [
            {
                "topology_variant": agg["topology_variant"],
                "topology_type": agg["topology_type"],
                "points": _series_points(rows, agg["topology_variant"], metric),
            }
            for agg in aggregate
        ]
    return chart_series


def _metric_leaders(aggregate: list[dict], field: str, reverse: bool) -> tuple[str, str]:
    ranked = sorted(
        aggregate,
        key=lambda row: float(row.get(field, 0.0)),
        reverse=reverse,
    )
    return ranked[0]["topology_variant"], ranked[-1]["topology_variant"]


def _build_strengths_and_weaknesses(
    row: dict,
    aggregate: list[dict],
    locale: str,
) -> tuple[list[str], list[str]]:
    strengths: list[str] = []
    weaknesses: list[str] = []
    best_completion, worst_completion = _metric_leaders(
        aggregate, "avg_completion_rate", reverse=True
    )
    best_throughput, worst_throughput = _metric_leaders(
        aggregate, "avg_demand_throughput_per_hour", reverse=True
    )
    best_wait, worst_wait = _metric_leaders(
        aggregate, "avg_total_wait_time_s", reverse=False
    )
    best_headon, worst_headon = _metric_leaders(
        aggregate, "avg_headon_total", reverse=False
    )
    best_section, worst_section = _metric_leaders(
        aggregate, "avg_section_conflict_total", reverse=False
    )

    variant = row["topology_variant"]
    completion = float(row.get("avg_completion_rate", 0.0))
    throughput = float(row.get("avg_demand_throughput_per_hour", 0.0))
    if variant == best_completion:
        strengths.append(_tr(locale, "highest_completion"))
    if variant == best_throughput:
        strengths.append(_tr(locale, "highest_throughput"))
    if variant == best_wait and completion >= 0.2:
        strengths.append(_tr(locale, "lowest_wait"))
    if variant == best_headon:
        strengths.append(_tr(locale, "lowest_headon"))
    if variant == best_section and completion >= 0.2:
        strengths.append(_tr(locale, "lowest_section_conflict"))

    if variant == worst_completion:
        weaknesses.append(_tr(locale, "lowest_completion"))
    if variant == worst_throughput:
        weaknesses.append(_tr(locale, "lowest_throughput"))
    if variant == worst_wait:
        weaknesses.append(_tr(locale, "highest_wait"))
    if variant == worst_headon:
        weaknesses.append(_tr(locale, "highest_headon"))
    if variant == worst_section:
        weaknesses.append(_tr(locale, "highest_section_conflict"))

    if row["avg_rank"] <= 1.5 and completion >= 0.2 and _tr(locale, "highest_completion") not in strengths:
        strengths.append(_tr(locale, "consistently_top_ranked"))
    if row["avg_rank"] >= max((r["avg_rank"] for r in aggregate), default=0.0) and _tr(locale, "lowest_completion") not in weaknesses:
        weaknesses.append(_tr(locale, "early_saturation_risk"))
    if completion < 0.15 or throughput < 25.0:
        weaknesses.append(_tr(locale, "throughput_constrained"))

    return strengths[:3], weaknesses[:3]


def _dominant_bottleneck(row: dict) -> str:
    costs = {
        "section_conflict": float(row.get("avg_section_conflict_total", 0.0)),
        "followon": float(row.get("avg_followon_total", 0.0)),
        "headon": float(row.get("avg_headon_total", 0.0)),
        "wait": float(row.get("avg_total_wait_time_s", 0.0)),
    }
    dominant = max(costs.items(), key=lambda item: item[1])[0]
    return dominant


def _localized_bottleneck(row: dict, locale: str) -> str:
    name = _dominant_bottleneck(row)
    if locale == "ko":
        mapping = {
            "section_conflict": "구간 충돌",
            "followon": "동일 방향 추종 차단",
            "headon": "정면 교행 충돌",
            "wait": "대기시간",
        }
        return mapping.get(name, name)
    return name.replace("_", " ")


def _tradeoff_summary(row: dict, locale: str) -> str:
    bottleneck = _localized_bottleneck(row, locale)
    completion = float(row.get("avg_completion_rate", 0.0))
    throughput = float(row.get("avg_demand_throughput_per_hour", 0.0))
    wait = float(row.get("avg_total_wait_time_s", 0.0))
    headon = float(row.get("avg_headon_total", 0.0))
    if completion >= 0.3 and throughput >= 50.0 and wait >= 200.0:
        return _tr(locale, "tradeoff_high_throughput", bottleneck=bottleneck)
    if completion >= 0.25 and headon <= 1.0 and wait < 180.0:
        return _tr(locale, "tradeoff_balanced_stable", bottleneck=bottleneck)
    if completion >= 0.25 and wait < 150.0:
        return _tr(locale, "tradeoff_balanced_low_congestion", bottleneck=bottleneck)
    if completion < 0.15:
        return _tr(locale, "tradeoff_low_completion", bottleneck=bottleneck)
    return _tr(locale, "tradeoff_constrained", bottleneck=bottleneck)


def _recommended_use_case(row: dict, locale: str) -> str:
    completion = float(row.get("avg_completion_rate", 0.0))
    wait = float(row.get("avg_total_wait_time_s", 0.0))
    headon = float(row.get("avg_headon_total", 0.0))
    throughput = float(row.get("avg_demand_throughput_per_hour", 0.0))
    if completion < 0.15 or throughput < 25.0:
        return _tr(locale, "limited_use_case")
    if completion >= 0.3 and wait >= 200.0:
        return _tr(locale, "high_density_use_case")
    if headon <= 1.0 and wait < 180.0:
        return _tr(locale, "stability_use_case")
    return _tr(locale, "moderate_use_case")


def _build_per_topology(rows: list[dict], aggregate: list[dict], locale: str, out_dir: Optional[Path] = None) -> list[dict]:
    per_topology: list[dict] = []
    for agg in aggregate:
        strengths, weaknesses = _build_strengths_and_weaknesses(agg, aggregate, locale)
        siding_placement = agg.get("siding_placement", "base")
        siding_policy = agg.get("siding_policy", "reachable")
        playback_link = _find_playback_link(
            out_dir, agg["topology_type"], siding_placement, siding_policy
        ) if out_dir is not None else ""
        description = _topology_description(agg["topology_type"], locale)
        per_topology.append({
            "topology_type": agg["topology_type"],
            "topology_variant": agg["topology_variant"],
            "siding_placement": siding_placement,
            "siding_policy": siding_policy,
            "description": description,
            "aggregate_metrics": {
                "avg_rank": agg["avg_rank"],
                "wins": agg["first_place_wins"],
                "evaluated_groups": agg["evaluated_groups"],
                "avg_completion_rate": agg["avg_completion_rate"],
                "avg_demand_throughput_per_hour": agg["avg_demand_throughput_per_hour"],
                "avg_total_wait_time_s": agg["avg_total_wait_time_s"],
                "avg_headon_total": agg["avg_headon_total"],
                "avg_followon_total": agg["avg_followon_total"],
                "avg_section_conflict_total": agg["avg_section_conflict_total"],
            },
            "strengths": strengths,
            "weaknesses": weaknesses,
            "dominant_bottleneck": _localized_bottleneck(agg, locale),
            "tradeoff_summary": _tradeoff_summary(agg, locale),
            "recommended_use_case": _recommended_use_case(agg, locale),
            "playback_link": playback_link,
        })
    return per_topology


def _build_comparisons(aggregate: list[dict], locale: str) -> list[dict]:
    if not aggregate:
        return []
    baseline = aggregate[0]
    comparisons: list[dict] = []
    for row in aggregate[1:]:
        delta_completion = round(
            row["avg_completion_rate"] - baseline["avg_completion_rate"], 4
        )
        delta_throughput = round(
            row["avg_demand_throughput_per_hour"] - baseline["avg_demand_throughput_per_hour"],
            3,
        )
        delta_wait = round(
            row["avg_total_wait_time_s"] - baseline["avg_total_wait_time_s"],
            3,
        )
        delta_headon = round(
            row["avg_headon_total"] - baseline["avg_headon_total"], 3
        )
        delta_section = round(
            row["avg_section_conflict_total"] - baseline["avg_section_conflict_total"], 3
        )
        def _direction(metric_delta: float, better_when_lower: bool = False) -> str:
            if abs(metric_delta) < 1e-9:
                return "unchanged"
            improved = metric_delta < 0 if better_when_lower else metric_delta > 0
            return "improved" if improved else "worsened"

        completion_state = _direction(delta_completion)
        throughput_state = _direction(delta_throughput)
        wait_state = _direction(delta_wait, better_when_lower=True)
        section_state = _direction(delta_section, better_when_lower=True)
        headon_state = _direction(delta_headon, better_when_lower=True)

        phrases: list[str] = []
        if completion_state == "unchanged" and throughput_state == "unchanged":
            phrases.append(_tr(locale, "flat_perf"))
        else:
            perf_bits: list[str] = []
            if completion_state != "unchanged":
                perf_bits.append(_tr(locale, f"{completion_state}_completion", value=delta_completion))
            if throughput_state != "unchanged":
                perf_bits.append(_tr(locale, f"{throughput_state}_throughput", value=delta_throughput))
            if perf_bits:
                phrases.append(", ".join(perf_bits))

        cost_bits: list[str] = []
        if wait_state != "unchanged":
            cost_bits.append(_tr(locale, f"{wait_state}_wait", value=delta_wait))
        if section_state != "unchanged":
            cost_bits.append(_tr(locale, f"{section_state}_section", value=delta_section))
        if headon_state != "unchanged":
            cost_bits.append(_tr(locale, f"{headon_state}_headon", value=delta_headon))
        if cost_bits:
            if locale == "ko":
                phrases.append("대신 " + ", ".join(cost_bits))
            else:
                phrases.append("while " + ", ".join(cost_bits))

        phrase_text = "; ".join(phrases) if phrases else (
            "유의미한 KPI 변화가 없습니다" if locale == "ko" else "showed no material KPI change"
        )
        interpretation = _tr(
            locale,
            "against",
            baseline=baseline["topology_variant"],
            target=row["topology_variant"],
            phrases=phrase_text,
        )
        comparisons.append({
            "lhs": baseline["topology_variant"],
            "rhs": row["topology_variant"],
            "delta_metrics": {
                "completion_rate": delta_completion,
                "demand_throughput_per_hour": delta_throughput,
                "total_wait_time_s": delta_wait,
                "headon_total": delta_headon,
                "section_conflict_total": delta_section,
            },
            "interpretation": interpretation,
        })
    return comparisons


_TOPOLOGY_DESCRIPTIONS_KO: dict[str, dict[str, str]] = {
    "A": {
        "headline": "1차선 단방향 순환 — head-on 구조적 불가",
        "lanes": "1차선",
        "direction": "단방향 (순환)",
        "conflict": "없음 (구조적으로 head-on 불가)",
        "details": (
            "메인 통로가 한 방향으로만 흐르는 단순 순환 구조. head-on 충돌이 발생할 수 없어 안정적이지만, "
            "AGV가 늘어도 우회로가 좁아 처리량이 빨리 포화한다."
        ),
    },
    "B": {
        "headline": "1차선 양방향 + siding 대피",
        "lanes": "1차선",
        "direction": "양방향",
        "conflict": "siding 대피 (head-on 발생 시 사이딩으로 회피)",
        "details": (
            "한 차선에서 양방향 통행. 충돌 시 가까운 사이딩(SD)으로 빠지는 정책으로 head-on을 해소한다. "
            "사이딩 배치/정책에 따라 성능 변동 폭이 큼 (base/mid/dense × adjacent/reachable)."
        ),
    },
    "C": {
        "headline": "2차선 단방향 분리 — 통로폭 2.0m",
        "lanes": "2차선 (L1 / L2)",
        "direction": "단방향 (lane별 분리)",
        "conflict": "없음 (same-lane head-on 불가)",
        "details": (
            "차선을 두 개로 분리해 각 차선이 단방향으로 흐른다. 같은 차선 내 head-on 불가, "
            "차선 변경은 station/charger access 시점에 발생. 총 통로폭 2.0m."
        ),
    },
    "D": {
        "headline": "2차선 단방향 wide — 통로폭 3.0m",
        "lanes": "2차선 (L1: 동→서, L2: 서→동)",
        "direction": "단방향 (lane별 분리)",
        "conflict": "없음, follow-on headway 짧음",
        "details": (
            "C와 같은 단방향 분리 구조이지만 총 통로폭 3.0m로 더 넓다. 같은 방향 follow-on 안전거리 "
            "(headway)가 짧아져 처리량 우위. lane section capacity는 C와 동일하게 1."
        ),
    },
    "E": {
        "headline": "1차선 양방향 + 크리프 감속",
        "lanes": "1차선",
        "direction": "양방향",
        "conflict": "크리프 감속 (head-on 감지 시 0.3m/s)",
        "details": (
            "양방향 1차선에서 head-on이 감지되면 양쪽 모두 크리프 속도(0.3m/s)로 감속해 통과. "
            "사이딩 없이 속도 양보로 해소. 고밀도에선 critical section 충돌이 지배적."
        ),
    },
}


def _topology_description(topology_type: str, locale: str) -> dict[str, str]:
    descs = _TOPOLOGY_DESCRIPTIONS_KO  # English locale would extend here
    return descs.get(topology_type, {
        "headline": "",
        "lanes": "",
        "direction": "",
        "conflict": "",
        "details": "",
    })


def _find_playback_link(out_dir: Path, topology_type: str, siding_placement: str, siding_policy: str) -> str:
    """Look for a representative playback.html in any variant dir matching the topology."""
    if out_dir is None or not out_dir.exists():
        return ""
    placement_suffix = (
        f"_siding{siding_placement}"
        if topology_type == "B" and siding_placement and siding_placement != "base"
        else ""
    )
    policy_suffix = (
        f"_policy{siding_policy}"
        if topology_type == "B" and siding_policy and siding_policy != "reachable"
        else ""
    )
    prefix = f"{topology_type}{placement_suffix}{policy_suffix}_"
    candidates = sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name.startswith(prefix))
    for cand in candidates:
        playback = cand / "playback.html"
        if playback.exists():
            return f"./{cand.name}/playback.html"
    return ""


def _build_report(config: ExperimentConfig, results: list[RunResult], out_dir: Optional[Path] = None) -> dict:
    locale = config.report_language
    rows = [_flatten_summary_row(r) for r in results]
    ranking_rows = _build_ranking_rows(results)
    aggregate = _build_ranking_aggregate(ranking_rows)
    winner = aggregate[0]["topology_variant"] if aggregate else ""
    overview_summary: list[str] = []
    if aggregate:
        top = aggregate[0]
        throughput_leader = max(
            aggregate,
            key=lambda row: float(row.get("avg_demand_throughput_per_hour", 0.0)),
        )
        overview_summary.append(
            _tr(
                locale,
                "winner_summary",
                winner=top["topology_variant"],
                completion=top["avg_completion_rate"],
                throughput=top["avg_demand_throughput_per_hour"],
            )
        )
        if len(aggregate) > 1:
            runner_up = aggregate[1]
            overview_summary.append(
                _tr(
                    locale,
                    "runner_up_summary",
                    runner_up=runner_up["topology_variant"],
                    avg_rank=runner_up["avg_rank"],
                )
            )
        if throughput_leader["topology_variant"] != top["topology_variant"]:
            overview_summary.append(
                _tr(
                    locale,
                    "throughput_leader_summary",
                    leader=throughput_leader["topology_variant"],
                    throughput=throughput_leader["avg_demand_throughput_per_hour"],
                )
            )
        if len(aggregate) >= 2:
            gap = round(
                float(aggregate[0]["avg_completion_rate"])
                - float(aggregate[-1]["avg_completion_rate"]),
                4,
            )
            if locale == "ko":
                overview_summary.append(
                    f"이번 샘플에서는 1위와 최하위의 완료율 차이가 {gap:.4f}로 벌어져 스케일링 차이가 분명하게 드러났습니다."
                )
            else:
                overview_summary.append(
                    f"In this sample, the completion gap between the top and bottom variants was {gap:.4f}, indicating a clear scaling difference."
                )
        for row in aggregate[: min(3, len(aggregate))]:
            overview_summary.append(
                _tr(
                    locale,
                    "topology_line",
                    variant=row["topology_variant"],
                    completion=row["avg_completion_rate"],
                    throughput=row["avg_demand_throughput_per_hour"],
                    wait=row["avg_total_wait_time_s"],
                    bottleneck=_localized_bottleneck(row, locale),
                )
            )
    overview_summary.append(
        _tr(locale, "ranking_policy_summary")
    )

    return {
        "overview": {
            "run_id": config.run_id,
            "experiment_name": config.run_id,
            "objective": "Topology comparison with decision-ready KPI summary",
            "input_parameters": _build_input_parameters(config, results),
            "ranking_policy": _ranking_policy(),
            "top_winner": winner,
            "summary": overview_summary,
        },
        "per_topology": _build_per_topology(rows, aggregate, locale, out_dir),
        "comparisons": _build_comparisons(aggregate, locale),
        "chart_series": _build_chart_series(rows, aggregate),
    }


def _build_report_html(report: dict) -> str:
    report_json = json.dumps(report, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Experiment Report</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-alt: #eef2f7;
      --text: #18202a;
      --muted: #5a6877;
      --border: #d7dee7;
      --accent: #1f6feb;
      --good: #0f9d58;
      --warn: #c77700;
      --bad: #c0392b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .page {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    .hero {{
      display: grid;
      gap: 18px;
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    .hero-top {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      align-items: start;
    }}
    .hero h1 {{
      font-size: 28px;
      line-height: 1.1;
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 13px;
    }}
    .hero-summary {{
      display: grid;
      gap: 8px;
      color: var(--text);
      line-height: 1.5;
    }}
    .meta-grid, .card-grid, .chart-grid {{
      display: grid;
      gap: 16px;
      margin-top: 18px;
    }}
    .meta-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .card-grid {{
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    .chart-grid {{
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }}
    .panel h2 {{
      font-size: 18px;
      margin-bottom: 12px;
    }}
    .metric-kv {{
      display: grid;
      gap: 6px;
      font-size: 14px;
    }}
    .metric-kv .label {{
      color: var(--muted);
      font-size: 12px;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 6px;
      font-size: 12px;
    }}
    .chip.bad {{ color: var(--bad); }}
    .chip.good {{ color: var(--good); }}
    .muted {{
      color: var(--muted);
    }}
    .playback-btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      transition: background 100ms, border-color 100ms;
    }}
    .playback-btn:hover {{
      background: #e6efff;
      border-color: #2563eb;
      color: #1d4ed8;
    }}
    .playback-btn.disabled {{
      color: var(--muted);
      cursor: not-allowed;
      background: #f5f7fb;
    }}
    .playback-btn.disabled:hover {{
      background: #f5f7fb;
      border-color: var(--border);
      color: var(--muted);
    }}
    .topology-defs {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .topology-def {{
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      background: #fbfcfe;
    }}
    .topology-def .def-head {{
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 6px;
    }}
    .topology-def .def-tag {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 22px;
      height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      background: #e6efff;
      color: #1d4ed8;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.02em;
    }}
    .topology-def .def-headline {{
      font-weight: 600;
      font-size: 13px;
      color: var(--text);
      flex: 1 1 auto;
    }}
    .topology-def dl {{
      margin: 0;
      display: grid;
      grid-template-columns: 60px 1fr;
      gap: 4px 8px;
      font-size: 12px;
    }}
    .topology-def dt {{
      color: var(--muted);
    }}
    .topology-def dd {{
      margin: 0;
      color: var(--text);
    }}
    .topology-def .def-details {{
      margin-top: 10px;
      font-size: 12.5px;
      line-height: 1.5;
      color: #475569;
    }}
    .topology-headline {{
      margin: 8px 0 0 0;
      padding: 6px 10px;
      background: #f1f5fb;
      border-radius: 6px;
      font-size: 12px;
      color: #1d4ed8;
      font-weight: 500;
    }}
    .comparison-list {{
      display: grid;
      gap: 12px;
    }}
    .comparison-item {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
      background: #fbfcfe;
    }}
    .chart-card {{
      display: grid;
      gap: 10px;
    }}
    .chart-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 12px;
      color: var(--muted);
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .swatch {{
      width: 10px;
      height: 10px;
      border-radius: 2px;
    }}
    svg {{
      width: 100%;
      height: 240px;
      background: #fcfdff;
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    .axis {{
      stroke: #9aa7b5;
      stroke-width: 1;
    }}
    .grid {{
      stroke: #e6ebf2;
      stroke-width: 1;
    }}
    .series-line {{
      fill: none;
      stroke-width: 2.5;
    }}
    .series-point {{
      stroke: white;
      stroke-width: 1.5;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 12px; }}
      .chart-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="hero-top">
        <div>
          <div class="eyebrow" id="eyebrow"></div>
          <h1 id="title"></h1>
        </div>
        <div class="metric-kv">
          <span class="label">Top Winner</span>
          <strong id="winner"></strong>
        </div>
      </div>
      <div class="hero-summary" id="hero-summary"></div>
    </section>

    <section class="meta-grid" id="meta-grid"></section>

    <section class="panel" style="margin-top: 18px;">
      <h2>토폴로지 정의</h2>
      <p class="muted" style="margin-top:6px;">실험에서 비교한 5가지 통로 구조의 의미. 각 카드의 KPI는 이 정의를 전제로 해석합니다.</p>
      <div class="topology-defs" id="topology-defs"></div>
    </section>

    <section class="panel" style="margin-top: 18px;">
      <h2>토폴로지 요약</h2>
      <div class="card-grid" id="topology-cards"></div>
    </section>

    <section class="panel" style="margin-top: 18px;">
      <h2>비교 해석</h2>
      <div class="comparison-list" id="comparison-list"></div>
    </section>

    <section class="panel" style="margin-top: 18px;">
      <h2>시각화</h2>
      <div class="chart-grid" id="chart-grid"></div>
    </section>
  </div>

  <script>
    const report = {report_json};
    const palette = ["#1f6feb", "#0f9d58", "#c77700", "#c0392b", "#7b61ff", "#00838f"];

    function byId(id) {{
      return document.getElementById(id);
    }}

    function formatValue(value, digits = 2) {{
      if (typeof value !== "number") return String(value);
      return value.toFixed(digits);
    }}

    function renderMeta() {{
      const input = report.overview.input_parameters;
      const items = [
        ["Demand Mode", input.demand_mode],
        ["Duration", `${{input.duration_s}}s`],
        ["AGV Counts", input.agv_counts.join(", ")],
        ["Seeds", input.random_seeds.join(", ")],
        ["Battery", input.battery.enabled ? "On" : "Off"],
        ["Language", input.report_language],
      ];
      byId("meta-grid").innerHTML = items.map(([label, value]) => `
        <div class="panel metric-kv">
          <span class="label">${{label}}</span>
          <strong>${{value}}</strong>
        </div>
      `).join("");
    }}

    function renderOverview() {{
      byId("eyebrow").textContent = `run_id=${{report.overview.run_id}}`;
      byId("title").textContent = "실험 결과 리포트";
      byId("winner").textContent = report.overview.top_winner || "-";
      byId("hero-summary").innerHTML = report.overview.summary
        .map(line => `<div>${{line}}</div>`)
        .join("");
    }}

    function renderTopologyDefinitions() {{
      const seen = new Set();
      const items = [];
      for (const t of report.per_topology) {{
        const code = t.topology_type;
        if (seen.has(code)) continue;
        seen.add(code);
        items.push({{ code, desc: t.description || {{}} }});
      }}
      // Stabilize order: A, B, C, D, E first, others appended.
      const order = ["A", "B", "C", "D", "E"];
      items.sort((x, y) => {{
        const ix = order.indexOf(x.code);
        const iy = order.indexOf(y.code);
        const xi = ix < 0 ? order.length : ix;
        const yi = iy < 0 ? order.length : iy;
        return xi - yi;
      }});
      const html = items.map(({{ code, desc }}) => `
        <div class="topology-def">
          <div class="def-head">
            <span class="def-tag">${{code}}</span>
            <span class="def-headline">${{desc.headline || ""}}</span>
          </div>
          <dl>
            <dt>차선</dt><dd>${{desc.lanes || "—"}}</dd>
            <dt>방향</dt><dd>${{desc.direction || "—"}}</dd>
            <dt>충돌 처리</dt><dd>${{desc.conflict || "—"}}</dd>
          </dl>
          ${{desc.details ? `<p class="def-details">${{desc.details}}</p>` : ""}}
        </div>
      `).join("");
      byId("topology-defs").innerHTML = html;
    }}

    function renderTopologyCards() {{
      const cards = report.per_topology.map((item, idx) => {{
        const metrics = item.aggregate_metrics;
        const strengthChips = item.strengths.map(s => `<span class="chip good">${{s}}</span>`).join("");
        const weaknessChips = item.weaknesses.map(s => `<span class="chip bad">${{s}}</span>`).join("");
        const playbackBtn = item.playback_link
          ? `<a class="playback-btn" href="${{item.playback_link}}" target="_blank">시뮬레이션 재생 →</a>`
          : `<span class="playback-btn disabled" title="이 토폴로지의 playback이 생성되지 않았습니다 (showcase 실험을 함께 실행하면 활성화됩니다)">재생 없음</span>`;
        const desc = item.description || {{}};
        const headlineHtml = desc.headline
          ? `<p class="topology-headline">${{desc.headline}}</p>`
          : "";
        return `
          <article class="panel">
            <div class="chart-head">
              <div>
                <div class="eyebrow">${{item.topology_type}}</div>
                <h3>${{item.topology_variant}}</h3>
              </div>
              <div class="metric-kv">
                <span class="label">평균 순위</span>
                <strong>${{formatValue(metrics.avg_rank, 2)}}</strong>
              </div>
            </div>
            ${{headlineHtml}}
            <div class="meta-grid" style="grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 12px;">
              <div class="metric-kv"><span class="label">완료율</span><strong>${{formatValue(metrics.avg_completion_rate, 4)}}</strong></div>
              <div class="metric-kv"><span class="label">처리량</span><strong>${{formatValue(metrics.avg_demand_throughput_per_hour, 1)}}/h</strong></div>
              <div class="metric-kv"><span class="label">대기</span><strong>${{formatValue(metrics.avg_total_wait_time_s, 1)}}s</strong></div>
              <div class="metric-kv"><span class="label">지배 병목</span><strong>${{item.dominant_bottleneck}}</strong></div>
            </div>
            <p style="margin-top: 12px; line-height: 1.5;">${{item.tradeoff_summary}}</p>
            <p class="muted" style="margin-top: 8px;">권장 운영: ${{item.recommended_use_case}}</p>
            <div class="chips">${{strengthChips}}${{weaknessChips}}</div>
            <div style="margin-top: 12px;">${{playbackBtn}}</div>
          </article>
        `;
      }});
      byId("topology-cards").innerHTML = cards.join("");
    }}

    function renderComparisons() {{
      byId("comparison-list").innerHTML = report.comparisons.map(item => `
        <div class="comparison-item">
          <div class="eyebrow">${{item.lhs}} vs ${{item.rhs}}</div>
          <div style="margin-top: 6px; line-height: 1.6;">${{item.interpretation}}</div>
        </div>
      `).join("");
    }}

    function svgLineChart(title, seriesList) {{
      const width = 640;
      const height = 240;
      const pad = {{ top: 20, right: 18, bottom: 28, left: 42 }};
      const values = seriesList.flatMap(s => s.points.map(p => p.mean));
      const xValues = [...new Set(seriesList.flatMap(s => s.points.map(p => p.agv_count)))].sort((a, b) => a - b);
      const minY = values.length ? Math.min(...values) : 0;
      const maxY = values.length ? Math.max(...values) : 1;
      const ySpan = maxY - minY || 1;
      const xSpan = Math.max(xValues.length - 1, 1);
      function xPos(index) {{
        return pad.left + ((width - pad.left - pad.right) * index / xSpan);
      }}
      function yPos(value) {{
        return height - pad.bottom - ((value - minY) / ySpan) * (height - pad.top - pad.bottom);
      }}
      const grid = [0, 0.25, 0.5, 0.75, 1].map(t => {{
        const y = pad.top + (height - pad.top - pad.bottom) * t;
        return `<line class="grid" x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" />`;
      }}).join("");
      const xAxisLabels = xValues.map((value, idx) => `
        <text x="${{xPos(idx)}}" y="${{height - 8}}" text-anchor="middle" font-size="11" fill="#5a6877">${{value}}</text>
      `).join("");
      const yAxisLabels = [0, 0.25, 0.5, 0.75, 1].map(t => {{
        const value = maxY - ySpan * t;
        const y = pad.top + (height - pad.top - pad.bottom) * t + 4;
        return `<text x="${{pad.left - 8}}" y="${{y}}" text-anchor="end" font-size="11" fill="#5a6877">${{formatValue(value, 1)}}</text>`;
      }}).join("");
      const lines = seriesList.map((series, idx) => {{
        const color = palette[idx % palette.length];
        const path = series.points.map((point, pointIdx) => `${{pointIdx === 0 ? "M" : "L"}} ${{xPos(xValues.indexOf(point.agv_count))}} ${{yPos(point.mean)}}`).join(" ");
        const points = series.points.map(point => `
          <circle class="series-point" cx="${{xPos(xValues.indexOf(point.agv_count))}}" cy="${{yPos(point.mean)}}" r="4" fill="${{color}}" />
        `).join("");
        return `
          <path class="series-line" d="${{path}}" stroke="${{color}}" />
          ${{points}}
        `;
      }}).join("");
      const legend = seriesList.map((series, idx) => `
        <span class="legend-item"><span class="swatch" style="background:${{palette[idx % palette.length]}}"></span>${{series.topology_variant}}</span>
      `).join("");
      return `
        <div class="chart-card panel">
          <div class="chart-head">
            <h3>${{title}}</h3>
            <div class="legend">${{legend}}</div>
          </div>
          <svg viewBox="0 0 ${{width}} ${{height}}" aria-label="${{title}}">
            ${{grid}}
            <line class="axis" x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" />
            <line class="axis" x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" />
            ${{xAxisLabels}}
            ${{yAxisLabels}}
            ${{lines}}
          </svg>
        </div>
      `;
    }}

    function renderCharts() {{
      const chartMap = [
        ["completion_by_agv", "완료율"],
        ["throughput_by_agv", "처리량 (demands/h)"],
        ["wait_by_agv", "대기시간 (s)"],
        ["headon_by_agv", "정면 교행 충돌"],
      ];
      byId("chart-grid").innerHTML = chartMap
        .map(([key, title]) => svgLineChart(title, report.chart_series[key] || []))
        .join("");
    }}

    renderOverview();
    renderMeta();
    renderTopologyDefinitions();
    renderTopologyCards();
    renderComparisons();
    renderCharts();
  </script>
</body>
</html>
"""


# ── 배치 실험 러너 ─────────────────────────────────────────────
class ExperimentRunner:
    def __init__(self, config: ExperimentConfig) -> None:
        self.config = config
        self.out_dir = Path(config.output_dir) / config.run_id
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> list[RunResult]:
        cfg = self.config
        seeds = cfg.random_seeds or [cfg.random_seed]
        siding_placements = cfg.siding_placements or ["base"]
        siding_policies = cfg.type_b_siding_policies or ["reachable"]

        def _type_b_variants() -> list[dict[str, str]]:
            if cfg.type_b_variants:
                return [
                    {
                        "name": str(v.get("name", "")),
                        "siding_placement": str(v.get("siding_placement", "base")),
                        "siding_policy": str(v.get("siding_policy", "reachable")),
                    }
                    for v in cfg.type_b_variants
                ]
            variants = []
            for placement in siding_placements:
                for policy in siding_policies:
                    variants.append({
                        "name": f"{placement}_{policy}",
                        "siding_placement": placement,
                        "siding_policy": policy,
                    })
            return variants

        def _variants_for(t: str) -> list[dict[str, str]]:
            if t == "B":
                return _type_b_variants()
            return [{
                "name": "base",
                "siding_placement": "base",
                "siding_policy": "reachable",
            }]

        total = sum(
            len(cfg.agv_counts) * len(seeds) * len(_variants_for(t))
            for t in cfg.types
        )
        print(f"\n{'='*56}")
        print(f"FAB Topology Experiment  run_id={cfg.run_id}")
        print(f"Types: {cfg.types}  AGV counts: {cfg.agv_counts}")
        print(f"Seeds: {seeds}")
        if "B" in cfg.types:
            print(
                "Type B variants: "
                + ", ".join(
                    f"{v['siding_placement']}/{v['siding_policy']}"
                    for v in _type_b_variants()
                )
            )
        print(f"Duration: {cfg.duration_s}s  Total runs: {total}")
        print(f"{'='*56}\n")

        results: list[RunResult] = []
        idx = 0
        for seed in seeds:
            for t in cfg.types:
                for variant in _variants_for(t):
                    for n in cfg.agv_counts:
                        idx += 1
                        label = f"Type-{t}"
                        if t == "B":
                            label += (
                                f"/{variant['siding_placement']}"
                                f"/{variant['siding_policy']}"
                            )
                        print(
                            f"[{idx:2d}/{total}] seed={seed}  {label}  AGV={n:2d}  ...",
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
                                siding_placement=variant["siding_placement"],
                                siding_policy=variant["siding_policy"],
                                type_b_variant=variant["name"],
                                battery_enabled=cfg.battery_enabled,
                                battery_initial_pct=cfg.battery_initial_pct,
                                battery_charge_band_entry_pct=cfg.battery_charge_band_entry_pct,
                                battery_charge_assign_pct=cfg.battery_charge_assign_pct,
                                battery_charge_target_pct=cfg.battery_charge_target_pct,
                                battery_charge_rate_pct_per_s=cfg.battery_charge_rate_pct_per_s,
                                battery_base_drain_pct_per_hour=cfg.battery_base_drain_pct_per_hour,
                                battery_loaded_drain_pct_per_hour=cfg.battery_loaded_drain_pct_per_hour,
                                playback_trace_enabled=cfg.playback_trace_enabled,
                                playback_sample_interval_s=cfg.playback_sample_interval_s,
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
        placement_suffix = (
            f"_siding{r.siding_placement}"
            if r.topology_type == "B" and r.siding_placement != "base"
            else ""
        )
        policy_suffix = (
            f"_policy{r.siding_policy}"
            if r.topology_type == "B" and r.siding_policy != "reachable"
            else ""
        )
        d = self.out_dir / (
            f"{r.topology_type}{placement_suffix}{policy_suffix}_{r.n_agv:02d}agv_seed{r.random_seed}"
        )
        d.mkdir(exist_ok=True)
        (d / "kpi_summary.json").write_text(
            json.dumps(asdict(r), ensure_ascii=False, indent=2)
        )
        playback_trace = r.diagnostics.get("playback_trace")
        if playback_trace:
            trace_path = d / "playback_trace.json"
            html_path = d / "playback.html"
            trace_path.write_text(
                json.dumps(playback_trace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            html_path.write_text(build_playback_html(playback_trace), encoding="utf-8")

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
            "n_agv", "random_seed", "demand_mode", "siding_placement", "siding_policy",
            "type_b_variant", "rank", "winner",
            "topology_type", "topology_variant", "completion_rate", "task_acceptance_rate",
            "demands_completed", "demand_throughput_per_hour",
            "total_wait_time_s", "section_conflict_total",
            "followon_total", "headon_total",
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

        report = _build_report(self.config, results, self.out_dir)
        report_path = self.out_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report_html_path = self.out_dir / "report.html"
        report_html_path.write_text(_build_report_html(report), encoding="utf-8")

        print(f"\n결과 저장: {self.out_dir}/")
        print(f"  summary.csv  ({len(results)}행)")
        print(f"  summary.json")
        print(f"  ranking.csv")
        print(f"  ranking.json")
        print(f"  ranking_aggregate.json")
        print(f"  report.json")
        print(f"  report.html")

    def _print_matrix(self, results: list[RunResult]) -> None:
        """처리량 매트릭스 콘솔 출력."""
        cfg = self.config
        col_w = 10
        siding_placements = cfg.siding_placements or ["base"]
        siding_policies = cfg.type_b_siding_policies or ["reachable"]
        # 출력 레이블: Type B는 placement별로 분리
        def _type_labels() -> list[str]:
            labels = []
            for t in cfg.types:
                if t == "B":
                    if cfg.type_b_variants:
                        for v in cfg.type_b_variants:
                            labels.append(
                                f"B/{v.get('siding_placement', 'base')}/{v.get('siding_policy', 'reachable')}"
                            )
                    elif len(siding_placements) > 1 or len(siding_policies) > 1:
                        for p in siding_placements:
                            for policy in siding_policies:
                                labels.append(f"B/{p}/{policy}")
                    else:
                        labels.append("B")
                else:
                    labels.append(t)
            return labels

        def avg_for(label: str, n_agv: int, attr: str) -> float | None:
            parts = label.split("/")
            t = parts[0]
            placement = parts[1] if len(parts) > 1 else None
            policy = parts[2] if len(parts) > 2 else None
            values = [
                float(getattr(r, attr))
                for r in results
                if r.topology_type == t
                and r.n_agv == n_agv
                and not r.error
                and (placement is None or r.siding_placement == placement)
                and (policy is None or r.siding_policy == policy)
            ]
            if not values:
                return None
            return sum(values) / len(values)

        type_labels = _type_labels()

        def _print_matrix_section(title: str, attr: str, fmt: str) -> None:
            print(f"\n{title}")
            print(f"{'Type':10s}", end="")
            for n in cfg.agv_counts:
                print(f"{'AGV='+str(n):>{col_w}}", end="")
            print()
            print("─" * (10 + col_w * len(cfg.agv_counts)))
            for lbl in type_labels:
                print(f"{lbl:10s}", end="")
                for n in cfg.agv_counts:
                    value = avg_for(lbl, n, attr)
                    if value is not None:
                        print(f"{value:>{col_w}{fmt}}", end="")
                    else:
                        print(f"{'ERR':>{col_w}}", end="")
                print()

        print(f"\n{'─'*56}")
        _print_matrix_section("실제 demand 처리량 (demands/hour) 매트릭스", "demand_throughput_per_hour", ".1f")
        _print_matrix_section("실제 demand 완료율 매트릭스", "demand_completion_rate", ".4f")
        _print_matrix_section("가동률 (agv_utilization) 매트릭스", "agv_utilization", ".4f")
        _print_matrix_section("Head-on 충돌 횟수 매트릭스", "headon_total", ".1f")
        _print_matrix_section("Same-direction follow-on 차단 횟수 매트릭스", "followon_total", ".1f")

        _print_matrix_section("Critical section conflict 횟수 매트릭스", "section_conflict_total", ".1f")
        print(f"{'─'*56}\n")

        aggregate = _build_ranking_aggregate(_build_ranking_rows(results))
        if aggregate:
            print("Topology ranking summary")
            for row in aggregate:
                print(
                    f"Type {row['topology_variant']}: "
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
    siding_placements = raw.get("siding_placements")
    type_b_siding_policies = raw.get("type_b_siding_policies")
    type_b_variants = raw.get("type_b_variants")
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
        siding_placements = (
            [str(p) for p in siding_placements]
            if siding_placements is not None else ["base"]
        ),
        type_b_siding_policies = (
            [str(p) for p in type_b_siding_policies]
            if type_b_siding_policies is not None else ["reachable"]
        ),
        type_b_variants = (
            [
                {
                    "name": str(v.get("name", "")),
                    "siding_placement": str(v.get("siding_placement", "base")),
                    "siding_policy": str(v.get("siding_policy", "reachable")),
                }
                for v in type_b_variants
            ]
            if type_b_variants is not None else None
        ),
        battery_enabled = bool(raw.get("battery_enabled", False)),
        battery_initial_pct = float(raw.get("battery_initial_pct", 100.0)),
        battery_charge_band_entry_pct = float(raw.get("battery_charge_band_entry_pct", 40.0)),
        battery_charge_assign_pct = float(raw.get("battery_charge_assign_pct", 30.0)),
        battery_charge_target_pct = float(raw.get("battery_charge_target_pct", 90.0)),
        battery_charge_rate_pct_per_s = float(raw.get("battery_charge_rate_pct_per_s", 1.0)),
        battery_base_drain_pct_per_hour = float(raw.get("battery_base_drain_pct_per_hour", 8.0)),
        battery_loaded_drain_pct_per_hour = float(raw.get("battery_loaded_drain_pct_per_hour", 12.0)),
        report_language = str(raw.get("report_language", "ko")),
        playback_trace_enabled = bool(raw.get("playback_trace_enabled", False)),
        playback_sample_interval_s = float(raw.get("playback_sample_interval_s", 0.5)),
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
            report_language= "ko",
        )

    runner = ExperimentRunner(config)
    runner.run()


if __name__ == "__main__":
    main()
