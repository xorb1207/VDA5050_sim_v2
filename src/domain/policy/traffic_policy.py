from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrafficPolicy:
    """
    운영 정책 — 맵과 분리.
    같은 맵 위에 다른 정책을 올려 비교 실험할 수 있는 핵심 계층.
    """
    lane_mode: str            # "one_way" | "bidirectional"
    lane_count: int           # 1 | 2
    reservation_mode: str     # "next_node" | "lookahead_n" | "critical_section"
    lookahead_depth: int      # reservation_mode=next_node 이면 항상 1
    allow_reroute: bool
    critical_section_enabled: bool = False

    # ── 팩토리 ────────────────────────────────────────────────────

    @classmethod
    def default(cls) -> TrafficPolicy:
        """baseline 기본값."""
        return cls(
            lane_mode="one_way",
            lane_count=1,
            reservation_mode="next_node",
            lookahead_depth=1,
            allow_reroute=False,
        )

    @classmethod
    def from_dict(cls, d: dict) -> TrafficPolicy:
        return cls(
            lane_mode=d.get("lane_mode", "one_way"),
            lane_count=int(d.get("lane_count", 1)),
            reservation_mode=d.get("reservation_mode", "next_node"),
            lookahead_depth=int(d.get("lookahead_depth", 1)),
            allow_reroute=bool(d.get("allow_reroute", False)),
            critical_section_enabled=bool(d.get("critical_section_enabled", False)),
        )

    def __post_init__(self) -> None:
        # constraint: next_node 이면 lookahead_depth 강제 1
        if self.reservation_mode == "next_node":
            self.lookahead_depth = 1
