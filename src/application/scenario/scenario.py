from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.domain.policy.traffic_policy import TrafficPolicy


@dataclass
class TaskProfile:
    """태스크 생성 패턴."""
    type: str                        # "cyclic" | "hotspot" | "poisson"
    generator: str                   # "fixed_interval" | "poisson"
    interval_seconds: float = 20.0   # fixed_interval 용
    lambda_per_minute: float = 10.0  # poisson 용
    pickup_nodes:  list[str] = field(default_factory=list)
    dropoff_nodes: list[str] = field(default_factory=list)
    hotspot_nodes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> TaskProfile:
        return cls(
            type=d.get("type", "cyclic"),
            generator=d.get("generator", "fixed_interval"),
            interval_seconds=float(d.get("interval_seconds", 20.0)),
            lambda_per_minute=float(d.get("lambda_per_minute", 10.0)),
            pickup_nodes=d.get("pickup_nodes", []),
            dropoff_nodes=d.get("dropoff_nodes", []),
            hotspot_nodes=d.get("hotspot_nodes", []),
        )


@dataclass
class Scenario:
    """
    실험 조건 단위.
    map / policy / fleet / task 가 모두 여기서 조합됨.
    """
    name:            str
    map_file:        str
    fleet_size:      int
    runtime_seconds: int
    random_seed:     int
    traffic_policy:  TrafficPolicy
    task_profile:    TaskProfile

    description:     str            = ""
    tick_hz:         int            = 50
    initial_nodes:   list[str]      = field(default_factory=list)
    stall_threshold_seconds: float  = 30.0   # deadlock 감지 기준
    # F1a: 다중 fleet 정의 (선택). 비어 있으면 단일 fleet (fleet_size 만 사용).
    # 항목 예: {"id": "TYPE_A", "graph_idx": 0, "count": 6,
    #           "capabilities": ["overhead"], "color": "#0f9d58",
    #           "max_speed_mps": 1.5, "priority": 1}
    fleets:          list[dict]     = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> Scenario:
        policy = TrafficPolicy.from_dict(d.get("traffic_policy", {}))
        profile = TaskProfile.from_dict(d.get("task_profile", {}))
        fleet = d.get("fleet", {})
        fleets_raw = d.get("fleets", []) or []
        fleets: list[dict] = []
        for f in fleets_raw:
            if not isinstance(f, dict) or f.get("id") is None:
                continue
            fleets.append({
                "id": str(f["id"]),
                "graph_idx": int(f.get("graph_idx", 0)),
                "count": int(f.get("count", 1)),
                "capabilities": list(f.get("capabilities", []) or []),
                "color": str(f.get("color", "#0f9d58")),
                "max_speed_mps": float(f.get("max_speed_mps", 1.5)),
                "priority": int(f.get("priority", 1)),
            })
        # fleets 가 있으면 fleet_size 를 그 합으로 자동 설정 (없으면 legacy 동작)
        if fleets:
            inferred_size = sum(int(f["count"]) for f in fleets)
            fleet_size = int(fleet.get("size", d.get("fleet_size", inferred_size)))
        else:
            fleet_size = int(fleet.get("size", d.get("fleet_size", 3)))
        return cls(
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            map_file=d.get("map_file", "maps/sample_fab.json"),
            fleet_size=fleet_size,
            runtime_seconds=int(d.get("runtime_seconds", 300)),
            tick_hz=int(d.get("tick_hz", 50)),
            random_seed=int(d.get("random_seed", 42)),
            initial_nodes=fleet.get("initial_nodes", []),
            traffic_policy=policy,
            task_profile=profile,
            stall_threshold_seconds=float(d.get("stall_threshold_seconds", 30.0)),
            fleets=fleets,
        )
