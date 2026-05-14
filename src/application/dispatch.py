from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV
    from src.domain.map.graph import MapGraph
    from src.interfaces.bus import IMessageBus


DispatchStatus = Literal[
    "success",
    "amr_not_found",
    "amr_busy",
    "node_not_found",
    "no_path",
]


@dataclass
class DispatchResult:
    status: DispatchStatus
    job_id: Optional[str] = None
    estimated_arrival_s: Optional[float] = None
    reason: Optional[str] = None


class JobDispatcher:
    """
    외부 시스템(Agent C UI 등)이 호출하는 dispatch 진입점.

    랜덤/demand_set 자동 발행과 무관하게, "특정 AMR + 목적지 노드" 단위로
    Order를 메시지 버스에 발행한다. TaskGenerator가 manual 모드일 때 이 API
    만으로 작업이 생성된다.

    검증 순서:
      1. AMR 존재
      2. 목적지 노드 존재
      3. AMR이 dispatch 가능한 상태 (is_available_for_dispatch)
      4. 현재 위치 → 목적지 path 존재
    실패 시 메시지 발행 없이 DispatchResult.reason으로 사유 반환.
    """

    def __init__(
        self,
        graph: "MapGraph",
        bus: "IMessageBus",
        agvs: dict[str, "AGV"],
        default_speed_mps: float = 1.0,
    ) -> None:
        self._graph = graph
        self._bus = bus
        self._agvs = agvs
        self._default_speed = max(default_speed_mps, 0.01)
        self._counter = 0
        self.history: list[DispatchResult] = []

    async def dispatch(
        self,
        amr_id: str,
        destination_node_id: str,
        sim_time: float = 0.0,
    ) -> DispatchResult:
        agv = self._agvs.get(amr_id)
        if agv is None:
            return self._fail("amr_not_found", f"unknown amr_id={amr_id}")

        if destination_node_id not in self._graph.nodes:
            return self._fail(
                "node_not_found",
                f"unknown destination_node_id={destination_node_id}",
            )

        if not agv.is_available_for_dispatch():
            return self._fail(
                "amr_busy",
                f"amr {amr_id} not idle (state={agv.state.value})",
            )

        start = agv.current_node_id
        if not start:
            return self._fail(
                "amr_busy",
                f"amr {amr_id} has no current node",
            )

        path = self._graph.get_path(start, destination_node_id)
        if not path or len(path) < 1:
            return self._fail(
                "no_path",
                f"no path {start} -> {destination_node_id}",
            )

        self._counter += 1
        job_id = f"dispatch_{self._counter:05d}"
        order_payload = self._build_order(job_id, amr_id, path, sim_time)
        await self._bus.publish(
            f"uagv/v2/NEXT/{amr_id}/order",
            order_payload,
        )

        eta = self._estimate_arrival_s(path, agv)
        result = DispatchResult(
            status="success",
            job_id=job_id,
            estimated_arrival_s=round(eta, 2),
        )
        self.history.append(result)
        return result

    # ---------------------------------------------------------------

    def _fail(self, status: DispatchStatus, reason: str) -> DispatchResult:
        result = DispatchResult(status=status, reason=reason)
        self.history.append(result)
        return result

    def _build_order(
        self,
        job_id: str,
        amr_id: str,
        node_ids: list[str],
        sim_time: float,
    ) -> dict:
        nodes = []
        edges = []
        for i, nid in enumerate(node_ids):
            nodes.append({
                "nodeId": nid,
                "sequenceId": i * 2,
                "released": True,
                "actions": [],
            })
            if i < len(node_ids) - 1:
                edges.append({
                    "edgeId": f"{nid}__{node_ids[i+1]}",
                    "sequenceId": i * 2 + 1,
                    "released": True,
                })
        return {
            "orderId": job_id,
            "orderUpdateId": 0,
            "agvId": amr_id,
            "nodes": nodes,
            "edges": edges,
            "dispatchTimeS": sim_time,
        }

    def _estimate_arrival_s(self, path: list[str], agv: "AGV") -> float:
        if len(path) < 2:
            return 0.0
        total = 0.0
        speed = max(getattr(agv._motion, "max_speed", self._default_speed), 0.01)
        for src, dst in zip(path[:-1], path[1:]):
            total += self._graph._calc_distance(src, dst) / speed
        return total
