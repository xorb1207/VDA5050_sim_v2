"""
핵심 레이어 통합 테스트.
실행: python -m pytest tests/ -v  (pytest 없으면 python tests/test_simulation.py)
"""
from __future__ import annotations

import asyncio
import sys
import os

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
    assert_true("head-on < 1000 (regression)",  summary["headon_total"] < 1000)
    assert_true("deadlock 없음",      results["deadlock_or_stall_count"] == 0)

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
    for key in ("headon_total", "retry_total", "avg_retry_per_headon", "top_headon_edges"):
        assert_true(f"{key} 포함", key in results)
    assert_eq("headon_total 기본값", results["headon_total"], 0)


def test_kpi_headon_fields():
    run(_test_kpi_headon_fields())


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
    print("\n[T30] Type D width metadata")
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

    assert_eq("Type C lane width", c_widths, {1.5})
    assert_eq("Type D lane width", d_widths, {2.0})
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
        (d.release_time_s, d.pickup_node_id, d.dropoff_node_id, d.processing_time_s)
        for d in common_a.demands
    ]
    sig_b = [
        (d.release_time_s, d.pickup_node_id, d.dropoff_node_id, d.processing_time_s)
        for d in common_b.demands
    ]
    assert_eq("common deterministic", sig_a == sig_b, True)
    assert_eq("common count", len(common_a.demands), 20)
    assert_eq("capability count", len(capability.demands), 20)

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
        test_type_c_d_station_pair_reachability,
        test_type_a_routeable_task_selection,
        test_type_d_width_metadata,
        test_demand_set_generation,
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
