"""
generate_synthetic_3fleet.py — 3-fleet graph isolation 검증용 합성 맵.

구조:
  ┌─────────────────────────────────────────┐
  │  graph_idx=0 (TYPE_A / overhead)        │ y=80 corridor  ← 양방향 →
  │  A_C (charger), A_W1~A_W4 (waypoints)  │
  ├─────────────────────────────────────────┤
  │  graph_idx=1 (TYPE_B / floor)           │ y=50 corridor  ← 양방향 →
  │  B_C (charger), B_W1~B_W4 (waypoints)  │
  ├─────────────────────────────────────────┤
  │  graph_idx=2 (TYPE_C / scan)            │ y=20 corridor  ← 양방향 →
  │  C_C (charger), C_W1~C_W4 (waypoints)  │
  └─────────────────────────────────────────┘

각 fleet 은 자신의 graph_idx corridor 만 주행 가능.
required_capability demand 는 정확히 1 fleet 에만 매칭.

노드 ID 규칙:
  {Fleet_prefix}_C   : charger
  {Fleet_prefix}_W{n}: waypoint (n=1..4)
  {Fleet_prefix}_HP  : holding point
  ST_{cap}_{n}       : station

사용:
    python scripts/generate_synthetic_3fleet.py --out maps/synthetic_3fleet.json
    python scripts/generate_synthetic_3fleet.py --seed 7 --out maps/synthetic_3fleet.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _r(v: float) -> float:
    return round(v, 2)


def generate_3fleet_map(seed: int = 42) -> dict:
    """Generate synthetic 3-fleet map. Returns dict without disk I/O."""
    nodes: list[dict] = []
    links: list[dict] = []
    _lid = [0]

    def lid() -> str:
        _lid[0] += 1
        return f"L{_lid[0]:04d}"

    # ── Fleet 정의 ─────────────────────────────────────────────────
    fleets = [
        {
            "id": "TYPE_A",
            "graph_idx": 0,
            "color": "#0f9d58",
            "capabilities": ["overhead"],
            "count": 4,
            "max_speed_mps": 1.5,
            "priority": 1,
        },
        {
            "id": "TYPE_B",
            "graph_idx": 1,
            "color": "#2563eb",
            "capabilities": ["floor"],
            "count": 4,
            "max_speed_mps": 1.3,
            "priority": 2,
        },
        {
            "id": "TYPE_C",
            "graph_idx": 2,
            "color": "#e0a000",
            "capabilities": ["scan"],
            "count": 2,
            "max_speed_mps": 1.0,
            "priority": 3,
        },
    ]

    # ── Corridor 레이아웃 ───────────────────────────────────────────
    # prefix, graph_idx, y, capability
    CORRIDORS = [
        ("A", 0, 80.0, "overhead"),
        ("B", 1, 50.0, "floor"),
        ("C", 2, 20.0, "scan"),
    ]
    # Waypoint x positions
    WAYPOINT_XS = [0.0, 20.0, 40.0, 60.0, 80.0]  # W1..W4 + W5 (indices 1-based)
    CHARGER_X = -10.0
    STATION_DY = 15.0  # station offset from corridor y

    for prefix, gidx, y, cap in CORRIDORS:
        # charger node: {prefix}_C
        nodes.append({
            "id": f"{prefix}_C",
            "position": {"x": _r(CHARGER_X), "y": _r(y)},
            "name": f"{prefix}_C",
            "type": "charger",
            "is_charger": True,
            "graph_idx": gidx,
        })

        # waypoint nodes: {prefix}_W1 .. {prefix}_W{n}
        wp_ids = []
        for wi, wx in enumerate(WAYPOINT_XS, start=1):
            wid = f"{prefix}_W{wi}"
            wp_ids.append(wid)
            nodes.append({
                "id": wid,
                "position": {"x": _r(wx), "y": _r(y)},
                "name": wid,
                "type": "",
                "graph_idx": gidx,
            })

        # charger → W1 (bidirectional)
        links.append({
            "id": lid(),
            "connected": {"from": f"{prefix}_C", "to": wp_ids[0]},
            "bidirectional": True,
            "graph_idx": gidx,
        })

        # corridor links: W1↔W2↔W3↔W4↔W5
        for i in range(len(wp_ids) - 1):
            links.append({
                "id": lid(),
                "connected": {"from": wp_ids[i], "to": wp_ids[i + 1]},
                "bidirectional": True,
                "graph_idx": gidx,
            })

        # holding point: {prefix}_HP (between W2 and W3, above corridor)
        hp_id = f"{prefix}_HP"
        nodes.append({
            "id": hp_id,
            "position": {"x": _r(WAYPOINT_XS[2]), "y": _r(y - STATION_DY * 0.3)},
            "name": hp_id,
            "type": "holding",
            "is_holding_point": True,
            "graph_idx": gidx,
        })
        links.append({
            "id": lid(),
            "connected": {"from": wp_ids[2], "to": hp_id},
            "bidirectional": True,
            "graph_idx": gidx,
        })

        # station nodes: ST_{CAP}_01 .. ST_{CAP}_04 (above W1..W4)
        station_ids = []
        for si, wx in enumerate(WAYPOINT_XS[:4], start=1):
            st_id = f"ST_{cap.upper()}_{si:02d}"
            station_ids.append(st_id)
            nodes.append({
                "id": st_id,
                "position": {"x": _r(wx), "y": _r(y + STATION_DY)},
                "name": st_id,
                "type": "station",
                "is_station": True,
                "capability": cap,
                "graph_idx": gidx,
            })
            # waypoint → station
            links.append({
                "id": lid(),
                "connected": {"from": wp_ids[si - 1], "to": st_id},
                "bidirectional": True,
                "graph_idx": gidx,
            })

    # ── Demands (required_capability 로 fleet 매칭) ─────────────────
    demands: list[dict] = []
    for prefix, gidx, y, cap in CORRIDORS:
        for si in range(1, 4):  # ST_{cap}_01→02, 02→03, 03→04
            demands.append({
                "pickup": f"ST_{cap.upper()}_{si:02d}",
                "dropoff": f"ST_{cap.upper()}_{si+1:02d}",
                "required_capability": cap,
            })

    return {
        "meta": {
            "name": "synthetic_3fleet",
            "description": "3-fleet graph isolation test map (F1a)",
            "seed": seed,
            "unit": "m",
        },
        "nodes": nodes,
        "links": links,
        "fleets": fleets,
        "demands": demands,
    }


def generate(seed: int = 42) -> dict:
    """Alias for backward compatibility."""
    return generate_3fleet_map(seed=seed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic 3-fleet test map")
    parser.add_argument("--out", default="maps/synthetic_3fleet.json",
                        help="Output JSON file path")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data = generate_3fleet_map(seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[generate_synthetic_3fleet] {out} 생성 완료")
    print(f"  nodes: {len(data['nodes'])}, links: {len(data['links'])}")
    print(f"  fleets: {[f['id'] for f in data['fleets']]}")
    print(f"  demands: {len(data['demands'])}")


if __name__ == "__main__":
    main()
