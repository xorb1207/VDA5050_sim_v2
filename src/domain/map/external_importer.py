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
# fleets / demands 파서 (JSON, YAML 공용)
# ────────────────────────────────────────────────────────────────────
def _parse_fleets(raw_fleets) -> list[dict]:
    """fleets 섹션을 정규화된 dict 리스트로 변환.

    각 dict 의 필수 키: id, graph_idx. 나머지는 보존.
    """
    if not raw_fleets:
        return []
    out: list[dict] = []
    for raw in raw_fleets:
        if not isinstance(raw, dict):
            continue
        fid = raw.get("id")
        if fid is None:
            continue
        fleet: dict = {
            "id": str(fid),
            "graph_idx": int(raw.get("graph_idx", 0)),
            "capabilities": list(raw.get("capabilities", []) or []),
            "color": str(raw.get("color", "#0f9d58")),
            "count": int(raw.get("count", 1)),
            "max_speed_mps": float(raw.get("max_speed_mps", 1.5)),
            "priority": int(raw.get("priority", 1)),
        }
        out.append(fleet)
    return out


def _parse_demands(raw_demands) -> list[dict]:
    """demands 섹션을 정규화된 dict 리스트로 변환."""
    if not raw_demands:
        return []
    out: list[dict] = []
    for raw in raw_demands:
        if not isinstance(raw, dict):
            continue
        pickup = raw.get("pickup")
        dropoff = raw.get("dropoff")
        if pickup is None or dropoff is None:
            continue
        cap = raw.get("required_capability", None)
        out.append({
            "pickup": str(pickup),
            "dropoff": str(dropoff),
            "required_capability": (None if cap in (None, "", "null") else str(cap)),
        })
    return out


# ────────────────────────────────────────────────────────────────────
# 1단계: 구조 임포트
# ────────────────────────────────────────────────────────────────────
def _import_nodes(raw_nodes: list[dict]) -> list[ImportedNode]:
    nodes = []
    for raw in raw_nodes:
        pos = raw.get("position", {}) or {}
        cap_raw = raw.get("capability", None)
        capability = None if cap_raw in (None, "", "null") else str(cap_raw)
        nodes.append(ImportedNode(
            node_id=str(raw.get("id", "")),
            x=float(pos.get("x", 0.0)),
            y=float(pos.get("y", 0.0)),
            name=str(raw.get("name", "") or raw.get("id", "")),
            raw_node_type_cd=str(raw.get("node_type_cd", "") or ""),
            raw_align_type_cd=str(raw.get("align_type_cd", "") or ""),
            capability=capability,
        ))
    return nodes


# ────────────────────────────────────────────────────────────────────
# 2-a: 양방향 자동 병합
# ────────────────────────────────────────────────────────────────────
def _detect_bidirectional(raw_links: list[dict], cfg: InferenceConfig) -> list[ImportedEdge]:
    """(from,to) + (to,from) 짝 발견 시 1개 양방향 엣지로 병합.

    원본 link id 는 merged_from 에 보존. 병합되지 않은 단방향 링크는 그대로.
    """
    # 정규화: (from, to) → raw link
    by_pair: dict[tuple[str, str], dict] = {}
    for raw in raw_links:
        conn = raw.get("connected") or {}
        f, t = str(conn.get("from", "")), str(conn.get("to", ""))
        if not f or not t:
            continue
        # 중복 (같은 방향) 링크가 있으면 첫 번째만 살림 (importer 단계에선 경고만)
        if (f, t) not in by_pair:
            by_pair[(f, t)] = raw

    def _gidx(raw: dict) -> int:
        # F1a: 링크의 graph_idx (없으면 0 = 단일 그래프 기본)
        v = raw.get("graph_idx", 0)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    edges: list[ImportedEdge] = []
    consumed: set[tuple[str, str]] = set()
    for (f, t), raw in by_pair.items():
        if (f, t) in consumed:
            continue
        reverse = (t, f)
        if reverse in by_pair and reverse not in consumed:
            # 양방향
            rev_raw = by_pair[reverse]
            edges.append(ImportedEdge(
                edge_id=str(raw.get("id", "") or f"{f}__{t}"),
                src=f, dst=t,
                inferred_bidirectional=True,
                raw_link_type_cd=str(raw.get("link_type_cd", "") or ""),
                merged_from=[str(raw.get("id", "")), str(rev_raw.get("id", ""))],
                graph_idx=_gidx(raw),
            ))
            consumed.add((f, t))
            consumed.add(reverse)
        else:
            # 단방향
            edges.append(ImportedEdge(
                edge_id=str(raw.get("id", "") or f"{f}__{t}"),
                src=f, dst=t,
                inferred_bidirectional=False,
                raw_link_type_cd=str(raw.get("link_type_cd", "") or ""),
                merged_from=[str(raw.get("id", ""))],
                graph_idx=_gidx(raw),
            ))
            consumed.add((f, t))
    return edges


# ────────────────────────────────────────────────────────────────────
# 2-b: 차수 계산
# ────────────────────────────────────────────────────────────────────
def _compute_degrees(nodes: list[ImportedNode], edges: list[ImportedEdge]) -> None:
    by_id = {n.node_id: n for n in nodes}
    for e in edges:
        if e.src in by_id:
            by_id[e.src].degree_out += 1
            if e.inferred_bidirectional:
                by_id[e.src].degree_in += 1
        if e.dst in by_id:
            by_id[e.dst].degree_in += 1
            if e.inferred_bidirectional:
                by_id[e.dst].degree_out += 1


# ────────────────────────────────────────────────────────────────────
# 2-c: 코리도 추론
# ────────────────────────────────────────────────────────────────────
def _infer_corridors(
    nodes: list[ImportedNode],
    edges: list[ImportedEdge],
    cfg: InferenceConfig,
) -> None:
    """각 엣지의 corridor 라벨을 좌표 기반으로 추론.

    전략:
      1. 엣지의 방향(거의 수평/수직/대각선) 판정
      2. 수평 엣지: 두 노드의 y좌표 평균으로 band 클러스터링 → north/center/south
      3. 수직 엣지: x좌표 평균으로 band 클러스터링 → bay (vertical corridors)
      4. 짧은 access 엣지 (길이 < threshold): station_access / charger_access 후보
      5. 클러스터 크기가 min_corridor_size 미만이면 "misc" 처리
    """
    pos_by_id = {n.node_id: (n.x, n.y) for n in nodes}

    # 1차: 전체 엣지 길이 분포로 short_threshold 결정 (access 후보 컷오프)
    all_lengths = []
    for e in edges:
        if e.src in pos_by_id and e.dst in pos_by_id:
            x1, y1 = pos_by_id[e.src]
            x2, y2 = pos_by_id[e.dst]
            all_lengths.append(math.hypot(x2 - x1, y2 - y1))
    short_threshold = _percentile(all_lengths, cfg.short_edge_percentile) if all_lengths else 0.0

    horizontal_edges: list[tuple[ImportedEdge, float]] = []  # (edge, y_mid)
    vertical_edges: list[tuple[ImportedEdge, float]] = []    # (edge, x_mid)
    diagonal_edges: list[ImportedEdge] = []
    short_edges: list[ImportedEdge] = []                     # access 후보 (corridor 분류 제외)

    for e in edges:
        if e.src not in pos_by_id or e.dst not in pos_by_id:
            continue
        x1, y1 = pos_by_id[e.src]
        x2, y2 = pos_by_id[e.dst]
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        length = math.hypot(x2 - x1, y2 - y1)

        if length < 0.1:  # 거의 같은 위치 — 비정상
            diagonal_edges.append(e)
            continue
        # 짧은 엣지: 메인 코리도 클러스터링에서 제외하고 access 후보로
        # (예: station/charger 진입로처럼 메인 흐름의 곁가지)
        if length <= short_threshold:
            short_edges.append(e)
            continue
        if dy <= cfg.corridor_y_tolerance and dx > dy * 2:
            horizontal_edges.append((e, (y1 + y2) / 2))
        elif dx <= cfg.corridor_x_tolerance and dy > dx * 2:
            vertical_edges.append((e, (x1 + x2) / 2))
        else:
            diagonal_edges.append(e)

    # 수평 클러스터링 → y band 별로 north/center/south 등 자동 라벨
    h_clusters = _cluster_1d(horizontal_edges, cfg.corridor_y_tolerance)
    # y_up 좌표계: 큰 y 가 north (위쪽). y_down: 작은 y 가 north.
    reverse = (cfg.y_axis == "y_up")
    h_clusters_sorted = sorted(h_clusters, key=lambda c: c["band_center"], reverse=reverse)
    h_names = _assign_horizontal_names(h_clusters_sorted, cfg.horizontal_corridor_names)

    for cluster, name in zip(h_clusters_sorted, h_names):
        if len(cluster["edges"]) < cfg.min_corridor_size:
            label = "misc"
        else:
            label = name
        for e in cluster["edges"]:
            e.inferred_corridor = label

    # 수직 클러스터링 → bay (각 x 위치별로 bay_<index> 라벨)
    v_clusters = _cluster_1d(vertical_edges, cfg.corridor_x_tolerance)
    v_clusters_sorted = sorted(v_clusters, key=lambda c: c["band_center"])
    for i, cluster in enumerate(v_clusters_sorted):
        if len(cluster["edges"]) < cfg.min_corridor_size:
            label = "misc"
        else:
            label = "bay"  # 우리 엔진 컨벤션에 맞춰 모두 'bay' 로 (구분 필요 시 bay_0, bay_1 ...)
        for e in cluster["edges"]:
            e.inferred_corridor = label

    # 대각선 (수평도 수직도 아닌) 엣지: 일단 misc — 사용자가 검토 UI 에서 분류
    for e in diagonal_edges:
        e.inferred_corridor = "misc"

    # 짧은 엣지: access 후보 (corridor='' + access_type 은 role 추론 단계에서 확정)
    for e in short_edges:
        e.inferred_corridor = ""
        e.inferred_access_type = "access"  # role 추론 후 station_access/charger_access 로 세분화


def _cluster_1d(items: list[tuple], tolerance: float) -> list[dict]:
    """items = [(edge, value), ...] 를 value 기준으로 tolerance 이내 그룹핑.

    반환: [{"band_center": float, "edges": [Edge, ...]}, ...]
    """
    if not items:
        return []
    sorted_items = sorted(items, key=lambda x: x[1])
    clusters: list[dict] = []
    current = {"band_center": sorted_items[0][1], "edges": [sorted_items[0][0]], "values": [sorted_items[0][1]]}
    for edge, value in sorted_items[1:]:
        if value - current["values"][-1] <= tolerance:
            current["edges"].append(edge)
            current["values"].append(value)
            current["band_center"] = sum(current["values"]) / len(current["values"])
        else:
            clusters.append(current)
            current = {"band_center": value, "edges": [edge], "values": [value]}
    clusters.append(current)
    return clusters


def _assign_horizontal_names(clusters: list[dict], name_pool: tuple[str, ...]) -> list[str]:
    """3개 풀(north/center/south) 을 클러스터 개수에 맞춰 분배.

    클러스터 수가 풀보다 적으면 가운데부터 채움 (1개면 center, 2개면 north+south, 3개면 다, 4개+면 nothN/north/center/south/southS).
    """
    n = len(clusters)
    if n == 0:
        return []
    if n == 1:
        return ["center"]
    if n == 2:
        return ["north", "south"]
    if n == 3:
        return list(name_pool)
    # 4개 이상: 양 끝부터 north/south 라벨 + 가운데는 center_0, center_1, ...
    names = ["north"]
    middle_count = n - 2
    for i in range(middle_count):
        if middle_count == 1:
            names.append("center")
        else:
            names.append(f"center_{i}")
    names.append("south")
    return names


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_v[int(k)]
    return sorted_v[f] * (c - k) + sorted_v[c] * (k - f)


# ────────────────────────────────────────────────────────────────────
# 2-d: role 추론
# ────────────────────────────────────────────────────────────────────
def _infer_roles(
    nodes: list[ImportedNode],
    edges: list[ImportedEdge],
    cfg: InferenceConfig,
    raw_nodes: list[dict],
) -> None:
    """각 노드의 role 후보를 마킹.

    규칙:
      - hint 코드 (node_type_cd) 가 명시적이면 우선 (charger/station 등 자주 쓰는 값 인식)
      - 차수가 매우 낮은 terminal 노드 → charger / holding 후보
      - degree_in == degree_out == 1 이고 라인 끝쪽 → standard waypoint
      - 위 어디에도 안 맞으면 standard
    """
    by_id = {n.node_id: n for n in nodes}

    # hint code 1차 적용 (있다면)
    if cfg.use_code_hints:
        for n in nodes:
            cd = (n.raw_node_type_cd or "").upper()
            if not cd:
                continue
            # 자주 쓸 법한 코드 별칭들 — 실제 데이터 보면 사용자가 매핑 yaml 로 override
            if cd in ("CH", "CHARGER", "CHRG"):
                n.inferred_role = "charger"
                n.inferred_is_charger = True
            elif cd in ("ST", "STN", "STATION", "WORK"):
                n.inferred_role = "station"
            elif cd in ("HP", "HOLD", "HOLDING", "IDLE"):
                n.inferred_role = "holding"
                n.inferred_is_holding = True
            elif cd in ("SD", "SIDING"):
                n.inferred_role = "siding"
            # 그 외는 standard 유지

    # 좌표/차수 기반 추론 (hint 가 없는 노드 만 보완)
    for n in nodes:
        if n.inferred_role != "standard":
            continue
        # terminal: degree (양방향 고려) 가 매우 낮음
        total_degree = n.degree_in + n.degree_out
        if total_degree <= cfg.terminal_degree_threshold:
            # 끝점 — charger 또는 holding 후보. 어느 쪽인지 좌표만으로 단정 불가 →
            # 일단 "holding" 후보로 마킹하고, 사용자가 검토 UI 에서 변경하게 함.
            n.inferred_role = "holding_candidate"
            n.inferred_is_holding = True


# ────────────────────────────────────────────────────────────────────
# 3단계: 검증 리포트
# ────────────────────────────────────────────────────────────────────
def _build_report(
    nodes: list[ImportedNode],
    edges: list[ImportedEdge],
    raw_edges_count: int,
) -> ImportReport:
    report = ImportReport(
        node_count=len(nodes),
        edge_count_raw=raw_edges_count,
        edge_count_after_merge=len(edges),
        bidirectional_count=sum(1 for e in edges if e.inferred_bidirectional),
        inferred_chargers=sum(1 for n in nodes if n.inferred_is_charger),
        inferred_stations=sum(1 for n in nodes if n.inferred_role == "station"),
        inferred_holding=sum(1 for n in nodes if n.inferred_is_holding),
    )

    # corridor 통계
    c = Counter(e.inferred_corridor or "(none)" for e in edges)
    report.corridor_stats = dict(c)

    # 고립 / dead-end 분석
    by_id = {n.node_id: n for n in nodes}
    for n in nodes:
        if n.degree_in == 0 and n.degree_out == 0:
            report.isolated_nodes.append(n.node_id)
        elif n.degree_in > 0 and n.degree_out == 0 and not n.inferred_is_charger:
            # 들어오기만 하고 나갈 수 없음 (one-way trap) — charger 가 아니면 경고
            report.dead_end_nodes.append(n.node_id)

    # connected components (양방향은 양방향, 단방향은 weak connect 로 카운트)
    report.connected_components = _count_weak_components(nodes, edges)

    # 경고 생성
    if report.isolated_nodes:
        report.warnings.append(ImportWarning(
            severity="error",
            code="isolated_nodes",
            message=f"{len(report.isolated_nodes)} 개 노드가 어떤 링크에도 연결되지 않음",
            nodes=report.isolated_nodes[:20],
        ))
    if report.dead_end_nodes:
        report.warnings.append(ImportWarning(
            severity="warn",
            code="dead_end_nodes",
            message=f"{len(report.dead_end_nodes)} 개 노드가 dead-end (들어오기만 가능)",
            nodes=report.dead_end_nodes[:20],
        ))
    if report.connected_components > 1:
        report.warnings.append(ImportWarning(
            severity="warn",
            code="multiple_components",
            message=f"그래프가 {report.connected_components} 개 단편으로 나뉨 (AGV 도달 불가 구간 존재)",
        ))
    if report.inferred_chargers == 0:
        report.warnings.append(ImportWarning(
            severity="error",
            code="no_chargers",
            message="charger 로 추론된 노드가 없음. 검토 UI 에서 charger 지정 필요",
        ))
    misc_corridor = report.corridor_stats.get("misc", 0) + report.corridor_stats.get("(none)", 0)
    if misc_corridor > 0:
        report.warnings.append(ImportWarning(
            severity="info",
            code="unlabeled_corridor",
            message=f"{misc_corridor} 개 엣지 corridor 추론 실패 (access/짧은 엣지일 수 있음)",
        ))

    return report


def _count_weak_components(nodes: list[ImportedNode], edges: list[ImportedEdge]) -> int:
    """weakly connected components (단방향도 양방향처럼 본 후 분리 카운트)."""
    parent: dict[str, str] = {n.node_id: n.node_id for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        if e.src in parent and e.dst in parent:
            union(e.src, e.dst)
    roots = set(find(n.node_id) for n in nodes)
    return len(roots)


# ────────────────────────────────────────────────────────────────────
# YAML 임포트 헬퍼
# ────────────────────────────────────────────────────────────────────
def _import_from_yaml_level(
    level_data: dict,
) -> tuple[list[ImportedNode], list[ImportedEdge], int]:
    """RMF YAML 레벨 데이터 → nodes/edges.

    vertices: [[x, y, "name", {is_charger, is_holding_point, ...}], ...]
    lanes: [[src_idx, dst_idx, {bidirectional, graph_idx, speed_limit, ...}], ...]

    반환: (nodes, edges, raw_edges_count)
    """
    nodes: list[ImportedNode] = []
    edges: list[ImportedEdge] = []

    raw_vertices = level_data.get("vertices", [])
    raw_lanes = level_data.get("lanes", [])

    # 1. vertices → nodes
    idx_to_id: dict[int, str] = {}
    for i, vertex in enumerate(raw_vertices):
        if not vertex or len(vertex) < 2:
            continue
        x = float(vertex[0])
        y = float(vertex[1])
        # 3번째 요소가 name (또는 params dict)
        name = None
        params: dict = {}

        if len(vertex) > 2:
            third = vertex[2]
            if isinstance(third, str):
                name = third
            elif isinstance(third, dict):
                params = third
                name = params.get("name")

        if len(vertex) > 3 and isinstance(vertex[3], dict):
            params.update(vertex[3])

        node_id = name or f"node_{i:04d}"
        node = ImportedNode(
            node_id=node_id,
            x=x,
            y=y,
            name=node_id,
            inferred_is_charger=bool(params.get("is_charger", False)),
            inferred_is_holding=bool(params.get("is_holding_point", False)),
        )
        # YAML 에 role 힌트가 있으면 적용
        if "rmf_role" in params:
            node.inferred_role = params["rmf_role"]
        nodes.append(node)
        idx_to_id[i] = node_id

    # 2. lanes → edges
    for li, lane in enumerate(raw_lanes):
        if not lane or len(lane) < 2:
            continue
        src_idx = int(lane[0])
        dst_idx = int(lane[1])
        params: dict = lane[2] if len(lane) > 2 else {}

        if src_idx not in idx_to_id or dst_idx not in idx_to_id:
            continue

        src_id = idx_to_id[src_idx]
        dst_id = idx_to_id[dst_idx]
        bidir = bool(params.get("bidirectional", False))

        # graph_idx 추출 (F1a 호환)
        graph_idx = None
        if "graph_idx" in params:
            graph_idx = int(params["graph_idx"])

        edge = ImportedEdge(
            edge_id=f"lane_{li:04d}",
            src=src_id,
            dst=dst_id,
            inferred_bidirectional=bidir,
            v_max=float(params["v_max"]) if "v_max" in params and params["v_max"] is not None else None,
            graph_idx=graph_idx,
        )

        edges.append(edge)

    return nodes, edges, len(raw_lanes)


def _infer_roles_for_yaml(
    nodes: list[ImportedNode],
    edges: list[ImportedEdge],
    cfg: InferenceConfig,
) -> None:
    """YAML 에서 온 노드의 role 추론 (기존 값을 해치지 않음)."""
    by_id = {n.node_id: n for n in nodes}

    # is_charger 플래그가 있으면 role=charger (JSON 처럼)
    for n in nodes:
        if n.inferred_is_charger and n.inferred_role == "standard":
            n.inferred_role = "charger"
        elif n.inferred_is_holding and n.inferred_role == "standard":
            n.inferred_role = "holding"

    # 차수 기반 추론 (low degree → holding candidate)
    for n in nodes:
        if n.inferred_role != "standard":
            continue
        total_degree = n.degree_in + n.degree_out
        if total_degree <= cfg.terminal_degree_threshold:
            n.inferred_role = "holding_candidate"
            n.inferred_is_holding = True


# ────────────────────────────────────────────────────────────────────
# Edit.json 적용 (Editor 페이지에서 Save 한 결과)
# ────────────────────────────────────────────────────────────────────
def apply_edits(imported: ImportedMap, edits: dict | str | Path) -> ImportedMap:
    """ImportedMap + edit.json → 편집 적용된 ImportedMap.

    edits 포맷: editor_html.exportEdits() 결과 (format_version=1)
      · deleted_node_ids / deleted_edge_ids
      · added_nodes [{id, x, y, role, is_charger, is_holding}]
      · added_edges [{id, src, dst, bidir}]
      · node_overrides {id: {role?, is_charger?, is_holding?}}
      · edge_overrides {id: {bidir?, src?, dst?}}

    원본 ImportedMap 은 mutate 하지 않고 새 ImportedMap 반환.
    """
    if isinstance(edits, (str, Path)):
        edits = json.loads(Path(edits).read_text(encoding="utf-8"))

    # 깊은 복사 (필드 단위)
    nodes = []
    for n in imported.nodes:
        nn = ImportedNode(
            node_id=n.node_id, x=n.x, y=n.y, name=n.name,
            inferred_role=n.inferred_role,
            inferred_is_charger=n.inferred_is_charger,
            inferred_is_holding=n.inferred_is_holding,
            raw_node_type_cd=n.raw_node_type_cd,
            raw_align_type_cd=n.raw_align_type_cd,
            degree_in=n.degree_in, degree_out=n.degree_out,
            capability=n.capability,
        )
        nodes.append(nn)
    edges = []
    for e in imported.edges:
        ee = ImportedEdge(
            edge_id=e.edge_id, src=e.src, dst=e.dst,
            inferred_bidirectional=e.inferred_bidirectional,
            inferred_corridor=e.inferred_corridor,
            inferred_access_type=e.inferred_access_type,
            raw_link_type_cd=e.raw_link_type_cd,
            merged_from=list(e.merged_from),
            v_max=e.v_max,
            graph_idx=e.graph_idx,
        )
        edges.append(ee)

    # 1. 삭제
    deleted_nodes = set(edits.get("deleted_node_ids", []))
    deleted_edges = set(edits.get("deleted_edge_ids", []))
    nodes = [n for n in nodes if n.node_id not in deleted_nodes]
    edges = [e for e in edges if e.edge_id not in deleted_edges]
    # 삭제된 노드와 연결된 엣지도 같이 정리 (방어적)
    nodes_set = {n.node_id for n in nodes}
    edges = [e for e in edges if e.src in nodes_set and e.dst in nodes_set]

    # 2. node_overrides
    node_overrides = edits.get("node_overrides", {})
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
            cap_ov = ov["capability"]
            n.capability = None if cap_ov in (None, "", "null") else str(cap_ov)

    # 3. edge_overrides
    edge_overrides = edits.get("edge_overrides", {})
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
            e.v_max = (None if ov["v_max"] is None else float(ov["v_max"]))

    # 4. added_nodes
    for an in edits.get("added_nodes", []):
        cap_raw = an.get("capability", None)
        nodes.append(ImportedNode(
            node_id=an["id"], x=float(an["x"]), y=float(an["y"]),
            name=an.get("name", an["id"]),
            inferred_role=an.get("role", "standard"),
            inferred_is_charger=bool(an.get("is_charger", False)),
            inferred_is_holding=bool(an.get("is_holding", False)),
            capability=(None if cap_raw in (None, "", "null") else str(cap_raw)),
        ))

    # 5. added_edges
    for ae in edits.get("added_edges", []):
        v_max_raw = ae.get("v_max", None)
        graph_idx_raw = ae.get("graph_idx", None)
        edges.append(ImportedEdge(
            edge_id=ae["id"], src=ae["src"], dst=ae["dst"],
            inferred_bidirectional=bool(ae.get("bidir", False)),
            v_max=(None if v_max_raw is None else float(v_max_raw)),
            graph_idx=(None if graph_idx_raw is None else int(graph_idx_raw)),
        ))

    # 차수 + 리포트 재계산
    _compute_degrees(nodes, edges)
    raw_edges_count = len(imported.edges) - len(deleted_edges) + len(edits.get("added_edges", []))
    report = _build_report(nodes, edges, raw_edges_count)

    # 배경 이미지: edit에서 제공되면 그것을, 아니면 원본 유지
    background_image = edits.get("background_image") or imported.background_image

    # F1a: fleets / demands — edit 에서 명시 override 되지 않았으면 원본 보존
    fleets = _parse_fleets(edits.get("fleets")) if "fleets" in edits else list(imported.fleets)
    demands = _parse_demands(edits.get("demands")) if "demands" in edits else list(imported.demands)

    return ImportedMap(nodes=nodes, edges=edges, report=report, config=imported.config,
                      background_image=background_image,
                      fleets=fleets, demands=demands)


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
