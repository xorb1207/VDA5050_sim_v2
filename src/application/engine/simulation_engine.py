from __future__ import annotations

import asyncio
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV
    from src.domain.map.graph import MapGraph
    from src.domain.reservation.scheduler import TimeWindowScheduler
    from src.application.scenario.task_generator import TaskGenerator
    from src.domain.policy.traffic_policy import TrafficPolicy
    from src.analytics.playback_trace import PlaybackTraceRecorder

# Deadlock 감지 주기 (초)
DEADLOCK_CHECK_INTERVAL_S: float = 5.0


class SimulationEngine:
    """
    단일 asyncio 이벤트 루프 위에서 동작하는 시뮬레이션 틱 제어.
    Deadlock 감지 루프 추가.
    """

    TICK_RATE_HZ: int = 50

    def __init__(
        self,
        graph: MapGraph,
        scheduler: TimeWindowScheduler,
        task_generator: Optional[TaskGenerator] = None,
        policy: Optional[TrafficPolicy] = None,
        trace_recorder: Optional["PlaybackTraceRecorder"] = None,
    ) -> None:
        self.graph     = graph
        self.scheduler = scheduler
        self.task_generator = task_generator
        self.policy    = policy
        self.trace_recorder = trace_recorder
        self.agvs:     dict[str, AGV] = {}
        self.sim_time: float = 0.0
        self._running: bool  = False

        self._deadlock_count:    int   = 0
        self._next_deadlock_check: float = DEADLOCK_CHECK_INTERVAL_S

        if policy and policy.lane_mode == "one_way":
            self._apply_one_way(graph)
        if self.trace_recorder is not None:
            setattr(self.scheduler, "_trace_recorder", self.trace_recorder)
            setattr(self.graph, "_trace_recorder", self.trace_recorder)

    @staticmethod
    def _apply_one_way(graph: MapGraph) -> None:
        rev_ids = [eid for eid in list(graph.edges.keys()) if eid.endswith("_rev")]
        for eid in rev_ids:
            graph.edges.pop(eid)
        graph._out_edges = {nid: [] for nid in graph.nodes}
        for eid, edge in graph.edges.items():
            graph._out_edges.setdefault(edge.start_node_id, []).append(eid)

    def register_agv(self, agv: AGV) -> None:
        self.agvs[agv.agv_id] = agv

    async def run(self, duration_s: float) -> dict:
        self._running = True
        dt = 1.0 / self.TICK_RATE_HZ

        if self.task_generator:
            await self.task_generator.start()
        await asyncio.gather(*(agv.start() for agv in self.agvs.values()))

        while self.sim_time < duration_s and self._running:
            await self._tick(dt)
            self.sim_time += dt

            # Deadlock 감지 (주기적)
            if self.sim_time >= self._next_deadlock_check:
                await self._check_and_resolve_deadlocks()
                self._next_deadlock_check = self.sim_time + DEADLOCK_CHECK_INTERVAL_S

            await asyncio.sleep(0)

        self._running = False
        return self._build_analytics()

    async def _tick(self, dt: float) -> None:
        coros = [agv.tick(dt, self.sim_time) for agv in self.agvs.values()]
        if self.task_generator:
            coros.append(self.task_generator.step(self.sim_time, self.agvs))
        await asyncio.gather(*coros)
        if self.trace_recorder is not None:
            self.trace_recorder.sample(self.sim_time, self.agvs, self.scheduler)

    async def _check_and_resolve_deadlocks(self) -> None:
        """
        대기 그래프에서 순환 감지.
        사이클 발견 시 우선순위 낮은 AGV를 강제 재계획.
        """
        cycles = self.scheduler.detect_deadlock()
        for cycle in cycles:
            self._deadlock_count += 1
            victim_id = self.scheduler.resolve_deadlock(cycle)
            victim = self.agvs.get(victim_id)
            if victim:
                if self.trace_recorder is not None:
                    self.trace_recorder.record_event(
                        "deadlock_resolved",
                        self.sim_time,
                        agv_id=victim_id,
                        cycle=cycle,
                    )
                # victim AGV 강제 재계획
                self.scheduler.clear_waiting(victim_id)
                asyncio.create_task(victim._reroute(self.sim_time))

    def _build_analytics(self) -> dict:
        from src.analytics.kpi import KPICalculator
        kpis = KPICalculator().compute(self.agvs, self.scheduler, self.sim_time)
        kpis["sim_time_s"]               = round(self.sim_time, 2)
        kpis["avg_wait_per_agv_s"]       = kpis.get("avg_wait_time_s", 0.0)
        kpis["deadlock_or_stall_count"]  = self._deadlock_count
        return kpis
