from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.map.graph import MapGraph


@dataclass(frozen=True)
class TaskDemand:
    """Topology 비교용 공통 수요 단위."""
    task_id: str
    release_time_s: float
    pickup_node_id: str
    dropoff_node_id: str
    processing_time_s: float
    pickup_processing_time_s: float = 0.0
    dropoff_processing_time_s: float = 0.0
    priority: int = 0


@dataclass
class DemandSet:
    """
    실험 전에 고정되는 task demand sequence.

    mode=common_demand:
      모든 topology가 같은 pickup/dropoff sequence를 받는다.
      불가능 task는 runner/KPI에서 rejected_unreachable로 세야 한다.

    mode=capability:
      해당 topology에서 routeable한 pickup/dropoff pair만 생성한다.
      topology 내부 효율을 보는 용도다.
    """
    mode: str
    random_seed: int
    demands: list[TaskDemand] = field(default_factory=list)

    @classmethod
    def common_from_graph(
        cls,
        graph: MapGraph,
        count: int,
        interval_s: float,
        random_seed: int,
        pickup_processing_time_range_s: tuple[float, float] = (20.0, 60.0),
        dropoff_processing_time_range_s: tuple[float, float] = (30.0, 120.0),
    ) -> "DemandSet":
        nodes = _work_node_ids(graph)
        return cls._generate(
            mode="common_demand",
            pairs=[(p, d) for p in nodes for d in nodes if p != d],
            count=count,
            interval_s=interval_s,
            random_seed=random_seed,
            pickup_processing_time_range_s=pickup_processing_time_range_s,
            dropoff_processing_time_range_s=dropoff_processing_time_range_s,
        )

    @classmethod
    def capability_from_graph(
        cls,
        graph: MapGraph,
        count: int,
        interval_s: float,
        random_seed: int,
        pickup_processing_time_range_s: tuple[float, float] = (20.0, 60.0),
        dropoff_processing_time_range_s: tuple[float, float] = (30.0, 120.0),
    ) -> "DemandSet":
        nodes = _work_node_ids(graph)
        pairs = [
            (pickup, dropoff)
            for pickup in nodes
            for dropoff in nodes
            if pickup != dropoff and graph.get_path(pickup, dropoff)
        ]
        return cls._generate(
            mode="capability",
            pairs=pairs,
            count=count,
            interval_s=interval_s,
            random_seed=random_seed,
            pickup_processing_time_range_s=pickup_processing_time_range_s,
            dropoff_processing_time_range_s=dropoff_processing_time_range_s,
        )

    @classmethod
    def _generate(
        cls,
        mode: str,
        pairs: list[tuple[str, str]],
        count: int,
        interval_s: float,
        random_seed: int,
        pickup_processing_time_range_s: tuple[float, float],
        dropoff_processing_time_range_s: tuple[float, float],
    ) -> "DemandSet":
        if count < 0:
            raise ValueError("count must be >= 0")
        if interval_s < 0:
            raise ValueError("interval_s must be >= 0")
        if count > 0 and not pairs:
            raise ValueError("cannot generate demands without candidate pairs")

        rng = random.Random(random_seed)
        pickup_low, pickup_high = pickup_processing_time_range_s
        dropoff_low, dropoff_high = dropoff_processing_time_range_s
        demands: list[TaskDemand] = []
        for i in range(count):
            pickup, dropoff = rng.choice(pairs)
            pickup_processing_time = round(rng.uniform(pickup_low, pickup_high), 3)
            dropoff_processing_time = round(rng.uniform(dropoff_low, dropoff_high), 3)
            processing_time = round(
                pickup_processing_time + dropoff_processing_time,
                3,
            )
            demands.append(
                TaskDemand(
                    task_id=f"demand_{i + 1:05d}",
                    release_time_s=round(i * interval_s, 3),
                    pickup_node_id=pickup,
                    dropoff_node_id=dropoff,
                    processing_time_s=processing_time,
                    pickup_processing_time_s=pickup_processing_time,
                    dropoff_processing_time_s=dropoff_processing_time,
                )
            )
        return cls(mode=mode, random_seed=random_seed, demands=demands)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "random_seed": self.random_seed,
            "count": len(self.demands),
            "demands": [
                {
                    "task_id": d.task_id,
                    "release_time_s": d.release_time_s,
                    "pickup_node_id": d.pickup_node_id,
                    "dropoff_node_id": d.dropoff_node_id,
                    "processing_time_s": d.processing_time_s,
                    "pickup_processing_time_s": d.pickup_processing_time_s,
                    "dropoff_processing_time_s": d.dropoff_processing_time_s,
                    "priority": d.priority,
                }
                for d in self.demands
            ],
        }


def _work_node_ids(graph: MapGraph) -> list[str]:
    from src.domain.map.graph import NodeRole

    return [
        node_id
        for node_id, node in graph.nodes.items()
        if node.role == NodeRole.WORK or node.is_parking_spot
    ]
