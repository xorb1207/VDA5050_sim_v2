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
from src.domain.fleet import Fleet, get_eligible_agvs
from src.domain.map.graph import MapGraph, NodeRole
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler


# ── Fleet 색 기본 팔레트 (3 fleet 까지 충돌 적은 채도) ─────────
_DEFAULT_FLEET_COLORS = ("#0f9d58", "#2563eb", "#e0a000", "#9333ea", "#dc2626")


def _edge_key_to_tuple(edge_key: str) -> Optional[tuple[str, str]]:
    """edge_key "src__dst" → (src, dst) tuple. 형식 오류면 None."""
    if not edge_key or "__" not in edge_key:
        return None
    src, _, dst = edge_key.partition("__")
    if not src or not dst:
        return None
    return (src, dst)


def _agv_planned_edge_keys(agv) -> list[str]:
    """AGV 현재 _path 에서 남은 edge_key 시퀀스 추출. 차단 영향 판별용."""
    path = getattr(agv, "_path", None)
    if not path or len(path) < 2:
        return []
    idx = max(0, getattr(agv, "_path_index", 0) - 1)
    out: list[str] = []
    for i in range(idx, len(path) - 1):
        out.append(f"{path[i]}__{path[i + 1]}")
    return out


def _build_fleets(
    fleet_defs: list[dict] | None,
    total_agv_count: int,
    count_overrides: dict[str, int] | None = None,
) -> list[Fleet]:
    """ImportedMap.fleets (raw dict 리스트) → Fleet 객체 리스트.

    fleet_defs 가 없거나 비어있으면 단일 fleet ("default") 생성 — total_agv_count 사용.
    count_overrides 가 있으면 해당 fleet 의 count 를 덮어씀 (UI 슬라이더 값).
    """
    co = count_overrides or {}
    if not fleet_defs:
        return [Fleet(
            id="default",
            graph_idx=0,
            color=_DEFAULT_FLEET_COLORS[0],
            count=max(1, total_agv_count),
        )]
    fleets: list[Fleet] = []
    for i, raw in enumerate(fleet_defs):
        fid = str(raw.get("id", f"FLEET_{i+1}"))
        cnt_override = co.get(fid)
        try:
            cnt_default = int(raw.get("count", 1))
        except (TypeError, ValueError):
            cnt_default = 1
        cnt = int(cnt_override) if cnt_override is not None else cnt_default
        try:
            max_speed = float(raw.get("max_speed_mps", 1.5))
        except (TypeError, ValueError):
            max_speed = 1.5
        try:
            priority = int(raw.get("priority", i + 1))
        except (TypeError, ValueError):
            priority = i + 1
        try:
            gi = int(raw.get("graph_idx", 0))
        except (TypeError, ValueError):
            gi = 0
        color = raw.get("color") or _DEFAULT_FLEET_COLORS[i % len(_DEFAULT_FLEET_COLORS)]
        caps = list(raw.get("capabilities", []) or [])
        fleets.append(Fleet(
            id=fid,
            graph_idx=gi,
            capabilities=caps,
            color=color,
            max_speed_mps=max_speed,
            priority=priority,
            count=max(0, cnt),
        ))
    return fleets


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
        imported_fleets: Optional[list[dict]] = None,  # F1a: 임포트 맵의 fleet 정의
        agv_count_by_fleet: Optional[dict[str, int]] = None,  # F1a: UI 슬라이더 override
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
        self._imported_fleets = list(imported_fleets or [])
        self._agv_count_by_fleet = dict(agv_count_by_fleet or {})
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
        # F1a: fleets 와 AGV→fleet 매핑
        self.fleets: list[Fleet] = []
        self._fleet_by_agv: dict[str, Fleet] = {}
        # Rolling demand-completed 누적치 (fleet 별)
        self._fleet_demand_completed: dict[str, int] = {}
        # GAP-B: 수동 demand 발행용 (UI 의 📋 토글에서 호출)
        self._bus: Optional[LocalMemoryBus] = None
        self._manual_counter: int = 0
        self._pending_manual_demands: list[dict] = []

    # ── 외부 컨트롤 ─────────────────────────────────────────
    def stop(self) -> None:
        self._stop = True

    def block_edge(self, edge_key: str, blocked: bool) -> dict:
        """GAP-A: 사용자가 ⛔ 로 엣지 차단/해제.

        edge_key: "src__dst" 형식.
        blocked: True 면 추가, False 면 해제.
        반환: {"ok": bool, "currently_blocked": [edge_key...], "affected_agvs": [...]}.

        영향:
          1) self.blocked_edges 갱신 + graph._user_blocked_edges 동기화
             → 이후 모든 path-find 가 차단 엣지를 회피.
          2) 차단 시점에 그 엣지를 path 에 포함한 AGV 들에 대해 _reroute 트리거.
        """
        tup = _edge_key_to_tuple(edge_key)
        if tup is None:
            return {"ok": False, "error": f"invalid edge_key: {edge_key}",
                    "currently_blocked": sorted(self.blocked_edges),
                    "affected_agvs": []}
        if blocked:
            self.blocked_edges.add(edge_key)
        else:
            self.blocked_edges.discard(edge_key)

        if self._graph is not None:
            self._graph._user_blocked_edges = {
                _edge_key_to_tuple(k) for k in self.blocked_edges
                if _edge_key_to_tuple(k)
            }

        affected: list[str] = []
        if blocked and self._engine is not None:
            sim_time = self._engine.sim_time
            for agv in self._engine.agvs.values():
                planned = _agv_planned_edge_keys(agv)
                if edge_key in planned:
                    affected.append(agv.agv_id)
                    asyncio.create_task(agv._reroute(sim_time, blocked_edge=tup))
        return {
            "ok": True,
            "currently_blocked": sorted(self.blocked_edges),
            "affected_agvs": affected,
        }

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

    def fleets_payload(self) -> list[dict]:
        """F1a: /init 응답에 포함될 fleet 메타.

        반환: [{id, color, graph_idx, count, capabilities, agv_ids}, ...]
        단일 default fleet 의 경우에도 안전하게 1개 항목으로 반환.
        """
        out = []
        for fl in self.fleets:
            out.append({
                "id": fl.id,
                "color": fl.color,
                "graph_idx": fl.graph_idx,
                "count": fl.count,
                "capabilities": list(fl.capabilities),
                "agv_ids": list(self._agv_ids_by_fleet.get(fl.id, [])),
            })
        return out

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
        # 사용자가 ⛔ 로 차단한 엣지 — graph.get_path() 가 자동 회피한다.
        # 포맷: edge_key 문자열 "src__dst" → (src, dst) tuple.
        graph._user_blocked_edges = {
            _edge_key_to_tuple(k) for k in self.blocked_edges if _edge_key_to_tuple(k)
        }
        self._graph = graph
        self._scheduler = TimeWindowScheduler()
        bus = LocalMemoryBus()
        self._bus = bus
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

        # F1a: fleet 정의 구성 + AGV→fleet 분배.
        self.fleets = _build_fleets(
            self._imported_fleets,
            total_agv_count=self.agv_count,
            count_overrides=self._agv_count_by_fleet,
        )
        # 단일-default fleet 면 self.agv_count 만큼 AGV 생성, 아니면 fleet.count 합산.
        if len(self.fleets) == 1 and self.fleets[0].id == "default":
            total = max(1, self.agv_count)
            self.fleets[0].count = total
            agv_alloc: list[tuple[str, Fleet]] = [
                (f"AGV_{i+1:03d}", self.fleets[0]) for i in range(total)
            ]
        else:
            agv_alloc = []
            running = 0
            for fl in self.fleets:
                for _ in range(max(0, fl.count)):
                    running += 1
                    agv_alloc.append((f"AGV_{running:03d}", fl))
            if not agv_alloc:
                # 모든 fleet count=0 인 비정상 케이스 → 기본 1대
                fl = self.fleets[0]
                fl.count = 1
                agv_alloc.append(("AGV_001", fl))

        self._fleet_by_agv = {agv_id: fl for agv_id, fl in agv_alloc}
        self._fleet_demand_completed = {fl.id: 0 for fl in self.fleets}

        for i, (agv_id, fl) in enumerate(agv_alloc):
            agv = AGV(agv_id, bus, graph, self._scheduler,
                      max_speed_mps=fl.max_speed_mps, fleet=fl)
            agv.current_node_id = chargers[i % len(chargers)]
            agv.physics.x = graph.nodes[agv.current_node_id].x
            agv.physics.y = graph.nodes[agv.current_node_id].y
            self._engine.register_agv(agv)
            self._agvs.append(agv)
        # /init 응답에서 사용할 fleet 메타 (agv_ids 포함)
        agv_ids_by_fleet: dict[str, list[str]] = {fl.id: [] for fl in self.fleets}
        for agv_id, fl in agv_alloc:
            agv_ids_by_fleet[fl.id].append(agv_id)
        self._agv_ids_by_fleet = agv_ids_by_fleet

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

        # F1a: snapshot.agvs 각 항목에 fleet_id 부여 (recorder 는 fleet 모름)
        for a in snapshot.get("agvs", []):
            fl = self._fleet_by_agv.get(a.get("agv_id", ""))
            a["fleet_id"] = fl.id if fl else ""

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

        # Rolling KPI (overall)
        agv_states = [a._fsm.state for a in self._agvs]
        ho_summary = self._scheduler.get_headon_summary()
        demand_done = 0
        if self._task_gen is not None and hasattr(self._task_gen, "_diagnostics"):
            demand_done = getattr(self._task_gen._diagnostics, "demands_completed", 0)
        overall_kpi = self._kpi.update(
            sim_time=self.sim_time,
            agv_states=agv_states,
            demand_completed_total=demand_done,
            headon_total=ho_summary["headon_total"],
        )

        # F1a: by_fleet KPI 계산
        # - tasksPerHr: demand_completed 이벤트를 fleet 별로 카운트 → 분당 rate
        # - utilization: fleet 소속 AGV 의 NAVIGATING+PROCESSING 비율
        # - avgWait: fleet 소속 AGV 의 WAITING_RESERVATION 비율 × 60s
        # - headOn: fleet 소속 AGV 가 등장한 headon_block 이벤트 누적
        by_fleet = self._compute_by_fleet_kpi(rec_events)
        kpi = dict(overall_kpi)
        kpi["by_fleet"] = by_fleet

        # F1a: unmatched_demand_count = capability 매칭 fleet 가 없어 보류된 demand
        # 합. 자동 demand_set(누적) + 수동 manual(현재 pending 길이).
        auto_unmatched = 0
        if self._task_gen is not None and hasattr(self._task_gen, "_diagnostics"):
            auto_unmatched = getattr(
                self._task_gen._diagnostics, "unmatched_demand_count", 0
            )
        unmatched_demand_count = auto_unmatched + len(self._pending_manual_demands)

        await self.broadcast({
            "type": "tick",
            "snapshot": snapshot,
            "new_events": new_events,
            "kpi": kpi,
            "edge_density": edge_density,
            "unmatched_demand_count": unmatched_demand_count,
        })

    def _compute_by_fleet_kpi(self, rec_events: list) -> dict:
        """fleet 별 throughput / utilization / wait / headon 누적치 계산.

        Engine 이 fleet 별 KPI 를 직접 노출하지 않으므로, 여기서 AGV 상태와 이벤트를
        조합해 추정. by_fleet 값은 fleet 가 1개 (default) 인 단일 fleet 케이스에서도
        안전하게 작동 (overall 과 동일 추세).
        """
        result: dict = {}
        if not self.fleets:
            return result
        # AGV 상태를 fleet 별로 묶음
        agv_states_by_fleet: dict[str, list] = {fl.id: [] for fl in self.fleets}
        for a in self._agvs:
            fl = a.fleet
            fid = fl.id if fl else (self.fleets[0].id if self.fleets else "")
            if fid in agv_states_by_fleet:
                agv_states_by_fleet[fid].append(a._fsm.state)
        # 이벤트로부터 demand_completed / headon_block 누적 카운트 (fleet 별)
        completed_by_fleet: dict[str, int] = {fl.id: 0 for fl in self.fleets}
        headon_by_fleet: dict[str, int] = {fl.id: 0 for fl in self.fleets}
        for ev in rec_events:
            kind = ev.get("kind", "")
            agv_id = ev.get("agv_id", "")
            fl = self._fleet_by_agv.get(agv_id)
            if not fl:
                continue
            if kind == "demand_completed":
                completed_by_fleet[fl.id] = completed_by_fleet.get(fl.id, 0) + 1
            elif kind == "headon_block":
                headon_by_fleet[fl.id] = headon_by_fleet.get(fl.id, 0) + 1
        # 단순 rate 환산: 누적/elapsed × 3600
        elapsed = max(self.sim_time, 0.001)
        for fl in self.fleets:
            states = agv_states_by_fleet.get(fl.id, [])
            n = max(1, len(states))
            util = sum(1 for s in states
                       if s in (AGVState.NAVIGATING, AGVState.PROCESSING)) / n
            wait_ratio = sum(1 for s in states
                             if s == AGVState.WAITING_RESERVATION) / n
            result[fl.id] = {
                "tasksPerHr": completed_by_fleet.get(fl.id, 0) * 3600.0 / elapsed,
                "utilization": util,
                "avgWait": wait_ratio * 60.0,  # rolling 60s 윈도우 가정
                "headOn": headon_by_fleet.get(fl.id, 0),
                "color": fl.color,
                "count": len(states),
            }
        return result

    # ── GAP-B: 수동 demand 발행 ────────────────────────────
    async def dispatch_manual_demand(
        self,
        pickup_node_id: str,
        dropoff_node_id: str,
        required_capability: Optional[str] = None,
    ) -> dict:
        """UI 📋 토글에서 두 노드 클릭 → 수동 demand 발행.

        - pickup/dropoff 노드 존재 검증 (graph)
        - required_capability 가 있으면 fleet.capabilities 매칭 AGV 만
        - idle 가능 AGV 중 pickup 까지 hop 수가 가장 짧은 AGV 선택
        - agv→pickup→dropoff 전체 path 로 VDA5050 Order 발행 (자동 발행과 동일 큐)
        반환: {"ok": bool, "demand_id": str|None, "agv_id": str|None,
               "status": "dispatched"|"pending"|"rejected", "reason": str|None}
        """
        if self._graph is None or self._bus is None:
            return {"ok": False, "demand_id": None, "agv_id": None,
                    "status": "rejected", "reason": "runner not setup"}
        if pickup_node_id not in self._graph.nodes:
            return {"ok": False, "demand_id": None, "agv_id": None,
                    "status": "rejected",
                    "reason": f"unknown pickup_node: {pickup_node_id}"}
        if dropoff_node_id not in self._graph.nodes:
            return {"ok": False, "demand_id": None, "agv_id": None,
                    "status": "rejected",
                    "reason": f"unknown dropoff_node: {dropoff_node_id}"}
        if pickup_node_id == dropoff_node_id:
            return {"ok": False, "demand_id": None, "agv_id": None,
                    "status": "rejected", "reason": "pickup == dropoff"}

        # capability 매칭 + idle 필터
        idle = [a for a in self._agvs if a.is_available_for_dispatch()]
        eligible = get_eligible_agvs(idle, required_capability)
        # F1a: fleet.graph_idx 분리 — 각 AGV 의 fleet 그래프에서만 path 검사
        eligible = [
            a for a in eligible
            if self._graph.get_path(
                a.current_node_id, pickup_node_id, fleet=a.fleet
            )
            and self._graph.get_path(
                pickup_node_id, dropoff_node_id, fleet=a.fleet
            )
        ]
        if not eligible:
            # 매칭 idle AGV 없음 — pending 상태로 보고 (자동 retry 안 함, UI 가 인지)
            self._manual_counter += 1
            demand_id = f"manual_{self._manual_counter:05d}"
            self._pending_manual_demands.append({
                "demand_id": demand_id,
                "pickup_node_id": pickup_node_id,
                "dropoff_node_id": dropoff_node_id,
                "required_capability": required_capability,
            })
            reason = "no eligible idle AGV"
            if required_capability:
                reason += f" with capability '{required_capability}'"
            return {"ok": True, "demand_id": demand_id, "agv_id": None,
                    "status": "pending", "reason": reason}

        # 가장 가까운 AGV (hop count 기준, 각자 fleet 그래프 안에서)
        agv = min(
            eligible,
            key=lambda a: len(
                self._graph.get_path(a.current_node_id, pickup_node_id, fleet=a.fleet)
            ),
        )
        path_to_pickup = self._graph.get_path(
            agv.current_node_id, pickup_node_id, fleet=agv.fleet
        )
        path_to_dropoff = self._graph.get_path(
            pickup_node_id, dropoff_node_id, fleet=agv.fleet
        )
        if not path_to_pickup:
            return {"ok": False, "demand_id": None, "agv_id": agv.agv_id,
                    "status": "rejected", "reason": "no path agv→pickup"}
        if not path_to_dropoff:
            return {"ok": False, "demand_id": None, "agv_id": agv.agv_id,
                    "status": "rejected", "reason": "no path pickup→dropoff"}

        full_path = path_to_pickup + path_to_dropoff[1:]
        self._manual_counter += 1
        demand_id = f"manual_{self._manual_counter:05d}"
        nodes = [
            {"nodeId": nid, "sequenceId": i * 2, "released": True, "actions": []}
            for i, nid in enumerate(full_path)
        ]
        edges = [
            {"edgeId": f"{full_path[i]}__{full_path[i+1]}",
             "sequenceId": i * 2 + 1, "released": True}
            for i in range(len(full_path) - 1)
        ]
        order_payload = {
            "orderId": demand_id,
            "orderUpdateId": 0,
            "agvId": agv.agv_id,
            "nodes": nodes,
            "edges": edges,
            "dispatchTimeS": self.sim_time,
            "demandId": demand_id,
            "pickupNodeId": pickup_node_id,
            "dropoffNodeId": dropoff_node_id,
        }
        await self._bus.publish(f"uagv/v2/NEXT/{agv.agv_id}/order", order_payload)
        # task_generator 의 diagnostics 에도 반영 (수동 + 자동 합산)
        if self._task_gen is not None:
            self._task_gen._diagnostics.orders_published += 1
            self._task_gen._diagnostics.tasks_dispatched += 1
        return {"ok": True, "demand_id": demand_id, "agv_id": agv.agv_id,
                "status": "dispatched", "reason": None}

    async def _publish_end(self) -> None:
        try:
            await self.broadcast({
                "type": "end",
                "reason": "stopped" if self._stop else "completed",
                "simTime": round(self.sim_time, 3),
            })
        except Exception:
            pass
