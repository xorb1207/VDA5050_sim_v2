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

    @classmethod
    def from_dict(cls, d: dict) -> Scenario:
        policy = TrafficPolicy.from_dict(d.get("traffic_policy", {}))
        profile = TaskProfile.from_dict(d.get("task_profile", {}))
        fleet = d.get("fleet", {})
        return cls(
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            map_file=d.get("map_file", "maps/sample_fab.json"),
            fleet_size=int(fleet.get("size", d.get("fleet_size", 3))),
            runtime_seconds=int(d.get("runtime_seconds", 300)),
            tick_hz=int(d.get("tick_hz", 50)),
            random_seed=int(d.get("random_seed", 42)),
            initial_nodes=fleet.get("initial_nodes", []),
            traffic_policy=policy,
            task_profile=profile,
            stall_threshold_seconds=float(d.get("stall_threshold_seconds", 30.0)),
        )
