"""
F1a 다중 fleet 기능 테스트.
- Graph isolation (graph_idx)
- Capability matching
- Fleet priority reservation tiebreaker

실행: python -m pytest tests/unit/test_f1a_multi_fleet.py -v
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.domain.fleet import Fleet
from src.domain.map.graph import MapGraph, NodeRole, Edge, Node
from src.domain.reservation.scheduler import TimeWindowScheduler, SIMULTANEOUS_RESERVATION_THRESHOLD_S
from src.application.scenario.demand import TaskDemand
from src.adapters.bus.adapters import LocalMemoryBus
from src.domain.agv.agv import AGV


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
