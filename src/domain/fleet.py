"""
Fleet domain class — 이기종 AGV 그룹 정의 (F1a)

각 Fleet은:
- 자신의 lane graph (graph_idx)에서만 이동
- 처리 가능 task type (capabilities)
- 시각화용 색상, 최대 속도, 우선순위, 시뮬 시작 시 배치 수
"""
from dataclasses import dataclass, field


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
