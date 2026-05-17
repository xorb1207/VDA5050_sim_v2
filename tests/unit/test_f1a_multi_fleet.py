"""
F1a 다중 fleet 기능 테스트.
- Graph isolation (graph_idx)
- Capability matching
- Fleet priority reservation tiebreaker
- T66: 합성 3-fleet end-to-end integration

실행: python -m pytest tests/unit/test_f1a_multi_fleet.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.domain.fleet import Fleet
from src.domain.map.graph import MapGraph, NodeRole, Edge, Node
from src.domain.reservation.scheduler import TimeWindowScheduler, SIMULTANEOUS_RESERVATION_THRESHOLD_S
from src.application.scenario.demand import TaskDemand
from src.adapters.bus.adapters import LocalMemoryBus
from src.domain.agv.agv import AGV

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SYNTHETIC_3FLEET_SCRIPT = PROJECT_ROOT / "scripts" / "generate_synthetic_3fleet.py"


def _load_generator_module():
    """generate_synthetic_3fleet.py 를 모듈로 로드 (maps/ 출력 없이 함수 호출용)."""
    spec = importlib.util.spec_from_file_location(
        "generate_synthetic_3fleet",
        str(SYNTHETIC_3FLEET_SCRIPT),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def run(coro):
    return asyncio.run(coro)


def assert_eq(label: str, got, expected) -> None:
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r}  expected={expected!r}")
    if got != expected:
        raise AssertionError(f"{label}: {got!r} != {expected!r}")


def assert_true(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


def assert_not_empty(label: str, collection) -> None:
    status = "PASS" if collection else "FAIL"
    print(f"  [{status}] {label}: {len(collection)} items")
    if not collection:
        raise AssertionError(f"{label}: collection is empty")


# ─────────────────────────────────────────────
# T60.1-T60.4: Edge.graph_idx 기본값
# ─────────────────────────────────────────────

def test_edge_graph_idx_default_zero():
    print("\n[T60.1] Edge.graph_idx 기본값 = 0")
    edge = Edge(
        edge_id="test_edge",
        start_node_id="A",
        end_node_id="B",
    )
    assert_eq("기본 graph_idx", edge.graph_idx, 0)


def test_edge_graph_idx_explicit():
    print("\n[T60.2] Edge.graph_idx 명시적 설정")
    edge = Edge(
        edge_id="test_edge",
        start_node_id="A",
        end_node_id="B",
        graph_idx=1,
    )
    assert_eq("명시 graph_idx", edge.graph_idx, 1)


def test_fleet_class_defaults():
    print("\n[T60.3] Fleet 클래스 기본값")
    fleet = Fleet(id="TYPE_1", graph_idx=0)
    assert_eq("id", fleet.id, "TYPE_1")
    assert_eq("graph_idx", fleet.graph_idx, 0)
    assert_eq("capabilities 기본값", fleet.capabilities, [])
    assert_eq("priority 기본값", fleet.priority, 1)
    assert_eq("count 기본값", fleet.count, 1)
    assert_eq("max_speed_mps 기본값", fleet.max_speed_mps, 1.5)


def test_fleet_class_full():
    print("\n[T60.4] Fleet 클래스 전체 필드")
    fleet = Fleet(
        id="TYPE_A",
        graph_idx=1,
        capabilities=["overhead", "pickup_small"],
        color="#ff0000",
        max_speed_mps=2.0,
        priority=2,
        count=5,
    )
    assert_eq("id", fleet.id, "TYPE_A")
    assert_eq("graph_idx", fleet.graph_idx, 1)
    assert_eq("capabilities", fleet.capabilities, ["overhead", "pickup_small"])
    assert_eq("color", fleet.color, "#ff0000")
    assert_eq("max_speed_mps", fleet.max_speed_mps, 2.0)
    assert_eq("priority", fleet.priority, 2)
    assert_eq("count", fleet.count, 5)


# ─────────────────────────────────────────────
# T60.5-T60.8: Graph isolation (레인 필터링)
# ─────────────────────────────────────────────

def _make_multi_graph_test() -> tuple[MapGraph, Fleet, Fleet]:
    """2개 fleet용 테스트 그래프 생성.
    A--B (graph_idx=0)
    |  |
    C--D
    +--E (graph_idx=1, graph 0과 다름)
    """
    graph = MapGraph()

    # Nodes
    for nid in ["A", "B", "C", "D", "E"]:
        graph._add_node(Node(node_id=nid, x=float(ord(nid)), y=0.0))

    # Graph 0 edges: A-B, A-C, B-D, C-D (양방향)
    for edge_id, src, dst in [
        ("e_ab", "A", "B"),
        ("e_ac", "A", "C"),
        ("e_bd", "B", "D"),
        ("e_cd", "C", "D"),
    ]:
        edge = Edge(
            edge_id=edge_id,
            start_node_id=src,
            end_node_id=dst,
            bidirectional=True,
            graph_idx=0,
        )
        graph._add_edge(edge)

    # Graph 1 edges: A-E, E-D (양방향, graph_idx=1)
    for edge_id, src, dst in [
        ("e_ae", "A", "E"),
        ("e_ed", "E", "D"),
    ]:
        edge = Edge(
            edge_id=edge_id,
            start_node_id=src,
            end_node_id=dst,
            bidirectional=True,
            graph_idx=1,
        )
        graph._add_edge(edge)

    fleet0 = Fleet(id="FLEET_0", graph_idx=0)
    fleet1 = Fleet(id="FLEET_1", graph_idx=1)

    return graph, fleet0, fleet1


def test_router_filters_lanes_by_fleet():
    print("\n[T60.5] 라우터가 fleet의 graph_idx로 레인 필터링")
    graph, fleet0, fleet1 = _make_multi_graph_test()

    # Fleet 0: graph_idx=0 사용 → A→B 경로 가능
    path0 = graph.get_path("A", "B", fleet=fleet0)
    assert_not_empty("Fleet 0 A→B 경로", path0)
    assert_eq("Fleet 0 경로 시작", path0[0], "A")
    assert_eq("Fleet 0 경로 종료", path0[-1], "B")

    # Fleet 0: A→D 경로도 가능 (A-B-D 또는 A-C-D)
    path0_ad = graph.get_path("A", "D", fleet=fleet0)
    assert_not_empty("Fleet 0 A→D 경로", path0_ad)


def test_router_fleet1_uses_graph1_only():
    print("\n[T60.6] Fleet 1은 graph_idx=1 edge만 사용")
    graph, fleet0, fleet1 = _make_multi_graph_test()

    # Fleet 1: graph_idx=1 사용 → A→E→D 경로만 가능
    path1 = graph.get_path("A", "D", fleet=fleet1)
    assert_not_empty("Fleet 1 A→D 경로", path1)
    # E를 반드시 거쳐야 함 (A-E-D만 가능)
    assert_true("Fleet 1 경로가 E 포함", "E" in path1)

    # Fleet 1은 B로 갈 수 없음 (B가 graph_idx=1 edge와 연결 안 됨)
    path1_ab = graph.get_path("A", "B", fleet=fleet1)
    assert_eq("Fleet 1 A→B 경로 불가", path1_ab, [])


def test_router_no_fleet_uses_all_lanes():
    print("\n[T60.7] fleet=None 이면 모든 레인 사용 (legacy)")
    graph, fleet0, fleet1 = _make_multi_graph_test()

    # fleet=None: 모든 edge 사용
    path = graph.get_path("A", "D", fleet=None)
    assert_not_empty("fleet=None A→D 경로", path)


def test_shared_vertex_two_fleets():
    print("\n[T60.8] 공유 vertex A/D는 두 fleet 모두 접근 가능")
    graph, fleet0, fleet1 = _make_multi_graph_test()

    # 둘 다 A에 접근 가능 (vertex는 공유)
    assert_true("Fleet 0이 A 접근", "A" in graph.nodes)
    assert_true("Fleet 1이 A 접근", "A" in graph.nodes)


# ─────────────────────────────────────────────
# T60.9-T60.11: Reservation tiebreaker
# ─────────────────────────────────────────────

def test_reservation_fifo_normal_case():
    print("\n[T60.9] ≥5ms 시간 차이 — priority와 무관 FIFO")
    sched = TimeWindowScheduler()
    fleet_a = Fleet(id="A", graph_idx=0, priority=1)
    fleet_b = Fleet(id="B", graph_idx=0, priority=2)

    winner = sched.resolve_simultaneous_reservation(
        agv_id_a="agv_1",
        agv_id_b="agv_2",
        time_a=10.0,
        time_b=10.006,  # 6ms 차이 > 1ms
        fleet_a=fleet_a,
        fleet_b=fleet_b,
    )
    assert_eq("FIFO 우승자", winner, "agv_1")


def test_reservation_priority_tiebreaker():
    print("\n[T60.10] <1ms 시간 차이 — priority 낮은 fleet 우선")
    sched = TimeWindowScheduler()
    fleet_a = Fleet(id="A", graph_idx=0, priority=1)
    fleet_b = Fleet(id="B", graph_idx=0, priority=2)

    winner = sched.resolve_simultaneous_reservation(
        agv_id_a="agv_1",
        agv_id_b="agv_2",
        time_a=10.0,
        time_b=10.0005,  # 0.5ms 차이 < 1ms
        fleet_a=fleet_a,
        fleet_b=fleet_b,
    )
    assert_eq("Priority tiebreaker — priority 1 우선", winner, "agv_1")


def test_reservation_same_priority_deterministic():
    print("\n[T60.11] priority 같으면 agv_id 작은 쪽")
    sched = TimeWindowScheduler()
    fleet_a = Fleet(id="A", graph_idx=0, priority=1)
    fleet_b = Fleet(id="B", graph_idx=0, priority=1)

    winner = sched.resolve_simultaneous_reservation(
        agv_id_a="agv_2",
        agv_id_b="agv_1",
        time_a=10.0,
        time_b=10.0005,  # <1ms
        fleet_a=fleet_a,
        fleet_b=fleet_b,
    )
    assert_eq("Deterministic — agv_id 작은 쪽", winner, "agv_1")


# ─────────────────────────────────────────────
# T60.12-T60.14: Capability matching
# ─────────────────────────────────────────────

def test_demand_required_capability_none_legacy():
    print("\n[T60.12] required_capability=None — 모든 fleet 처리 가능 (legacy)")
    demand = TaskDemand(
        task_id="task_1",
        release_time_s=0.0,
        pickup_node_id="A",
        dropoff_node_id="B",
        processing_time_s=60.0,
        required_capability=None,
    )
    assert_eq("required_capability", demand.required_capability, None)


def test_demand_capability_match():
    print("\n[T60.13] required_capability 필드 추가")
    demand = TaskDemand(
        task_id="task_1",
        release_time_s=0.0,
        pickup_node_id="A",
        dropoff_node_id="B",
        processing_time_s=60.0,
        required_capability="overhead",
    )
    assert_eq("required_capability", demand.required_capability, "overhead")


def test_agv_fleet_field():
    print("\n[T60.14] AGV.fleet 필드")
    bus = LocalMemoryBus()
    graph = MapGraph.from_json("maps/sample_fab.json")
    sched = TimeWindowScheduler()
    fleet = Fleet(id="TYPE_1", graph_idx=0)

    # fleet 지정
    agv = AGV(
        agv_id="agv_1",
        bus=bus,
        graph=graph,
        scheduler=sched,
        fleet=fleet,
    )
    assert_eq("AGV.fleet 설정", agv.fleet.id, "TYPE_1")

    # fleet=None (legacy)
    agv_legacy = AGV(
        agv_id="agv_2",
        bus=bus,
        graph=graph,
        scheduler=sched,
        fleet=None,
    )
    assert_eq("AGV.fleet None (legacy)", agv_legacy.fleet, None)


# ─────────────────────────────────────────────
# T66: 합성 3-fleet end-to-end integration
# ─────────────────────────────────────────────

def _ensure_synthetic_3fleet_map() -> dict:
    """generate_synthetic_3fleet.generate_3fleet_map() 호출로 dict 반환 (disk I/O 없음)."""
    mod = _load_generator_module()
    return mod.generate_3fleet_map()


def _build_graph_from_synthetic(data: dict) -> MapGraph:
    """synthetic_3fleet.json 스키마({nodes,links,connected,graph_idx}) → MapGraph.
    링크는 양방향 처리 (TYPE_X AGV가 영역 내 왕복 가능하도록)."""
    g = MapGraph()
    for n in data["nodes"]:
        pos = n["position"]
        g._add_node(Node(node_id=n["id"], x=float(pos["x"]), y=float(pos["y"])))
    for l in data["links"]:
        c = l["connected"]
        g._add_edge(Edge(
            edge_id=l["id"],
            start_node_id=c["from"],
            end_node_id=c["to"],
            bidirectional=True,
            graph_idx=int(l.get("graph_idx", 0)),
        ))
    return g


def _fleets_from_data(data: dict) -> list[Fleet]:
    return [
        Fleet(
            id=str(f["id"]),
            graph_idx=int(f["graph_idx"]),
            capabilities=list(f.get("capabilities", [])),
            color=str(f.get("color", "#888888")),
            max_speed_mps=float(f.get("max_speed_mps", 1.5)),
            priority=int(f.get("priority", 1)),
            count=int(f.get("count", 1)),
        )
        for f in data.get("fleets", [])
    ]


def test_synthetic_3fleet_generator_produces_valid_json():
    print("\n[T66.1] generate_synthetic_3fleet.py 실행 → 3 fleet JSON 생성 (tmp out)")
    # 임시 디렉토리로 출력하여 maps/ 더럽히지 않음
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "synthetic_3fleet.json"
        result = subprocess.run(
            [sys.executable, str(SYNTHETIC_3FLEET_SCRIPT),
             "--out", str(out_path)],
            check=True,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert_true("script exit 0", result.returncode == 0)
        assert_true("output file 존재", out_path.exists())

        data = json.loads(out_path.read_text(encoding="utf-8"))

    assert_true("nodes 키 존재", "nodes" in data)
    assert_true("links 키 존재", "links" in data)
    assert_true("fleets 키 존재", "fleets" in data)
    assert_true("demands 키 존재", "demands" in data)

    assert_eq("fleet 수", len(data["fleets"]), 3)
    fleet_ids = {f["id"] for f in data["fleets"]}
    assert_eq("fleet ID 셋", fleet_ids, {"TYPE_A", "TYPE_B", "TYPE_C"})
    fleet_gidx = {f["graph_idx"] for f in data["fleets"]}
    assert_eq("graph_idx 셋", fleet_gidx, {0, 1, 2})

    # 각 fleet 의 capability 가 비어있지 않고 fleet 간 disjoint
    caps_lists = [tuple(sorted(f.get("capabilities", []))) for f in data["fleets"]]
    for caps in caps_lists:
        assert_true("fleet capabilities 비어있지 않음", len(caps) > 0)
    flat_caps = [c for caps in caps_lists for c in caps]
    assert_eq("capability 중복 없음 (disjoint)", len(flat_caps), len(set(flat_caps)))


def test_synthetic_3fleet_fleet_graph_isolation():
    print("\n[T66.2] 각 fleet 의 AGV 는 자신의 graph_idx 레인만 사용")
    data = _ensure_synthetic_3fleet_map()
    graph = _build_graph_from_synthetic(data)
    fleets = _fleets_from_data(data)
    by_id = {f.id: f for f in fleets}

    # TYPE_A (graph_idx=0): A_C→A_W2 가능
    path_a = graph.get_path("A_C", "A_W2", fleet=by_id["TYPE_A"])
    assert_not_empty("TYPE_A: A_C→A_W2 (graph 0 내부)", path_a)

    # TYPE_A 는 B 영역 노드 (graph_idx=1) 도달 불가
    cross = graph.get_path("A_C", "B_W1", fleet=by_id["TYPE_A"])
    assert_eq("TYPE_A: A_C→B_W1 (cross-graph 불가)", cross, [])

    # TYPE_B (graph_idx=1): B_C→B_W2 가능, A_W1 불가, C_W1 불가
    path_b = graph.get_path("B_C", "B_W2", fleet=by_id["TYPE_B"])
    assert_not_empty("TYPE_B: B_C→B_W2 (graph 1 내부)", path_b)
    assert_eq("TYPE_B: B_C→A_W1 불가", graph.get_path("B_C", "A_W1", fleet=by_id["TYPE_B"]), [])
    assert_eq("TYPE_B: B_C→C_W1 불가", graph.get_path("B_C", "C_W1", fleet=by_id["TYPE_B"]), [])

    # TYPE_C (graph_idx=2): C_C→C_W1 가능, A_W1 불가, B_W1 불가
    path_c = graph.get_path("C_C", "C_W1", fleet=by_id["TYPE_C"])
    assert_not_empty("TYPE_C: C_C→C_W1 (graph 2 내부)", path_c)
    assert_eq("TYPE_C: C_C→A_W1 불가", graph.get_path("C_C", "A_W1", fleet=by_id["TYPE_C"]), [])
    assert_eq("TYPE_C: C_C→B_W1 불가", graph.get_path("C_C", "B_W1", fleet=by_id["TYPE_C"]), [])


def test_synthetic_3fleet_capability_matching_unique():
    print("\n[T66.3] required_capability 있는 demand 는 정확히 1 개 fleet 와 매칭")
    data = _ensure_synthetic_3fleet_map()
    fleets = _fleets_from_data(data)
    demands = data.get("demands", [])
    assert_true("demand 정의 존재", len(demands) >= 1)

    for d in demands:
        req = d.get("required_capability")
        if req is None:
            continue
        matched = [f for f in fleets if req in f.capabilities]
        assert_eq(f"demand({d['pickup']}→{d['dropoff']}, req={req}) 매칭 fleet 수",
                  len(matched), 1)


def test_synthetic_3fleet_demand_reachable_by_matching_fleet():
    print("\n[T66.4] 매칭 fleet 의 graph 로 demand pickup→dropoff 경로 존재")
    data = _ensure_synthetic_3fleet_map()
    graph = _build_graph_from_synthetic(data)
    fleets = _fleets_from_data(data)
    demands = data.get("demands", [])

    for d in demands:
        req = d.get("required_capability")
        if req is None:
            continue
        fl = next((f for f in fleets if req in f.capabilities), None)
        assert_true(f"req={req} 매칭 fleet 존재", fl is not None)
        path = graph.get_path(d["pickup"], d["dropoff"], fleet=fl)
        assert_not_empty(f"{fl.id}: {d['pickup']}→{d['dropoff']} 경로", path)


def test_synthetic_3fleet_demand_unreachable_by_other_fleets():
    print("\n[T66.5] 매칭되지 않는 fleet 는 demand pickup 도달 불가 (graph isolation)")
    data = _ensure_synthetic_3fleet_map()
    graph = _build_graph_from_synthetic(data)
    fleets = _fleets_from_data(data)
    demands = data.get("demands", [])

    # fleet 별 영역 시작 노드 (구조상 고정)
    fleet_start = {"TYPE_A": "A_C", "TYPE_B": "B_C", "TYPE_C": "C_C"}

    for d in demands:
        req = d.get("required_capability")
        if req is None:
            continue
        for f in fleets:
            if req in f.capabilities:
                continue
            start = fleet_start.get(f.id)
            if start is None:
                continue
            # 다른 fleet 의 영역 시작 노드에서 demand pickup 까지 경로 시도
            path = graph.get_path(start, d["pickup"], fleet=f)
            assert_eq(
                f"{f.id}(start={start})→{d['pickup']} (cross-graph 불가)",
                path,
                [],
            )


def test_synthetic_3fleet_fleet_kpi_attribution_per_agv():
    print("\n[T66.6] AGV 가 fleet 에 정확히 귀속되어 KPI 분리 가능")
    data = _ensure_synthetic_3fleet_map()
    graph = _build_graph_from_synthetic(data)
    fleets = _fleets_from_data(data)
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()

    # fleet.count 만큼 AGV 인스턴스화 + fleet 귀속 검증
    agvs: list[AGV] = []
    for fl in fleets:
        for i in range(fl.count):
            agvs.append(AGV(
                agv_id=f"{fl.id}_agv_{i+1}",
                bus=bus,
                graph=graph,
                scheduler=sched,
                max_speed_mps=fl.max_speed_mps,
                fleet=fl,
            ))

    # 총 AGV 수 = 각 fleet.count 합
    expected_total = sum(f.count for f in fleets)
    assert_eq("AGV 총 수 = fleet.count 합", len(agvs), expected_total)

    # fleet 별 AGV 수 = fleet.count
    for fl in fleets:
        per_fleet = [a for a in agvs if a.fleet and a.fleet.id == fl.id]
        assert_eq(f"{fl.id} AGV 수", len(per_fleet), fl.count)

    # capability 매칭: 각 demand 의 required_capability 로 dispatchable AGV 필터
    demands = data.get("demands", [])
    for d in demands:
        req = d.get("required_capability")
        if req is None:
            continue
        matching = [
            a for a in agvs
            if a.fleet and req in (a.fleet.capabilities or [])
        ]
        # 매칭 AGV 는 정확히 한 fleet (capability disjoint) 의 count 만큼
        matched_fleet = next(f for f in fleets if req in f.capabilities)
        assert_eq(
            f"req={req} dispatchable AGV 수 = {matched_fleet.id}.count",
            len(matching),
            matched_fleet.count,
        )
        # 모든 매칭 AGV 가 동일 fleet 소속
        assert_eq(
            f"req={req} 매칭 AGV 모두 {matched_fleet.id}",
            {a.fleet.id for a in matching},
            {matched_fleet.id},
        )


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_edge_graph_idx_default_zero,
        test_edge_graph_idx_explicit,
        test_fleet_class_defaults,
        test_fleet_class_full,
        test_router_filters_lanes_by_fleet,
        test_router_fleet1_uses_graph1_only,
        test_router_no_fleet_uses_all_lanes,
        test_shared_vertex_two_fleets,
        test_reservation_fifo_normal_case,
        test_reservation_priority_tiebreaker,
        test_reservation_same_priority_deterministic,
        test_demand_required_capability_none_legacy,
        test_demand_capability_match,
        test_agv_fleet_field,
        test_synthetic_3fleet_generator_produces_valid_json,
        test_synthetic_3fleet_fleet_graph_isolation,
        test_synthetic_3fleet_capability_matching_unique,
        test_synthetic_3fleet_demand_reachable_by_matching_fleet,
        test_synthetic_3fleet_demand_unreachable_by_other_fleets,
        test_synthetic_3fleet_fleet_kpi_attribution_per_agv,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    if failed > 0:
        sys.exit(1)
