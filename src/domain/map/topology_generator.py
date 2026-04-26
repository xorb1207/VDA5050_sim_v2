"""
MapTopologyGenerator v2 — Topology Invariants 보장

변경사항:
  - _add_corridor() 방향 버그 수정 (west_to_east 엣지 pairs 오류)
  - validate_invariants() 추가 — A/C: head-on 엣지 없음, D: same-lane head-on 없음
  - Type E: lane_mode 태그를 그래프 메타데이터에 저장
  - _add_bays() 순환 완성 보장 (끝단 베이 처리)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.domain.map.graph import Edge, MapGraph, Node, NodeRole

# ── FAB 물리 상수 ──────────────────────────────────────────────
FAB_WIDTH_M   = 640
FAB_HEIGHT_M  = 120
FAB_OUTER_OFFSET_M = 3.0
Y_NORTH  = 100
Y_CENTER = 60
Y_SOUTH  = 20
BAY_X    = [0, 160, 320, 480, 640]
HOLDING_X = [80, 240, 400, 560]
WP_STEP  = 40
WP_X     = list(range(0, FAB_WIDTH_M + 1, WP_STEP))
STATION_X  = list(range(0, FAB_WIDTH_M + 1, 80))
CHARGER_X  = [0, 320, 640]

SPEED_MAIN_MS    = 1.5
SPEED_BAY_MS     = 0.7
SPEED_STATION_MS = 0.5
SPEED_CHARGER_MS = 0.3
SPEED_CREEP_MS   = 0.3
LANE_OFFSET = 2
ACCESS_LANE_STEP_M = 1.5
FACILITY_OFFSET_M = 3.0
WIDTH_SINGLE_LANE_M = 1.5
WIDTH_NARROW_CORRIDOR_M = 2.0
WIDTH_WIDE_CORRIDOR_M = 3.0
WIDTH_NARROW_LANE_M = WIDTH_NARROW_CORRIDOR_M / 2.0
WIDTH_WIDE_LANE_M = WIDTH_WIDE_CORRIDOR_M / 2.0
WIDTH_BAY_M = 1.5
WIDTH_ACCESS_M = 1.2

TopologyType = Literal["A", "B", "C", "D", "E"]

# ── Type B 사이딩 배치 프리셋 ──────────────────────────────────
# base:  bay 위치만 (5개, 160m 간격)
# mid:   80m 간격 (9개)
# dense: 40m 간격 = WP 전체 (17개)
SIDING_POSITIONS: dict[str, list[int]] = {
    "base":  BAY_X,                                 # [0, 160, 320, 480, 640]
    "mid":   list(range(0, FAB_WIDTH_M + 1, 80)),   # [0, 80, 160, ..., 640]
    "dense": WP_X,                                  # [0, 40, 80, ..., 640]
}


# ── Invariant 검증 결과 ────────────────────────────────────────
@dataclass
class InvariantResult:
    passed: bool
    violations: list[str]

    def __str__(self) -> str:
        if self.passed:
            return "OK"
        return f"FAIL ({len(self.violations)} violations): " + "; ".join(self.violations[:3])


class MapTopologyGenerator:

    @staticmethod
    def _validate_siding_placement(placement: str) -> str:
        if placement not in SIDING_POSITIONS:
            valid = ", ".join(sorted(SIDING_POSITIONS))
            raise ValueError(
                f"Unknown siding placement: {placement!r}. Valid values: {valid}"
            )
        return placement

    def generate(
        self,
        type_code: TopologyType,
        siding_placement: str = "base",
    ) -> MapGraph:
        if type_code == "B":
            siding_placement = self._validate_siding_placement(siding_placement)
            g = self._build_type_b(siding_placement=siding_placement)
        else:
            builders = {
                "A": self._build_type_a,
                "C": self._build_type_c,
                "D": self._build_type_d,
                "E": self._build_type_e,
            }
            if type_code not in builders:
                raise ValueError(f"Unknown topology type: {type_code}")
            g = builders[type_code]()
        g._topology_type = type_code  # 메타데이터 태그
        g._siding_placement = siding_placement  # 사이딩 배치 프리셋 태그
        return g

    # ── Type A ────────────────────────────────────────────────
    def _build_type_a(self) -> MapGraph:
        g = MapGraph()
        self._add_corridor(g, Y_NORTH, "N", bidirectional=False,
                           direction="east_to_west", corridor="north")
        self._add_corridor(g, Y_CENTER, "C", bidirectional=False,
                           direction="east_to_west", corridor="center",
                           role=NodeRole.APPROACH)
        self._add_corridor(g, Y_SOUTH, "S", bidirectional=False,
                           direction="west_to_east", corridor="south")
        self._add_bays(g)
        self._add_stations(g)
        self._add_chargers(g)
        self._add_holding_points(g)
        return g

    # ── Type B ────────────────────────────────────────────────
    def _build_type_b(self, siding_placement: str = "base") -> MapGraph:
        g = MapGraph()
        self._add_corridor(g, Y_NORTH,  "N", bidirectional=True, corridor="north")
        self._add_corridor(g, Y_CENTER, "C", bidirectional=True, corridor="center",
                           role=NodeRole.APPROACH)
        self._add_corridor(g, Y_SOUTH,  "S", bidirectional=True, corridor="south")
        self._add_sidings(g, placement=siding_placement)
        self._add_bays(g)
        self._add_stations(g)
        self._add_chargers(g)
        self._add_holding_points(g)
        return g

    # ── Type C: 2차선 단방향 ───────────────────────────────────
    def _build_type_c(self) -> MapGraph:
        g = MapGraph()
        g._lane_width_m = WIDTH_NARROW_LANE_M
        g._corridor_total_width_m = WIDTH_NARROW_CORRIDOR_M
        g._safety_model = "narrow_one_way"
        for y_base, tag in [(Y_NORTH, "N"), (Y_CENTER, "C"), (Y_SOUTH, "S")]:
            corr = "center" if tag == "C" else ("north" if tag == "N" else "south")
            role = NodeRole.APPROACH if tag == "C" else NodeRole.STANDARD
            # L1: 동→서, L2: 서→동 — 완전 분리 단방향
            self._add_corridor(g, y_base + LANE_OFFSET, f"{tag}L1",
                               bidirectional=False, direction="east_to_west",
                               corridor=f"{corr}_l1", role=role,
                               width_m=WIDTH_NARROW_LANE_M,
                               safety_model="narrow_one_way")
            self._add_corridor(g, y_base - LANE_OFFSET, f"{tag}L2",
                               bidirectional=False, direction="west_to_east",
                               corridor=f"{corr}_l2", role=role,
                               width_m=WIDTH_NARROW_LANE_M,
                               safety_model="narrow_one_way")
            self._add_uturn(g, y_base, tag)
        self._add_bays(g, two_lane=True)
        self._add_stations(g, two_lane=True)
        self._add_chargers(g, two_lane=True)
        self._add_holding_points(g, two_lane=True)
        return g

    # ── Type D: 2차선 양방향 (L1: 동→서, L2: 서→동 완전 분리) ──
    def _build_type_d(self) -> MapGraph:
        g = MapGraph()
        g._lane_width_m = WIDTH_WIDE_LANE_M
        g._corridor_total_width_m = WIDTH_WIDE_CORRIDOR_M
        g._safety_model = "wide_one_way"
        for y_base, tag in [(Y_NORTH, "N"), (Y_CENTER, "C"), (Y_SOUTH, "S")]:
            corr = "center" if tag == "C" else ("north" if tag == "N" else "south")
            role = NodeRole.APPROACH if tag == "C" else NodeRole.STANDARD
            # L1: 동→서 단방향
            self._add_corridor(g, y_base + LANE_OFFSET, f"{tag}L1",
                               bidirectional=False, direction="east_to_west",
                               corridor=f"{corr}_l1", role=role,
                               width_m=WIDTH_WIDE_LANE_M,
                               safety_model="wide_one_way")
            # L2: 서→동 단방향
            self._add_corridor(g, y_base - LANE_OFFSET, f"{tag}L2",
                               bidirectional=False, direction="west_to_east",
                               corridor=f"{corr}_l2", role=role,
                               width_m=WIDTH_WIDE_LANE_M,
                               safety_model="wide_one_way")
            self._add_uturn(g, y_base, tag)
        self._add_bays(g, two_lane=True)
        self._add_stations(g, two_lane=True)
        self._add_chargers(g, two_lane=True)
        self._add_holding_points(g, two_lane=True)
        return g

    # ── Type E: 1차선 양방향 크리프 ───────────────────────────
    def _build_type_e(self) -> MapGraph:
        g = MapGraph()
        g._lane_mode = "bidirectional_creep"  # policy 주입용 태그
        self._add_corridor(g, Y_NORTH,  "N", bidirectional=True,
                           corridor="north",  speed=SPEED_CREEP_MS)
        self._add_corridor(g, Y_CENTER, "C", bidirectional=True,
                           corridor="center", speed=SPEED_CREEP_MS,
                           role=NodeRole.APPROACH)
        self._add_corridor(g, Y_SOUTH,  "S", bidirectional=True,
                           corridor="south",  speed=SPEED_CREEP_MS)
        self._add_bays(g)
        self._add_stations(g)
        self._add_chargers(g)
        self._add_holding_points(g)
        return g

    # ── Invariant 검증 ─────────────────────────────────────────
    def validate_invariants(self, g: MapGraph, type_code: str) -> InvariantResult:
        """
        타입별 불변조건 검증.
        A/C: 모든 메인통로 엣지가 단방향 → same-corridor head-on 엣지 쌍 없어야 함
        D:   같은 lane 내 head-on 없어야 함 (L1끼리, L2끼리)
        E:   _lane_mode == "bidirectional_creep" 태그 존재해야 함
        """
        violations: list[str] = []

        if type_code in ("A", "C"):
            # 메인통로에서 역방향 엣지 쌍 탐지
            main_corridors = {"north", "south", "center",
                              "north_l1", "north_l2", "south_l1", "south_l2",
                              "center_l1", "center_l2"}
            main_edges = {
                e.edge_id: e for e in g.edges.values()
                if e.corridor in main_corridors
            }
            for eid, e in main_edges.items():
                reverse_key = f"{e.end_node_id}__{e.start_node_id}"
                # 역방향 엣지가 존재하면 불변조건 위반
                for other in main_edges.values():
                    if (other.start_node_id == e.end_node_id and
                            other.end_node_id == e.start_node_id and
                            other.corridor == e.corridor):
                        violations.append(
                            f"Type {type_code} head-on pair: "
                            f"{e.start_node_id}↔{e.end_node_id} "
                            f"corridor={e.corridor}"
                        )
                        break

        elif type_code == "D":
            # D: L1끼리, L2끼리 same-lane head-on 없어야 함
            # (L1↔L2 cross-lane은 허용 — 실제로는 물리적으로 다른 차선)
            for lane_tag in ("L1", "L2"):
                lane_edges = [
                    e for e in g.edges.values()
                    if lane_tag.lower() in e.corridor
                ]
                for e in lane_edges:
                    for other in lane_edges:
                        if (other.start_node_id == e.end_node_id and
                                other.end_node_id == e.start_node_id and
                                other.corridor == e.corridor):
                            violations.append(
                                f"Type D same-lane head-on: "
                                f"{e.start_node_id}↔{e.end_node_id} "
                                f"lane={lane_tag} corridor={e.corridor}"
                            )
                            break

        elif type_code == "E":
            if not getattr(g, "_lane_mode", None) == "bidirectional_creep":
                violations.append("Type E: _lane_mode tag missing — creep policy won't inject")

        return InvariantResult(passed=len(violations) == 0, violations=violations)

    # ── 공통: 메인 통로 ────────────────────────────────────────
    def _add_corridor(
        self,
        g: MapGraph,
        y: float,
        tag: str,
        bidirectional: bool,
        corridor: str,
        direction: str = "",
        role: NodeRole = NodeRole.STANDARD,
        speed: float = SPEED_MAIN_MS,
        width_m: float = WIDTH_SINGLE_LANE_M,
        safety_model: str = "",
    ) -> None:
        node_ids: list[str] = []
        for x in WP_X:
            nid = f"WP_{tag}_{x:03d}"
            g._add_node(Node(node_id=nid, x=float(x), y=float(y), role=role))
            node_ids.append(nid)

        # ✅ 버그 수정: 방향별 pairs를 명확히 분기
        if bidirectional:
            pairs = list(zip(node_ids, node_ids[1:]))
        elif direction == "west_to_east":
            # 서→동: 인덱스 오름차순 (x 증가 방향)
            pairs = list(zip(node_ids, node_ids[1:]))
        elif direction == "east_to_west":
            # 동→서: 인덱스 내림차순 (x 감소 방향)
            pairs = list(zip(reversed(node_ids), list(reversed(node_ids))[1:]))
        else:
            pairs = list(zip(node_ids, node_ids[1:]))

        eid_base = len(g.edges)
        for i, (src, dst) in enumerate(pairs):
            e = Edge(
                edge_id=f"e_{tag}_{eid_base+i:05d}",
                start_node_id=src,
                end_node_id=dst,
                bidirectional=bidirectional,
                max_speed=speed,
                width_m=width_m,
                safety_model=safety_model,
                corridor=corridor,
            )
            g._add_edge(e)

    # ── 공통: U턴 ──────────────────────────────────────────────
    def _add_uturn(self, g: MapGraph, y_base: float, tag: str) -> None:
        for x in [0, FAB_WIDTH_M]:
            u_nid = f"UT_{tag}_{x:03d}"
            g._add_node(Node(node_id=u_nid, x=float(x), y=float(y_base),
                             role=NodeRole.STANDARD))
            l1 = f"WP_{tag}L1_{x:03d}"
            l2 = f"WP_{tag}L2_{x:03d}"
            if l1 in g.nodes and l2 in g.nodes:
                g._add_edge(Edge(edge_id=f"e_ut_{tag}_{x:03d}_a",
                                 start_node_id=l1, end_node_id=u_nid,
                                 bidirectional=True, max_speed=SPEED_MAIN_MS))
                g._add_edge(Edge(edge_id=f"e_ut_{tag}_{x:03d}_b",
                                 start_node_id=u_nid, end_node_id=l2,
                                 bidirectional=True, max_speed=SPEED_MAIN_MS))

    # ── 공통: 베이 (교대 단방향) ───────────────────────────────
    def _add_bays(self, g: MapGraph, two_lane: bool = False) -> None:
        for i, bx in enumerate(BAY_X):
            north_to_south = (i % 2 == 0)

            if two_lane:
                # 2차선: 방향에 맞는 차선 선택
                # 북→남: L1(동→서)이 북쪽에 있으므로 NL1 사용
                # 남→북: L2(서→동)이 남쪽에서 올라오므로 NL2 사용
                n_tag = "NL1" if north_to_south else "NL2"
                c_tag = "CL1" if north_to_south else "CL2"
                s_tag = "SL1" if north_to_south else "SL2"
            else:
                n_tag, c_tag, s_tag = "N", "C", "S"

            n_nid = f"WP_{n_tag}_{bx:03d}"
            c_nid = f"WP_{c_tag}_{bx:03d}"
            s_nid = f"WP_{s_tag}_{bx:03d}"

            if n_nid not in g.nodes or c_nid not in g.nodes or s_nid not in g.nodes:
                continue

            dir_label = "NS" if north_to_south else "SN"
            if north_to_south:
                self._add_bay_path(g, [n_nid, c_nid, s_nid], bx, dir_label)
            else:
                self._add_bay_path(g, [s_nid, c_nid, n_nid], bx, dir_label)

    def _add_bay_path(
        self,
        g: MapGraph,
        endpoints: list[str],
        bay_x: int,
        dir_label: str,
    ) -> None:
        path = [endpoints[0]]
        for seg_idx, (src_id, dst_id) in enumerate(zip(endpoints, endpoints[1:]), start=1):
            src = g.nodes[src_id]
            dst = g.nodes[dst_id]
            y_step = 10 if dst.y > src.y else -10
            intermediate_y = list(range(int(src.y + y_step), int(dst.y), y_step))
            for hop_idx, y in enumerate(intermediate_y, start=1):
                nid = f"BAY_{dir_label}_{bay_x:03d}_{seg_idx}_{hop_idx}"
                g._add_node(Node(
                    node_id=nid,
                    x=float(bay_x),
                    y=float(y),
                    role=NodeRole.STANDARD,
                ))
                path.append(nid)
            path.append(dst_id)

        for idx, (src_id, dst_id) in enumerate(zip(path, path[1:]), start=1):
            g._add_edge(Edge(
                edge_id=f"e_bay_{dir_label}_{bay_x:03d}_{idx:02d}",
                start_node_id=src_id,
                end_node_id=dst_id,
                bidirectional=False,
                max_speed=SPEED_BAY_MS,
                corridor="bay",
                width_m=WIDTH_BAY_M,
            ))

    # ── 공통: 스테이션 ─────────────────────────────────────────
    def _add_stations(self, g: MapGraph, two_lane: bool = False) -> None:
        n_tag = "NL1" if two_lane else "N"
        c_tag = "CL1" if two_lane else "C"
        s_tag = "SL1" if two_lane else "S"
        sid = 1
        for x in STATION_X:
            wp = f"WP_{n_tag}_{x:03d}"
            if wp not in g.nodes:
                continue
            nid = f"ST_N_{sid:02d}"
            self._add_access_lane(
                g,
                source_wp_ids=[wp] + ([f"WP_NL2_{x:03d}"] if two_lane else []),
                access_node_id=f"SA_N_{sid:02d}",
                facility_node_id=nid,
                access_x=float(x),
                facility_x=float(x),
                access_y=Y_NORTH - ACCESS_LANE_STEP_M,
                facility_y=Y_NORTH - FACILITY_OFFSET_M,
                access_type="station_access",
                facility_role=NodeRole.WORK,
                facility_flags={"is_parking_spot": True},
                speed=SPEED_STATION_MS,
            )
            sid += 1
        for bx in BAY_X:
            wp = f"WP_{c_tag}_{bx:03d}"
            if wp not in g.nodes:
                continue
            nid = f"ST_C_{sid:02d}"
            self._add_access_lane(
                g,
                source_wp_ids=[wp] + ([f"WP_CL2_{bx:03d}"] if two_lane else []),
                access_node_id=f"SA_C_{sid:02d}",
                facility_node_id=nid,
                access_x=float(bx),
                facility_x=float(bx),
                access_y=Y_CENTER + ACCESS_LANE_STEP_M,
                facility_y=Y_CENTER + FACILITY_OFFSET_M,
                access_type="station_access",
                facility_role=NodeRole.WORK,
                facility_flags={"is_parking_spot": True},
                speed=SPEED_STATION_MS,
            )
            sid += 1
        for x in STATION_X:
            wp = f"WP_{s_tag}_{x:03d}"
            if wp not in g.nodes:
                continue
            nid = f"ST_S_{sid:02d}"
            self._add_access_lane(
                g,
                source_wp_ids=[wp] + ([f"WP_SL2_{x:03d}"] if two_lane else []),
                access_node_id=f"SA_S_{sid:02d}",
                facility_node_id=nid,
                access_x=float(x),
                facility_x=float(x),
                access_y=Y_SOUTH + ACCESS_LANE_STEP_M,
                facility_y=Y_SOUTH + FACILITY_OFFSET_M,
                access_type="station_access",
                facility_role=NodeRole.WORK,
                facility_flags={"is_parking_spot": True},
                speed=SPEED_STATION_MS,
            )
            sid += 1

    # ── 공통: 충전소 ───────────────────────────────────────────
    def _add_chargers(self, g: MapGraph, two_lane: bool = False) -> None:
        n_tag = "NL1" if two_lane else "N"
        c_tag = "CL1" if two_lane else "C"
        s_tag = "SL1" if two_lane else "S"
        charger_specs = [
            ("CH_01", CHARGER_X[0], Y_NORTH,  n_tag),
            ("CH_02", CHARGER_X[1], Y_NORTH,  n_tag),
            ("CH_03", CHARGER_X[2], Y_NORTH,  n_tag),
            ("CH_04", CHARGER_X[0], Y_CENTER, c_tag),
            ("CH_05", CHARGER_X[2], Y_CENTER, c_tag),
            ("CH_06", CHARGER_X[0], Y_SOUTH,  s_tag),
            ("CH_07", CHARGER_X[1], Y_SOUTH,  s_tag),
            ("CH_08", CHARGER_X[2], Y_SOUTH,  s_tag),
        ]
        for cid, x, y, tag in charger_specs:
            wp = f"WP_{tag}_{x:03d}"
            if wp not in g.nodes:
                continue
            if y == Y_NORTH:
                access_y = y + ACCESS_LANE_STEP_M
                facility_y = y + FACILITY_OFFSET_M
            elif y == Y_SOUTH:
                access_y = y - ACCESS_LANE_STEP_M
                facility_y = y - FACILITY_OFFSET_M
            else:
                edge_x = 0.0 if x <= FAB_WIDTH_M / 2.0 else float(FAB_WIDTH_M)
                access_x = edge_x + ACCESS_LANE_STEP_M if edge_x > 0.0 else -ACCESS_LANE_STEP_M
                facility_x = edge_x + FAB_OUTER_OFFSET_M if edge_x > 0.0 else -FAB_OUTER_OFFSET_M
                l2_tag = tag.replace("L1", "L2") if two_lane else None
                self._add_access_lane(
                    g,
                    source_wp_ids=[wp] + ([f"WP_{l2_tag}_{x:03d}"] if l2_tag else []),
                    access_node_id=f"CA_{cid.split('_')[-1]}",
                    facility_node_id=cid,
                    access_x=float(access_x),
                    facility_x=float(facility_x),
                    access_y=float(y),
                    facility_y=float(y),
                    access_type="charger_access",
                    facility_role=NodeRole.CHARGER,
                    facility_flags={"is_charger": True, "is_holding_point": True},
                    speed=SPEED_CHARGER_MS,
                )
                continue
            l2_tag = tag.replace("L1", "L2") if two_lane else None
            self._add_access_lane(
                g,
                source_wp_ids=[wp] + ([f"WP_{l2_tag}_{x:03d}"] if l2_tag else []),
                access_node_id=f"CA_{cid.split('_')[-1]}",
                facility_node_id=cid,
                access_x=float(x),
                facility_x=float(x),
                access_y=access_y,
                facility_y=facility_y,
                access_type="charger_access",
                facility_role=NodeRole.CHARGER,
                facility_flags={"is_charger": True, "is_holding_point": True},
                speed=SPEED_CHARGER_MS,
            )

    def _add_holding_points(self, g: MapGraph, two_lane: bool = False) -> None:
        n_tag = "NL1" if two_lane else "N"
        c_tag = "CL1" if two_lane else "C"
        s_tag = "SL1" if two_lane else "S"
        hid = 1
        for x in HOLDING_X:
            # north corridor: fab 바깥 상단
            wp_n = f"WP_{n_tag}_{x:03d}"
            if wp_n in g.nodes:
                self._add_access_lane(
                    g,
                    source_wp_ids=[wp_n] + ([f"WP_NL2_{x:03d}"] if two_lane else []),
                    access_node_id=f"HA_N_{hid:02d}",
                    facility_node_id=f"HP_N_{hid:02d}",
                    access_x=float(x),
                    facility_x=float(x),
                    access_y=Y_NORTH + ACCESS_LANE_STEP_M,
                    facility_y=Y_NORTH + FACILITY_OFFSET_M,
                    access_type="holding_access",
                    facility_role=NodeRole.STANDARD,
                    facility_flags={"is_holding_point": True},
                    speed=SPEED_STATION_MS,
                )
            # center corridor: bay 축과 분리되게 상단 측면
            wp_c = f"WP_{c_tag}_{x:03d}"
            if wp_c in g.nodes:
                self._add_access_lane(
                    g,
                    source_wp_ids=[wp_c] + ([f"WP_CL2_{x:03d}"] if two_lane else []),
                    access_node_id=f"HA_C_{hid:02d}",
                    facility_node_id=f"HP_C_{hid:02d}",
                    access_x=float(x),
                    facility_x=float(x),
                    access_y=Y_CENTER + ACCESS_LANE_STEP_M,
                    facility_y=Y_CENTER + FACILITY_OFFSET_M,
                    access_type="holding_access",
                    facility_role=NodeRole.STANDARD,
                    facility_flags={"is_holding_point": True},
                    speed=SPEED_STATION_MS,
                )
            # south corridor: fab 바깥 하단
            wp_s = f"WP_{s_tag}_{x:03d}"
            if wp_s in g.nodes:
                self._add_access_lane(
                    g,
                    source_wp_ids=[wp_s] + ([f"WP_SL2_{x:03d}"] if two_lane else []),
                    access_node_id=f"HA_S_{hid:02d}",
                    facility_node_id=f"HP_S_{hid:02d}",
                    access_x=float(x),
                    facility_x=float(x),
                    access_y=Y_SOUTH - ACCESS_LANE_STEP_M,
                    facility_y=Y_SOUTH - FACILITY_OFFSET_M,
                    access_type="holding_access",
                    facility_role=NodeRole.STANDARD,
                    facility_flags={"is_holding_point": True},
                    speed=SPEED_STATION_MS,
                )
            hid += 1

    def _add_access_lane(
        self,
        g: MapGraph,
        source_wp_ids: list[str],
        access_node_id: str,
        facility_node_id: str,
        access_x: float,
        facility_x: float,
        access_y: float,
        facility_y: float,
        access_type: str,
        facility_role: NodeRole,
        facility_flags: dict[str, bool],
        speed: float,
    ) -> None:
        g._add_node(Node(
            node_id=access_node_id,
            x=float(access_x),
            y=float(access_y),
            role=NodeRole.APPROACH,
        ))
        g._add_node(Node(
            node_id=facility_node_id,
            x=float(facility_x),
            y=float(facility_y),
            role=facility_role,
            is_parking_spot=facility_flags.get("is_parking_spot", False),
            is_charger=facility_flags.get("is_charger", False),
            is_holding_point=facility_flags.get("is_holding_point", False),
        ))

        unique_sources = []
        for wp_id in source_wp_ids:
            if wp_id in g.nodes and wp_id not in unique_sources:
                unique_sources.append(wp_id)
        for src_idx, wp_id in enumerate(unique_sources, start=1):
            g._add_edge(Edge(
                edge_id=f"e_{access_type}_{facility_node_id.lower()}_{src_idx}",
                start_node_id=wp_id,
                end_node_id=access_node_id,
                bidirectional=True,
                max_speed=speed,
                width_m=WIDTH_ACCESS_M,
                access_type=access_type,
            ))
        g._add_edge(Edge(
            edge_id=f"e_{access_type}_{facility_node_id.lower()}_final",
            start_node_id=access_node_id,
            end_node_id=facility_node_id,
            bidirectional=True,
            max_speed=speed,
            width_m=WIDTH_ACCESS_M,
            access_type=access_type,
        ))

    # ── Type B 전용: siding ────────────────────────────────────
    def _add_sidings(self, g: MapGraph, placement: str = "base") -> None:
        """
        placement:
          "base"  — bay 위치만 (5개 x 축)
          "mid"   — 80m 간격 (9개 x 축)
          "dense" — 40m 간격, WP 전체 (17개 x 축)
        각 x 위치마다 N/C/S 3개 코리도에 siding 추가.
        siding 노드는 WP 노드 옆 오프셋 위치에 배치하고 bidirectional 엣지로 연결.
        """
        x_positions = SIDING_POSITIONS[self._validate_siding_placement(placement)]
        sid = 1
        for bx in x_positions:
            for y, tag in [(Y_NORTH, "N"), (Y_CENTER, "C"), (Y_SOUTH, "S")]:
                # siding 물리 위치: WP 기준 ±20m 오프셋 (FAB 경계 처리)
                if bx == 0:
                    sx = bx + 20
                elif bx == FAB_WIDTH_M:
                    sx = bx - 20
                else:
                    sx = bx - 20  # 왼쪽 오프셋 기본
                nid = f"SD_{tag}_{bx:03d}_{sid:02d}"
                g._add_node(Node(node_id=nid, x=float(sx), y=float(y),
                                 role=NodeRole.SIDING))
                # WP 연결: bx 위치의 WP 노드에 직접 연결 (snap to WP grid)
                wp_x = max(0, min(FAB_WIDTH_M, (bx // WP_STEP) * WP_STEP))
                wp_nid = f"WP_{tag}_{int(wp_x):03d}"
                if wp_nid in g.nodes:
                    g._add_edge(Edge(
                        edge_id=f"e_sd_{tag}_{bx:03d}_{sid:02d}",
                        start_node_id=wp_nid, end_node_id=nid,
                        bidirectional=True, max_speed=SPEED_STATION_MS,
                        corridor="siding",
                    ))
                sid += 1
