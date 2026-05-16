"""
실 SimulationEngine 을 Quick Sim 백엔드에서 돌리기 위한 wrapper.

  - graph → 정규화된 map JSON 변환 (프론트가 SVG 렌더할 형태)
  - SimulationEngine async run loop 을 백그라운드 task 로 실행
  - tick 마다 AGV 상태/KPI snapshot 추출 → broadcast 콜백 호출
  - speed 비례 wall-clock 페이싱 (실제 sim_time 진행은 엔진이 dt 누적으로 처리하므로
    여기선 wall-clock 슬립으로 throttle 만 함)
  - rolling 60s KPI: tasks/h, utilization, head-on, avg wait

엔진 mutation 안 함 — params 는 init 시점에만 적용. B-pure UX.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from src.adapters.bus.adapters import LocalMemoryBus
from src.analytics.playback_trace import PlaybackTraceRecorder
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.agv.fsm import AGVState
from src.domain.map.graph import MapGraph, NodeRole
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler


# ── 노드 kind 매핑 ─────────────────────────────────────────────
def _node_kind(node) -> str:
    if node.is_charger or node.role == NodeRole.CHARGER:
        return "charger"
    if node.role == NodeRole.WORK or node.is_parking_spot:
        return "station"
    if node.is_holding_point:
        return "holding"
    if node.role == NodeRole.SIDING:
        return "siding"
    return "wp"


def graph_to_map_json(graph: MapGraph) -> dict:
    """엔진 MapGraph → playback_trace._serialize_map 과 동일한 형태.
    node_id/role/is_charger 등 playback JS 가 기대하는 필드명 사용."""
    nodes = []
    for n in graph.nodes.values():
        nodes.append({
            "node_id": n.node_id,
            "x": float(n.x),
            "y": float(n.y),
            "role": n.role.value if hasattr(n.role, "value") else str(n.role),
            "is_charger": bool(n.is_charger),
            "is_parking_spot": bool(getattr(n, "is_parking_spot", False)),
        })
    edges = []
    for e in graph.edges.values():
        src = graph.nodes.get(e.start_node_id)
        dst = graph.nodes.get(e.end_node_id)
        if src is None or dst is None:
            continue
        edge_key = f"{e.start_node_id}__{e.end_node_id}"
        edges.append({
            "edge_id": e.edge_id,
            "edge_key": edge_key,
            "start_node_id": e.start_node_id,
            "end_node_id": e.end_node_id,
            "x1": float(src.x),
            "y1": float(src.y),
            "x2": float(dst.x),
            "y2": float(dst.y),
            "corridor": e.corridor or "",
            "access_type": e.access_type or "",
            "width_m": float(getattr(e, "width_m", 0.0)),
            "bidirectional": bool(getattr(e, "bidirectional", False)),
        })
    return {"nodes": nodes, "edges": edges}


# ── 상태 매핑 ──────────────────────────────────────────────────
def _agv_state_to_ui(state: AGVState) -> str:
    """playback_trace snapshot 과 동일한 state 문자열 반환."""
    return state.value if hasattr(state, "value") else str(state)


# ── Rolling KPI ────────────────────────────────────────────────
class RollingKpi:
    """60s 슬라이딩 윈도우 KPI 계산.

    - tasksPerHr: 윈도우 내 demand 완료 수 → /h 로 환산
    - utilization: 윈도우 내 NAVIGATING+PROCESSING 비율 (모든 AGV 평균)
    - headOn: cumulative head-on 카운트 (윈도우 차분 가능, 단순 cumulative 로 시작)
    - avgWait: 윈도우 내 WAITING 비율 × 윈도우 길이 → 평균 대기 추정
    - trends: 직전 윈도우 대비 % 변화
    """
    WINDOW_S = 60.0

    def __init__(self):
        # (sim_time, value) 형태의 deque
        self._utilization_samples: deque[tuple[float, float]] = deque()
        self._wait_samples: deque[tuple[float, float]] = deque()
        self._demand_completed_history: deque[tuple[float, int]] = deque()
        self._headon_history: deque[tuple[float, int]] = deque()
        # trend 계산용: 직전 window 값
        self._prev_window: dict[str, float] = {}

    def _trim(self, q: deque, sim_time: float) -> None:
        cutoff = sim_time - self.WINDOW_S
        while q and q[0][0] < cutoff:
            q.popleft()

    def update(
        self,
        sim_time: float,
        agv_states: list[AGVState],
        demand_completed_total: int,
        headon_total: int,
    ) -> dict:
        # 가동률 (NAV + PROC)
        util = sum(1 for s in agv_states
                   if s in (AGVState.NAVIGATING, AGVState.PROCESSING)) / max(1, len(agv_states))
        wait = sum(1 for s in agv_states
                   if s == AGVState.WAITING_RESERVATION) / max(1, len(agv_states))
        self._utilization_samples.append((sim_time, util))
        self._wait_samples.append((sim_time, wait))
        self._demand_completed_history.append((sim_time, demand_completed_total))
        self._headon_history.append((sim_time, headon_total))
        for q in (self._utilization_samples, self._wait_samples,
                  self._demand_completed_history, self._headon_history):
            self._trim(q, sim_time)

        # 평균 utilization / wait
        avg_util = (sum(v for _, v in self._utilization_samples)
                    / max(1, len(self._utilization_samples)))
        avg_wait_ratio = (sum(v for _, v in self._wait_samples)
                          / max(1, len(self._wait_samples)))
        # avg_wait_s 추정: ratio × WINDOW = 윈도우 안에서 한 AGV 가 평균적으로 대기한 시간
        avg_wait_s = avg_wait_ratio * self.WINDOW_S

        # tasks/h: 윈도우 시작 시점의 누적치 → 현재 누적치 차이 ÷ 윈도우 길이
        if len(self._demand_completed_history) >= 2:
            t0, c0 = self._demand_completed_history[0]
            t1, c1 = self._demand_completed_history[-1]
            elapsed = max(t1 - t0, 0.001)
            tasks_per_h = (c1 - c0) * 3600.0 / elapsed
        else:
            tasks_per_h = 0.0

        # head-on: cumulative
        headon_cum = headon_total

        current = {
            "tasksPerHr": tasks_per_h,
            "utilization": avg_util,
            "headOn": headon_cum,
            "avgWait": avg_wait_s,
        }
        # trend: prev_window 가 있으면 % 변화
        trends = {}
        for k, v in current.items():
            prev = self._prev_window.get(k)
            if prev is None or abs(prev) < 1e-6:
                trends[k] = 0.0
            else:
                trends[k] = (v - prev) / prev * 100.0
        # WINDOW 절반 지날 때마다 prev_window 갱신 (간이 trend)
        if (sim_time // 30.0) != (self._prev_window.get("__t_bucket", -1)):
            self._prev_window = dict(current)
            self._prev_window["__t_bucket"] = sim_time // 30.0

        return {**current, "trends": trends}


# ── 메인 runner ────────────────────────────────────────────────
class RealRunner:
    """단일 sim 인스턴스. server.py 에서 task 로 띄움.

    broadcast: async (msg: dict) -> None  — WS push 콜백
    """

    def __init__(
        self,
        topology: str,
        agv_count: int,
        speed: float,
        duration: float,
        blocked_edges: set[str],
        broadcast: Callable[[dict], Awaitable[None]],
        random_seed: int = 42,
        push_interval_s: float = 0.1,  # wall-clock push 주기
        task_interval_s: float = 5.0,
        imported_graph: Optional[MapGraph] = None,  # 외부 맵 임포트 시 전달
    ) -> None:
        self.topology = topology
        self.agv_count = max(1, agv_count)
        self.speed = max(0.1, speed)
        self.duration = max(1.0, duration)
        self.blocked_edges = set(blocked_edges)
        self.broadcast = broadcast
        self.random_seed = random_seed
        self.push_interval_s = push_interval_s
        self.task_interval_s = max(0.5, task_interval_s)
        self._imported_graph = imported_graph
        self._stop = False
        self.sim_time = 0.0
        self._kpi = RollingKpi()
        self._engine: Optional[SimulationEngine] = None
        self._graph: Optional[MapGraph] = None
        self._agvs: list[AGV] = []
        self._scheduler: Optional[TimeWindowScheduler] = None
        self._task_gen: Optional[TaskGenerator] = None
        self._recorder: Optional[PlaybackTraceRecorder] = None
        self._last_sent_event_idx: int = 0

    # ── 외부 컨트롤 ─────────────────────────────────────────
    def stop(self) -> None:
        self._stop = True

    @property
    def map_json(self) -> dict:
        if self._graph is None:
            raise RuntimeError("graph not built yet (call setup() first)")
        return graph_to_map_json(self._graph)

    @property
    def map_json_for_live(self) -> dict:
        """playback JS 와 동일한 node_id/edge_key 형태. live HTML 이 사용."""
        if self._recorder is None:
            raise RuntimeError("setup() not called")
        return self._recorder._serialize_map()

    # ── 엔진 셋업 ──────────────────────────────────────────
    def setup(self) -> None:
        random.seed(self.random_seed)
        if self._imported_graph is not None:
            # 외부 맵: topology generator 안 거치고 그대로 사용
            graph = self._imported_graph
        else:
            tgen = MapTopologyGenerator()
            type_code = self.topology.split("/")[0]
            siding_placement = "base"
            if type_code == "B" and "/" in self.topology:
                parts = self.topology.split("/")
                if len(parts) >= 2:
                    siding_placement = parts[1]
            graph = tgen.generate(type_code, siding_placement=siding_placement)
        # 사용자가 미리 지정한 차단 엣지: 그래프에 표시만 (실제 차단 X — 엔진 mutation
        # 금지 원칙. 향후 path-find 시 blocked_edges 인자로 전달하는 식으로 결선).
        # MVP: 시각적으로만 빨간색으로 노출, 경로 차단은 안 함 (B-pure 의 한계 명시).
        graph._user_blocked_edges = set(self.blocked_edges)
        self._graph = graph
        self._scheduler = TimeWindowScheduler()
        bus = LocalMemoryBus()
        self._task_gen = TaskGenerator(graph, bus, task_interval_s=self.task_interval_s)
        # recorder 를 엔진에 연결 → AGV/스케줄러가 이벤트 자동 기록
        self._recorder = PlaybackTraceRecorder(graph, sample_interval_s=0.5)
        self._engine = SimulationEngine(
            graph, self._scheduler,
            task_generator=self._task_gen,
            trace_recorder=self._recorder,
        )
        chargers = [n.node_id for n in graph.get_chargers()]
        if not chargers:
            raise RuntimeError("no chargers in topology")
        for i in range(self.agv_count):
            agv = AGV(f"AGV_{i+1:03d}", bus, graph, self._scheduler)
            agv.current_node_id = chargers[i % len(chargers)]
            agv.physics.x = graph.nodes[agv.current_node_id].x
            agv.physics.y = graph.nodes[agv.current_node_id].y
            self._engine.register_agv(agv)
            self._agvs.append(agv)

    # ── 메인 루프 (asyncio task 로 호출) ───────────────────
    async def run(self) -> None:
        if self._engine is None:
            raise RuntimeError("setup() not called")

        # SimulationEngine.run 의 시작 의식: task_generator + 각 AGV start.
        if self._engine.task_generator:
            await self._engine.task_generator.start()
        await asyncio.gather(*(agv.start() for agv in self._engine.agvs.values()))

        dt = 1.0 / self._engine.TICK_RATE_HZ  # 보통 0.1s
        next_push_wall = time.time()
        last_push_sim = 0.0
        # SimulationEngine._next_deadlock_check 는 deadlock 검사 타이밍.
        # 엔진 안에 `_check_and_resolve_deadlocks` 가 있고 SimulationEngine.run 이
        # DEADLOCK_CHECK_INTERVAL_S 마다 호출함. 우리도 동일 타이밍 재현.
        from src.application.engine.simulation_engine import DEADLOCK_CHECK_INTERVAL_S
        try:
            while not self._stop and self.sim_time < self.duration:
                # 한 wall iteration 동안 sim 을 (push_interval × speed) 만큼 진행.
                # 즉 speed=1 → wall 1s 에 sim 1s, speed=10 → wall 1s 에 sim 10s.
                sim_step_target = self.push_interval_s * self.speed
                steps = max(1, int(round(sim_step_target / dt)))
                for _ in range(steps):
                    if self._stop:
                        break
                    await self._engine._tick(dt)
                    # SimulationEngine.sim_time 도 우리 sim_time 도 같이 갱신.
                    # task_generator / AGV 가 engine.sim_time 을 참조하므로 필수.
                    self._engine.sim_time += dt
                    self.sim_time += dt
                    if self._engine.sim_time >= self._engine._next_deadlock_check:
                        await self._engine._check_and_resolve_deadlocks()
                        self._engine._next_deadlock_check = (
                            self._engine.sim_time + DEADLOCK_CHECK_INTERVAL_S
                        )
                    await asyncio.sleep(0)

                now = time.time()
                if now < next_push_wall:
                    await asyncio.sleep(next_push_wall - now)
                next_push_wall = time.time() + self.push_interval_s

                if self.sim_time - last_push_sim >= 0.5 or self._stop:
                    last_push_sim = self.sim_time
                    await self._publish_snapshot()
        finally:
            await self._publish_end()

    # ── snapshot 변환 ──────────────────────────────────────
    async def _publish_snapshot(self) -> None:
        if not self._engine or not self._scheduler or not self._recorder:
            return
        # recorder 가 engine._tick() 에서 이미 sample() 을 호출했으므로
        # 최신 snapshot 을 그대로 가져온다.
        rec_snaps = self._recorder.snapshots
        rec_events = self._recorder.events
        if not rec_snaps:
            return
        snapshot = rec_snaps[-1]

        # 이전 push 이후에 추가된 이벤트만 전송
        new_events = rec_events[self._last_sent_event_idx:]
        self._last_sent_event_idx = len(rec_events)

        # 엣지 밀도 계산: 모든 edge_enter 이벤트 누적
        edge_density = {}
        for ev in rec_events:
            if ev.get("kind") == "edge_enter":
                edge_key = ev.get("edge_key", "")
                if edge_key:
                    edge_density[edge_key] = edge_density.get(edge_key, 0) + 1

        # Rolling KPI
        agv_states = [a._fsm.state for a in self._agvs]
        ho_summary = self._scheduler.get_headon_summary()
        demand_done = 0
        if self._task_gen is not None and hasattr(self._task_gen, "_diagnostics"):
            demand_done = getattr(self._task_gen._diagnostics, "demands_completed", 0)
        kpi = self._kpi.update(
            sim_time=self.sim_time,
            agv_states=agv_states,
            demand_completed_total=demand_done,
            headon_total=ho_summary["headon_total"],
        )

        await self.broadcast({
            "type": "tick",
            "snapshot": snapshot,
            "new_events": new_events,
            "kpi": kpi,
            "edge_density": edge_density,
        })

    async def _publish_end(self) -> None:
        try:
            await self.broadcast({
                "type": "end",
                "reason": "stopped" if self._stop else "completed",
                "simTime": round(self.sim_time, 3),
            })
        except Exception:
            pass
