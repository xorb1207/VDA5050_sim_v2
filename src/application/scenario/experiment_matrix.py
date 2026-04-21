from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import yaml

from src.application.scenario.scenario import Scenario, TaskProfile
from src.domain.policy.traffic_policy import TrafficPolicy


@dataclass
class ExperimentMatrix:
    """
    YAML experiment set → Scenario 목록 자동 생성.
    constraints로 무의미한 조합 제거.
    """
    name:            str
    base_map_file:   str
    runtime_seconds: int
    tick_hz:         int
    random_seed:     int
    task_profile:    TaskProfile
    output_dir:      str

    matrix: dict[str, list[Any]] = field(default_factory=dict)
    constraints: list[dict] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str) -> ExperimentMatrix:
        with open(path) as f:
            d = yaml.safe_load(f)

        profile = TaskProfile.from_dict(d.get("task_profile", {}))
        outputs = d.get("outputs", {})

        return cls(
            name=d.get("name", "experiment"),
            base_map_file=d.get("base_map_file", "maps/sample_fab.json"),
            runtime_seconds=int(d.get("runtime_seconds", 300)),
            tick_hz=int(d.get("tick_hz", 50)),
            random_seed=int(d.get("random_seed", 42)),
            task_profile=profile,
            output_dir=outputs.get("directory", "outputs/reports/experiment"),
            matrix=d.get("matrix", {}),
            constraints=d.get("constraints", []),
        )

    def generate_scenarios(self) -> list[Scenario]:
        """matrix 조합 생성 → constraints 적용 → Scenario 목록 반환."""
        keys   = list(self.matrix.keys())
        values = list(self.matrix.values())
        combos = list(itertools.product(*values))

        scenarios = []
        for i, combo in enumerate(combos, 1):
            params = dict(zip(keys, combo))

            # constraints 적용
            if self._violates_constraints(params):
                continue

            policy = TrafficPolicy(
                lane_mode=params.get("lane_mode", "one_way"),
                lane_count=int(params.get("lane_count", 1)),
                reservation_mode=params.get("reservation_mode", "next_node"),
                lookahead_depth=int(params.get("lookahead_depth", 1)),
                allow_reroute=bool(params.get("allow_reroute", False)),
            )

            s = Scenario(
                name=f"scenario_{i:03d}",
                description=self._describe(params),
                map_file=self.base_map_file,
                fleet_size=int(params.get("fleet_size", 3)),
                runtime_seconds=self.runtime_seconds,
                tick_hz=self.tick_hz,
                random_seed=self.random_seed,
                traffic_policy=policy,
                task_profile=self.task_profile,
            )
            scenarios.append(s)

        return scenarios

    def _violates_constraints(self, params: dict) -> bool:
        """constraints 체크. 위반 시 True (조합 제외)."""
        for constraint in self.constraints:
            if_block   = constraint.get("if", {})
            then_block = constraint.get("then", {})

            # if 조건 전부 일치 여부
            if all(str(params.get(k)) == str(v) for k, v in if_block.items()):
                # then 조건과 현재 params가 불일치하면 위반
                for k, v in then_block.items():
                    if str(params.get(k)) != str(v):
                        return True
        return False

    @staticmethod
    def _describe(params: dict) -> str:
        parts = [f"{k}={v}" for k, v in params.items()]
        return " | ".join(parts)
