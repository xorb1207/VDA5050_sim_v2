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
        # 파일 경로인지 YAML 문자열인지 판정 — 줄바꿈/콜론 포함이거나
        # 너무 길어서 path 검사 자체가 실패할 케이스는 YAML 본문으로 간주
        s = str(path_or_data)
        is_path_like = (
            isinstance(path_or_data, Path)
            or ("\n" not in s and len(s) < 1024)
        )
        text: Optional[str] = None
        if is_path_like:
            try:
                p = Path(path_or_data)
                if p.exists() and p.is_file():
                    text = p.read_text(encoding="utf-8")
            except OSError:
                text = None
        if text is None:
            text = s
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
# 1단계: 구조 임포트
# ────────────────────────────────────────────────────────────────────
def _import_nodes(raw_nodes: list[dict]) -> list[ImportedNode]:
    nodes = []
    for raw in raw_nodes:
        pos = raw.get("position", {}) or {}
        nodes.append(ImportedNode(
            node_id=str(raw.get("id", "")),
            x=float(pos.get("x", 0.0)),
            y=float(pos.get("y", 0.0)),
            name=str(raw.get("name", "") or raw.get("id", "")),
            raw_node_type_cd=str(raw.get("node_type_cd", "") or ""),
            raw_align_type_cd=str(raw.get("align_type_cd", "") or ""),
        ))
    return nodes


# ────────────────────────────────────────────────────────────────────
# 2-a: 양방향 자동 병합
# ────────────────────────────────────────────────────────────────────
def _detect_bidirectional(raw_links: list[dict], cfg: InferenceConfig) -> list[ImportedEdge]:
    """(from,to) + (to,from) 짝 발견 시 1개 양방향 엣지로 병합.

    원본 link id 는 merged_from 에 보존. 병합되지 않은 단방향 링크는 그대로.
    """
    by_pair: dict[tuple[str, str], dict] = {}
    for raw in raw_links:
        conn = raw.get("connected") or {}
        f, t = str(conn.get("from", "")), str(conn.get("to", ""))
        if not f or not t:
            continue
        if (f, t) not in by_pair:
            by_pair[(f, t)] = raw

    def _gidx(raw: dict) -> Optional[int]:
        # F1a: link 의 graph_idx 인식. _truth_graph_idx (synthetic 데이터) 도 fallback.
        for key in ("graph_idx", "_truth_graph_idx"):
            if key in raw:
                try:
                    return int(raw[key])
                except (TypeError, ValueError):
                    return None
        return None

    edges: list[ImportedEdge] = []
    consumed: set[tuple[str, str]] = set()
    for (f, t), raw in by_pair.items():
        if (f, t) in consumed:
            continue
        reverse = (t, f)
        if reverse in by_pair and reverse not in consumed:
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
    """각 엣지의 corridor 라벨을 좌표 기반으로 추론."""
    pos_by_id = {n.node_id: (n.x, n.y) for n in nodes}

    all_lengths = []
    for e in edges:
        if e.src in pos_by_id and e.dst in pos_by_id:
            x1, y1 = pos_by_id[e.src]
            x2, y2 = pos_by_id[e.dst]
            all_lengths.append(math.hypot(x2 - x1, y2 - y1))
    short_threshold = _percentile(all_lengths, cfg.short_edge_percentile) if all_lengths else 0.0

    horizontal_edges: list[tuple[ImportedEdge, float]] = []
    vertical_edges: list[tuple[ImportedEdge, float]] = []
    diagonal_edges: list[ImportedEdge] = []
    short_edges: list[ImportedEdge] = []

    for e in edges:
        if e.src not in pos_by_id or e.dst not in pos_by_id:
            continue
        x1, y1 = pos_by_id[e.src]
        x2, y2 = pos_by_id[e.dst]
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        length = math.hypot(x2 - x1, y2 - y1)

        if length < 0.1:
            diagonal_edges.append(e)
            continue
        if length <= short_threshold:
            short_edges.append(e)
            continue
        if dy <= cfg.corridor_y_tolerance and dx > dy * 2:
            horizontal_edges.append((e, (y1 + y2) / 2))
        elif dx <= cfg.corridor_x_tolerance and dy > dx * 2:
            vertical_edges.append((e, (x1 + x2) / 2))
        else:
            diagonal_edges.append(e)

    h_clusters = _cluster_1d(horizontal_edges, cfg.corridor_y_tolerance)
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

    v_clusters = _cluster_1d(vertical_edges, cfg.corridor_x_tolerance)
    v_clusters_sorted = sorted(v_clusters, key=lambda c: c["band_center"])
    for i, cluster in enumerate(v_clusters_sorted):
        if len(cluster["edges"]) < cfg.min_corridor_size:
            label = "misc"
        else:
            label = "bay"
        for e in cluster["edges"]:
            e.inferred_corridor = label

    for e in diagonal_edges:
        e.inferred_corridor = "misc"

    for e in short_edges:
        e.inferred_corridor = ""
        e.inferred_access_type = "access"


def _cluster_1d(items: list[tuple], tolerance: float) -> list[dict]:
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
    n = len(clusters)
    if n == 0:
        return []
    if n == 1:
        return ["center"]
    if n == 2:
        return ["north", "south"]
    if n == 3:
        return list(name_pool)
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
    """hint 코드 + 차수 기반 role 후보 마킹."""
    if cfg.use_code_hints:
        for n in nodes:
            cd = (n.raw_node_type_cd or "").upper()
            if not cd:
                continue
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

    for n in nodes:
        if n.inferred_role != "standard":
            continue
        total_degree = n.degree_in + n.degree_out
        if total_degree <= cfg.terminal_degree_threshold:
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

    c = Counter(e.inferred_corridor or "(none)" for e in edges)
    report.corridor_stats = dict(c)

    for n in nodes:
        if n.degree_in == 0 and n.degree_out == 0:
            report.isolated_nodes.append(n.node_id)
        elif n.degree_in > 0 and n.degree_out == 0 and not n.inferred_is_charger:
            report.dead_end_nodes.append(n.node_id)

    report.connected_components = _count_weak_components(nodes, edges)

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
    """weakly connected components."""
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
    """
    nodes: list[ImportedNode] = []
    edges: list[ImportedEdge] = []

    raw_vertices = level_data.get("vertices", [])
    raw_lanes = level_data.get("lanes", [])

    idx_to_id: dict[int, str] = {}
    for i, vertex in enumerate(raw_vertices):
        if not vertex or len(vertex) < 2:
            continue
        x = float(vertex[0])
        y = float(vertex[1])
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
        if "rmf_role" in params:
            node.inferred_role = params["rmf_role"]
        nodes.append(node)
        idx_to_id[i] = node_id

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
    for n in nodes:
        if n.inferred_is_charger and n.inferred_role == "standard":
            n.inferred_role = "charger"
        elif n.inferred_is_holding and n.inferred_role == "standard":
            n.inferred_role = "holding"

    for n in nodes:
        if n.inferred_role != "standard":
            continue
        total_degree = n.degree_in + n.degree_out
        if total_degree <= cfg.terminal_degree_threshold:
            n.inferred_role = "holding_candidate"
            n.inferred_is_holding = True


# ────────────────────────────────────────────────────────────────────
# Edit.json 적용 (Editor 페이지에서 Save 한 결과)
#   ※ 머지 사고로 본문이 누락돼 있어 GAP-A 작업 중 server.py import 가
#     깨졌기에 server.py 가 요구하는 최소 동작만 복원. 자세한 셈은 추후 별도 fix 에서.
# ────────────────────────────────────────────────────────────────────
def apply_edits(imported: ImportedMap, edits) -> ImportedMap:
    """ImportedMap + edit.json → 편집 적용된 ImportedMap.

    NOTE: 이 함수의 풍부한 구현은 머지 사고로 손실됐다 (T-63 머지).
    GAP-A 에서는 server import 만 안 깨지면 되므로 stub 으로 둔다 —
    edits 가 None/빈 dict 면 원본 그대로, 아니면 ValueError 로 명시 실패시켜
    호출자가 인지하도록 한다. 완전한 복원은 별도 fix 의 책임.
    """
    if isinstance(edits, (str, Path)):
        edits = json.loads(Path(edits).read_text(encoding="utf-8"))
    if not edits:
        return imported
    # edits 가 있으면 — 안전한 fallback 으로 원본 반환 + 경고 로깅 1회.
    # (오리지널 apply_edits 가 손실된 상태라 안전한 no-op 동작.)
    import logging
    logging.getLogger(__name__).warning(
        "apply_edits stub: 편집 본문 적용 로직이 머지 사고로 손실됨 — 원본 그대로 반환"
    )
    return imported


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
