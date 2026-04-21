from __future__ import annotations

import yaml
from pathlib import Path

from src.application.scenario.scenario import Scenario


class ScenarioLoader:
    """YAML 파일 → Scenario 객체 변환."""

    @staticmethod
    def load(path: str) -> Scenario:
        with open(path) as f:
            data = yaml.safe_load(f)
        scenario = Scenario.from_dict(data)
        ScenarioValidator.validate(scenario)
        return scenario

    @staticmethod
    def load_many(paths: list[str]) -> list[Scenario]:
        return [ScenarioLoader.load(p) for p in paths]


class ScenarioValidator:
    """
    시나리오 유효성 검사.
    맵 파일 존재, fleet/node 일치, policy 정합성 확인.
    """

    @staticmethod
    def validate(scenario: Scenario) -> None:
        errors = []

        # 1. 맵 파일 존재 여부
        if not Path(scenario.map_file).exists():
            errors.append(f"map_file not found: {scenario.map_file}")

        # 2. fleet_size > 0
        if scenario.fleet_size <= 0:
            errors.append("fleet_size must be > 0")

        # 3. initial_nodes 수 vs fleet_size
        if scenario.initial_nodes and len(scenario.initial_nodes) != scenario.fleet_size:
            errors.append(
                f"initial_nodes count ({len(scenario.initial_nodes)}) "
                f"!= fleet_size ({scenario.fleet_size})"
            )

        # 4. reservation_mode=next_node → lookahead_depth 자동 1 (경고만)
        p = scenario.traffic_policy
        if p.reservation_mode == "next_node" and p.lookahead_depth != 1:
            # __post_init__에서 이미 강제되지만 로그 남김
            print(
                f"[WARN] {scenario.name}: reservation_mode=next_node "
                f"→ lookahead_depth forced to 1"
            )

        # 5. lane_count=2 경고 (맵 실제 병렬 에지 검증은 STEP 3에서)
        if p.lane_count == 2:
            print(
                f"[WARN] {scenario.name}: lane_count=2 — "
                "ensure map has parallel edges (validated in STEP 3)"
            )

        # 6. runtime > 0
        if scenario.runtime_seconds <= 0:
            errors.append("runtime_seconds must be > 0")

        if errors:
            raise ValueError(
                f"Scenario '{scenario.name}' validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
