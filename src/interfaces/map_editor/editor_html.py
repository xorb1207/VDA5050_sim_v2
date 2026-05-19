"""
editor_html.py — Map Editor HTML 페이지 생성기.

ImportedMap → self-contained HTML 페이지. 사용자가 브라우저에서:
  · 방향성 편집 (Paint 모드)
  · 노드 역할 마킹 (Stamp 도구)
  · Build 모드 (노드/엣지 추가·삭제)
  · 다중 선택, Undo/Redo
  · Save (download 또는 server PUT) / Export YAML (OpenRMF building.yaml)

HTML 본체는 `editor_template.html` (이 모듈 옆) 단일 파일이 source-of-truth.
이 모듈은 ImportedMap 을 PAYLOAD JSON 으로 직렬화하고 sentinel 위치에 주입할 뿐.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.domain.map.external_importer import ImportedMap

# 템플릿 파일 위치 (모듈 옆). 폐쇄망에서도 외부 의존성 없이 동작하도록 패키지 자원처럼 둠.
_TEMPLATE_PATH = Path(__file__).parent / "editor_template.html"

# PAYLOAD 주입 sentinel — 템플릿에는 `const PAYLOAD = /*__PAYLOAD_JSON__*/{};` 로 들어가 있어
# (a) 운영 시: Python 이 이 문자열을 `<직렬화된 JSON>` 으로 교체해 서빙.
# (b) 디자이너가 템플릿을 그대로 브라우저로 열면: JS syntax 유효 (`{}` 빈 dict) → 빈 캔버스 + 패널 로딩.
_PAYLOAD_SENTINEL = "/*__PAYLOAD_JSON__*/{}"


@lru_cache(maxsize=1)
def _load_template() -> str:
    """editor_template.html 을 read. 모듈 로드 후 한 번만 disk 접근."""
    if not _TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"editor_template.html not found at {_TEMPLATE_PATH}. "
            "Map editor 통합 작업 (PR #?) 이후엔 이 파일이 source-of-truth 입니다."
        )
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def build_editor_html(
    imported: ImportedMap,
    title: str = "Map Editor",
    source_name: str = "imported_map",
    server_map_id: str | None = None,
) -> str:
    """ImportedMap → self-contained HTML 페이지 문자열.

    source_name: Save 시 다운로드 파일명 (예: "synthetic_plant" → "synthetic_plant.edit.json")
    server_map_id: 서버 메모리 map id. 있으면 Save 시 서버에도 PUT. None 이면 다운로드만.
    """
    # ── 데이터 직렬화 (JS 가 그대로 사용) ──────────────────────────
    nodes_payload = [{
        "id": n.node_id,
        "x": n.x,
        "y": n.y,
        "name": n.name,
        "role": n.inferred_role,
        "is_charger": n.inferred_is_charger,
        "is_holding": n.inferred_is_holding,
        "degree_in": n.degree_in,
        "degree_out": n.degree_out,
        # F1a: 노드 capability 태그 (engine 에서 지원 시)
        "capability": getattr(n, "capability", None),
    } for n in imported.nodes]

    edges_payload = [{
        "id": e.edge_id,
        "src": e.src,
        "dst": e.dst,
        "bidir": e.inferred_bidirectional,
        "corridor": e.inferred_corridor,
        "access": e.inferred_access_type,
        "v_max": e.v_max,   # F1b-ux: per-edge 속도 제한 (None=미설정)
        # F1a: 어떤 graph 에 속한 lane 인지. None/누락이면 0 (단일 그래프 fallback)
        "graph_idx": (e.graph_idx if e.graph_idx is not None else 0),
    } for e in imported.edges]

    # F1a: fleets 정보 (engine 에서 지원 시)
    raw_fleets = getattr(imported, "fleets", None) or []
    fleets_payload: list[dict] = []
    for f in raw_fleets:
        # dict 형 (T-61 integration) 또는 Fleet dataclass 둘 다 수용
        if isinstance(f, dict):
            fleets_payload.append({
                "id": str(f.get("id", "")),
                "graph_idx": int(f.get("graph_idx", 0)),
                "color": str(f.get("color", "#0f9d58")),
                "capabilities": list(f.get("capabilities", []) or []),
                "count": int(f.get("count", 1)),
                "max_speed_mps": float(f.get("max_speed_mps", 1.5)),
                "priority": int(f.get("priority", 1)),
            })
        else:
            fleets_payload.append({
                "id": str(getattr(f, "id", "")),
                "graph_idx": int(getattr(f, "graph_idx", 0)),
                "color": str(getattr(f, "color", "#0f9d58")),
                "capabilities": list(getattr(f, "capabilities", []) or []),
                "count": int(getattr(f, "count", 1)),
                "max_speed_mps": float(getattr(f, "max_speed_mps", 1.5)),
                "priority": int(getattr(f, "priority", 1)),
            })

    report = imported.report
    report_payload = {
        "node_count": report.node_count,
        "edge_count_raw": report.edge_count_raw,
        "edge_count_after_merge": report.edge_count_after_merge,
        "bidirectional_count": report.bidirectional_count,
        "inferred_chargers": report.inferred_chargers,
        "inferred_stations": report.inferred_stations,
        "inferred_holding": report.inferred_holding,
        "connected_components": report.connected_components,
        "isolated_nodes": report.isolated_nodes[:20],
        "dead_end_nodes": report.dead_end_nodes[:20],
        "corridor_stats": report.corridor_stats,
        "warnings": [{"severity": w.severity, "code": w.code, "message": w.message}
                     for w in report.warnings],
    }

    payload = {
        "title": title,
        "source_name": source_name,
        "server_map_id": server_map_id,
        "nodes": nodes_payload,
        "edges": edges_payload,
        "report": report_payload,
        "fleets": fleets_payload,
    }

    # 배경 이미지 포함 (있을 때만)
    if imported.background_image:
        payload["background_image"] = imported.background_image

    payload_json = json.dumps(payload, ensure_ascii=False)

    template = _load_template()
    if _PAYLOAD_SENTINEL not in template:
        raise RuntimeError(
            f"PAYLOAD sentinel {_PAYLOAD_SENTINEL!r} not found in {_TEMPLATE_PATH}. "
            "템플릿이 변경되었거나 손상되었습니다."
        )
    return template.replace(_PAYLOAD_SENTINEL, payload_json, 1)
