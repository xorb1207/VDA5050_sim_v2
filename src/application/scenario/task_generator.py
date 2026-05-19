from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.map.graph import MapGraph
    from src.interfaces.bus import IMessageBus
    from src.domain.agv.agv import AGV
    from src.application.scenario.demand import DemandSet, TaskDemand
from src.domain.agv.fsm import AGVState


@dataclass
class TaskSpec:
    """단순 이송 태스크: pickup → dropoff."""
    task_id: str
    pickup_node_id: str
    dropoff_node_id: str


@dataclass
class TaskGenerationDiagnostics:
    """TaskGenerator가 태스크를 못 낸 이유를 집계한다."""
    tasks_requested: int = 0
    tasks_dispatched: int = 0
    tasks_rejected_unreachable: int = 0
    tasks_backlogged: int = 0
    demands_completed: int = 0
    orders_published: int = 0
    no_idle_agv: int = 0
    not_enough_work_nodes: int = 0
    not_enough_available_nodes: int = 0
    no_routeable_available_pair: int = 0
    no_routeable_current_pair: int = 0
    no_path_to_pickup: int = 0
    no_path_pickup_to_dropoff: int = 0
    # F1a: required_capability 매칭 AGV 가 없어서 보류된 demand 누적
    unmatched_demand_count: int = 0
    # PR-D: dropoff / pickup station 이 다른 AGV 에게 점유/예약 중이라 보류된 횟수.
    # Open-RMF 식 station capacity 거부 흐름. station 중복 점유의 1차 방어.
    station_capacity_reject: int = 0
    path_failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tasks_requested": self.tasks_requested,
            "tasks_dispatched": self.tasks_dispatched,
            "tasks_rejected_unreachable": self.tasks_rejected_unreachable,
            "tasks_backlogged": self.tasks_backlogged,
            "demands_completed": self.demands_completed,
            "orders_published": self.orders_published,
            "no_idle_agv": self.no_idle_agv,
            "not_enough_work_nodes": self.not_enough_work_nodes,
            "not_enough_available_nodes": self.not_enough_available_nodes,
            "no_routeable_available_pair": self.no_routeable_available_pair,
            "no_routeable_current_pair": self.no_routeable_current_pair,
            "no_path_to_pickup": self.no_path_to_pickup,
            "no_path_pickup_to_dropoff": self.no_path_pickup_to_dropoff,
            "unmatched_demand_count": self.unmatched_demand_count,
            "station_capacity_reject": self.station_capacity_reject,
            "path_failures": dict(sorted(self.path_failures.items())),
        }


class TaskGenerator:
    """
    시뮬레이션 중 IDLE AGV에게 VDA5050 Order를 자동 발행.
    pickup/dropoff 쌍은 작업 가능 노드(WORK role 또는 is_parking_spot)
    중에서 무작위 선택.

    통합 지점: engine.run() 루프 안에서 step() 호출.
    """

    def __init__(
        self,
        graph: MapGraph,
        bus: IMessageBus,
        task_interval_s: float = 30.0,
        demand_set: DemandSet | None = None,
        mode: str = "auto",
        scheduler=None,
    ) -> None:
        # mode:
        #   "auto"   — demand_set이 있으면 demand_set, 없으면 랜덤 (기존 동작).
        #   "manual" — 자동 발행 비활성. JobDispatcher.dispatch()로만 작업 주입.
        # scheduler:
        #   TimeWindowScheduler. PR-D: dispatch 전 station capacity (다른 AGV
        #   reservation 검사) 거부에 사용. 기존 호출 호환 위해 optional —
        #   None 이면 capacity 검사 skip (기존 동작).
        if mode not in ("auto", "manual"):
            raise ValueError(f"unknown TaskGenerator mode: {mode}")
        self._graph = graph
        self._bus = bus
        self._scheduler = scheduler
        self._interval = task_interval_s
        self._next_task_time: float = 0.0
        self._task_counter: int = 0
        self._diagnostics = TaskGenerationDiagnostics()
        self._demand_set = demand_set
        self._demand_index: int = 0
        self._backlogged_demand: TaskDemand | None = None
        self._started: bool = False
        self._completed_demand_ids: set[str] = set()
        # F1a: capability 매칭 가능 fleet 가 없어 보류된 demand 의 task_id 집합.
        # retry 시 중복 카운트 방지용 (diagnostics.unmatched_demand_count 와 동기).
        self._unmatched_demand_ids: set[str] = set()
        self._mode = mode

        # 작업 가능 노드 추출:
        #   - sample_fab.json 기준: NodeRole.WORK
        #   - fab_nav_graph.yaml 기준: is_parking_spot=True (스테이션)
        from src.domain.map.graph import NodeRole
        self._work_nodes = [
            nid for nid, n in graph.nodes.items()
            if n.role == NodeRole.WORK or n.is_parking_spot
        ]
        self._routeable_pairs = [
            (pickup, dropoff)
            for pickup in self._work_nodes
            for dropoff in self._work_nodes
            if pickup != dropoff and graph.get_path(pickup, dropoff)
        ]

    @property
    def diagnostics(self) -> dict:
        data = self._diagnostics.to_dict()
        data["routeable_pair_count"] = len(self._routeable_pairs)
        data["dispatch_mode"] = self._mode
        if self._demand_set:
            data["demand_mode"] = self._demand_set.mode
            data["demand_count"] = len(self._demand_set.demands)
            data["tasks_backlogged"] = max(
                0,
                data["tasks_requested"]
                - data["tasks_dispatched"]
                - data["tasks_rejected_unreachable"],
            )
            data["demand_completion_rate"] = (
                round(data["demands_completed"] / data["tasks_requested"], 4)
                if data["tasks_requested"] else 0.0
            )
        else:
            data["demand_mode"] = "generated"
            data["demand_count"] = 0
            data["demand_completion_rate"] = 0.0
        return data

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._bus.subscribe(
            "uagv/v2/NEXT/demand/completed",
            self._on_demand_completed,
        )

    async def _on_demand_completed(self, payload: dict) -> None:
        demand_id = payload.get("demandId")
        if not demand_id or demand_id in self._completed_demand_ids:
            return
        self._completed_demand_ids.add(demand_id)
        self._diagnostics.demands_completed += 1

    async def step(
        self,
        sim_time: float,
        agvs: dict[str, AGV],
    ) -> None:
        """엔진 틱마다 호출. interval 경과 시 IDLE AGV에 Order 발행."""
        # manual 모드: 외부 dispatch만. demand_set이 있을 때도 자동 발행 금지.
        if self._mode == "manual":
            return

        if self._demand_set is not None:
            await self._step_demand_set(sim_time, agvs)
            return

        if sim_time < self._next_task_time:
            return
        if len(self._work_nodes) < 2:
            self._diagnostics.not_enough_work_nodes += 1
            return

        idle_agvs = [
            agv for agv in agvs.values()
            if agv.is_available_for_dispatch()
        ]
        if not idle_agvs:
            self._diagnostics.no_idle_agv += 1
            return

        agv = random.choice(idle_agvs)

        # 이미 다른 AGV가 향하거나 점유 중인 스테이션 제외
        from src.domain.agv.fsm import AGVState
        occupied_nodes: set[str] = set()
        for other in agvs.values():
            if other.agv_id == agv.agv_id:
                continue
            if other._path:
                occupied_nodes.add(other._path[-1])
            if other.target_node_id:
                occupied_nodes.add(other.target_node_id)
            if other.current_node_id and other.state == AGVState.PROCESSING:
                occupied_nodes.add(other.current_node_id)

        available = [n for n in self._work_nodes if n not in occupied_nodes]
        if len(available) < 2:
            available = self._work_nodes  # 여유 없으면 전체에서 선택
        if len(available) < 2:
            self._diagnostics.not_enough_available_nodes += 1
            return

        candidate_pairs = [
            (pickup, dropoff)
            for pickup, dropoff in self._routeable_pairs
            if pickup in available
            and dropoff in available
            and self._graph.get_path(agv.current_node_id, pickup)
        ]
        if not candidate_pairs and len(available) < len(self._work_nodes):
            self._diagnostics.no_routeable_available_pair += 1
            available = self._work_nodes
            candidate_pairs = [
                (pickup, dropoff)
                for pickup, dropoff in self._routeable_pairs
                if self._graph.get_path(agv.current_node_id, pickup)
            ]

        if not candidate_pairs:
            reachable_pickups = [
                node_id for node_id in available
                if self._graph.get_path(agv.current_node_id, node_id)
            ]
            if not reachable_pickups:
                self._diagnostics.no_path_to_pickup += 1
                for node_id in available[:3]:
                    self._record_path_failure(agv.current_node_id, node_id)
            else:
                self._diagnostics.no_routeable_current_pair += 1
                for pickup in reachable_pickups[:3]:
                    self._record_path_failure(pickup, "<routeable_dropoff>")
            self._next_task_time = sim_time + self._interval
            return

        # PR-D: station capacity 사전 필터 — pickup / dropoff 가 다른 AGV 에게
        # 활성 reservation 잡혀 있으면 (= 그 station 에서 작업 중 또는 가고
        # 있음) 후보에서 제외. random.choice 전에 거름으로써 거부률 ↓.
        # 모든 후보가 막혀 있으면만 station_capacity_reject 로 보류.
        capacity_filtered = [
            (p, d) for (p, d) in candidate_pairs
            if not self._is_node_capacity_blocked(p, agv.agv_id)
            and not self._is_node_capacity_blocked(d, agv.agv_id)
        ]
        if not capacity_filtered:
            self._diagnostics.station_capacity_reject += 1
            self._next_task_time = sim_time + self._interval
            return
        pickup, dropoff = random.choice(capacity_filtered)

        # 현재 위치 → pickup → dropoff 전체 경로
        full_path = self._graph.get_path(agv.current_node_id, pickup)
        if not full_path:
            self._diagnostics.no_path_to_pickup += 1
            self._record_path_failure(agv.current_node_id, pickup)
            self._next_task_time = sim_time + self._interval
            return

        if dropoff != pickup:
            tail = self._graph.get_path(pickup, dropoff)
            if not tail:
                self._diagnostics.no_path_pickup_to_dropoff += 1
                self._record_path_failure(pickup, dropoff)
                self._next_task_time = sim_time + self._interval
                return
            full_path = full_path + tail[1:]  # 중복 노드 제거

        self._task_counter += 1
        self._diagnostics.orders_published += 1
        order_payload = self._build_order(
            task_id=f"task_{self._task_counter:04d}",
            agv_id=agv.agv_id,
            node_ids=full_path,
            dispatch_time_s=sim_time,
        )

        await self._bus.publish(
            f"uagv/v2/NEXT/{agv.agv_id}/order",
            order_payload,
        )
        self._next_task_time = sim_time + self._interval

    # ------------------------------------------------------------------

    async def _step_demand_set(
        self,
        sim_time: float,
        agvs: dict[str, AGV],
    ) -> None:
        released_count = sum(
            1 for demand in self._demand_set.demands
            if demand.release_time_s <= sim_time
        )
        self._diagnostics.tasks_requested = max(
            self._diagnostics.tasks_requested,
            released_count,
        )
        self._diagnostics.tasks_backlogged = max(
            0,
            self._diagnostics.tasks_requested
            - self._diagnostics.tasks_dispatched
            - self._diagnostics.tasks_rejected_unreachable,
        )

        if self._backlogged_demand is not None:
            dispatched = await self._dispatch_demand(
                self._backlogged_demand,
                agvs,
                sim_time,
            )
            if not dispatched:
                # F1a: capability 매칭 가능 fleet 자체가 없으면 영구 실패 →
                # backlog 비우고 다음 demand 진행 (queue 점령 방지).
                if self._backlogged_demand.task_id in self._unmatched_demand_ids:
                    self._backlogged_demand = None
                else:
                    return
            else:
                self._backlogged_demand = None

        while self._demand_index < len(self._demand_set.demands):
            demand = self._demand_set.demands[self._demand_index]
            if demand.release_time_s > sim_time:
                return

            self._demand_index += 1

            if not self._graph.get_path(demand.pickup_node_id, demand.dropoff_node_id):
                self._diagnostics.tasks_rejected_unreachable += 1
                self._record_path_failure(
                    demand.pickup_node_id,
                    demand.dropoff_node_id,
                )
                continue

            dispatched = await self._dispatch_demand(demand, agvs, sim_time)
            if not dispatched:
                # F1a: 매칭 fleet 없는 경우 backlog 안 두고 skip (다음 demand 처리 계속).
                if demand.task_id in self._unmatched_demand_ids:
                    continue
                self._backlogged_demand = demand
                return

    async def _dispatch_demand(
        self,
        demand: TaskDemand,
        agvs: dict[str, AGV],
        sim_time: float,
    ) -> bool:
        from src.domain.fleet import get_eligible_agvs

        # F1a: capability 매칭 → fleet.graph_idx 내 path 가능한 AGV 만
        idle = [agv for agv in agvs.values() if agv.is_available_for_dispatch()]
        eligible = get_eligible_agvs(idle, demand.required_capability)
        idle_agvs = [
            agv for agv in eligible
            if self._graph.get_path(
                agv.current_node_id, demand.pickup_node_id, fleet=agv.fleet
            )
            and self._graph.get_path(
                demand.pickup_node_id, demand.dropoff_node_id, fleet=agv.fleet
            )
        ]
        if not idle_agvs:
            # capability 매칭이 0 이면 이 demand 는 어느 fleet 도 처리 불가 →
            # unmatched 로 마킹 (per-demand 1회 카운트, retry 중복 방지).
            # capability 매칭은 있지만 idle/path 가 없을 뿐이면 no_idle_agv 로만
            # 기록하고 다음 tick 재시도.
            if demand.required_capability and not eligible:
                if demand.task_id not in self._unmatched_demand_ids:
                    self._unmatched_demand_ids.add(demand.task_id)
                    self._diagnostics.unmatched_demand_count += 1
            else:
                self._diagnostics.no_idle_agv += 1
            return False

        agv = min(
            idle_agvs,
            key=lambda a: len(
                self._graph.get_path(a.current_node_id, demand.pickup_node_id, fleet=a.fleet)
            ),
        )
        # PR-D: station capacity 사전 거부 — pickup/dropoff 가 다른 AGV 에게
        # active reservation 잡혀 있으면 보류 (backlog 로 다음 tick 재시도).
        if self._is_node_capacity_blocked(demand.pickup_node_id, agv.agv_id) \
                or self._is_node_capacity_blocked(demand.dropoff_node_id, agv.agv_id):
            self._diagnostics.station_capacity_reject += 1
            return False
        path_to_pickup = self._graph.get_path(
            agv.current_node_id, demand.pickup_node_id, fleet=agv.fleet
        )
        path_to_dropoff = self._graph.get_path(
            demand.pickup_node_id,
            demand.dropoff_node_id,
            fleet=agv.fleet,
        )
        if not path_to_pickup:
            self._diagnostics.no_path_to_pickup += 1
            self._record_path_failure(agv.current_node_id, demand.pickup_node_id)
            return False
        if not path_to_dropoff:
            self._diagnostics.no_path_pickup_to_dropoff += 1
            self._record_path_failure(demand.pickup_node_id, demand.dropoff_node_id)
            return False

        full_path = path_to_pickup + path_to_dropoff[1:]
        order_payload = self._build_order(
            task_id=demand.task_id,
            agv_id=agv.agv_id,
            node_ids=full_path,
            processing_time_s=demand.processing_time_s,
            pickup_processing_time_s=demand.pickup_processing_time_s,
            dropoff_processing_time_s=demand.dropoff_processing_time_s,
            dispatch_time_s=sim_time,
            demand_id=demand.task_id,
            pickup_node_id=demand.pickup_node_id,
            dropoff_node_id=demand.dropoff_node_id,
        )
        await self._bus.publish(
            f"uagv/v2/NEXT/{agv.agv_id}/order",
            order_payload,
        )
        self._diagnostics.tasks_dispatched += 1
        self._diagnostics.orders_published += 1
        return True

    def _record_path_failure(self, src: str | None, dst: str) -> None:
        key = f"{src or '<none>'}->{dst}"
        self._diagnostics.path_failures[key] = (
            self._diagnostics.path_failures.get(key, 0) + 1
        )

    def _is_node_capacity_blocked(self, node_id: str, requester_agv_id: str) -> bool:
        """PR-D: station capacity 거부 검사.

        다른 AGV 가 해당 node 에 active reservation 을 들고 있으면 True.
        scheduler 가 없으면 (legacy 호출) False (기존 동작 유지).
        """
        if self._scheduler is None:
            return False
        for r in self._scheduler._reservations.get(node_id, []):
            if not r.released and r.agv_id != requester_agv_id:
                return True
        return False

    def _build_order(
        self,
        task_id: str,
        agv_id: str,
        node_ids: list[str],
        processing_time_s: float | None = None,
        pickup_processing_time_s: float | None = None,
        dropoff_processing_time_s: float | None = None,
        dispatch_time_s: float | None = None,
        demand_id: str | None = None,
        pickup_node_id: str | None = None,
        dropoff_node_id: str | None = None,
    ) -> dict:
        """
        node_ids 리스트를 VDA5050 Order 포맷으로 직렬화.
        노드 sequenceId: 0, 2, 4 ...
        에지 sequenceId: 1, 3, 5 ...
        """
        nodes = []
        edges = []

        for i, nid in enumerate(node_ids):
            nodes.append({
                "nodeId":     nid,
                "sequenceId": i * 2,
                "released":   True,
                "actions":    [],
            })
            if i < len(node_ids) - 1:
                edges.append({
                    "edgeId":     f"{nid}__{node_ids[i+1]}",
                    "sequenceId": i * 2 + 1,
                    "released":   True,
                })

        payload = {
            "orderId":        task_id,
            "orderUpdateId":  0,
            "agvId":          agv_id,
            "nodes":          nodes,
            "edges":          edges,
        }
        if processing_time_s is not None:
            payload["processingTimeS"] = processing_time_s
        if pickup_processing_time_s is not None:
            payload["pickupProcessingTimeS"] = pickup_processing_time_s
        if dropoff_processing_time_s is not None:
            payload["dropoffProcessingTimeS"] = dropoff_processing_time_s
        if dispatch_time_s is not None:
            payload["dispatchTimeS"] = dispatch_time_s
        if demand_id is not None:
            payload["demandId"] = demand_id
        if pickup_node_id is not None:
            payload["pickupNodeId"] = pickup_node_id
        if dropoff_node_id is not None:
            payload["dropoffNodeId"] = dropoff_node_id
        return payload
