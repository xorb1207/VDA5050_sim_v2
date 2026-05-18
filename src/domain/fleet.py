"""
Fleet domain class — 이기종 AGV 그룹 정의 (F1a)

각 Fleet은:
- 자신의 lane graph (graph_idx)에서만 이동
- 처리 가능 task type (capabilities)
- 시각화용 색상, 최대 속도, 우선순위, 시뮬 시작 시 배치 수
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV


@dataclass
class Fleet:
    """이기종 AGV fleet 정의."""
    id: str                             # "TYPE_1", "TYPE_2", "TYPE_3" 등
    graph_idx: int                      # 사용할 lane graph index
    capabilities: list[str] = field(default_factory=list)
                                        # 처리 가능 task 종류 (예: ["overhead", "pickup_small"])
    color: str = "#0f9d58"              # 시각화용 (UI에서 사용)
    max_speed_mps: float = 1.5
    priority: int = 1                   # 낮을수록 우선 (1 = 가장 우선)
    count: int = 1                      # 시뮬 시작 시 배치할 AGV 수


def get_eligible_agvs(
    agvs: Iterable["AGV"],
    required_capability: Optional[str],
) -> list["AGV"]:
    """capability 매칭으로 AGV 후보 필터.

    required_capability=None → 전체 통과 (legacy demand).
    그 외에는 agv.fleet.capabilities 에 capability 포함된 AGV 만.
    Fleet 미부여(self-tests 등)는 capability 검사에서 자동 탈락.
    """
    if not required_capability:
        return list(agvs)
    out: list["AGV"] = []
    for agv in agvs:
        fl = getattr(agv, "fleet", None)
        caps = list(getattr(fl, "capabilities", None) or [])
        if required_capability in caps:
            out.append(agv)
    return out
