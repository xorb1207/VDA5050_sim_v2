"""
graph.py — Open-RMF nav graph 기반 MapGraph
from_rmf_yaml()로 fab_nav_graph.yaml 로드
기존 from_json() 인터페이스는 sample_fab.json 테스트용으로 유지
"""
from __future__ import annotations

import heapq
import json
import math
import yaml
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NodeRole(Enum):
    STANDARD = "standard"
    APPROACH = "approach"   # 교차로/Bay 진입 전 판단점 — 예약 확정
    SIDING   = "siding"     # 교행 대피
    WORK     = "work"       # station pick/drop (lock_radius 확대)
    CHARGER  = "charger"    # 충전


# Open-RMF rmf_role 문자열 → NodeRole 매핑
_RMF_ROLE_MAP: dict[str, NodeRole] = {
    "standard": NodeRole.STANDARD,
    "approach": NodeRole.APPROACH,
    "siding":   NodeRole.SIDING,
    "work":     NodeRole.WORK,
    "charger":  NodeRole.CHARGER,
}


@dataclass
class Node:
    node_id: str
    x: float
    y: float
    role: NodeRole = NodeRole.STANDARD
    lock_radius: int = 1
    # Open-RMF 확장
    is_holding_point: bool = False
    is_charger: bool = False
    is_parking_spot: bool = False
    allowed_deviation_xy: float = 0.0
    allowed_orientations: list[float] = field(default_factory=list)


@dataclass
class Edge:
    edge_id: str
    start_node_id: str
    end_node_id: str
    capacity: int = 1
    bidirectional: bool = False
    max_speed: float = 1.0
    distance: float = 0.0
    width_m: float = 1.5
    safety_model: str = ""
    # Open-RMF 확장
    corridor: str = ""
    access_type: str = ""     # station_access / charger_access
    # F1b-core: 개별 edge 속도 제한 (블라인드 스팟 등). None이면 AGV intrinsic max_speed 사용.
    v_max: Optional[float] = None


class MapGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self._out_edges: dict[str, list[str]] = {}

    # ──────────────────────────────────────────────────
    # Open-RMF nav graph YAML 로더 (메인 경로)
    # ──────────────────────────────────────────────────
    @classmethod
    def from_rmf_yaml(cls, path: str, level: str = None) -> "MapGraph":
        """
        Open-RMF nav graph YAML 로드.
        level=None 이면 첫 번째 레벨 자동 선택.
        포맷:
          levels:
            FAB_L1:
              vertices: [[x, y, {name, rmf_role, is_charger, ...}], ...]
              lanes:    [[src_idx, dst_idx, {speed_limit, bidirectional, ...}], ...]
        """
        with open(path) as f:
            data = yaml.safe_load(f)

        levels = data.get("levels", {})
        if not levels:
            raise ValueError(f"No levels found in {path}")
        level_name = level or next(iter(levels))
        lvl = levels[level_name]

        graph = cls()
        raw_vertices = lvl.get("vertices", [])

        # vertex → Node
        idx_to_id: dict[int, str] = {}
        for i, v in enumerate(raw_vertices):
            x, y = float(v[0]), float(v[1])
            params: dict = v[2] if len(v) > 2 else {}

            name = params.get("name", f"node_{i:04d}")
            rmf_role = params.get("rmf_role", "standard")

            # charger flag가 있으면 role=CHARGER 강제
            if params.get("is_charger"):
                role = NodeRole.CHARGER
            else:
                role = _RMF_ROLE_MAP.get(rmf_role, NodeRole.STANDARD)

            node = Node(
                node_id=name,
                x=x,
                y=y,
                role=role,
                lock_radius=2 if role == NodeRole.WORK else 1,
                is_holding_point=bool(params.get("is_holding_point", False)),
                is_charger=bool(params.get("is_charger", False)),
                is_parking_spot=bool(params.get("is_parking_spot", False)),
            )
            graph._add_node(node)
            idx_to_id[i] = name

        # lane → Edge
        for li, lane in enumerate(lvl.get("lanes", [])):
            src_idx, dst_idx = int(lane[0]), int(lane[1])
            params: dict = lane[2] if len(lane) > 2 else {}

            src_id = idx_to_id[src_idx]
            dst_id = idx_to_id[dst_idx]
            bidir = bool(params.get("bidirectional", False))
            speed = float(params.get("speed_limit", 1.0))

            v_max_raw = params.get("v_max", None)
            v_max_val = float(v_max_raw) if v_max_raw is not None else None
            edge = Edge(
                edge_id=f"lane_{li:04d}",
                start_node_id=src_id,
                end_node_id=dst_id,
                bidirectional=bidir,
                max_speed=speed,
                width_m=float(params.get("width_m", params.get("lane_width", 1.5))),
                safety_model=params.get("safety_model", ""),
                corridor=params.get("corridor", ""),
                access_type=params.get("access_type", ""),
                v_max=v_max_val,
            )
            graph._add_edge(edge)

        return graph

    # ──────────────────────────────────────────────────
    # 기존 sample_fab.json 포맷 로더 (테스트 호환용)
    # ──────────────────────────────────────────────────
    @classmethod
    def from_json(cls, path: str) -> "MapGraph":
        with open(path) as f:
            data = json.load(f)
        graph = cls()
        for n in data.get("nodes", []):
            graph._add_node(Node(
                node_id=n["nodeId"],
                x=float(n["x"]),
                y=float(n["y"]),
                role=NodeRole(n.get("role", "standard")),
                lock_radius=n.get("lock_radius", 1),
                allowed_deviation_xy=float(n.get("allowed_deviation_xy", 0.0)),
            ))
        for e in data.get("edges", []):
            v_max_raw = e.get("vMax", e.get("v_max", None))
            v_max_val = float(v_max_raw) if v_max_raw is not None else None
            graph._add_edge(Edge(
                edge_id=e["edgeId"],
                start_node_id=e["startNodeId"],
                end_node_id=e["endNodeId"],
                capacity=e.get("capacity", 1),
                bidirectional=e.get("bidirectional", False),
                max_speed=float(e.get("maxSpeed", 1.0)),
                distance=float(e.get("distance", 0.0)),
                width_m=float(e.get("width_m", e.get("laneWidth", 1.5))),
                safety_model=e.get("safety_model", ""),
                v_max=v_max_val,
            ))
        return graph

    # ──────────────────────────────────────────────────
    # 내부 메서드
    # ──────────────────────────────────────────────────
    def _add_node(self, node: Node) -> None:
        self.nodes[node.node_id] = node
        self._out_edges.setdefault(node.node_id, [])

    def _add_edge(self, edge: Edge) -> None:
        if edge.distance == 0.0:
            edge.distance = self._calc_distance(edge.start_node_id, edge.end_node_id)
        self.edges[edge.edge_id] = edge
        self._out_edges.setdefault(edge.start_node_id, []).append(edge.edge_id)
        if edge.bidirectional:
            rev_id = f"{edge.edge_id}_rev"
            rev = Edge(
                edge_id=rev_id,
                start_node_id=edge.end_node_id,
                end_node_id=edge.start_node_id,
                capacity=edge.capacity,
                bidirectional=False,
                max_speed=edge.max_speed,
                distance=edge.distance,
                width_m=edge.width_m,
                safety_model=edge.safety_model,
                corridor=edge.corridor,
                access_type=edge.access_type,
                v_max=edge.v_max,
            )
            self.edges[rev_id] = rev
            self._out_edges.setdefault(edge.end_node_id, []).append(rev_id)

    def get_neighbors(self, node_id: str) -> list[Node]:
        return [
            self.nodes[self.edges[eid].end_node_id]
            for eid in self._out_edges.get(node_id, [])
            if self.edges[eid].end_node_id in self.nodes
        ]

    _ROLE_PENALTY: dict[NodeRole, float] = {
        NodeRole.STANDARD: 0.0,
        NodeRole.APPROACH: 0.0,
        NodeRole.SIDING:   0.5,
        NodeRole.WORK:     2.0,
        NodeRole.CHARGER:  3.0,
    }

    def get_path(
        self,
        start_id: str,
        end_id: str,
        blocked_edges: Optional[set[tuple[str, str]]] = None,
    ) -> list[str]:
        """
        A* 경로 탐색.
        blocked_edges: {(src, dst), ...} — 해당 엣지를 무한 비용으로 처리.
        그래프를 수정하지 않으므로 동시 호출 안전.
        """
        if start_id == end_id:
            return [start_id]
        if start_id not in self.nodes or end_id not in self.nodes:
            return []
        blocked = blocked_edges or set()
        open_heap: list[tuple[float, str]] = [(0.0, start_id)]
        came_from: dict[str, str] = {}
        g: dict[str, float] = {start_id: 0.0}
        while open_heap:
            _, current = heapq.heappop(open_heap)
            if current == end_id:
                path: list[str] = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.append(start_id)
                return list(reversed(path))
            for eid in self._out_edges.get(current, []):
                edge = self.edges[eid]
                nb = edge.end_node_id
                if nb not in self.nodes:
                    continue
                # blocked_edges: 해당 엣지 스킵
                if (current, nb) in blocked:
                    continue
                penalty = self._ROLE_PENALTY.get(self.nodes[nb].role, 0.0)
                tentative_g = g[current] + edge.distance + penalty
                if tentative_g < g.get(nb, float("inf")):
                    came_from[nb] = current
                    g[nb] = tentative_g
                    h = self._calc_distance(nb, end_id)
                    heapq.heappush(open_heap, (tentative_g + h, nb))
        return []

    def get_approach_node(self, target_node_id: str) -> Optional[Node]:
        for edge in self.edges.values():
            if edge.end_node_id == target_node_id:
                pred = self.nodes.get(edge.start_node_id)
                if pred and pred.role == NodeRole.APPROACH:
                    return pred
        return None

    # Open-RMF 전용 쿼리
    def get_chargers(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_charger]

    def get_holding_points(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_holding_point]

    def get_stations(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_parking_spot]

    def _calc_distance(self, a_id: str, b_id: str) -> float:
        a, b = self.nodes[a_id], self.nodes[b_id]
        return math.hypot(b.x - a.x, b.y - a.y)


# ──────────────────────────────────────────────────────
# 빠른 검증
# ──────────────────────────────────────────────────────
if __name__ == "__main__":
    g = MapGraph.from_rmf_yaml("/home/claude/fab_nav_graph.yaml")
    print(f"Nodes    : {len(g.nodes)}")
    print(f"Edges    : {len(g.edges)}")
    print(f"Chargers : {len(g.get_chargers())}")
    print(f"Stations : {len(g.get_stations())}")
    print(f"Holding  : {len(g.get_holding_points())}")

    # 경로 탐색 테스트: 충전소 → 스테이션
    chargers = g.get_chargers()
    stations = g.get_stations()
    if chargers and stations:
        c = chargers[0].node_id
        s = stations[5].node_id
        path = g.get_path(c, s)
        print(f"\nA* {c} → {s}")
        print(f"  경로 ({len(path)} 노드): {' → '.join(path[:6])} ...")
        print(f"  총 거리: {sum(g.edges[eid].distance for eid in g._out_edges.get(path[0],[])[:1]):.1f}m (첫 엣지)")

    # 단방향 검증: 중앙통로 역방향 불가
    wp_c_000 = "WP_C_000"
    wp_c_040 = "WP_C_040"
    fwd = g.get_path(wp_c_040, wp_c_000)  # 동→서 (정방향)
    bwd = g.get_path(wp_c_000, wp_c_040)  # 서→동 (역방향, 불가)
    print(f"\n중앙 단방향 검증:")
    print(f"  WP_C_040→WP_C_000 (정방향): {len(fwd)} 노드 {'OK' if fwd else 'FAIL'}")
    print(f"  WP_C_000→WP_C_040 (역방향): {bwd} {'OK(빈리스트)' if not bwd else 'FAIL(역방향이 뚫려있음)'}")

    # APPROACH 노드 확인
    approach_nodes = [n for n in g.nodes.values() if n.role == NodeRole.APPROACH]
    print(f"\nAPPROACH 노드 ({len(approach_nodes)}개): {[n.node_id for n in approach_nodes[:5]]} ...")
