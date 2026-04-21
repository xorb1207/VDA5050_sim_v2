"""
AGV v3 — Conflict Resolution Policy

변경사항:
  - collision_retry_count 추가
  - retry 1~3: short wait (EDGE_RETRY_INTERVAL_S)
  - retry 4~10: bounded exponential backoff (최대 0.3s)
  - retry 11+: force_reroute() — 점유 엣지 회피 A*
  - Type B: siding candidate 탐색
  - Type E: creep 감속 우선
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Optional, TYPE_CHECKING

from src.domain.agv.fsm import AGVState, AGVStateMachine
from src.domain.agv.motion import MotionModel
from src.domain.map.graph import NodeRole
from src.domain.map.topology_generator import SPEED_CREEP_MS

if TYPE_CHECKING:
    from src.interfaces.bus import IMessageBus
    from src.domain.map.graph import MapGraph
    from src.domain.reservation.scheduler import TimeWindowScheduler

BATTERY_ENABLED = False

# ── Conflict Resolution 파라미터 ──────────────────────────────
EDGE_RETRY_INTERVAL_S  = 0.1   # 1~3회 기본 재시도 간격
BACKOFF_MAX_S          = 0.3   # backoff 상한 (FAB: 장시간 정지 방지)
REROUTE_THRESHOLD      = 11    # 이 횟수 이상이면 force_reroute
WAIT_TIMEOUT_S         = 30.0  # 전체 대기 타임아웃

# 타입별 reroute 임계치
REROUTE_THRESHOLD_BY_TYPE = {
    "A": 5,   # 단방향 — 우회 경로 있음, 빨리 reroute
    "B": 15,  # siding 탐색 우선 — 좀 더 기다림
    "C": 5,   # 2차선 단방향 — 우회 쉬움
    "D": 8,   # 2차선 양방향
    "E": 8,   # 크리프 우선, 그다음 reroute
}


class AGV:

    BATTERY_DRAIN_PER_METER: float = 0.01
    CHARGE_TRIGGER_PCT:      float = 35.0

    def __init__(
        self,
        agv_id: str,
        bus: IMessageBus,
        graph: MapGraph,
        scheduler: TimeWindowScheduler,
        max_speed_mps: float = 1.5,
        policy=None,
    ) -> None:
        self.agv_id   = agv_id
        self._bus     = bus
        self._graph   = graph
        self._sched   = scheduler
        self._policy  = policy

        self._fsm    = AGVStateMachine()
        self._motion = MotionModel(max_speed_mps)

        self.current_node_id: Optional[str] = None
        self.target_node_id:  Optional[str] = None

        self._path:       list[str] = []
        self._path_index: int       = 0
        self._process_remaining: float = 0.0

        # Conflict Resolution
        self.collision_retry_count: int   = 0
        self._edge_wait_elapsed:    float = 0.0
        self._pending_edge_src:  Optional[str] = None
        self._pending_edge_dst:  Optional[str] = None

        # 통계
        self._wait_time_s:       float = 0.0
        self._travel_distance_m: float = 0.0
        self._task_count:        int   = 0
        self._state_time:        dict[str, float] = {}
        self._task_completion_times: list[float] = []
        self._task_start_time:   float = 0.0
        self._edge_time:         dict[str, float] = {}
        self._reroute_count:     int = 0

    @property
    def state(self) -> AGVState:
        return self._fsm.state

    @property
    def physics(self):
        return self._motion.state

    async def start(self) -> None:
        base = f"uagv/v2/NEXT/{self.agv_id}"
        await self._bus.subscribe(f"{base}/order",          self._on_order_received)
        await self._bus.subscribe(f"{base}/instantActions", self._on_instant_action)

    async def _on_order_received(self, payload: dict) -> None:
        from src.vda5050.parser import VDA5050Parser, VDA5050Validator
        if not VDA5050Validator.validate_order_sequence(payload):
            return
        order = VDA5050Parser.parse_order(payload)
        self._path       = [n.nodeId for n in order.base_nodes if n.released]
        self._path_index = 0
        if self._path and self._fsm.state == AGVState.IDLE:
            await self._navigate_to_next_node(sim_time=0.0)

    async def _on_instant_action(self, payload: dict) -> None:
        for action in payload.get("instantActions", []):
            t = action.get("actionType", "")
            if t == "cancelOrder":
                await self._cancel_current_edge()
                self._path.clear()
                self._fsm.force(AGVState.IDLE)
            elif t == "pauseOrder":
                if self._fsm.state == AGVState.NAVIGATING:
                    self._motion.state.speed = 0.0

    async def tick(self, dt: float, sim_time: float) -> None:
        s = self._fsm.state
        self._state_time[s.value] = self._state_time.get(s.value, 0.0) + dt

        if s == AGVState.NAVIGATING:
            await self._tick_navigating(dt, sim_time)

        elif s == AGVState.WAITING_RESERVATION:
            self._wait_time_s      += dt
            self._edge_wait_elapsed += dt

            # 타임아웃 먼저 체크 — reroute 후 retry 실행 방지
            if self._wait_time_s >= WAIT_TIMEOUT_S:
                self._reroute_count += 1
                self._sched.clear_waiting(self.agv_id)
                await self._reroute(sim_time)
                return  # ← 이 틱에서 retry 실행 방지

            retry_interval = self._get_retry_interval()
            if self._edge_wait_elapsed >= retry_interval:
                self._edge_wait_elapsed = 0.0
                await self._try_reserve_edge_and_move(sim_time)

        elif s == AGVState.PROCESSING:
            self._process_remaining -= dt
            if self._process_remaining <= 0.0:
                self._process_remaining = 0.0
                self._task_count += 1
                if self._task_start_time > 0:
                    self._task_completion_times.append(sim_time - self._task_start_time)
                    self._task_start_time = 0.0
                self._fsm.force(AGVState.IDLE)
                asyncio.create_task(self._navigate_to_next_node(sim_time))

        await self._publish_state()

    def _get_retry_interval(self) -> float:
        """
        retry 횟수에 따른 대기 간격.
        1~3회:  EDGE_RETRY_INTERVAL_S (0.1s)
        4~10회: bounded exponential backoff (최대 BACKOFF_MAX_S)
        11+회:  이미 reroute 트리거되므로 기본값 반환
        """
        n = self.collision_retry_count
        if n <= 3:
            return EDGE_RETRY_INTERVAL_S
        elif n <= 10:
            # 2^(n-3) * 0.1, 최대 BACKOFF_MAX_S
            backoff = min(BACKOFF_MAX_S, (2 ** (n - 3)) * EDGE_RETRY_INTERVAL_S)
            return backoff
        return EDGE_RETRY_INTERVAL_S

    async def _tick_navigating(self, dt: float, sim_time: float) -> None:
        if not self.target_node_id:
            return
        t = self._graph.nodes.get(self.target_node_id)
        if not t:
            return

        if self.current_node_id and self.target_node_id:
            eid = f"{self.current_node_id}__{self.target_node_id}"
            self._edge_time[eid] = self._edge_time.get(eid, 0.0) + dt

        effective_speed = self._get_effective_speed(sim_time)
        self._motion.max_speed = effective_speed

        dist_moved, arrived = self._motion.update(dt, t.x, t.y)
        self._travel_distance_m += dist_moved

        if arrived:
            prev = self.current_node_id
            self.current_node_id = self.target_node_id
            self.target_node_id  = None

            if prev and self.current_node_id:
                await self._sched.release_edge(prev, self.current_node_id, self.agv_id)
            if prev:
                asyncio.create_task(self._sched.release(prev, self.agv_id))

            self._sched.clear_waiting(self.agv_id)
            self.collision_retry_count = 0  # 도착 시 retry 초기화

            if t.role.value in ("work", "station") or t.is_parking_spot:
                self._process_remaining = random.uniform(30.0, 120.0)
                self._fsm.force(AGVState.PROCESSING)
            else:
                asyncio.create_task(self._navigate_to_next_node(sim_time))

    async def _navigate_to_next_node(self, sim_time: float) -> None:
        if self._path_index >= len(self._path):
            self._fsm.force(AGVState.IDLE)
            return

        next_id = self._path[self._path_index]
        current_node = self._graph.nodes.get(self.current_node_id) if self.current_node_id else None

        if current_node and current_node.role.value == "approach":
            self._fsm.force(AGVState.WAITING_RESERVATION)
            depth = 1
            if self._policy and self._policy.reservation_mode == "lookahead_n":
                depth = max(1, self._policy.lookahead_depth)
            lookahead_ids = self._path[self._path_index: self._path_index + depth]
            for lid in lookahead_ids:
                dist      = self._graph._calc_distance(self.current_node_id, lid)
                estimated = dist / max(self._motion.max_speed, 0.01)
                await self._sched.wait_for_slot(lid, self.agv_id, estimated, sim_time)

        if self._task_start_time == 0.0:
            self._task_start_time = sim_time

        self._pending_edge_src  = self.current_node_id
        self._pending_edge_dst  = next_id
        self._edge_wait_elapsed = 0.0
        await self._try_reserve_edge_and_move(sim_time)

    async def _try_reserve_edge_and_move(self, sim_time: float) -> None:
        src = self._pending_edge_src
        dst = self._pending_edge_dst
        if not src or not dst:
            return

        # Type E: head-on 감지 시 크리프 감속으로 먼저 시도
        topology_type = getattr(self._graph, "_topology_type", "")
        if topology_type == "E" and self._sched.is_head_on(src, dst, sim_time):
            # 크리프 속도로 진입 허용 (예약은 시도)
            pass

        dist     = self._graph._calc_distance(src, dst) if (src in self._graph.nodes and dst in self._graph.nodes) else 1.0
        speed    = max(self._get_effective_speed(sim_time), 0.01)
        travel_t = dist / speed

        ok = await self._sched.reserve_edge(
            src, dst, self.agv_id,
            start_time=sim_time,
            end_time=sim_time + travel_t,
        )

        if ok:
            self._pending_edge_src  = None
            self._pending_edge_dst  = None
            self._sched.clear_waiting(self.agv_id)
            self.collision_retry_count = 0
            self.target_node_id  = dst
            self._path_index    += 1
            self._fsm.force(AGVState.NAVIGATING)
        else:
            self.collision_retry_count += 1
            blocking = self._find_blocking_agv(src, dst, sim_time)
            if blocking:
                self._sched.register_waiting(self.agv_id, blocking)

            # reroute 임계치 확인
            threshold = REROUTE_THRESHOLD_BY_TYPE.get(topology_type, REROUTE_THRESHOLD)

            if self.collision_retry_count >= threshold:
                # Type B: siding 먼저 탐색
                if topology_type == "B":
                    siding = self._find_siding_candidate(src, sim_time)
                    if siding:
                        self._reroute_via_siding(siding, sim_time)
                        return

                # force reroute
                self._reroute_count += 1
                self._sched.clear_waiting(self.agv_id)
                self._pending_edge_src = None   # ← reroute 전 pending 클리어
                self._pending_edge_dst = None
                await self._reroute(sim_time, blocked_edge=(src, dst))
                return  # ← 같은 틱에서 WAITING 재진입 방지
            else:
                self._fsm.force(AGVState.WAITING_RESERVATION)

    def _find_siding_candidate(self, near_node: str, sim_time: float) -> Optional[str]:
        """
        근처 siding 노드 중:
        - 현재 비점유
        - 곧 예약되지 않음 (다음 10초 내 예약 없음)
        - 재진입 가능 (siding → 메인통로 엣지 존재)
        """
        sidings = [
            n for n in self._graph.get_neighbors(near_node)
            if n.role == NodeRole.SIDING
        ]
        for siding_node in sidings:
            sid = siding_node.node_id
            # 비점유 확인
            active = [r for r in self._sched._reservations.get(sid, [])
                      if not r.released]
            if active:
                continue
            # 재진입 가능 확인 (siding에서 메인통로로 나가는 엣지 존재)
            neighbors = self._graph.get_neighbors(sid)
            if any(n.role != NodeRole.SIDING for n in neighbors):
                return sid
        return None

    def _reroute_via_siding(self, siding_id: str, sim_time: float) -> None:
        """경로에 siding을 중간 경유지로 삽입."""
        if self._path_index < len(self._path):
            goal = self._path[-1]
            # siding → goal 경로
            tail = self._graph.get_path(siding_id, goal)
            if tail:
                self._path = self._path[:self._path_index] + [siding_id] + tail[1:]
                self.collision_retry_count = 0
                self._pending_edge_dst = siding_id
                self._fsm.force(AGVState.WAITING_RESERVATION)

    async def _reroute(
        self,
        sim_time: float,
        blocked_edge: Optional[tuple[str, str]] = None,
    ) -> None:
        """
        force_reroute — blocked_edge를 A* penalty로 회피.
        그래프를 직접 수정하지 않아 다른 AGV와 race condition 없음.
        """
        if not self._path or self._path_index >= len(self._path):
            self._fsm.force(AGVState.IDLE)
            return

        goal = self._path[-1]
        new_path = self._graph.get_path(
            self.current_node_id, goal,
            blocked_edges={blocked_edge} if blocked_edge else None,
        )

        if new_path and len(new_path) > 1:
            self._path       = new_path
            self._path_index = 0
            self._wait_time_s = 0.0
            self.collision_retry_count = 0
            self._pending_edge_src = None
            self._pending_edge_dst = None
            await self._navigate_to_next_node(sim_time)
        else:
            self._fsm.force(AGVState.IDLE)

    def _find_blocking_agv(self, src: str, dst: str, sim_time: float) -> Optional[str]:
        reverse_key = f"{dst}__{src}"
        for r in self._sched._edge_reservations.get(reverse_key, []):
            if not r.released:
                return r.agv_id
        edge_key = f"{src}__{dst}"
        for r in self._sched._edge_reservations.get(edge_key, []):
            if not r.released:
                return r.agv_id
        return None

    async def _cancel_current_edge(self) -> None:
        if self.current_node_id and self.target_node_id:
            await self._sched.release_edge(
                self.current_node_id, self.target_node_id, self.agv_id
            )

    def _get_effective_speed(self, sim_time: float) -> float:
        """
        Type E: head-on 감지 시 크리프 속도.
        policy 없어도 graph._lane_mode로 판단.
        """
        lane_mode = getattr(self._graph, "_lane_mode", "")
        if lane_mode == "bidirectional_creep":
            if self.current_node_id and self.target_node_id:
                if self._sched.is_head_on(self.current_node_id, self.target_node_id, sim_time):
                    return SPEED_CREEP_MS
        if self._policy:
            if getattr(self._policy, "lane_mode", "") == "bidirectional_creep":
                if self.current_node_id and self.target_node_id:
                    if self._sched.is_head_on(self.current_node_id, self.target_node_id, sim_time):
                        return SPEED_CREEP_MS
        return self._motion.max_speed

    async def _publish_state(self) -> None:
        from src.vda5050.parser import VDA5050Parser
        msg = VDA5050Parser.build_state_message(
            self.agv_id,
            {
                "agv_id":       self.agv_id,
                "state":        self._fsm.state.value,
                "x":            round(self._motion.state.x, 3),
                "y":            round(self._motion.state.y, 3),
                "heading":      round(self._motion.state.heading, 3),
                "battery_pct":  100.0,
                "current_node": self.current_node_id,
            },
        )
        if msg:
            await self._bus.publish(f"uagv/v2/NEXT/{self.agv_id}/state", msg)