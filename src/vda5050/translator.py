from __future__ import annotations

from src.domain.task.task import Task, TaskState


class VDA5050Translator:
    """
    VDA5050 Order → 내부 Task 변환.
    parser(포맷 파싱) · validator(규격 검증)와 분리.
    domain/task는 VDA5050을 모름 — 이 계층이 다리 역할.
    """

    @staticmethod
    def to_task(order, sim_time: float = 0.0) -> Task:
        """
        파싱된 Order → Task.
        node_path는 base_nodes released=True 순서.
        """
        node_path = [n.nodeId for n in order.base_nodes if n.released]
        return Task(
            task_id=order.order_id,
            agv_id=order.agv_id,
            node_path=node_path,
            state=TaskState.ASSIGNED,
            created_at=sim_time,
            assigned_at=sim_time,
            source_order_id=order.order_id,
        )
