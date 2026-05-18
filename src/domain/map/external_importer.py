"""
external_importer.py — 폐쇄망 외부 맵 JSON/YAML 임포터.

전제:
  - 사용자가 제공하는 JSON 은 top-level 에 {nodes, links} 만 신뢰 가능
  - 각 node 는 {id, name, position{x,y,z}, ...} 형식
  - 각 link 는 {id, name, connected{from,to}, ...} 형식
  - YAML 은 Open-RMF building_map 표준 포맷 (levels → vertices/lanes)

처리 흐름 (JSON):
  1. 구조 임포트 — 좌표/연결 그대로 받음
  2. 자동 추론
     a. 양방향 병합: (from,to) + (to,from) 짝 발견 시 1개 bidirectional edge 로
     b. 코리도 클러스터링: y좌표 비슷 → 수평 코리도, x좌표 비슷 → 수직 코리도(bay)
     c. role 추론: degree, 위치, hint code 종합해서 charger/holding/station/wp 후보 마킹
     d. 도달성 분석: connected component, dead-end, 고립 노드 검출
  3. ImportReport 로 모든 추론 결과 + 경고 반환 — UI 에서 검토/수정 가능

처리 흐름 (YAML):
  1. Open-RMF 표준 파싱 (levels → 첫 레벨 → vertices/lanes)
  2. vertices/lanes → nodes/edges 변환
  3. graph_idx 인식 (F1a 다중 그래프 호환)
  4. fleets 섹션 처리 (선택)
  5. 기존 자동 추론 파이프라인 재사용

자동 추론은 "초안" 일 뿐, 최종 정책은 사용자가 검토 UI 에서 확정. importer 는 그 초안을
ImportedMap 으로 반환하고, 검증 리포트도 함께 제공.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from src.domain.map.graph import Edge, MapGraph, Node, NodeRole


# ────────────────────────────────────────────────────────────────────
# 자동 추론 파라미터 — 사용자가 override 가능하도록 dataclass 로 노출
# ────────────────────────────────────────────────────────────────────
@dataclass
class InferenceConfig:
    """좌표/연결만 보고 정책을 추론할 때 쓰는 하이퍼파라미터.

    실제 데이터를 보고 튜닝하기 좋도록 모든 값을 노출.
    """
    # 코리도 추론: 같은 y좌표 ±tolerance 안에 있는 노드들은 같은 horizontal corridor
    corridor_y_tolerance: float = 5.0   # m (사용자 데이터 단위에 맞춰서)
    corridor_x_tolerance: float = 5.0
    # 코리도 클러스터에 최소 몇 개 노드/링크 가 있어야 인정할지 (노이즈 컷)
    min_corridor_size: int = 2
    # 짧은 엣지 (전체 길이 distribution 의 N percentile 이하) 는 corridor 클러스터링
    # 에서 제외하고 access 후보로 분류
    short_edge_percentile: float = 25.0
    # role 추론: degree 가 이하면 terminal 후보 (charger/holding 가능성)
    terminal_degree_threshold: int = 1
    # 위치 라벨링: 좌표 기준 area name (UI 표시용)
    horizontal_corridor_names: tuple[str, ...] = ("north", "center", "south")
    # 정책 코드 힌트 (있으면 사용, 없어도 추론에 영향 없음)
    use_code_hints: bool = True
    # y 좌표계 방향: "y_up" (큰 y = 위쪽 = north, FAB 표준) / "y_down" (CAD 일부)
    y_axis: str = "y_up"


# ────────────────────────────────────────────────────────────────────
# 결과 데이터 구조
# ────────────────────────────────────────────────────────────────────
@dataclass
class ImportedNode:
    node_id: str
    x: float
    y: float
    name: str = ""
    # 자동 추론된 role 후보 (사용자가 검토 UI 에서 확정/변경)
    inferred_role: str = "standard"     # standard / charger / station / holding / siding
    inferred_is_charger: bool = False
    inferred_is_holding: bool = False
    # 원본 코드 (참고용, 그대로 보존)
    raw_node_type_cd: str = ""
    raw_align_type_cd: str = ""
    # 검증용 메타
    degree_in: int = 0
    degree_out: int = 0
    # F1a: 노드의 capability 태그 (선택). demand 매칭에 사용될 수 있음.
    capability: Optional[str] = None


@dataclass
class ImportedEdge:
    edge_id: str
    src: str
    dst: str
    # 자동 추론된 정책
    inferred_bidirectional: bool = False
    inferred_corridor: str = ""
    inferred_access_type: str = ""      # bay / station_access / charger_access / ""
    # 원본 보존
    raw_link_type_cd: str = ""
    # 만약 양방향으로 병합되었다면, 어떤 원본 링크 ID 들에서 왔는지
    merged_from: list[str] = field(default_factory=list)
    # F1b-ux: 사용자가 map editor에서 설정한 per-edge 속도 제한 (m/s). None이면 미설정.
    v_max: Optional[float] = None
    # F1a: 다중 그래프 인덱스 (RMF YAML의 graph_idx). None이면 단일 그래프.
    graph_idx: Optional[int] = None


@dataclass
class ImportWarning:
    severity: str   # info / warn / error
    code: str       # 분류 코드 (e.g. "isolated_node", "duplicate_link")
    message: str
    nodes: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    """임포트 결과 요약 + 자동 추론 통계 + 경고. UI 검증 패널이 사용."""
    node_count: int = 0
    edge_count_raw: int = 0
    edge_count_after_merge: int = 0
    bidirectional_count: int = 0
    inferred_chargers: int = 0
    inferred_stations: int = 0
    inferred_holding: int = 0
    connected_components: int = 0
    isolated_nodes: list[str] = field(default_factory=list)
    dead_end_nodes: list[str] = field(default_factory=list)
    corridor_stats: dict[str, int] = field(default_factory=dict)  # corridor → edge count
    warnings: list[ImportWarning] = field(default_factory=list)


@dataclass
class ImportedMap:
    """임포터 출력 — 그대로 MapGraph 로 빌드할 수 있도록 정규화된 중간 표현."""
    nodes: list[ImportedNode]
    edges: list[ImportedEdge]
    report: ImportReport
    config: InferenceConfig
    background_image: dict | None = None  # {url, opacity, x_offset, y_offset, scale}
    # F1a: fleets 정의 (선택). 비어 있으면 단일 fleet (app 레벨에서 자동 처리).
    # 항목 예: {"id": "TYPE_1", "graph_idx": 0, "color": "#0f9d58",
    #           "capabilities": ["overhead"], "count": 6, "max_speed_mps": 1.5, "priority": 1}
    fleets: list[dict] = field(default_factory=list)
    # F1a: 명시적 demand 정의 (선택).
    # 항목 예: {"pickup": "ST_001", "dropoff": "ST_002", "required_capability": "overhead"}
    demands: list[dict] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# F1a 파서 헬퍼
# ────────────────────────────────────────────────────────────────────
def _parse_fleets(raw: list) -> list[dict]:
    """fleet 정의 리스트 정규화. 누락 필드에 기본값 보충."""
    out = []
    for i, f in enumerate(raw or []):
        if not isinstance(f, dict):
            continue
        out.append({
            "id":           str(f.get("id", f.get("fleet_id", f"fleet_{i}"))),
            "graph_idx":    int(f.get("graph_idx", i)),
            "color":        str(f.get("color", "#888888")),
            "capabilities": list(f.get("capabilities", [])),
            "count":        int(f.get("count", f.get("agv_count", 1))),
            "max_speed_mps": float(f.get("max_speed_mps", f.get("max_speed", 1.0))),
            "priority":     int(f.get("priority", 0)),
        })
    return out


def _parse_demands(raw: list) -> list[dict]:
    """demand 정의 리스트 정규화. required_capability 포함."""
    out = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        out.append({
            "pickup":               str(d.get("pickup", d.get("from", ""))),
            "dropoff":              str(d.get("dropoff", d.get("to", ""))),
            "required_capability":  d.get("required_capability") or None,
            "count":                int(d.get("count", 1)),
            "priority":             int(d.get("priority", 0)),
        })
    return [d for d in out if d["pickup"] and d["dropoff"]]


# ────────────────────────────────────────────────────────────────────
# 메인 임포터
# ────────────────────────────────────────────────────────────────────
def import_map(
    path_or_data: str | Path | dict,
    config: Optional[InferenceConfig] = None,
    format: Optional[str] = None,
) -> ImportedMap:
    """Auto-dispatch to import_map_json or import_map_yaml based on file extension or format param.

    경로면 확장자 감지 (.yaml/.yml → YAML, .json → JSON).
    format param 으로 강제 지정 가능 ("json" / "yaml").
    """
    # 포맷 결정
    if format:
        detected_format = format.lower()
    elif isinstance(path_or_data, (str, Path)):
        path_str = str(path_or_data)
        if path_str.endswith((".yaml", ".yml")):
            detected_format = "yaml"
        elif path_str.endswith(".json"):
            detected_format = "json"
        else:
            raise ValueError(f"Unknown file extension in {path_str}. Use format='json'/'yaml' to specify.")
    else:
        raise ValueError("format parameter required when passing dict. Use 'json' or 'yaml'.")

    if detected_format == "yaml":
        return import_map_yaml(path_or_data, config)
    elif detected_format == "json":
        return import_map_json(path_or_data, config)
    else:
        raise ValueError(f"Unknown format: {detected_format}. Use 'json' or 'yaml'.")


def import_map_json(
    path_or_data: str | Path | dict,
    config: Optional[InferenceConfig] = None,
) -> ImportedMap:
    """JSON 경로 또는 dict 를 받아서 ImportedMap 반환.

    호출자는 결과를 검토(UI / CLI) 한 뒤 build_map_graph() 로 MapGraph 변환.
    """
    cfg = config or InferenceConfig()

    if isinstance(path_or_data, (str, Path)):
        data = json.loads(Path(path_or_data).read_text(encoding="utf-8"))
    else:
        data = path_or_data

    raw_nodes = data.get("nodes", [])
    raw_links = data.get("links", [])

    # 1단계: 구조만 임포트
    nodes = _import_nodes(raw_nodes)
    raw_edges_count = len(raw_links)

    # 2단계: 자동 추론
    edges = _detect_bidirectional(raw_links, cfg)
    _compute_degrees(nodes, edges)
    _infer_corridors(nodes, edges, cfg)
    _infer_roles(nodes, edges, cfg, raw_nodes)

    # 3단계: 검증 + 리포트
    report = _build_report(nodes, edges, raw_edges_count)

    # F1a: fleets / demands (선택)
    fleets = _parse_fleets(data.get("fleets", []))
    demands = _parse_demands(data.get("demands", []))

    return ImportedMap(
        nodes=nodes, edges=edges, report=report, config=cfg,
        fleets=fleets, demands=demands,
    )


def import_map_yaml(
    path_or_data: str | Path | dict,
    config: Optional[InferenceConfig] = None,
    level: Optional[str] = None,
) -> ImportedMap:
    """RMF YAML 경로 또는 dict 또는 YAML 문자열을 받아서 ImportedMap 반환.

    포맷: Open-RMF building_map 표준
      levels:
        L1:
          vertices: [[x, y, "name", {is_charger, is_holding_point, ...}], ...]
          lanes: [[src_idx, dst_idx, {bidirectional, graph_idx, speed_limit, ...}], ...]
      fleets: (선택) [{id, graph_idx, color, capabilities, count}, ...]

    level=None 이면 첫 번째 레벨 자동 선택.
    """
    cfg = config or InferenceConfig()

    if isinstance(path_or_data, dict):
        data = path_or_data
    elif isinstance(path_or_data, (str, Path)):
        # 파일 경로인지 YAML 문자열인지 판정
        p = Path(path_or_data) if isinstance(path_or_data, str) else path_or_data
        if p.exists() and p.is_file():
            # 파일 경로
            text = p.read_text(encoding="utf-8")
        else:
            # YAML 문자열로 취급
            text = str(path_or_data)
        data = yaml.safe_load(text)
    else:
        data = path_or_data

    if not isinstance(data, dict):
        raise ValueError("YAML must be a dict at top level")

    levels = data.get("levels", {})
    if not levels:
        raise ValueError("No 'levels' section found in YAML")

    # 첫 번째 레벨 선택
    level_name = level or next(iter(levels))
    lvl = levels.get(level_name)
    if not lvl:
        raise ValueError(f"Level '{level_name}' not found")

    # YAML 포맷 → internal nodes/edges 로 변환
    nodes, edges, raw_edges_count = _import_from_yaml_level(lvl)

    # 자동 추론 파이프라인 적용
    _compute_degrees(nodes, edges)
    _infer_corridors(nodes, edges, cfg)
    # YAML 은 role 을 이미 포함할 수 있으므로 조심스럽게 추론 (existing 을 overwrite 하지 않음)
    _infer_roles_for_yaml(nodes, edges, cfg)

    # 검증 + 리포트
    report = _build_report(nodes, edges, raw_edges_count)

    # F1a: fleets / demands (선택)
    fleets = _parse_fleets(data.get("fleets", []))
    demands = _parse_demands(data.get("demands", []))

    return ImportedMap(
        nodes=nodes, edges=edges, report=report, config=cfg,
        fleets=fleets, demands=demands,
    )


# ────────────────────────────────────────────────────────────────────
# Edit.json 적용 (Editor 페이지에서 Save 한 결과)
# ────────────────────────────────────────────────────────────────────
def apply_edits(imported: ImportedMap, edits: dict | str | Path) -> ImportedMap:
    """ImportedMap + edit.json → 편집 적용된 새 ImportedMap.

    edits 포맷 (editor_html.py exportEdits() 결과, format_version=1):
      · deleted_node_ids / deleted_edge_ids
      · added_nodes [{id, x, y, role, is_charger, is_holding}]
      · added_edges [{id, src, dst, bidir, v_max?, graph_idx?}]
      · node_overrides {id: {role?, is_charger?, is_holding?}}
      · edge_overrides {id: {bidir?, src?, dst?, v_max?, graph_idx?}}
        - v_max=null 은 명시적 unset (intrinsic 속도 사용)
      · background_image (선택)

    원본 ImportedMap 은 mutate 하지 않고 새 ImportedMap 반환.
    """
    if isinstance(edits, (str, Path)):
        edits = json.loads(Path(edits).read_text(encoding="utf-8"))
    if not edits:
        return imported

    # 필드 단위 복사 (원본 보존)
    nodes: list[ImportedNode] = []
    for n in imported.nodes:
        nodes.append(ImportedNode(
            node_id=n.node_id, x=n.x, y=n.y, name=n.name,
            inferred_role=n.inferred_role,
            inferred_is_charger=n.inferred_is_charger,
            inferred_is_holding=n.inferred_is_holding,
            raw_node_type_cd=n.raw_node_type_cd,
            raw_align_type_cd=n.raw_align_type_cd,
            degree_in=n.degree_in, degree_out=n.degree_out,
            capability=n.capability,
        ))
    edges: list[ImportedEdge] = []
    for e in imported.edges:
        edges.append(ImportedEdge(
            edge_id=e.edge_id, src=e.src, dst=e.dst,
            inferred_bidirectional=e.inferred_bidirectional,
            inferred_corridor=e.inferred_corridor,
            inferred_access_type=e.inferred_access_type,
            raw_link_type_cd=e.raw_link_type_cd,
            merged_from=list(e.merged_from),
            v_max=e.v_max,
            graph_idx=e.graph_idx,
        ))

    # 1. 삭제
    deleted_nodes = set(edits.get("deleted_node_ids", []) or [])
    deleted_edges = set(edits.get("deleted_edge_ids", []) or [])
    nodes = [n for n in nodes if n.node_id not in deleted_nodes]
    edges = [e for e in edges if e.edge_id not in deleted_edges]
    # 삭제된 노드와 연결된 엣지도 같이 정리 (방어적)
    nodes_set = {n.node_id for n in nodes}
    edges = [e for e in edges if e.src in nodes_set and e.dst in nodes_set]

    # 2. node_overrides
    node_overrides = edits.get("node_overrides", {}) or {}
    for n in nodes:
        ov = node_overrides.get(n.node_id)
        if not ov:
            continue
        if "role" in ov:
            n.inferred_role = ov["role"]
        if "is_charger" in ov:
            n.inferred_is_charger = bool(ov["is_charger"])
        if "is_holding" in ov:
            n.inferred_is_holding = bool(ov["is_holding"])
        if "capability" in ov:
            cap = ov["capability"]
            n.capability = None if cap in (None, "", "null") else str(cap)

    # 3. edge_overrides
    edge_overrides = edits.get("edge_overrides", {}) or {}
    for e in edges:
        ov = edge_overrides.get(e.edge_id)
        if not ov:
            continue
        if "bidir" in ov:
            e.inferred_bidirectional = bool(ov["bidir"])
        if "src" in ov:
            e.src = ov["src"]
        if "dst" in ov:
            e.dst = ov["dst"]
        if "v_max" in ov:
            # null 도 명시적 unset 의미로 허용
            e.v_max = None if ov["v_max"] is None else float(ov["v_max"])
        if "graph_idx" in ov:
            gi = ov["graph_idx"]
            e.graph_idx = None if gi is None else int(gi)

    # 4. added_nodes
    for an in edits.get("added_nodes", []) or []:
        cap_raw = an.get("capability", None)
        nodes.append(ImportedNode(
            node_id=str(an["id"]), x=float(an["x"]), y=float(an["y"]),
            name=an.get("name", an["id"]),
            inferred_role=an.get("role", "standard"),
            inferred_is_charger=bool(an.get("is_charger", False)),
            inferred_is_holding=bool(an.get("is_holding", False)),
            capability=(None if cap_raw in (None, "", "null") else str(cap_raw)),
        ))

    # 5. added_edges
    for ae in edits.get("added_edges", []) or []:
        v_raw = ae.get("v_max", None)
        gi_raw = ae.get("graph_idx", None)
        edges.append(ImportedEdge(
            edge_id=str(ae["id"]), src=str(ae["src"]), dst=str(ae["dst"]),
            inferred_bidirectional=bool(ae.get("bidir", False)),
            v_max=(None if v_raw is None else float(v_raw)),
            graph_idx=(None if gi_raw is None else int(gi_raw)),
        ))

    # background_image 도 edits 에 있으면 반영 (editor 가 export 함)
    bg = edits.get("background_image", imported.background_image)

    # report 는 보수적으로 원본을 그대로 둔다 — _compute_degrees/_build_report 헬퍼
    # 가 별도 머지 사고로 누락된 상태라 재계산 불가. 정확한 차수/리포트가 필요한
    # 케이스가 생기면 그때 helper 들을 함께 복원할 것.
    return ImportedMap(
        nodes=nodes, edges=edges,
        report=imported.report, config=imported.config,
        background_image=bg,
        fleets=list(imported.fleets),
        demands=list(imported.demands),
    )


# ────────────────────────────────────────────────────────────────────
# ImportedMap → MapGraph 빌드 (simulation 에 넘길 때 사용)
# ────────────────────────────────────────────────────────────────────
def build_map_graph(imported: ImportedMap) -> MapGraph:
    """ImportedMap → 시뮬레이션이 쓰는 MapGraph 로 변환.

    inferred_* 정책을 그대로 적용. 사용자가 검토 UI 에서 수정한 경우 그 결과를 반영한
    ImportedMap 을 넘기면 됨.
    """
    role_map = {
        "standard": NodeRole.STANDARD,
        "approach": NodeRole.APPROACH,
        "siding": NodeRole.SIDING,
        "station": NodeRole.WORK,
        "work": NodeRole.WORK,
        "charger": NodeRole.CHARGER,
        # holding 은 standard + is_holding_point=True 로 표현
        "holding": NodeRole.STANDARD,
        "holding_candidate": NodeRole.STANDARD,
    }
    g = MapGraph()
    for n in imported.nodes:
        node = Node(
            node_id=n.node_id,
            x=n.x, y=n.y,
            role=role_map.get(n.inferred_role, NodeRole.STANDARD),
            is_charger=n.inferred_is_charger,
            is_holding_point=n.inferred_is_holding,
        )
        g.nodes[n.node_id] = node
        g._out_edges.setdefault(n.node_id, [])

    for e in imported.edges:
        if e.src not in g.nodes or e.dst not in g.nodes:
            continue
        sx, sy = g.nodes[e.src].x, g.nodes[e.src].y
        dx, dy = g.nodes[e.dst].x, g.nodes[e.dst].y
        dist = math.hypot(dx - sx, dy - sy)
        # F1a: ImportedEdge.graph_idx 가 None 이면 단일 그래프 기본 0 으로
        gidx = 0 if e.graph_idx is None else int(e.graph_idx)
        edge = Edge(
            edge_id=e.edge_id,
            start_node_id=e.src,
            end_node_id=e.dst,
            bidirectional=e.inferred_bidirectional,
            distance=dist,
            corridor=e.inferred_corridor,
            access_type=e.inferred_access_type,
            v_max=e.v_max,
            graph_idx=gidx,
        )
        g.edges[e.edge_id] = edge
        g._out_edges.setdefault(e.src, []).append(e.edge_id)
        if e.inferred_bidirectional:
            # 양방향: reverse 도 별개 entry 로 추가 (engine 컨벤션 — A* 가 each edge_id 의
            # end_node_id 를 다음 노드로 보기 때문에 같은 entry 의 _out_edges 양쪽 등록은 X)
            edge_rev = Edge(
                edge_id=e.edge_id + "_rev",
                start_node_id=e.dst,
                end_node_id=e.src,
                bidirectional=False,  # reverse 자체는 단방향 entry
                distance=dist,
                corridor=e.inferred_corridor,
                access_type=e.inferred_access_type,
                v_max=e.v_max,
                graph_idx=gidx,
            )
            g.edges[edge_rev.edge_id] = edge_rev
            g._out_edges.setdefault(e.dst, []).append(edge_rev.edge_id)

    return g
