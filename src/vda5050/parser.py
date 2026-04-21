from __future__ import annotations

from src.domain.task.order import Order, OrderState, VDA5050NodeRef, VDA5050EdgeRef


class VDA5050Parser:
    """VDA5050 JSON 메시지 ↔ 내부 모델 변환."""

    @staticmethod
    def parse_order(payload: dict) -> Order:
        """Order 메시지 파싱. base/horizon 노드·에지 분리."""
        nodes_raw = payload.get("nodes", [])
        edges_raw = payload.get("edges", [])

        base_nodes = [
            VDA5050NodeRef(
                nodeId=n["nodeId"],
                sequenceId=n["sequenceId"],
                released=n.get("released", True),
                actions=n.get("actions", []),
            )
            for n in nodes_raw if n.get("released", True)
        ]
        horizon_nodes = [
            VDA5050NodeRef(
                nodeId=n["nodeId"],
                sequenceId=n["sequenceId"],
                released=False,
                actions=n.get("actions", []),
            )
            for n in nodes_raw if not n.get("released", True)
        ]
        base_edges = [
            VDA5050EdgeRef(
                edgeId=e["edgeId"],
                sequenceId=e["sequenceId"],
                released=e.get("released", True),
                maxSpeed=e.get("maxSpeed"),
            )
            for e in edges_raw if e.get("released", True)
        ]
        horizon_edges = [
            VDA5050EdgeRef(
                edgeId=e["edgeId"],
                sequenceId=e["sequenceId"],
                released=False,
                maxSpeed=e.get("maxSpeed"),
            )
            for e in edges_raw if not e.get("released", True)
        ]

        return Order(
            order_id=payload["orderId"],
            order_update_id=payload.get("orderUpdateId", 0),
            agv_id=payload.get("agvId", ""),
            base_nodes=base_nodes,
            horizon_nodes=horizon_nodes,
            base_edges=base_edges,
            horizon_edges=horizon_edges,
            state=OrderState.ASSIGNED,
        )

    @staticmethod
    def build_state_message(agv_id: str, agv_state: dict) -> dict:
        """내부 AGV 상태 → VDA5050 State 메시지 직렬화."""
        return {
            "headerId":    0,
            "timestamp":   "",          # 실 구현 시 ISO8601
            "version":     "2.0.0",
            "manufacturer": "simulator",
            "serialNumber": agv_id,
            "agvPosition": {
                "x":               agv_state.get("x", 0.0),
                "y":               agv_state.get("y", 0.0),
                "theta":           agv_state.get("heading", 0.0),
                "positionInitialized": True,
            },
            "batteryState": {
                "batteryCharge": agv_state.get("battery_pct", 100.0),
            },
            "operatingMode":  "AUTOMATIC",
            "nodeStates":     [],
            "edgeStates":     [],
            "actionStates":   [],
            "driving":        agv_state.get("state") == "NAVIGATING",
            "errors":         [],
            "informations":   [],
        }


class VDA5050Validator:
    """VDA5050 규격 준수 검증."""

    @staticmethod
    def validate_order_sequence(payload: dict) -> bool:
        """
        노드 sequenceId 짝수, 에지 홀수 검증.
        단순 구현 — 실 규격의 base/horizon 연속성 검증은 TODO.
        """
        for n in payload.get("nodes", []):
            if n.get("sequenceId", 0) % 2 != 0:
                return False
        for e in payload.get("edges", []):
            if e.get("sequenceId", 1) % 2 != 1:
                return False
        return True

    @staticmethod
    def validate_order_update(current: Order, update: Order) -> bool:
        """order_id 동일 + order_update_id 증가 검증."""
        return (
            current.order_id == update.order_id
            and update.order_update_id > current.order_update_id
        )
