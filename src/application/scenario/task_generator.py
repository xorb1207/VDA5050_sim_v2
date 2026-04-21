from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.map.graph import MapGraph
    from src.interfaces.bus import IMessageBus
    from src.domain.agv.agv import AGV
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
    orders_published: int = 0
    no_idle_agv: int = 0
    not_enough_work_nodes: int = 0
    not_enough_available_nodes: int = 0
    no_path_to_pickup: int = 0
    no_path_pickup_to_dropoff: int = 0
    path_failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "orders_published": self.orders_published,
            "no_idle_agv": self.no_idle_agv,
            "not_enough_work_nodes": self.not_enough_work_nodes,
            "not_enough_available_nodes": self.not_enough_available_nodes,
            "no_path_to_pickup": self.no_path_to_pickup,
            "no_path_pickup_to_dropoff": self.no_path_pickup_to_dropoff,
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
    ) -> None:
        self._graph = graph
        self._bus = bus
        self._interval = task_interval_s
        self._next_task_time: float = 0.0
        self._task_counter: int = 0
        self._diagnostics = TaskGenerationDiagnostics()

        # 작업 가능 노드 추출:
        #   - sample_fab.json 기준: NodeRole.WORK
        #   - fab_nav_graph.yaml 기준: is_parking_spot=True (스테이션)
        from src.domain.map.graph import NodeRole
        self._work_nodes = [
            nid for nid, n in graph.nodes.items()
            if n.role == NodeRole.WORK or n.is_parking_spot
        ]

    @property
    def diagnostics(self) -> dict:
        return self._diagnostics.to_dict()

    async def step(
        self,
        sim_time: float,
        agvs: dict[str, AGV],
    ) -> None:
        """엔진 틱마다 호출. interval 경과 시 IDLE AGV에 Order 발행."""
        if sim_time < self._next_task_time:
            return
        if len(self._work_nodes) < 2:
            self._diagnostics.not_enough_work_nodes += 1
            return

        idle_agvs = [
            agv for agv in agvs.values()
            if agv.state.value == "IDLE" and agv.current_node_id
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

        pickup, dropoff = random.sample(available, 2)

        # 현재 위치 → pickup → dropoff 전체 경로
        full_path = self._graph.get_path(agv.current_node_id, pickup)
        if not full_path:
            self._diagnostics.no_path_to_pickup += 1
            self._record_path_failure(agv.current_node_id, pickup)
            return

        if dropoff != pickup:
            tail = self._graph.get_path(pickup, dropoff)
            if not tail:
                self._diagnostics.no_path_pickup_to_dropoff += 1
                self._record_path_failure(pickup, dropoff)
                return
            full_path = full_path + tail[1:]  # 중복 노드 제거

        self._task_counter += 1
        self._diagnostics.orders_published += 1
        order_payload = self._build_order(
            task_id=f"task_{self._task_counter:04d}",
            agv_id=agv.agv_id,
            node_ids=full_path,
        )

        await self._bus.publish(
            f"uagv/v2/NEXT/{agv.agv_id}/order",
            order_payload,
        )
        self._next_task_time = sim_time + self._interval

    # ------------------------------------------------------------------

    def _record_path_failure(self, src: str | None, dst: str) -> None:
        key = f"{src or '<none>'}->{dst}"
        self._diagnostics.path_failures[key] = (
            self._diagnostics.path_failures.get(key, 0) + 1
        )

    def _build_order(
        self,
        task_id: str,
        agv_id: str,
        node_ids: list[str],
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

        return {
            "orderId":        task_id,
            "orderUpdateId":  0,
            "agvId":          agv_id,
            "nodes":          nodes,
            "edges":          edges,
        }
