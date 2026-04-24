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

ROBOT_LENGTH_M = 1.8
PROTECTIVE_ZONE_M = 0.2
WARNING_ZONE_M = 0.5
BASE_FOLLOWING_DISTANCE_M = ROBOT_LENGTH_M + PROTECTIVE_ZONE_M + WARNING_ZONE_M
MIN_FOLLOW_ON_HEADWAY_S = 0.5
RESTART_DELAY_S = 1.0

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
        self._order_processing_time_s: Optional[float] = None
        self._pickup_processing_time_s: Optional[float] = None
        self._dropoff_processing_time_s: Optional[float] = None
        self._current_demand_id: Optional[str] = None
        self._current_pickup_node_id: Optional[str] = None
        self._current_dropoff_node_id: Optional[str] = None
        self._processing_node_id: Optional[str] = None

        # Conflict Resolution
        self.collision_retry_count: int   = 0
        self._edge_wait_elapsed:    float = 0.0
        self._pending_edge_src:  Optional[str] = None
        self._pending_edge_dst:  Optional[str] = None
        self._pre_reserved_edges: set[tuple[str, str]] = set()
        self._pre_reserved_edge_starts: dict[tuple[str, str], float] = {}
        self._restart_delay_remaining: float = 0.0
        self._restart_delay_time_s: float = 0.0

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
        processing_time = payload.get("processingTimeS")
        self._order_processing_time_s = (
            float(processing_time) if processing_time is not None else None
        )
        pickup_processing_time = payload.get("pickupProcessingTimeS")
        dropoff_processing_time = payload.get("dropoffProcessingTimeS")
        self._pickup_processing_time_s = (
            float(pickup_processing_time) if pickup_processing_time is not None else None
        )
        self._dropoff_processing_time_s = (
            float(dropoff_processing_time) if dropoff_processing_time is not None else None
        )
        self._current_demand_id = payload.get("demandId")
        self._current_pickup_node_id = payload.get("pickupNodeId")
        self._current_dropoff_node_id = payload.get("dropoffNodeId")
        self._processing_node_id = None
        self._pre_reserved_edges.clear()
        self._pre_reserved_edge_starts.clear()
        if self._path and self._fsm.state == AGVState.IDLE:
            dispatch_time = float(payload.get("dispatchTimeS", 0.0))
            await self._pre_reserve_itinerary(dispatch_time)
            await self._navigate_to_next_node(sim_time=dispatch_time)

    async def _on_instant_action(self, payload: dict) -> None:
        for action in payload.get("instantActions", []):
            t = action.get("actionType", "")
            if t == "cancelOrder":
                await self._cancel_current_edge()
                await self._sched.release_agv_reservations(self.agv_id)
                self._path.clear()
                self._pre_reserved_edges.clear()
                self._pre_reserved_edge_starts.clear()
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
            if self._tick_restart_delay(dt):
                await self._publish_state()
                return
            self._wait_time_s      += dt
            self._edge_wait_elapsed += dt
            self._motion.state.speed = max(
                0.0,
                self._motion.state.speed - self._motion.deceleration * dt,
            )

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
                if self._is_current_demand_dropoff_complete():
                    await self._publish_demand_completed(sim_time)
                    self._clear_demand_context()
                self._processing_node_id = None
                self._restart_delay_remaining = RESTART_DELAY_S
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
                self._processing_node_id = self.current_node_id
                self._process_remaining = self._processing_time_for_current_node()
                self._fsm.force(AGVState.PROCESSING)
            else:
                asyncio.create_task(self._navigate_to_next_node(sim_time))

    def _is_current_demand_dropoff_complete(self) -> bool:
        return (
            self._current_demand_id is not None
            and self._current_dropoff_node_id is not None
            and self._processing_node_id == self._current_dropoff_node_id
        )

    async def _publish_demand_completed(self, sim_time: float) -> None:
        await self._bus.publish(
            "uagv/v2/NEXT/demand/completed",
            {
                "eventType": "demandCompleted",
                "demandId": self._current_demand_id,
                "agvId": self.agv_id,
                "pickupNodeId": self._current_pickup_node_id,
                "dropoffNodeId": self._current_dropoff_node_id,
                "simTime": round(sim_time, 4),
            },
        )

    def _clear_demand_context(self) -> None:
        self._current_demand_id = None
        self._current_pickup_node_id = None
        self._current_dropoff_node_id = None

    def _processing_time_for_current_node(self) -> float:
        if (
            self._current_pickup_node_id is not None
            and self._processing_node_id == self._current_pickup_node_id
            and self._pickup_processing_time_s is not None
        ):
            return self._pickup_processing_time_s
        if (
            self._current_dropoff_node_id is not None
            and self._processing_node_id == self._current_dropoff_node_id
            and self._dropoff_processing_time_s is not None
        ):
            return self._dropoff_processing_time_s
        if self._order_processing_time_s is not None:
            return self._order_processing_time_s
        return random.uniform(30.0, 120.0)

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
        if self._restart_delay_remaining > 0.0:
            self._fsm.force(AGVState.WAITING_RESERVATION)
            return
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

        edge     = self._find_edge(src, dst)
        dist     = edge.distance if edge is not None else (
            self._graph._calc_distance(src, dst) if (src in self._graph.nodes and dst in self._graph.nodes) else 1.0
        )
        speed    = max(self._get_effective_speed(sim_time), 0.01)
        travel_t = dist / speed
        follow_on_headway_s = self._calc_follow_on_headway_s(src, dst, speed)

        pre_reserved_edge = (src, dst)
        if pre_reserved_edge in self._pre_reserved_edges:
            reserved_start = self._pre_reserved_edge_starts.get(pre_reserved_edge, sim_time)
            if sim_time < reserved_start:
                self._fsm.force(AGVState.WAITING_RESERVATION)
                return
            ok = True
        else:
            ok = await self._sched.reserve_edge(
                src, dst, self.agv_id,
                start_time=sim_time,
                end_time=sim_time + travel_t,
                same_direction_headway_s=follow_on_headway_s,
                section_key=self._critical_section_key(edge) if edge is not None else "",
                section_capacity=(
                    self._critical_section_capacity(edge) if edge is not None else 1
                ),
            )

        if ok:
            if self._restart_delay_remaining > 0.0:
                self._fsm.force(AGVState.WAITING_RESERVATION)
                return
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
                    goal = self._path[-1] if self._path else None
                    siding = self._find_siding_candidate(
                        src,
                        goal,
                        blocked_edge=(src, dst),
                    )
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

    def _tick_restart_delay(self, dt: float) -> bool:
        if self._restart_delay_remaining <= 0.0:
            return False
        elapsed = min(dt, self._restart_delay_remaining)
        self._restart_delay_remaining -= elapsed
        self._restart_delay_time_s += elapsed
        self._wait_time_s += elapsed
        self._motion.state.speed = 0.0
        return True

    def _find_siding_candidate(
        self,
        near_node: str,
        goal_node: Optional[str],
        blocked_edge: Optional[tuple[str, str]] = None,
    ) -> Optional[str]:
        """
        Type B reachable siding 탐색.
        - 인접 siding만 보지 않고 현재 위치에서 도달 가능한 siding 전체를 후보로 본다.
        - siding -> goal 재진입 경로가 존재해야 한다.
        - blocked_edge가 있으면 현재 막힌 엣지를 경유하는 후보는 제외한다.
        - 최단 path distance 기준으로 가장 가까운 siding을 선택한다.
        """
        policy = getattr(self._graph, "_type_b_siding_policy", "reachable")
        if policy == "adjacent":
            return self._find_adjacent_siding_candidate(near_node)
        if goal_node is None:
            return None

        blocked_edges = {blocked_edge} if blocked_edge else None
        best_sid: Optional[str] = None
        best_distance = float("inf")
        best_hops = float("inf")

        for sid, node in self._graph.nodes.items():
            if node.role != NodeRole.SIDING:
                continue
            if not self._is_siding_available(sid):
                continue
            if not self._has_siding_reentry(sid):
                continue

            path_to_siding = self._graph.get_path(
                near_node,
                sid,
                blocked_edges=blocked_edges,
            )
            if not path_to_siding or len(path_to_siding) < 2:
                continue

            tail = self._graph.get_path(sid, goal_node)
            if not tail or len(tail) < 2:
                continue

            distance = self._path_distance(path_to_siding)
            hops = len(path_to_siding)
            if distance < best_distance or (
                distance == best_distance and hops < best_hops
            ):
                best_sid = sid
                best_distance = distance
                best_hops = hops

        return best_sid

    def _find_adjacent_siding_candidate(self, near_node: str) -> Optional[str]:
        sidings = [
            n for n in self._graph.get_neighbors(near_node)
            if n.role == NodeRole.SIDING
        ]
        for siding_node in sidings:
            sid = siding_node.node_id
            if not self._is_siding_available(sid):
                continue
            if self._has_siding_reentry(sid):
                return sid
        return None

    def _is_siding_available(self, siding_id: str) -> bool:
        active = [
            r for r in self._sched._reservations.get(siding_id, [])
            if not r.released
        ]
        return not active

    def _has_siding_reentry(self, siding_id: str) -> bool:
        neighbors = self._graph.get_neighbors(siding_id)
        return any(n.role != NodeRole.SIDING for n in neighbors)

    def _path_distance(self, node_ids: list[str]) -> float:
        total = 0.0
        for src, dst in zip(node_ids, node_ids[1:]):
            edge = self._find_edge(src, dst)
            if edge is None:
                return float("inf")
            total += edge.distance
        return total

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

    async def _pre_reserve_itinerary(self, start_time: float) -> bool:
        segments = self._build_itinerary_segments(start_time)
        if not segments:
            return False
        ok = await self._sched.reserve_itinerary(segments)
        if not ok:
            return False
        self._pre_reserved_edges = {
            (segment.src_id, segment.dst_id)
            for segment in segments
            if segment.segment_type == "edge"
        }
        self._pre_reserved_edge_starts = {
            (segment.src_id, segment.dst_id): segment.start_time
            for segment in segments
            if segment.segment_type == "edge"
        }
        return True

    def _build_itinerary_segments(self, start_time: float) -> list:
        from src.domain.reservation.scheduler import ItinerarySegment

        if not self.current_node_id or len(self._path) < 2:
            return []

        path = self._path
        current = self.current_node_id
        cursor = start_time
        speed = max(self._motion.max_speed, 0.01)
        processing_time = (
            self._order_processing_time_s if self._order_processing_time_s is not None else 30.0
        )
        segments: list[ItinerarySegment] = []

        for dst in path:
            if dst == current:
                continue
            edge = self._find_edge(current, dst)
            if edge is None:
                return []
            edge_speed = max(min(speed, edge.max_speed), 0.01)
            travel_time = edge.distance / edge_speed
            edge_start = cursor
            edge_end = edge_start + travel_time
            segments.append(
                ItinerarySegment(
                    segment_type="edge",
                    key=f"{current}__{dst}",
                    agv_id=self.agv_id,
                    start_time=edge_start,
                    end_time=edge_end,
                    src_id=current,
                    dst_id=dst,
                    same_direction_headway_s=self._calc_follow_on_headway_s(
                        current,
                        dst,
                        edge_speed,
                    ),
                    section_key=self._critical_section_key(edge),
                    section_capacity=self._critical_section_capacity(edge),
                )
            )
            cursor = edge_end

            node = self._graph.nodes.get(dst)
            if node and (node.role.value in ("work", "station") or node.is_parking_spot):
                dwell_time = self._itinerary_processing_time_for_node(dst, processing_time)
                dwell_end = cursor + dwell_time
                segments.append(
                    ItinerarySegment(
                        segment_type="node",
                        key=dst,
                        agv_id=self.agv_id,
                        start_time=cursor,
                        end_time=dwell_end,
                    )
                )
                cursor = dwell_end
            current = dst

        return segments

    def _itinerary_processing_time_for_node(
        self,
        node_id: str,
        fallback_processing_time_s: float,
    ) -> float:
        if (
            self._current_pickup_node_id is not None
            and node_id == self._current_pickup_node_id
            and self._pickup_processing_time_s is not None
        ):
            return self._pickup_processing_time_s
        if (
            self._current_dropoff_node_id is not None
            and node_id == self._current_dropoff_node_id
            and self._dropoff_processing_time_s is not None
        ):
            return self._dropoff_processing_time_s
        return fallback_processing_time_s

    def _critical_section_key(self, edge) -> str:
        topology_type = getattr(self._graph, "_topology_type", "")
        if edge.access_type:
            facility_node_id = self._access_facility_node_id(edge)
            return f"access:{edge.access_type}:{facility_node_id}"
        if edge.corridor == "bay":
            return f"bay:{edge.start_node_id.split('_')[-1]}"
        if edge.corridor == "siding":
            return f"siding:{edge.start_node_id}->{edge.end_node_id}"
        if topology_type in ("B", "E") and edge.corridor in ("north", "center", "south"):
            return f"shared_corridor:{edge.corridor}:{self._undirected_edge_key(edge)}"
        if edge.corridor in (
            "north_l1",
            "north_l2",
            "center_l1",
            "center_l2",
            "south_l1",
            "south_l2",
        ):
            return f"lane:{edge.corridor}:{self._undirected_edge_key(edge)}"
        return ""

    def _access_facility_node_id(self, edge) -> str:
        for node_id in (edge.start_node_id, edge.end_node_id):
            node = self._graph.nodes.get(node_id)
            if not node:
                continue
            if edge.access_type == "station_access" and (
                node.role == NodeRole.WORK or node.is_parking_spot
            ):
                return node_id
            if edge.access_type == "charger_access" and (
                node.role == NodeRole.CHARGER or node.is_charger
            ):
                return node_id
        return self._undirected_edge_key(edge)

    @staticmethod
    def _critical_section_capacity(edge) -> int:
        if edge.access_type or edge.corridor in ("bay", "siding"):
            return 1
        return max(1, int(edge.capacity))

    @staticmethod
    def _undirected_edge_key(edge) -> str:
        a, b = sorted([edge.start_node_id, edge.end_node_id])
        return f"{a}<->{b}"

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

    def _calc_follow_on_headway_s(self, src: str, dst: str, speed_mps: float) -> float:
        edge = self._find_edge(src, dst)
        width_m = edge.width_m if edge else 1.5
        if edge and edge.safety_model in ("narrow_one_way", "wide_one_way"):
            width_m = getattr(
                self._graph,
                "_corridor_total_width_m",
                width_m * 2.0,
            )
        width_factor = 1.5 / max(width_m, 0.1)
        following_distance_m = BASE_FOLLOWING_DISTANCE_M * width_factor
        return max(MIN_FOLLOW_ON_HEADWAY_S, following_distance_m / max(speed_mps, 0.01))

    def _find_edge(self, src: str, dst: str):
        for edge_id in self._graph._out_edges.get(src, []):
            edge = self._graph.edges[edge_id]
            if edge.end_node_id == dst:
                return edge
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
