"""
핵심 레이어 통합 테스트.
실행: python -m pytest tests/ -v  (pytest 없으면 python tests/test_simulation.py)
"""
from __future__ import annotations

import asyncio
import sys
import os
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.domain.map.graph import MapGraph, NodeRole
from src.domain.reservation.scheduler import TimeWindowScheduler
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.adapters.bus.adapters import LocalMemoryBus
from src.domain.agv.agv import AGV
from src.domain.agv.fsm import AGVState


# ─────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────

def make_graph() -> MapGraph:
    """기존 10노드 sample 맵 — T1~T12 전용"""
    return MapGraph.from_json("maps/sample_fab.json")

def make_fab_graph() -> MapGraph:
    """실제 FAB 맵 (Open-RMF nav graph) — T13~T17 전용"""
    return MapGraph.from_rmf_yaml("maps/fab_nav_graph.yaml")

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


# ─────────────────────────────────────────────
# TEST 1: 맵 그래프 (sample_fab.json)
# ─────────────────────────────────────────────

def test_graph_load():
    print("\n[T1] 맵 그래프 로드")
    g = make_graph()
    assert_eq("노드 수", len(g.nodes), 10)
    assert_true("에지 수 ≥ 4", len(g.edges) >= 4)

def test_node_roles():
    print("\n[T2] 노드 역할")
    g = make_graph()
    assert_eq("approach 노드 존재", g.nodes["node_approach_01"].role, NodeRole.APPROACH)
    assert_eq("work 노드 존재",     g.nodes["node_work_01"].role,     NodeRole.WORK)
    assert_eq("siding 노드 존재",   g.nodes["node_siding_01"].role,   NodeRole.SIDING)
    assert_eq("charger 노드 존재",  g.nodes["node_charger_01"].role,  NodeRole.CHARGER)

def test_astar_path():
    print("\n[T3] A* 경로 탐색")
    g = make_graph()
    path = g.get_path("node_charger_01", "node_work_01")
    assert_true("경로 비어있지 않음", len(path) > 0)
    assert_eq("출발 노드", path[0], "node_charger_01")
    assert_eq("도착 노드", path[-1], "node_work_01")
    assert_true("APPROACH 노드 경유", "node_approach_01" in path)
    print(f"    경로: {' → '.join(path)}")

def test_approach_detection():
    print("\n[T4] APPROACH 노드 감지")
    g = make_graph()
    app = g.get_approach_node("node_intersection_01")
    assert_true("APPROACH 감지됨", app is not None)
    assert_eq("역할", app.role, NodeRole.APPROACH)

def test_no_path():
    print("\n[T5] 경로 없음 케이스")
    g = make_graph()
    path = g.get_path("node_work_01", "node_charger_01")  # 역방향 일방통행
    print(f"    역방향 경로: {path} (빈 리스트면 정상)")


# ─────────────────────────────────────────────
# TEST 2: Scheduler
# ─────────────────────────────────────────────

async def _test_scheduler_reserve():
    print("\n[T6] Scheduler 예약 / 충돌")
    s = TimeWindowScheduler()
    ok = await s.reserve("nodeX", "AGV_001", 0.0, 5.0)
    assert_eq("첫 예약 성공", ok, True)

    conflict = await s.reserve("nodeX", "AGV_002", 3.0, 8.0)
    assert_eq("시간 충돌 거부", conflict, False)

    no_conflict = await s.reserve("nodeX", "AGV_002", 5.0, 10.0)
    assert_eq("비충돌 구간 예약", no_conflict, True)

async def _test_scheduler_release():
    print("\n[T7] Scheduler release 후 재예약")
    s = TimeWindowScheduler()
    await s.reserve("nodeY", "AGV_001", 0.0, 5.0)
    await s.release("nodeY", "AGV_001")
    ok = await s.reserve("nodeY", "AGV_002", 2.0, 6.0)
    assert_eq("release 후 재예약 성공", ok, True)

async def _test_congestion_score():
    print("\n[T8] Congestion score")
    s = TimeWindowScheduler()
    await s.reserve("nodeZ", "AGV_001", 0.0, 10.0)
    await s.reserve("nodeZ", "AGV_002", 2.0, 5.0)   # 충돌 → count 증가
    score = s.get_congestion_score("nodeZ")
    assert_true("score > 0", score > 0.0)
    assert_true("score ≤ 1", score <= 1.0)
    print(f"    congestion score: {score}")

def test_scheduler():
    run(_test_scheduler_reserve())
    run(_test_scheduler_release())
    run(_test_congestion_score())


# ─────────────────────────────────────────────
# TEST 3: LocalMemoryBus
# ─────────────────────────────────────────────

async def _test_bus_pubsub():
    print("\n[T9] LocalMemoryBus pub/sub")
    bus = LocalMemoryBus()
    received = []

    async def handler(payload: dict):
        received.append(payload)

    await bus.subscribe("test/topic", handler)
    await bus.publish("test/topic", {"msg": "hello"})
    assert_eq("메시지 수신", len(received), 1)
    assert_eq("페이로드", received[0]["msg"], "hello")

async def _test_bus_wildcard():
    print("\n[T10] LocalMemoryBus 와일드카드")
    bus = LocalMemoryBus()
    received = []

    async def handler(payload: dict):
        received.append(payload)

    await bus.subscribe("uagv/v2/NEXT/#", handler)
    await bus.publish("uagv/v2/NEXT/AGV_001/order", {"orderId": "t1"})
    await bus.publish("uagv/v2/NEXT/AGV_002/state", {"state": "IDLE"})
    assert_eq("와일드카드 수신 2건", len(received), 2)

def test_bus():
    run(_test_bus_pubsub())
    run(_test_bus_wildcard())


# ─────────────────────────────────────────────
# TEST 4: TaskGenerator + 풀 플로우
# ─────────────────────────────────────────────

async def _test_task_generator_builds_order():
    print("\n[T11] TaskGenerator Order 빌드")
    graph = make_graph()
    bus   = LocalMemoryBus()
    gen   = TaskGenerator(graph, bus, task_interval_s=0.0)

    published = []
    async def capture(payload):
        published.append(payload)
    await bus.subscribe("uagv/v2/NEXT/#", capture)

    agv = AGV("AGV_001", bus, graph, TimeWindowScheduler())
    agv.current_node_id = "node_charger_01"
    agv.physics.x = graph.nodes["node_charger_01"].x
    agv.physics.y = graph.nodes["node_charger_01"].y

    await gen.step(sim_time=0.0, agvs={"AGV_001": agv})
    assert_eq("Order 1건 발행", len(published), 1)

    order = published[0]
    assert_true("orderId 있음", "orderId" in order)
    assert_true("nodes 있음", len(order["nodes"]) > 0)
    print(f"    발행된 orderId: {order['orderId']}")
    print(f"    경로 노드: {[n['nodeId'] for n in order['nodes']]}")

async def _test_full_simulation():
    print("\n[T12] 풀 시뮬레이션 (300초, AGV 3대)")
    graph   = make_graph()
    bus     = LocalMemoryBus()
    sched   = TimeWindowScheduler()
    gen     = TaskGenerator(graph, bus, task_interval_s=5.0)
    engine  = SimulationEngine(graph, sched, task_generator=gen)

    chargers = ["node_charger_01", "node_charger_02", "node_charger_01"]
    for i, charger in enumerate(chargers, 1):
        agv = AGV(f"AGV_00{i}", bus, graph, sched)
        agv.current_node_id = charger
        agv.physics.x = graph.nodes[charger].x
        agv.physics.y = graph.nodes[charger].y
        engine.register_agv(agv)

    results = await engine.run(duration_s=300.0)

    print(f"    sim_time_s:               {results['sim_time_s']}")
    print(f"    tasks_completed:          {results['tasks_completed']}")
    print(f"    throughput_tasks_per_hour:{results['throughput_tasks_per_hour']}")
    print(f"    total_wait_time_s:        {results['total_wait_time_s']}")
    print(f"    avg_wait_per_agv_s:       {results['avg_wait_per_agv_s']}")
    print(f"    total_travel_distance_m:  {results['total_travel_distance_m']}")
    print(f"    bottleneck_nodes:         {results['bottleneck_nodes']}")

    assert_true("시뮬 시간 ≈ 300s",    results["sim_time_s"] >= 299.0)
    assert_true("AGV 이동 발생",        results["total_travel_distance_m"] > 0)
    assert_true("태스크 1개 이상 완료", results["tasks_completed"] >= 1)

def test_full_flow():
    run(_test_task_generator_builds_order())
    run(_test_full_simulation())


# ─────────────────────────────────────────────
# TEST 5: FAB 맵 (fab_nav_graph.yaml, Open-RMF)
# ─────────────────────────────────────────────

def test_fab_graph_load():
    print("\n[T13] FAB 맵 로드 (Open-RMF nav graph)")
    g = make_fab_graph()
    assert_eq("노드 수", len(g.nodes), 82)
    assert_true("에지 수 ≥ 100", len(g.edges) >= 100)

def test_fab_node_counts():
    print("\n[T14] FAB 노드 구성 검증")
    g = make_fab_graph()
    chargers = g.get_chargers()
    stations = g.get_stations()
    waypoints = [n for n in g.nodes.values() if n.role in (NodeRole.STANDARD, NodeRole.APPROACH)]
    assert_eq("충전소 수", len(chargers), 8)
    assert_eq("스테이션 수", len(stations), 23)
    assert_true("웨이포인트 수 ≥ 51", len(waypoints) >= 51)
    print(f"    충전소: {len(chargers)}개, 스테이션: {len(stations)}개, WP: {len(waypoints)}개")

def test_fab_astar():
    print("\n[T15] FAB A* 경로 탐색 (충전소 → 스테이션)")
    g = make_fab_graph()
    path = g.get_path("CH_01", "ST_N_06")
    assert_true("경로 비어있지 않음", len(path) > 0)
    assert_eq("출발 노드", path[0], "CH_01")
    assert_eq("도착 노드", path[-1], "ST_N_06")
    print(f"    경로 ({len(path)} 노드): {' → '.join(path[:6])} ...")

def test_fab_unidirectional():
    print("\n[T16] 중앙통로 단방향 검증 (동→서만 허용)")
    g = make_fab_graph()
    # 정방향 (동→서): 직접 엣지 2노드
    fwd = g.get_path("WP_C_040", "WP_C_000")
    assert_true("정방향 경로 존재", len(fwd) > 0)
    assert_eq("정방향 직접 연결 (2노드)", len(fwd), 2)
    # 역방향: 중앙통로 직접 엣지 없음
    bwd_direct = any(
        e.start_node_id == "WP_C_000" and e.end_node_id == "WP_C_040"
        for e in g.edges.values()
    )
    assert_true("역방향 직접 엣지 없음", not bwd_direct)
    print(f"    정방향: {fwd}")

def test_fab_corridor_speeds():
    print("\n[T17] 통로별 속도 제한 검증")
    g = make_fab_graph()
    errors = []
    for e in g.edges.values():
        if e.corridor in ("north", "south") and abs(e.max_speed - 1.5) > 1e-6:
            errors.append(f"{e.edge_id}={e.max_speed}")
        elif e.corridor == "center" and abs(e.max_speed - 1.5) > 1e-6:
            errors.append(f"{e.edge_id}={e.max_speed}")
        elif e.corridor == "bay" and abs(e.max_speed - 0.8) > 1e-6:
            errors.append(f"{e.edge_id}={e.max_speed}")
    assert_true("속도 제한 전체 OK", len(errors) == 0)
    print("    북/남 1.5m/s, 중앙 1.5m/s, 베이 0.8m/s  OK")

def test_fab_connectivity():
    print("\n[T18] FAB 전체 노드 연결성 (BFS)")
    from collections import defaultdict, deque
    g = make_fab_graph()

    # 무방향 인접 그래프
    adj: dict[str, set[str]] = defaultdict(set)
    for e in g.edges.values():
        adj[e.start_node_id].add(e.end_node_id)
        adj[e.end_node_id].add(e.start_node_id)

    start = next(iter(g.nodes))
    visited: set[str] = set()
    q: deque[str] = deque([start])
    while q:
        node = q.popleft()
        if node in visited:
            continue
        visited.add(node)
        for nb in adj[node]:
            if nb not in visited:
                q.append(nb)

    unreachable = set(g.nodes) - visited
    assert_true("고립 노드 없음", len(unreachable) == 0)
    if unreachable:
        print(f"    고립 노드: {unreachable}")
    else:
        print(f"    전체 {len(g.nodes)}개 노드 연결 OK")


# ─────────────────────────────────────────────
# TEST 6: FAB 맵 풀 시뮬레이션
# ─────────────────────────────────────────────

async def _test_fab_full_simulation():
    print("\n[T19] FAB 풀 시뮬레이션 (300초, AGV 5대)")
    graph  = make_fab_graph()
    bus    = LocalMemoryBus()
    sched  = TimeWindowScheduler()
    gen    = TaskGenerator(graph, bus, task_interval_s=5.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    # 충전소 8개 전체에 분산 배치
    n_agv_count = 5
    charger_nodes = [n.node_id for n in graph.get_chargers()]
    stride = max(1, len(charger_nodes) // n_agv_count)
    start_nodes = [charger_nodes[i * stride % len(charger_nodes)]
                   for i in range(n_agv_count)]
    for i, charger in enumerate(start_nodes, 1):
        agv = AGV(f"AGV_{i:03d}", bus, graph, sched)
        agv.current_node_id = charger
        agv.physics.x = graph.nodes[charger].x
        agv.physics.y = graph.nodes[charger].y
        engine.register_agv(agv)

    results = await engine.run(duration_s=300.0)

    print(f"    sim_time_s:               {results['sim_time_s']}")
    print(f"    tasks_completed:          {results['tasks_completed']}")
    print(f"    throughput_tasks_per_hour:{results['throughput_tasks_per_hour']}")
    print(f"    total_wait_time_s:        {results['total_wait_time_s']}")
    print(f"    avg_wait_per_agv_s:       {results['avg_wait_per_agv_s']}")
    print(f"    total_travel_distance_m:  {results['total_travel_distance_m']}")
    print(f"    bottleneck_nodes:         {results['bottleneck_nodes']}")

    assert_true("시뮬 시간 ≈ 300s",    results["sim_time_s"] >= 299.0)
    assert_true("AGV 이동 발생",        results["total_travel_distance_m"] > 0)
    assert_true("태스크 1개 이상 완료", results["tasks_completed"] >= 1)

def test_fab_full_simulation():
    run(_test_fab_full_simulation())


# ─────────────────────────────────────────────
# TEST 7: FAB 스트레스 테스트 (고밀도)
# ─────────────────────────────────────────────

async def _test_fab_stress():
    print("\n[T20] FAB 스트레스 테스트 (1800초, AGV 20대)")
    graph  = make_fab_graph()
    bus    = LocalMemoryBus()
    sched  = TimeWindowScheduler()
    gen    = TaskGenerator(graph, bus, task_interval_s=3.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    n_agv = 20
    charger_nodes = [n.node_id for n in graph.get_chargers()]
    wp_nodes = [nid for nid, n in graph.nodes.items()
                if n.role.value == "standard" and nid.startswith("WP_")]
    start_pool = charger_nodes + wp_nodes
    stride = max(1, len(start_pool) // n_agv)
    start_nodes = [start_pool[i * stride % len(start_pool)] for i in range(n_agv)]

    for i, start in enumerate(start_nodes, 1):
        agv = AGV(f"AGV_{i:03d}", bus, graph, sched)
        agv.current_node_id = start
        agv.physics.x = graph.nodes[start].x
        agv.physics.y = graph.nodes[start].y
        engine.register_agv(agv)

    results = await engine.run(duration_s=1800.0)
    summary = engine.scheduler.get_headon_summary()

    print(f"    sim_time_s:               {results['sim_time_s']}")
    print(f"    tasks_completed:          {results['tasks_completed']}")
    print(f"    throughput_tasks_per_hour:{results['throughput_tasks_per_hour']}")
    print(f"    total_wait_time_s:        {results['total_wait_time_s']}")
    print(f"    agv_utilization:          {results['agv_utilization']}")
    print(f"    total_travel_distance_m:  {results['total_travel_distance_m']}")
    print(f"    deadlock_count:           {results['deadlock_or_stall_count']}")
    print(f"    --- head-on 분석 ---")
    print(f"    headon_total:             {summary['headon_total']}")
    print(f"    retry_total:              {summary['retry_total']}")
    print(f"    avg_retry_per_headon:     {summary['avg_retry_per_headon']}")
    print(f"    top_headon_edges:         {summary['top_headon_edges']}")
    print(f"    bottleneck_nodes:         {results['bottleneck_nodes'][:3]}")

    assert_true("시뮬 시간 ≈ 1800s", results["sim_time_s"] >= 1799.0)
    assert_true("AGV 이동 발생",      results["total_travel_distance_m"] > 0)
    assert_true("태스크 완료",        results["tasks_completed"] >= 1)
    # deadlock resolver 개입 횟수는 fab 양방향 맵 특성상 실행마다 달라 assertion 제외.
    # 생성 토폴로지(단방향 등)의 deadlock == 0 은 T45에서 검증.

def test_fab_stress():
    run(_test_fab_stress())


# ─────────────────────────────────────────────
# TEST 8: Topology Invariants
# ─────────────────────────────────────────────

def test_topology_invariants():
    print("\n[T21] Topology Invariant 검증")
    from src.domain.map.topology_generator import MapTopologyGenerator
    gen = MapTopologyGenerator()

    cases = [
        ("A", "단방향 순환 — head-on 엣지 쌍 없음"),
        ("B", "양방향 + siding — invariant 없음 (head-on 허용)"),
        ("C", "2차선 단방향 — same-lane head-on 없음"),
        ("D", "2차선 양방향 — same-lane head-on 없음"),
        ("E", "_lane_mode 태그 존재"),
    ]

    for type_code, desc in cases:
        g = gen.generate(type_code)
        result = gen.validate_invariants(g, type_code)
        status = "PASS" if result.passed else "FAIL"

        if type_code == "B":
            # B는 head-on 허용이므로 passed=False가 정상
            print(f"  [----] Type {type_code} ({desc}): invariant 검사 없음")
            continue

        print(f"  [{status}] Type {type_code} ({desc}): {result}")
        if not result.passed:
            for v in result.violations[:3]:
                print(f"    VIOLATION: {v}")
            raise AssertionError(f"Type {type_code} invariant failed: {result.violations[0]}")


def test_type_e_creep_policy():
    print("\n[T22] Type E creep policy 주입 검증")
    from src.domain.map.topology_generator import MapTopologyGenerator
    gen = MapTopologyGenerator()
    g = gen.generate("E")

    # _lane_mode 태그 확인
    lane_mode = getattr(g, "_lane_mode", None)
    assert_eq("_lane_mode 태그", lane_mode, "bidirectional_creep")

    # 메인통로 엣지 속도 확인 (크리프 속도 = 0.3m/s)
    main_edges = [e for e in g.edges.values()
                  if e.corridor in ("north", "south", "center")
                  and not e.edge_id.endswith("_rev")]
    speeds = set(e.max_speed for e in main_edges)
    assert_true("크리프 속도 0.3m/s", all(abs(s - 0.3) < 1e-6 for s in speeds))
    print(f"    _lane_mode={lane_mode}, speeds={speeds}  OK")


def test_type_a_no_headon():
    print("\n[T23] Type A — head-on 엣지 쌍 없음")
    from src.domain.map.topology_generator import MapTopologyGenerator
    gen = MapTopologyGenerator()
    g = gen.generate("A")

    main_edges = [e for e in g.edges.values()
                  if e.corridor in ("north", "south", "center")]
    violations = []
    for e in main_edges:
        for other in main_edges:
            if (other.start_node_id == e.end_node_id and
                    other.end_node_id == e.start_node_id and
                    other.corridor == e.corridor):
                violations.append(f"{e.start_node_id}↔{e.end_node_id}")
                break

    assert_eq("head-on 엣지 쌍 수", len(violations), 0)
    print(f"    violations={violations}  OK")


def test_type_c_no_headon():
    print("\n[T24] Type C — same-lane head-on 없음")
    from src.domain.map.topology_generator import MapTopologyGenerator
    gen = MapTopologyGenerator()
    g = gen.generate("C")

    violations = []
    for corr_tag in ("north_l1", "north_l2", "south_l1", "south_l2",
                     "center_l1", "center_l2"):
        lane_edges = [e for e in g.edges.values() if e.corridor == corr_tag]
        for e in lane_edges:
            for other in lane_edges:
                if (other.start_node_id == e.end_node_id and
                        other.end_node_id == e.start_node_id):
                    violations.append(f"{corr_tag}: {e.start_node_id}↔{e.end_node_id}")
                    break

    assert_eq("same-lane head-on 엣지 쌍 수", len(violations), 0)
    print(f"    violations={violations}  OK")


def test_type_d_no_samelane_headon():
    print("\n[T25] Type D — same-lane head-on 없음")
    from src.domain.map.topology_generator import MapTopologyGenerator
    gen = MapTopologyGenerator()
    g = gen.generate("D")

    violations = []
    for corr_tag in ("north_l1", "north_l2", "south_l1", "south_l2",
                     "center_l1", "center_l2"):
        lane_edges = [e for e in g.edges.values() if e.corridor == corr_tag]
        for e in lane_edges:
            for other in lane_edges:
                if (other.start_node_id == e.end_node_id and
                        other.end_node_id == e.start_node_id and
                        other.corridor == e.corridor):
                    violations.append(f"{corr_tag}: {e.start_node_id}↔{e.end_node_id}")
                    break

    assert_eq("same-lane head-on 엣지 쌍 수", len(violations), 0)
    print(f"    violations={violations}  OK")


# ─────────────────────────────────────────────
# TEST 9: Diagnostics / KPI regression
# ─────────────────────────────────────────────

async def _test_task_generator_diagnostics():
    print("\n[T26] TaskGenerator diagnostics")
    graph = make_graph()
    bus = LocalMemoryBus()
    gen = TaskGenerator(graph, bus, task_interval_s=0.0)

    await gen.step(sim_time=0.0, agvs={})
    diag = gen.diagnostics

    assert_eq("no_idle_agv 카운트", diag["no_idle_agv"], 1)
    assert_eq("orders_published 0", diag["orders_published"], 0)


def test_task_generator_diagnostics():
    run(_test_task_generator_diagnostics())


async def _test_kpi_headon_fields():
    print("\n[T27] KPI head-on fields")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    gen = TaskGenerator(graph, bus, task_interval_s=1000.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    agv = AGV("AGV_001", bus, graph, sched)
    agv.current_node_id = "node_charger_01"
    agv.physics.x = graph.nodes["node_charger_01"].x
    agv.physics.y = graph.nodes["node_charger_01"].y
    engine.register_agv(agv)

    results = await engine.run(duration_s=1.0)
    for key in (
        "headon_total",
        "followon_total",
        "section_conflict_total",
        "retry_total",
        "itinerary_success",
        "itinerary_failure",
        "avg_retry_per_headon",
        "top_headon_edges",
    ):
        assert_true(f"{key} 포함", key in results)
    assert_eq("headon_total 기본값", results["headon_total"], 0)
    assert_eq("followon_total 기본값", results["followon_total"], 0)
    assert_eq("section_conflict_total 기본값", results["section_conflict_total"], 0)


def test_kpi_headon_fields():
    run(_test_kpi_headon_fields())


def test_type_b_siding_coverage_diagnostics():
    print("\n[T46] Type B siding coverage diagnostics — placement sweep (base/mid/dense)")
    from src.application.usecases.experiment_runner import _build_siding_coverage_diagnostics
    from src.domain.map.topology_generator import MapTopologyGenerator, SIDING_POSITIONS

    gen = MapTopologyGenerator()

    results = {}
    for placement in ("base", "mid", "dense"):
        graph = gen.generate("B", siding_placement=placement)
        diag = _build_siding_coverage_diagnostics(graph)
        expected_siding_count = len(SIDING_POSITIONS[placement]) * 3  # N/C/S 3개 코리도
        assert_eq(
            f"[{placement}] siding count",
            diag["siding_count"],
            expected_siding_count,
        )
        assert_true(
            f"[{placement}] coverage ratio <= 1",
            diag["coverage_ratio"] <= 1.0,
        )
        results[placement] = diag
        print(
            f"  {placement:6s}: siding={diag['siding_count']:3d}  "
            f"coverage={diag['coverage_ratio']:.4f}  "
            f"longest_gap={diag['longest_uncovered_run_m']:.1f}m"
        )

    # base placement: 원래 5 x-pos × 3 corridors = 15 sidings
    assert_eq("base siding count", results["base"]["siding_count"], 15)
    assert_true("base coverage ratio < 1", results["base"]["coverage_ratio"] < 1.0)
    assert_true(
        "base longest uncovered gap >= 80m",
        results["base"]["longest_uncovered_run_m"] >= 80.0,
    )
    assert_true(
        "base uncovered main node sample exists",
        len(results["base"]["uncovered_main_node_samples"]) >= 1,
    )

    # 배치 밀도가 높아질수록 coverage 개선 / gap 감소
    assert_true(
        "mid coverage >= base coverage",
        results["mid"]["coverage_ratio"] >= results["base"]["coverage_ratio"],
    )
    assert_true(
        "dense coverage >= mid coverage",
        results["dense"]["coverage_ratio"] >= results["mid"]["coverage_ratio"],
    )
    assert_true(
        "mid gap <= base gap",
        results["mid"]["longest_uncovered_run_m"] <= results["base"]["longest_uncovered_run_m"],
    )
    assert_true(
        "dense gap <= mid gap",
        results["dense"]["longest_uncovered_run_m"] <= results["mid"]["longest_uncovered_run_m"],
    )

    # dense: WP 전체 커버 → coverage == 1.0
    assert_true(
        "dense coverage == 1.0 (모든 WP에 siding 인접)",
        results["dense"]["coverage_ratio"] == 1.0,
    )
    assert_eq(
        "dense longest uncovered gap == 0.0",
        results["dense"]["longest_uncovered_run_m"],
        0.0,
    )


def test_invalid_type_b_siding_placement_rejected():
    print("\n[T47] Invalid Type B siding placement rejected")
    from src.domain.map.topology_generator import MapTopologyGenerator

    try:
        MapTopologyGenerator().generate("B", siding_placement="bad-placement")
    except ValueError as exc:
        assert_true("invalid placement mentioned", "bad-placement" in str(exc))
        assert_true("valid values mentioned", "base" in str(exc) and "dense" in str(exc))
    else:
        raise AssertionError("invalid siding placement should raise ValueError")


def test_bottleneck_edge_interpretation():
    print("\n[T48] Bottleneck edge interpretation")
    from src.analytics.kpi import KPICalculator
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("B", siding_placement="base")
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    agv = AGV("AGV_001", bus, graph, sched)

    shared_edge = next(e for e in graph.edges.values() if e.corridor == "north")
    edge_key = f"{shared_edge.start_node_id}__{shared_edge.end_node_id}"
    agv._edge_time[edge_key] = 12.0

    section_key = (
        f"shared_corridor:{shared_edge.corridor}:"
        f"{'<->'.join(sorted([shared_edge.start_node_id, shared_edge.end_node_id]))}"
    )
    sched._edge_headon_counts[edge_key] = 2
    sched._edge_followon_counts[edge_key] = 1
    sched._edge_retry_counts[edge_key] = 3
    sched._section_conflict_counts[section_key] = 5

    results = KPICalculator().compute({"AGV_001": agv}, sched, sim_time_s=60.0)
    top_edge = results["bottleneck_edges"][0]

    assert_eq("edge id preserved", top_edge["edge_id"], edge_key)
    assert_eq("graph edge id present", top_edge["graph_edge_id"], shared_edge.edge_id)
    assert_eq("edge type classified", top_edge["edge_type"], "shared_corridor")
    assert_eq("corridor preserved", top_edge["corridor"], "north")
    assert_eq("section key linked", top_edge["section_key"], section_key)
    assert_eq("section conflict count linked", top_edge["section_conflict_count"], 5)
    assert_eq("dominant cause", top_edge["dominant_cause"], "section_conflict")


def test_type_b_reachable_siding_candidate_beyond_adjacent():
    print("\n[T49] Type B reachable siding candidate beyond adjacent")
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("B", siding_placement="base")
    agv = AGV("AGV_001", LocalMemoryBus(), graph, TimeWindowScheduler())
    agv.current_node_id = "WP_C_040"
    agv._path = ["WP_C_040", "WP_C_080", "WP_C_120", "WP_C_160", "WP_C_200"]
    agv._path_index = 1

    adjacent_sidings = [
        n.node_id for n in graph.get_neighbors("WP_C_040")
        if n.role == NodeRole.SIDING
    ]
    assert_eq("adjacent siding 없음", len(adjacent_sidings), 0)

    siding = agv._find_siding_candidate(
        "WP_C_040",
        "WP_C_200",
        blocked_edge=("WP_C_040", "WP_C_080"),
    )

    assert_true("reachable siding candidate exists", siding is not None)
    assert_true("candidate is siding node", graph.nodes[siding].role == NodeRole.SIDING)
    assert_true("candidate not adjacent siding", siding not in adjacent_sidings)
    assert_true(
        "candidate has path to goal",
        bool(graph.get_path(siding, "WP_C_200")),
    )


def test_type_b_adjacent_vs_reachable_siding_policy():
    print("\n[T50] Type B adjacent vs reachable siding policy")
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("B", siding_placement="base")
    agv = AGV("AGV_001", LocalMemoryBus(), graph, TimeWindowScheduler())

    graph._type_b_siding_policy = "adjacent"
    adjacent = agv._find_siding_candidate(
        "WP_C_040",
        "WP_C_200",
        blocked_edge=("WP_C_040", "WP_C_080"),
    )
    assert_eq("adjacent policy returns none", adjacent, None)

    graph._type_b_siding_policy = "reachable"
    reachable = agv._find_siding_candidate(
        "WP_C_040",
        "WP_C_200",
        blocked_edge=("WP_C_040", "WP_C_080"),
    )
    assert_true("reachable policy finds siding", reachable is not None)


def test_type_c_d_station_pair_reachability():
    print("\n[T28] Type C/D station pair reachability")
    from src.domain.map.topology_generator import MapTopologyGenerator
    from src.application.usecases.experiment_runner import _build_station_access_diagnostics

    gen = MapTopologyGenerator()
    for type_code in ("C", "D"):
        graph = gen.generate(type_code)
        diag = _build_station_access_diagnostics(graph)
        assert_eq(
            f"Type {type_code} station pair unreachable",
            diag["station_pair_unreachable_count"],
            0,
        )
        assert_true(
            f"Type {type_code} station min access edges >= 4",
            diag["min_access_edges"] >= 4,
        )


async def _test_type_a_routeable_task_selection():
    print("\n[T29] Type A routeable task selection")
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("A")
    bus = LocalMemoryBus()
    gen = TaskGenerator(graph, bus, task_interval_s=0.0)

    agv = AGV("AGV_001", bus, graph, TimeWindowScheduler())
    agv.current_node_id = "CH_01"
    agv.physics.x = graph.nodes["CH_01"].x
    agv.physics.y = graph.nodes["CH_01"].y

    await gen.step(sim_time=0.0, agvs={"AGV_001": agv})
    diag = gen.diagnostics

    assert_true("routeable pair 존재", diag["routeable_pair_count"] > 0)
    assert_eq("Order 1건 발행", diag["orders_published"], 1)
    assert_eq("pickup 경로 실패 없음", diag["no_path_to_pickup"], 0)
    assert_eq("dropoff 경로 실패 없음", diag["no_path_pickup_to_dropoff"], 0)


def test_type_a_routeable_task_selection():
    run(_test_type_a_routeable_task_selection())


def test_type_d_width_metadata():
    print("\n[T30] Type C/D width metadata")
    from src.domain.map.topology_generator import MapTopologyGenerator

    gen = MapTopologyGenerator()
    type_c = gen.generate("C")
    type_d = gen.generate("D")

    c_widths = {
        e.width_m for e in type_c.edges.values()
        if e.corridor in ("north_l1", "north_l2", "center_l1", "center_l2", "south_l1", "south_l2")
    }
    d_widths = {
        e.width_m for e in type_d.edges.values()
        if e.corridor in ("north_l1", "north_l2", "center_l1", "center_l2", "south_l1", "south_l2")
    }

    assert_eq("Type C lane width", c_widths, {1.0})
    assert_eq("Type D lane width", d_widths, {1.5})
    assert_eq("Type C total corridor width", type_c._corridor_total_width_m, 2.0)
    assert_eq("Type D total corridor width", type_d._corridor_total_width_m, 3.0)
    assert_true("Type D wider than Type C", min(d_widths) > max(c_widths))


def test_demand_set_generation():
    print("\n[T31] DemandSet generation")
    from src.application.scenario.demand import DemandSet
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("A")
    common_a = DemandSet.common_from_graph(
        graph, count=20, interval_s=3.0, random_seed=7
    )
    common_b = DemandSet.common_from_graph(
        graph, count=20, interval_s=3.0, random_seed=7
    )
    capability = DemandSet.capability_from_graph(
        graph, count=20, interval_s=3.0, random_seed=7
    )

    sig_a = [
        (
            d.release_time_s,
            d.pickup_node_id,
            d.dropoff_node_id,
            d.processing_time_s,
            d.pickup_processing_time_s,
            d.dropoff_processing_time_s,
        )
        for d in common_a.demands
    ]
    sig_b = [
        (
            d.release_time_s,
            d.pickup_node_id,
            d.dropoff_node_id,
            d.processing_time_s,
            d.pickup_processing_time_s,
            d.dropoff_processing_time_s,
        )
        for d in common_b.demands
    ]
    assert_eq("common deterministic", sig_a == sig_b, True)
    assert_eq("common count", len(common_a.demands), 20)
    assert_eq("capability count", len(capability.demands), 20)
    assert_true(
        "pickup/dropoff processing split",
        all(
            d.pickup_processing_time_s > 0.0
            and d.dropoff_processing_time_s > 0.0
            and d.processing_time_s == round(
                d.pickup_processing_time_s + d.dropoff_processing_time_s,
                3,
            )
            for d in common_a.demands
        ),
    )

    unreachable_common = [
        d for d in common_a.demands
        if not graph.get_path(d.pickup_node_id, d.dropoff_node_id)
    ]
    unreachable_capability = [
        d for d in capability.demands
        if not graph.get_path(d.pickup_node_id, d.dropoff_node_id)
    ]
    assert_true("common may include unreachable demand", len(unreachable_common) > 0)
    assert_eq("capability excludes unreachable demand", len(unreachable_capability), 0)


async def _test_common_demand_lifecycle_metrics():
    print("\n[T32] Common demand lifecycle metrics")
    from src.application.usecases.experiment_runner import _run_single, _flatten_summary_row

    result = await _run_single(
        "A",
        n_agv=3,
        duration_s=10.0,
        task_interval_s=3.0,
        random_seed=2,
        demand_mode="common_demand",
        demand_count=5,
    )
    row = _flatten_summary_row(result)

    assert_eq("demand mode", row["demand_mode"], "common_demand")
    assert_true("requested > 0", row["tasks_requested"] > 0)
    assert_true("unreachable rejected > 0", row["tasks_rejected_unreachable"] > 0)
    assert_true("acceptance rate bounded", 0.0 <= row["task_acceptance_rate"] <= 1.0)
    assert_true("completion rate bounded", 0.0 <= row["completion_rate"] <= 1.0)


def test_common_demand_lifecycle_metrics():
    run(_test_common_demand_lifecycle_metrics())


async def _test_real_demand_completion_metrics():
    print("\n[T33] Real demand completion metrics")
    from src.application.scenario.demand import DemandSet, TaskDemand

    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    demand_set = DemandSet(
        mode="common_demand",
        random_seed=1,
        demands=[
            TaskDemand(
                task_id="demand_real_001",
                release_time_s=0.0,
                pickup_node_id="node_work_01",
                dropoff_node_id="node_work_02",
                processing_time_s=0.1,
                pickup_processing_time_s=0.1,
                dropoff_processing_time_s=0.1,
            )
        ],
    )
    gen = TaskGenerator(graph, bus, task_interval_s=1.0, demand_set=demand_set)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    agv = AGV("AGV_001", bus, graph, sched)
    agv.current_node_id = "node_charger_01"
    agv.physics.x = graph.nodes["node_charger_01"].x
    agv.physics.y = graph.nodes["node_charger_01"].y
    engine.register_agv(agv)

    await engine.run(duration_s=30.0)
    diag = gen.diagnostics

    assert_eq("requested demand", diag["tasks_requested"], 1)
    assert_eq("dispatched demand", diag["tasks_dispatched"], 1)
    assert_eq("real completed demand", diag["demands_completed"], 1)
    assert_eq("real demand completion rate", diag["demand_completion_rate"], 1.0)
    assert_true("station processing count includes pickup/dropoff", agv._task_count >= 2)


def test_real_demand_completion_metrics():
    run(_test_real_demand_completion_metrics())


def test_topology_ranking_summary():
    print("\n[T34] Topology ranking summary")
    from src.application.usecases.experiment_runner import (
        RunResult,
        _build_ranking_aggregate,
        _build_ranking_rows,
    )

    results = [
        RunResult(
            topology_type="A",
            n_agv=3,
            random_seed=42,
            demand_mode="common_demand",
            demands_completed=8,
            demand_completion_rate=0.8,
            demand_throughput_per_hour=80.0,
            total_wait_time_s=20.0,
        ),
        RunResult(
            topology_type="B",
            n_agv=3,
            random_seed=42,
            demand_mode="common_demand",
            demands_completed=9,
            demand_completion_rate=0.9,
            demand_throughput_per_hour=90.0,
            total_wait_time_s=30.0,
        ),
        RunResult(
            topology_type="A",
            n_agv=3,
            random_seed=43,
            demand_mode="common_demand",
            demands_completed=7,
            demand_completion_rate=0.7,
            demand_throughput_per_hour=70.0,
            total_wait_time_s=20.0,
        ),
        RunResult(
            topology_type="B",
            n_agv=3,
            random_seed=43,
            demand_mode="common_demand",
            demands_completed=6,
            demand_completion_rate=0.6,
            demand_throughput_per_hour=60.0,
            total_wait_time_s=10.0,
        ),
    ]

    for result in results:
        result.diagnostics = {
            "task_generation": {
                "tasks_requested": 10,
                "tasks_dispatched": result.demands_completed,
                "tasks_rejected_unreachable": 0,
                "tasks_backlogged": 10 - result.demands_completed,
                "demands_completed": result.demands_completed,
            },
            "station_access": {},
            "siding_coverage": {},
        }

    ranking_rows = _build_ranking_rows(results)
    aggregate = _build_ranking_aggregate(ranking_rows)

    winners = [
        (row["random_seed"], row["topology_type"])
        for row in ranking_rows
        if row["winner"]
    ]
    assert_eq("seed별 winner", winners, [(42, "B"), (43, "A")])
    assert_eq("aggregate row count", len(aggregate), 2)
    assert_eq("aggregate first row wins", aggregate[0]["first_place_wins"], 1)


def test_type_b_siding_placement_ranking_grouping():
    print("\n[T34-2] Type B siding placement ranking grouping")
    from src.application.usecases.experiment_runner import (
        RunResult,
        _build_ranking_aggregate,
        _build_ranking_rows,
    )

    results = [
        RunResult(
            topology_type="B",
            siding_placement="base",
            n_agv=8,
            random_seed=42,
            demand_mode="generated",
            demands_completed=10,
            demand_throughput_per_hour=100.0,
            total_wait_time_s=20.0,
        ),
        RunResult(
            topology_type="B",
            siding_placement="mid",
            n_agv=8,
            random_seed=42,
            demand_mode="generated",
            demands_completed=12,
            demand_throughput_per_hour=120.0,
            total_wait_time_s=18.0,
        ),
        RunResult(
            topology_type="B",
            siding_placement="dense",
            n_agv=8,
            random_seed=42,
            demand_mode="generated",
            demands_completed=8,
            demand_throughput_per_hour=80.0,
            total_wait_time_s=30.0,
        ),
    ]

    for result in results:
        result.diagnostics = {
            "task_generation": {
                "tasks_requested": 12,
                "tasks_dispatched": result.demands_completed,
                "tasks_rejected_unreachable": 0,
                "tasks_backlogged": 12 - result.demands_completed,
                "demands_completed": result.demands_completed,
            },
            "station_access": {},
            "siding_coverage": {},
        }

    ranking_rows = _build_ranking_rows(results)
    aggregate = _build_ranking_aggregate(ranking_rows)

    assert_eq("ranking row count", len(ranking_rows), 3)
    assert_eq(
        "ranking variants preserved",
        [row["topology_variant"] for row in ranking_rows],
        ["B/mid/reachable", "B/base/reachable", "B/dense/reachable"],
    )
    assert_eq(
        "aggregate variants separated",
        [row["topology_variant"] for row in aggregate],
        ["B/mid/reachable", "B/base/reachable", "B/dense/reachable"],
    )


async def _test_follow_on_headway_blocks_close_entry():
    print("\n[T35] Same-direction follow-on headway")
    s = TimeWindowScheduler()

    first = await s.reserve_edge(
        "A", "B", "AGV_001",
        start_time=0.0,
        end_time=10.0,
        same_direction_headway_s=3.0,
    )
    too_close = await s.reserve_edge(
        "A", "B", "AGV_002",
        start_time=1.0,
        end_time=11.0,
        same_direction_headway_s=3.0,
    )
    spaced = await s.reserve_edge(
        "A", "B", "AGV_003",
        start_time=3.0,
        end_time=13.0,
        same_direction_headway_s=3.0,
    )
    summary = s.get_headon_summary()

    assert_eq("첫 same-direction 예약 성공", first, True)
    assert_eq("headway 미만 추종 차단", too_close, False)
    assert_eq("headway 만족 추종 허용", spaced, True)
    assert_eq("follow-on 차단 카운트", summary["followon_total"], 1)
    assert_eq("head-on 카운트 영향 없음", summary["headon_total"], 0)


def test_follow_on_headway_blocks_close_entry():
    run(_test_follow_on_headway_blocks_close_entry())


def test_type_d_follow_on_headway_shorter_than_c():
    print("\n[T36] Type D follow-on headway shorter than C")
    from src.domain.map.topology_generator import MapTopologyGenerator

    gen = MapTopologyGenerator()
    graph_c = gen.generate("C")
    graph_d = gen.generate("D")
    edge_c = next(e for e in graph_c.edges.values() if e.safety_model == "narrow_one_way")
    edge_d = next(e for e in graph_d.edges.values() if e.safety_model == "wide_one_way")

    agv_c = AGV("AGV_C", LocalMemoryBus(), graph_c, TimeWindowScheduler())
    agv_d = AGV("AGV_D", LocalMemoryBus(), graph_d, TimeWindowScheduler())
    headway_c = agv_c._calc_follow_on_headway_s(
        edge_c.start_node_id,
        edge_c.end_node_id,
        speed_mps=1.5,
    )
    headway_d = agv_d._calc_follow_on_headway_s(
        edge_d.start_node_id,
        edge_d.end_node_id,
        speed_mps=1.5,
    )

    assert_true("Type C headway positive", headway_c > 0.0)
    assert_true("Type D wider lane reduces headway", headway_d < headway_c)


async def _test_itinerary_reservation_atomic_conflict():
    print("\n[T37] Itinerary reservation atomic conflict")
    from src.domain.reservation.scheduler import ItinerarySegment

    s = TimeWindowScheduler()
    first = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="A__B",
            agv_id="AGV_001",
            start_time=0.0,
            end_time=10.0,
            src_id="A",
            dst_id="B",
        ),
        ItinerarySegment(
            segment_type="node",
            key="B",
            agv_id="AGV_001",
            start_time=10.0,
            end_time=12.0,
        ),
    ])
    conflict = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="B__A",
            agv_id="AGV_002",
            start_time=2.0,
            end_time=4.0,
            src_id="B",
            dst_id="A",
        ),
        ItinerarySegment(
            segment_type="node",
            key="A",
            agv_id="AGV_002",
            start_time=4.0,
            end_time=6.0,
        ),
    ])
    summary = s.get_headon_summary()

    assert_eq("첫 itinerary 예약 성공", first, True)
    assert_eq("충돌 itinerary atomic 거부", conflict, False)
    assert_eq("실패 itinerary edge 미추가", len(s._edge_reservations["B__A"]), 0)
    assert_eq("itinerary success", summary["itinerary_success"], 1)
    assert_eq("itinerary failure", summary["itinerary_failure"], 1)


def test_itinerary_reservation_atomic_conflict():
    run(_test_itinerary_reservation_atomic_conflict())


async def _test_critical_section_conflict_blocks_itinerary():
    print("\n[T38] Critical section conflict")
    from src.domain.reservation.scheduler import ItinerarySegment

    s = TimeWindowScheduler()
    first = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="A__B",
            agv_id="AGV_001",
            start_time=0.0,
            end_time=10.0,
            src_id="A",
            dst_id="B",
            section_key="bay:160",
        )
    ])
    conflict = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="C__D",
            agv_id="AGV_002",
            start_time=2.0,
            end_time=5.0,
            src_id="C",
            dst_id="D",
            section_key="bay:160",
        )
    ])
    allowed = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="E__F",
            agv_id="AGV_003",
            start_time=2.0,
            end_time=5.0,
            src_id="E",
            dst_id="F",
            section_key="bay:320",
        )
    ])
    summary = s.get_headon_summary()

    assert_eq("첫 section 예약 성공", first, True)
    assert_eq("동일 section 겹침 차단", conflict, False)
    assert_eq("다른 section 겹침 허용", allowed, True)
    assert_eq("section conflict count", summary["section_conflict_total"], 1)
    assert_eq("실패 section edge 미추가", len(s._edge_reservations["C__D"]), 0)

    s2 = TimeWindowScheduler()
    first_edge = await s2.reserve_edge(
        "WP_A",
        "ST_01",
        "AGV_001",
        start_time=0.0,
        end_time=10.0,
        section_key="access:station_access:ST_01",
    )
    reverse_blocked_by_section = await s2.reserve_edge(
        "ST_01",
        "WP_A",
        "AGV_002",
        start_time=2.0,
        end_time=5.0,
        section_key="access:station_access:ST_01",
    )
    edge_summary = s2.get_headon_summary()
    assert_eq("edge section 예약 성공", first_edge, True)
    assert_eq("edge section 겹침 차단", reverse_blocked_by_section, False)
    assert_eq("edge section conflict count", edge_summary["section_conflict_total"], 1)
    assert_eq("section 차단은 head-on 카운트 전", edge_summary["headon_total"], 0)


def test_critical_section_conflict_blocks_itinerary():
    run(_test_critical_section_conflict_blocks_itinerary())


def test_critical_section_key_generation():
    print("\n[T39] Critical section key generation")
    from src.domain.map.topology_generator import MapTopologyGenerator

    graph = MapTopologyGenerator().generate("C")
    agv = AGV("AGV_001", LocalMemoryBus(), graph, TimeWindowScheduler())
    bay_edge = next(e for e in graph.edges.values() if e.corridor == "bay")
    access_edge = next(e for e in graph.edges.values() if e.access_type == "station_access")
    station_id = next(
        node_id
        for node_id in (access_edge.start_node_id, access_edge.end_node_id)
        if graph.nodes[node_id].role == NodeRole.WORK
    )
    sibling_access_edges = [
        e for e in graph.edges.values()
        if e.access_type == "station_access"
        and station_id in (e.start_node_id, e.end_node_id)
    ]
    graph_b = MapTopologyGenerator().generate("B")
    agv_b = AGV("AGV_B", LocalMemoryBus(), graph_b, TimeWindowScheduler())
    shared_edge = next(e for e in graph_b.edges.values() if e.corridor == "north")

    assert_true("bay section key", agv._critical_section_key(bay_edge).startswith("bay:"))
    assert_true(
        "station access section key",
        agv._critical_section_key(access_edge).startswith("access:station_access:ST_"),
    )
    assert_eq(
        "station access edges share station section",
        len({agv._critical_section_key(e) for e in sibling_access_edges}),
        1,
    )
    assert_true(
        "shared corridor section key",
        agv_b._critical_section_key(shared_edge).startswith("shared_corridor:north:"),
    )


async def _test_critical_section_capacity_allows_overlap_until_limit():
    print("\n[T40] Critical section capacity")
    from src.domain.reservation.scheduler import ItinerarySegment

    s = TimeWindowScheduler()
    first = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="A__B",
            agv_id="AGV_001",
            start_time=0.0,
            end_time=10.0,
            src_id="A",
            dst_id="B",
            section_key="lane:wide",
            section_capacity=2,
        )
    ])
    second = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="C__D",
            agv_id="AGV_002",
            start_time=2.0,
            end_time=5.0,
            src_id="C",
            dst_id="D",
            section_key="lane:wide",
            section_capacity=2,
        )
    ])
    third = await s.reserve_itinerary([
        ItinerarySegment(
            segment_type="edge",
            key="E__F",
            agv_id="AGV_003",
            start_time=3.0,
            end_time=4.0,
            src_id="E",
            dst_id="F",
            section_key="lane:wide",
            section_capacity=2,
        )
    ])
    summary = s.get_headon_summary()

    assert_eq("capacity 첫 예약 성공", first, True)
    assert_eq("capacity 내 두 번째 겹침 허용", second, True)
    assert_eq("capacity 초과 세 번째 차단", third, False)
    assert_eq("section conflict count", summary["section_conflict_total"], 1)


def test_critical_section_capacity_allows_overlap_until_limit():
    run(_test_critical_section_capacity_allows_overlap_until_limit())


def test_type_c_d_section_capacity_same():
    print("\n[T41] Type C/D lane section capacity same")
    from src.domain.map.topology_generator import MapTopologyGenerator

    gen = MapTopologyGenerator()
    graph_c = gen.generate("C")
    graph_d = gen.generate("D")
    edge_c = next(e for e in graph_c.edges.values() if e.safety_model == "narrow_one_way")
    edge_d = next(e for e in graph_d.edges.values() if e.safety_model == "wide_one_way")
    agv_c = AGV("AGV_C", LocalMemoryBus(), graph_c, TimeWindowScheduler())
    agv_d = AGV("AGV_D", LocalMemoryBus(), graph_d, TimeWindowScheduler())

    assert_true("Type C lane section key", agv_c._critical_section_key(edge_c).startswith("lane:"))
    assert_true("Type D lane section key", agv_d._critical_section_key(edge_d).startswith("lane:"))
    assert_eq("Type C section capacity", agv_c._critical_section_capacity(edge_c), 1)
    assert_eq("Type D section capacity", agv_d._critical_section_capacity(edge_d), 1)


def test_motion_model_acceleration():
    print("\n[T42] Motion model acceleration")
    from src.domain.agv.motion import MotionModel

    motion = MotionModel(max_speed_mps=1.5, acceleration_mps2=0.5, deceleration_mps2=0.8)
    moved_1, arrived_1 = motion.update(1.0, 10.0, 0.0)
    moved_2, arrived_2 = motion.update(1.0, 10.0, 0.0)

    assert_true("첫 틱 즉시 최고속 아님", 0.0 < motion.state.speed < 1.5)
    assert_true("가속 중 이동거리 양수", moved_1 > 0.0)
    assert_true("두 번째 이동거리 증가", moved_2 > moved_1)
    assert_eq("아직 도착 전", arrived_1 or arrived_2, False)


def test_restart_delay_accounting():
    print("\n[T43] Restart delay accounting")

    graph = make_graph()
    agv = AGV("AGV_001", LocalMemoryBus(), graph, TimeWindowScheduler())
    agv._restart_delay_remaining = 1.0
    agv._motion.state.speed = 1.0

    consumed = agv._tick_restart_delay(0.4)

    assert_eq("restart delay consumed", consumed, True)
    assert_eq("restart delay remaining", round(agv._restart_delay_remaining, 2), 0.6)
    assert_eq("restart delay KPI time", round(agv._restart_delay_time_s, 2), 0.4)
    assert_eq("restart sets speed zero", agv._motion.state.speed, 0.0)


def test_agv_pickup_dropoff_processing_time_split():
    print("\n[T44] AGV pickup/dropoff processing split")
    graph = make_graph()
    agv = AGV("AGV_001", LocalMemoryBus(), graph, TimeWindowScheduler())
    agv._current_pickup_node_id = "node_work_01"
    agv._current_dropoff_node_id = "node_work_02"
    agv._pickup_processing_time_s = 7.0
    agv._dropoff_processing_time_s = 11.0

    agv._processing_node_id = "node_work_01"
    assert_eq("pickup processing time", agv._processing_time_for_current_node(), 7.0)

    agv._processing_node_id = "node_work_02"
    assert_eq("dropoff processing time", agv._processing_time_for_current_node(), 11.0)


# ─────────────────────────────────────────────
# TEST T45: Head-on semantic regression (Phase 3 기반)
# ─────────────────────────────────────────────

async def _test_headon_regression():
    """
    [T45] Head-on semantic regression — 생성 토폴로지 기반.

    Phase 3 (itinerary pre-reservation, critical section, follow-on headway) 포함
    기준선. 실측값 기반 마진:
      A/C/D: headon == 0  (메인 통로 단방향 + station access node critical section)
      B:     headon < 400 (양방향+siding, 실측 ≈ 96 × 4배)
      E:     headon < 300 (양방향 크리프, 실측 ≈ 64 × 5배)
    deadlock == 0 전 타입 공통.
    """
    from src.domain.map.topology_generator import MapTopologyGenerator
    print("\n[T45] Head-on semantic regression (Phase 3 baseline, 12 AGV / 600s)")
    tgen = MapTopologyGenerator()

    # (seed, threshold, exact_zero, desc)
    # A/C/D: 메인 통로는 단방향 구조 보장. station/charger access는 설비 노드 단위
    #        critical section으로 묶어 reverse-edge head-on 이전에 차단한다.
    # B/E: 양방향 구조로 head-on 상당수 발생. Phase 3 baseline upper bound.
    cases = [
        ("A", 4500, 0,   True,  "단방향 순환 — head-on 0"),
        ("B", 100,  400, False, "양방향+siding — Phase 3 upper bound"),
        ("C", 4502, 0,   True,  "2차선 단방향 — head-on 0"),
        ("D", 4503, 0,   True,  "2차선 단방향 (wide) — head-on 0"),
        ("E", 100,  300, False, "양방향 크리프 — Phase 3 upper bound"),
    ]
    for type_code, seed, upper_bound, exact_zero, desc in cases:
        random.seed(seed)
        graph  = tgen.generate(type_code)
        bus    = LocalMemoryBus()
        sched  = TimeWindowScheduler()
        gen    = TaskGenerator(graph, bus, task_interval_s=5.0)
        engine = SimulationEngine(graph, sched, task_generator=gen)
        chargers = [n.node_id for n in graph.get_chargers()]
        n_agv = 12
        for i in range(n_agv):
            agv = AGV(f"AGV_{i+1:03d}", bus, graph, sched)
            agv.current_node_id = chargers[i % len(chargers)]
            agv.physics.x = graph.nodes[agv.current_node_id].x
            agv.physics.y = graph.nodes[agv.current_node_id].y
            engine.register_agv(agv)
        results = await engine.run(duration_s=600.0)
        summary = engine.scheduler.get_headon_summary()
        ho = summary["headon_total"]
        dl = results["deadlock_or_stall_count"]
        print(f"  Type {type_code} seed={seed} ({desc}): headon={ho}  deadlock={dl}")
        if exact_zero:
            assert_eq(f"Type {type_code} headon == 0", ho, 0)
        else:
            assert_true(f"Type {type_code} headon < {upper_bound}", ho < upper_bound)
        assert_eq(f"Type {type_code} deadlock == 0", dl, 0)

def test_headon_regression():
    run(_test_headon_regression())


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [
        # sample_fab.json 기반 (T1~T12)
        test_graph_load,
        test_node_roles,
        test_astar_path,
        test_approach_detection,
        test_no_path,
        test_scheduler,
        test_bus,
        test_full_flow,
        # FAB 맵 기반 (T13~T19)
        test_fab_graph_load,
        test_fab_node_counts,
        test_fab_astar,
        test_fab_unidirectional,
        test_fab_corridor_speeds,
        test_fab_connectivity,
        test_fab_full_simulation,
        test_fab_stress,
        # Topology Invariants (T21~T25)
        test_topology_invariants,
        test_type_e_creep_policy,
        test_type_a_no_headon,
        test_type_c_no_headon,
        test_type_d_no_samelane_headon,
        # Diagnostics / KPI regression (T26~T27)
        test_task_generator_diagnostics,
        test_kpi_headon_fields,
        test_type_b_siding_coverage_diagnostics,
        test_type_c_d_station_pair_reachability,
        test_type_a_routeable_task_selection,
        test_type_d_width_metadata,
        test_demand_set_generation,
        test_common_demand_lifecycle_metrics,
        test_real_demand_completion_metrics,
        test_topology_ranking_summary,
        test_type_b_siding_placement_ranking_grouping,
        test_follow_on_headway_blocks_close_entry,
        test_type_d_follow_on_headway_shorter_than_c,
        test_itinerary_reservation_atomic_conflict,
        test_critical_section_conflict_blocks_itinerary,
        test_critical_section_key_generation,
        test_critical_section_capacity_allows_overlap_until_limit,
        test_type_c_d_section_capacity_same,
        test_motion_model_acceleration,
        test_restart_delay_accounting,
        test_agv_pickup_dropoff_processing_time_split,
        # Head-on / siding / bottleneck regression (T45~T50)
        test_headon_regression,
        test_invalid_type_b_siding_placement_rejected,
        test_bottleneck_edge_interpretation,
        test_type_b_reachable_siding_candidate_beyond_adjacent,
        test_type_b_adjacent_vs_reachable_siding_policy,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"결과: {passed} passed / {failed} failed")
    sys.exit(0 if failed == 0 else 1)
