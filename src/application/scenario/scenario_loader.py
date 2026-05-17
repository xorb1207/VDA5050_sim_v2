from __future__ import annotations

import yaml
from pathlib import Path

from src.application.scenario.scenario import Scenario


# 지원 맵 포맷 확장자 (GAP-D: RMF YAML + 기존 JSON)
_MAP_JSON_EXTS = (".json",)
_MAP_YAML_EXTS = (".yaml", ".yml")
_MAP_SUPPORTED_EXTS = _MAP_JSON_EXTS + _MAP_YAML_EXTS


def detect_map_format(path: str) -> str:
    """맵 파일 확장자로 포맷 자동 감지. 반환: 'json' | 'yaml'.

    .json → 'json', .yaml/.yml → 'yaml'. 그 외는 ValueError.
    """
    p = str(path).lower()
    if p.endswith(_MAP_YAML_EXTS):
        return "yaml"
    if p.endswith(_MAP_JSON_EXTS):
        return "json"
    raise ValueError(
        f"Unknown map file extension: {path}. "
        f"Supported: {', '.join(_MAP_SUPPORTED_EXTS)}"
    )


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

        # 1. 맵 파일 존재 + 지원 확장자 여부 (GAP-D: .json/.yaml/.yml 자동 감지)
        if not Path(scenario.map_file).exists():
            errors.append(f"map_file not found: {scenario.map_file}")
        else:
            try:
                detect_map_format(scenario.map_file)
            except ValueError as ex:
                errors.append(str(ex))

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

        # 7. F1a: fleets 섹션 검증 (있을 때만)
        if scenario.fleets:
            seen_ids: set[str] = set()
            for i, f in enumerate(scenario.fleets):
                fid = f.get("id")
                if not fid:
                    errors.append(f"fleets[{i}].id 누락")
                    continue
                if fid in seen_ids:
                    errors.append(f"fleets[{i}].id 중복: {fid}")
                seen_ids.add(fid)
                if int(f.get("count", 0)) <= 0:
                    errors.append(f"fleets[{fid}].count must be > 0")
                if int(f.get("graph_idx", -1)) < 0:
                    errors.append(f"fleets[{fid}].graph_idx must be >= 0")
            # fleet_size 와 sum(count) 정합성 (사용자가 fleet_size 를 명시한 경우)
            sum_count = sum(int(f.get("count", 0)) for f in scenario.fleets)
            if scenario.fleet_size != sum_count:
                # 경고만 — 사용자가 의도적으로 다르게 설정할 수도 있음
                print(
                    f"[WARN] {scenario.name}: fleet_size ({scenario.fleet_size}) "
                    f"!= sum(fleets.count) ({sum_count})"
                )

        if errors:
            raise ValueError(
                f"Scenario '{scenario.name}' validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
