"""
generate_synthetic_plant.py — 가짜 FAB 평면도 생성 (우리 topology_generator 와 무관!).

사용자 폐쇄망 데이터를 흉내내기 위한 합성 plant. 의도적으로:
  - 격자 완벽 정렬 아님 (좌표에 약한 노이즈)
  - 노드/링크 명명 규칙 안 따름 (N001, L_023 식 — 우리 WP_N_xxx 와 다름)
  - 메인 코리도가 비대칭 (북쪽만 양방향, 남쪽은 단방향)
  - 베이가 일부 위치에만 있음
  - 노드 type/link type 코드는 빈 문자열 (사용자 시나리오: 정책 없이 구조만)

이 데이터로 importer 의 자동 추론이 "처음 보는 맵" 에서도 합리적인지 검증.

사용:
    python scripts/generate_synthetic_plant.py --out maps/synthetic_plant.json
    python scripts/generate_synthetic_plant.py --seed 7 --out maps/synthetic_plant_b.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def _round(v: float, ndigits: int = 2) -> float:
    return round(v, ndigits)


def generate(seed: int = 42) -> dict:
    """가짜 FAB 평면도.

    형태 (모두 m 단위, y-up 좌표계 — FAB 도면 표준):
      - main horizontal corridors: y=80(north), y=50(center), y=20(south)
      - 가로 길이: x=0 ~ x=120 (north/center), x=10 ~ x=110 (south, 짧음)
      - bays: 일부 위치 (x=20, 50, 80) 에서 north 와 center 사이만 (south 는 없음)
      - 충전소: 좌측 끝에 클러스터 (x ≈ -10, 다양한 y)
      - 스테이션: 베이 끝
      - 일부 노드는 격자 정렬 약간 어긋남 (실제 도면 noise)
    """
    rng = random.Random(seed)
    NOISE_XY = 0.4   # m

    nodes = []
    links = []
    node_id_counter = [0]
    link_id_counter = [0]

    def new_node_id() -> str:
        node_id_counter[0] += 1
        return f"N{node_id_counter[0]:04d}"

    def new_link_id() -> str:
        link_id_counter[0] += 1
        return f"L{link_id_counter[0]:04d}"

    def add_node(x: float, y: float, name: str = "") -> str:
        nid = new_node_id()
        # 약한 좌표 노이즈 (실제 도면 측량 오차 흉내)
        nx = _round(x + rng.uniform(-NOISE_XY, NOISE_XY))
        ny = _round(y + rng.uniform(-NOISE_XY, NOISE_XY))
        nodes.append({
            "id": nid,
            "node_type_cd": "",
            "align_type_cd": "",
            "name": name or nid,
            "position": {"x": nx, "y": ny, "z": 0},
        })
        return nid

    def add_link(f: str, t: str, *, bidirectional: bool = False) -> None:
        """단방향이면 forward 1개, 양방향이면 forward + reverse 2개 링크."""
        links.append({
            "id": new_link_id(),
            "link_type_cd": "",
            "name": "Link",
            "connected": {"from": f, "to": t},
        })
        if bidirectional:
            links.append({
                "id": new_link_id(),
                "link_type_cd": "",
                "name": "Link",
                "connected": {"from": t, "to": f},
            })

    # ── 1. 메인 코리도 3개 ──────────────────────────────────────
    # north (y=80): x=0,10,20,...,120  → 13개 노드, 양방향
    north_xs = list(range(0, 121, 10))
    north_ids = [add_node(x, 80, f"N_north_x{x}") for x in north_xs]
    for a, b in zip(north_ids, north_ids[1:]):
        add_link(a, b, bidirectional=True)

    # center (y=50): x=0,10,...,120 → 13개 노드, 단방향 (왼→오)
    center_xs = list(range(0, 121, 10))
    center_ids = [add_node(x, 50, f"N_center_x{x}") for x in center_xs]
    for a, b in zip(center_ids, center_ids[1:]):
        add_link(a, b, bidirectional=False)

    # south (y=20): x=10,20,...,110 → 11개 노드 (북/중보다 짧음), 단방향 (오→왼)
    south_xs = list(range(10, 111, 10))
    south_ids = [add_node(x, 20, f"N_south_x{x}") for x in south_xs]
    for a, b in zip(south_ids[::-1], south_ids[::-1][1:]):
        add_link(a, b, bidirectional=False)

    # ── 2. 베이 (수직 통로) — x=20, x=50, x=80 위치만 ─────────
    # 베이는 north ↔ center 만 연결 (south 까지는 안 감). 비대칭 FAB.
    bay_xs = [20, 50, 80]
    for bx in bay_xs:
        # 베이는 보통 station/setup 작업 공간이라 양방향으로 가정
        north_node = next(n for n, x in zip(north_ids, north_xs) if x == bx)
        center_node = next(n for n, x in zip(center_ids, center_xs) if x == bx)
        # 베이 중간 노드 1~2개 (y=65)
        mid_id = add_node(bx, 65, f"N_bay_x{bx}_mid")
        add_link(north_node, mid_id, bidirectional=True)
        add_link(mid_id, center_node, bidirectional=True)

        # 베이 끝에 station (왼쪽으로 살짝 들어간 access)
        station_id = add_node(bx - 3, 65, f"N_station_x{bx}")
        add_link(mid_id, station_id, bidirectional=True)

    # ── 3. 충전소 클러스터 — 좌측 끝 (x ≈ -10) ─────────────────
    # 진입로 1개 + 충전 베이 3개
    charger_access_node = add_node(-3, 50, "N_charger_access")
    # access 노드와 center 가장 왼쪽 노드 연결 (양방향)
    add_link(charger_access_node, center_ids[0], bidirectional=True)
    # 충전소 3개 (y=45, 50, 55)
    for cy in [45, 50, 55]:
        ch_id = add_node(-10, cy, f"N_charger_y{cy}")
        add_link(charger_access_node, ch_id, bidirectional=True)

    # ── 4. north ↔ south 연결 (양 끝) ────────────────────────
    # 우측 끝: north x=120, south x=110 을 잇는 짧은 access (대각선)
    # 좌측 끝: north x=10, south x=10 을 잇는 수직
    add_link(north_ids[1], south_ids[0], bidirectional=True)   # 좌측 (x=10)
    add_link(north_ids[12], south_ids[10], bidirectional=True) # 우측 (x=120 ↔ x=110)

    # ── 5. holding zone (대기 영역) — 우측 상단 모서리 ────────
    # 메인 흐름과 분리된 작은 그룹 (degree 1~2)
    hp_access = add_node(125, 80, "N_holding_access")
    add_link(north_ids[12], hp_access, bidirectional=True)
    for i, dy in enumerate([0, 5, 10]):
        hp_id = add_node(130, 80 + dy, f"N_holding_{i}")
        add_link(hp_access, hp_id, bidirectional=True)

    return {
        "_meta": {
            "source": "synthetic_plant generator (NOT based on topology_generator)",
            "intent": "외부 폐쇄망 FAB 형태 흉내 — importer 의 generalization 검증",
            "seed": seed,
            "expected_features": {
                "north_corridor_bidirectional": True,
                "center_corridor_unidirectional_left_to_right": True,
                "south_corridor_unidirectional_right_to_left": True,
                "bays_only_between_north_and_center": True,
                "chargers_on_west_edge": True,
                "holding_zone_on_east_top_corner": True,
                "coordinate_noise_amplitude_m": NOISE_XY,
            },
        },
        "nodes": nodes,
        "links": links,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data = generate(seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    bidir_link_count = 0
    pairs = set()
    for l in data["links"]:
        f, t = l["connected"]["from"], l["connected"]["to"]
        if (t, f) in pairs:
            bidir_link_count += 2
        pairs.add((f, t))

    print(f"✓ {out}")
    print(f"  nodes={len(data['nodes'])}  links={len(data['links'])}")
    print(f"  bidirectional-encoded links (forward+reverse pairs): {bidir_link_count}")
    print(f"  seed={args.seed}")
    print(f"  → importer 가 좌표/연결만으로 어떻게 추론하는지 확인 가능")


if __name__ == "__main__":
    main()
