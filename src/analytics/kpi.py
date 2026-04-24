from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV
    from src.domain.reservation.scheduler import TimeWindowScheduler
    from src.domain.map.graph import Edge, MapGraph


class KPICalculator:
    """
    11개 KPI 계산.
    AGV 인스턴스와 Scheduler에서 수집된 통계를 받아 계산.
    엔진 / AGV 내부 로직과 완전히 분리.
    """

    @staticmethod
    def _find_graph_edge(graph: "MapGraph", edge_key: str) -> Optional["Edge"]:
        if "__" not in edge_key:
            return None
        src_id, dst_id = edge_key.split("__", 1)
        for eid in graph._out_edges.get(src_id, []):
            edge = graph.edges.get(eid)
            if edge and edge.end_node_id == dst_id:
                return edge
        return None

    @staticmethod
    def _edge_section_key(edge: Optional["Edge"], topology_type: str) -> str:
        if edge is None:
            return ""
        if edge.access_type:
            facility_node_id = edge.end_node_id
            if edge.access_type == "station_access":
                if edge.start_node_id.startswith("ST_"):
                    facility_node_id = edge.start_node_id
                elif edge.end_node_id.startswith("ST_"):
                    facility_node_id = edge.end_node_id
            elif edge.access_type == "charger_access":
                if edge.start_node_id.startswith("CH_"):
                    facility_node_id = edge.start_node_id
                elif edge.end_node_id.startswith("CH_"):
                    facility_node_id = edge.end_node_id
            return f"access:{edge.access_type}:{facility_node_id}"
        if edge.corridor == "bay":
            return f"bay:{edge.start_node_id.split('_')[-1]}"
        if edge.corridor == "siding":
            return f"siding:{edge.start_node_id}->{edge.end_node_id}"
        if topology_type in ("B", "E") and edge.corridor in ("north", "center", "south"):
            a, b = sorted([edge.start_node_id, edge.end_node_id])
            return f"shared_corridor:{edge.corridor}:{a}<->{b}"
        if edge.corridor in (
            "north_l1",
            "north_l2",
            "center_l1",
            "center_l2",
            "south_l1",
            "south_l2",
        ):
            a, b = sorted([edge.start_node_id, edge.end_node_id])
            return f"lane:{edge.corridor}:{a}<->{b}"
        return ""

    @staticmethod
    def _edge_type(edge: Optional["Edge"], topology_type: str) -> str:
        if edge is None:
            return "unknown"
        if edge.access_type == "station_access":
            return "station_access"
        if edge.access_type == "charger_access":
            return "charger_access"
        if edge.corridor == "bay":
            return "bay"
        if edge.corridor == "siding":
            return "siding"
        if topology_type in ("B", "E") and edge.corridor in ("north", "center", "south"):
            return "shared_corridor"
        if edge.corridor in (
            "north_l1",
            "north_l2",
            "center_l1",
            "center_l2",
            "south_l1",
            "south_l2",
        ):
            return "lane"
        return "main_corridor"

    @staticmethod
    def _dominant_edge_cause(
        *,
        headon_count: int,
        followon_count: int,
        section_conflict_count: int,
        retry_count: int,
    ) -> str:
        candidates = [
            ("section_conflict", section_conflict_count),
            ("headon", headon_count),
            ("followon", followon_count),
            ("retry", retry_count),
        ]
        label, value = max(candidates, key=lambda item: item[1])
        return label if value > 0 else "occupancy"

    def compute(
        self,
        agvs: dict[str, AGV],
        scheduler: TimeWindowScheduler,
        sim_time_s: float,
    ) -> dict:
        if sim_time_s <= 0:
            return {}

        hours = sim_time_s / 3600.0
        n_agv = max(len(agvs), 1)

        # ── 1. Throughput ───────────────────────────────────────────
        total_tasks = sum(a._task_count for a in agvs.values())
        throughput  = round(total_tasks / hours, 2) if hours else 0.0

        # ── 2. Time Efficiency ─────────────────────────────────────
        all_completion = [
            t for a in agvs.values()
            for t in a._task_completion_times
        ]
        avg_task_completion = (
            round(sum(all_completion) / len(all_completion), 2)
            if all_completion else 0.0
        )

        total_wait   = sum(a._wait_time_s for a in agvs.values())
        avg_wait     = round(total_wait / n_agv, 2)
        total_restart_delay = sum(a._restart_delay_time_s for a in agvs.values())
        charging_sessions = sum(a._charging_sessions for a in agvs.values())
        total_charging_time = sum(a._charging_time_s for a in agvs.values())
        low_battery_charge_requests = sum(
            a._low_battery_charge_requests for a in agvs.values()
        )
        avg_battery_pct = round(
            sum(a._battery_pct for a in agvs.values()) / n_agv,
            2,
        )
        min_battery_pct = round(
            min((a._min_battery_pct for a in agvs.values()), default=100.0),
            2,
        )

        # ── 3. Traffic Efficiency ──────────────────────────────────
        total_attempts = scheduler._reserve_success + scheduler._reserve_failure
        reservation_failure_rate = (
            round(scheduler._reserve_failure / total_attempts, 4)
            if total_attempts > 0 else 0.0
        )
        reroute_count = sum(a._reroute_count for a in agvs.values())

        # ── 4. Resource Utilization ────────────────────────────────
        # 노드 점유율: 예약 점유 시간 합 / (전체 노드 수 × sim_time)
        n_nodes = max(len(scheduler._node_occupancy_time), 1)
        total_node_occ = sum(scheduler._node_occupancy_time.values())
        node_occupancy_rate = round(
            total_node_occ / (n_nodes * sim_time_s), 4
        ) if sim_time_s > 0 else 0.0

        # 에지 점유율: AGV들의 에지별 점유 시간 합 / (전체 에지 수 × sim_time)
        edge_times: dict[str, float] = {}
        for a in agvs.values():
            for eid, t in a._edge_time.items():
                edge_times[eid] = edge_times.get(eid, 0.0) + t
        n_edges = max(len(edge_times), 1)
        total_edge_occ = sum(edge_times.values())
        edge_occupancy_rate = round(
            total_edge_occ / (n_edges * sim_time_s), 4
        ) if sim_time_s > 0 else 0.0

        # AGV 가동률: NAVIGATING + PROCESSING 시간 / sim_time
        active_time = sum(
            a._state_time.get("NAVIGATING", 0.0) +
            a._state_time.get("PROCESSING", 0.0)
            for a in agvs.values()
        )
        agv_utilization = round(active_time / (n_agv * sim_time_s), 4)

        # ── 5. Bottleneck ──────────────────────────────────────────
        # node: congestion_score 기준 Top N
        node_scores = scheduler.get_all_scores()
        bottleneck_nodes = [
            {"node_id": nid, "congestion_score": score,
             "occupancy_time_s": round(scheduler._node_occupancy_time.get(nid, 0.0), 2)}
            for nid, score in sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
            if score > 0
        ][:5]

        # edge: 점유 시간 기준 Top N
        edge_scores = scheduler.get_all_edge_scores()
        sample_agv = next(iter(agvs.values()), None)
        graph = sample_agv._graph if sample_agv is not None else None
        topology_type = getattr(graph, "_topology_type", "") if graph is not None else ""
        bottleneck_edges = []
        for eid, t in sorted(edge_times.items(), key=lambda x: x[1], reverse=True)[:5]:
            edge = self._find_graph_edge(graph, eid) if graph is not None else None
            section_key = self._edge_section_key(edge, topology_type)
            section_conflict_count = scheduler._section_conflict_counts.get(section_key, 0)
            headon_count = scheduler._edge_headon_counts.get(eid, 0)
            followon_count = scheduler._edge_followon_counts.get(eid, 0)
            retry_count = scheduler._edge_retry_counts.get(eid, 0)
            bottleneck_edges.append({
                "edge_id": eid,
                "graph_edge_id": edge.edge_id if edge is not None else "",
                "edge_type": self._edge_type(edge, topology_type),
                "corridor": edge.corridor if edge is not None else "",
                "access_type": edge.access_type if edge is not None else "",
                "section_key": section_key,
                "occupancy_time_s": round(t, 2),
                "occupancy_rate": round(t / sim_time_s, 4),
                "congestion_score": edge_scores.get(eid, 0.0),
                "headon_count": headon_count,
                "followon_count": followon_count,
                "section_conflict_count": section_conflict_count,
                "retry_count": retry_count,
                "dominant_cause": self._dominant_edge_cause(
                    headon_count=headon_count,
                    followon_count=followon_count,
                    section_conflict_count=section_conflict_count,
                    retry_count=retry_count,
                ),
            })

        headon_summary = scheduler.get_headon_summary()

        # ── 6. Stability ───────────────────────────────────────────
        # stall은 engine의 deadlock_count에서 가져옴 (별도 주입)
        max_queue = max(
            (len([r for r in rs if not r.released])
             for rs in scheduler._reservations.values()),
            default=0,
        )

        return {
            # 1. Throughput
            "throughput_tasks_per_hour":   throughput,
            "tasks_completed":             total_tasks,

            # 2. Time Efficiency
            "avg_task_completion_time_s":  avg_task_completion,
            "avg_wait_time_s":             avg_wait,
            "total_wait_time_s":           round(total_wait, 2),
            "total_restart_delay_s":       round(total_restart_delay, 2),
            "charging_sessions":           charging_sessions,
            "total_charging_time_s":       round(total_charging_time, 2),
            "low_battery_charge_requests": low_battery_charge_requests,
            "avg_battery_pct":             avg_battery_pct,
            "min_battery_pct":             min_battery_pct,

            # 3. Traffic Efficiency
            "reservation_failure_rate":    reservation_failure_rate,
            "reroute_count":               reroute_count,
            "headon_total":                headon_summary["headon_total"],
            "followon_total":              headon_summary["followon_total"],
            "section_conflict_total":      headon_summary["section_conflict_total"],
            "retry_total":                 headon_summary["retry_total"],
            "avg_retry_per_headon":        headon_summary["avg_retry_per_headon"],
            "top_headon_edges":            headon_summary["top_headon_edges"],
            "itinerary_success":           headon_summary["itinerary_success"],
            "itinerary_failure":           headon_summary["itinerary_failure"],
            "top_section_conflicts":       headon_summary["top_section_conflicts"],

            # 4. Resource Utilization
            "node_occupancy_rate":         node_occupancy_rate,
            "edge_occupancy_rate":         edge_occupancy_rate,
            "agv_utilization":             agv_utilization,
            "total_travel_distance_m":     round(
                sum(a._travel_distance_m for a in agvs.values()), 2
            ),

            # 5. Bottleneck
            "bottleneck_nodes":            bottleneck_nodes,
            "bottleneck_edges":            bottleneck_edges,

            # 6. Stability
            "max_queue_length":            max_queue,
            "deadlock_or_stall_count":     0,  # engine에서 주입
        }
