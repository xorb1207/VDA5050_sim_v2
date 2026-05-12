"""
Mock plant map JSON 생성 — 사용자(폐쇄망) 데이터 형식 흉내.

기존 topology_generator 의 Type A/C 등으로 그래프 만든 다음,
사용자의 JSON 포맷 (nodes/links + position{x,y,z} + 빈 _cd 필드) 으로 export.

이 mock JSON 으로 importer 의 자동 추론이 원본을 얼마나 잘 복원하는지 검증.

사용:
    python scripts/generate_mock_plant_json.py --type A --out maps/mock_plant_a.json
    python scripts/generate_mock_plant_json.py --type C --out maps/mock_plant_c.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 — PYTHONPATH 설정 없이 동작 (Windows 친화)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.domain.map.topology_generator import MapTopologyGenerator


# 사용자 데이터엔 _cd 코드들이 들어있지만, 사용자 시나리오에선
# "구조만 임포트, 정책은 사후 부여" 이므로 mock 도 의도적으로 빈 값 / 더미 코드만 둠.
# (importer 가 _cd 를 무시하는지 검증)
NODE_TYPE_CD_DUMMY = ""
LINK_TYPE_CD_DUMMY = ""
ALIGN_TYPE_CD_DUMMY = ""


def graph_to_user_json(graph, drop_role_hints: bool = True) -> dict:
    """MapGraph → 사용자 폐쇄망 JSON 포맷.

    drop_role_hints=True (기본): node_type_cd / link_type_cd 를 빈 문자열로.
      → importer 가 정책 코드를 안 보고도 좌표/연결만으로 추론하는지 검증.
    drop_role_hints=False: 원본 role/bidirectional 을 코드로 박아 export.
      → 정책 코드가 들어왔을 때 사용 hint 화 검증.
    """
    nodes_out = []
    for n in graph.nodes.values():
        # role 힌트: 정답 데이터로 비교 가능하도록 별도 키로 빼둠 (importer 는 무시)
        role_hint = n.role.value if hasattr(n.role, "value") else str(n.role)
        if n.is_charger:
            role_hint = "charger"
        nodes_out.append({
            "id": n.node_id,
            "node_type_cd": "" if drop_role_hints else role_hint,
            "align_type_cd": ALIGN_TYPE_CD_DUMMY,
            "name": n.node_id,
            "position": {"x": float(n.x), "y": float(n.y), "z": 0},
            # 정답지 (검증용, importer 는 안 봄)
            "_truth_role": role_hint,
            "_truth_is_charger": bool(n.is_charger),
            "_truth_is_holding": bool(getattr(n, "is_holding_point", False)),
        })

    # graph.edges 가 양방향을 (A,B)+(B,A) 두 entry 로 저장하는 경우와
    # 1 entry (bidir=True) 만 저장하는 경우 모두 처리. 결과 mock 은 항상
    # 사용자 데이터 가설 (a) — 양방향은 forward + reverse 두 링크 — 형식.
    links_out = []
    seen_pairs: set[tuple[str, str]] = set()
    for e in graph.edges.values():
        pair = (e.start_node_id, e.end_node_id)
        if pair in seen_pairs:
            continue  # 같은 pair 다른 entry (양방향 reverse 표현) — 첫 entry 가 이미 처리함
        seen_pairs.add(pair)
        links_out.append({
            "id": e.edge_id,
            "link_type_cd": LINK_TYPE_CD_DUMMY,
            "name": "Link",
            "connected": {"from": e.start_node_id, "to": e.end_node_id},
            # 정답지 (importer 는 이 필드 안 봄)
            "_truth_corridor": e.corridor,
            "_truth_bidirectional": bool(e.bidirectional),
            "_truth_access_type": e.access_type,
        })
        if e.bidirectional:
            rev = (e.end_node_id, e.start_node_id)
            if rev not in seen_pairs:
                seen_pairs.add(rev)
                links_out.append({
                    "id": e.edge_id + "_rev",
                    "link_type_cd": LINK_TYPE_CD_DUMMY,
                    "name": "Link",
                    "connected": {"from": e.end_node_id, "to": e.start_node_id},
                    "_truth_corridor": e.corridor,
                    "_truth_bidirectional": True,
                    "_truth_access_type": e.access_type,
                })

    return {
        "_meta": {
            "source": "mock generator",
            "based_on": "topology_generator output",
            "note": "_truth_* fields are ground truth for importer validation. "
                    "Importer should ignore them in production.",
        },
        "nodes": nodes_out,
        "links": links_out,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default="A", help="topology type code (A/B/C/D/E)")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--keep-cd-hints", action="store_true",
                        help="keep node_type_cd/link_type_cd as hint codes "
                             "(default: empty — pure structure import)")
    args = parser.parse_args()

    tgen = MapTopologyGenerator()
    graph = tgen.generate(args.type)
    data = graph_to_user_json(graph, drop_role_hints=not args.keep_cd_hints)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    nodes = len(data["nodes"])
    links = len(data["links"])
    chargers = sum(1 for n in data["nodes"] if n["_truth_is_charger"])
    bidir = sum(1 for l in data["links"] if l["_truth_bidirectional"])
    print(f"✓ {out_path}")
    print(f"  nodes={nodes}  links={links}  chargers={chargers}  bidir-links={bidir}")
    print(f"  hint codes: {'kept' if args.keep_cd_hints else 'empty (pure structure)'}")


if __name__ == "__main__":
    main()
