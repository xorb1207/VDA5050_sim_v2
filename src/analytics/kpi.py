from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV
    from src.domain.reservation.scheduler import TimeWindowScheduler


class KPICalculator:
    """
    11개 KPI 계산.
    AGV 인스턴스와 Scheduler에서 수집된 통계를 받아 계산.
    엔진 / AGV 내부 로직과 완전히 분리.
    """

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
        bottleneck_edges = [
            {"edge_id": eid, "occupancy_time_s": round(t, 2),
             "occupancy_rate": round(t / sim_time_s, 4),
             "congestion_score": edge_scores.get(eid, 0.0),
             "headon_count": scheduler._edge_headon_counts.get(eid, 0),
             "followon_count": scheduler._edge_followon_counts.get(eid, 0),
             "retry_count": scheduler._edge_retry_counts.get(eid, 0)}
            for eid, t in sorted(edge_times.items(), key=lambda x: x[1], reverse=True)
        ][:5]

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
