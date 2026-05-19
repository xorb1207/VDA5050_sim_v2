"""
DeadlockDetector 단위/통합 테스트.

시나리오:
  1. 위치 anchor 갱신 & stuck 감지
  2. 2-AGV head-on wait-for 사이클 탐지
  3. 사이클 해소 (낮은 우선순위 AGV reroute / backup)
  4. tick payload 에 데드락 필드 노출 (integration)

실행: python -m pytest tests/unit/test_deadlock_detector.py -v
     혹은 python tests/unit/test_deadlock_detector.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.adapters.bus.adapters import LocalMemoryBus
from src.application.deadlock_detector import DeadlockDetector
from src.application.engine.simulation_engine import SimulationEngine
from src.domain.agv.agv import AGV
from src.domain.agv.fsm import AGVState
from src.domain.fleet import Fleet
from src.domain.map.graph import Edge, MapGraph, Node, NodeRole
from src.domain.reservation.scheduler import TimeWindowScheduler


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


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _make_corridor_graph() -> MapGraph:
    """A — B — C 양방향 단일 코리도. 2대 AGV head-on 강제용."""
    g = MapGraph()
    g._add_node(Node(node_id="A", x=0.0, y=0.0, role=NodeRole.STANDARD))
    g._add_node(Node(node_id="B", x=10.0, y=0.0, role=NodeRole.STANDARD))
    g._add_node(Node(node_id="C", x=20.0, y=0.0, role=NodeRole.STANDARD))
    g._add_edge(Edge(
        edge_id="e_AB", start_node_id="A", end_node_id="B",
        bidirectional=True, max_speed=1.5, width_m=1.5,
    ))
    g._add_edge(Edge(
        edge_id="e_BC", start_node_id="B", end_node_id="C",
        bidirectional=True, max_speed=1.5, width_m=1.5,
    ))
    return g


def _place_agv(agv: AGV, node_id: str) -> None:
    """AGV 를 그래프의 특정 노드에 스냅 + path 부여."""
    agv.current_node_id = node_id
    n = agv._graph.nodes[node_id]
    agv._motion.snap_to(n.x, n.y)


# ──────────────────────────────────────────────────────
# T1: anchor 위치 추적 & stuck 감지
# ──────────────────────────────────────────────────────

def test_anchor_tracks_position_and_stuck_duration():
    print("\n[D1] anchor 위치 추적 & stuck 감지")
    g = _make_corridor_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    a = AGV("AGV_01", bus, g, sched)
    _place_agv(a, "B")

    det = DeadlockDetector(stuck_threshold_s=5.0)
    agvs = {a.agv_id: a}

    det.update_positions(agvs, sim_time=0.0)
    assert_eq("초기 stuck=0", det.stuck_duration("AGV_01", 0.0), 0.0)

    # 4.9초 머무름 — 아직 stuck 미만
    det.update_positions(agvs, sim_time=4.9)
    assert_true("4.9s 시점 stuck<5.0", det.stuck_duration("AGV_01", 4.9) < 5.0)

    # 5.1초 — stuck 도달
    det.update_positions(agvs, sim_time=5.1)
    assert_true("5.1s 시점 stuck>=5.0", det.stuck_duration("AGV_01", 5.1) >= 5.0)

    # 위치 이동 → anchor 리셋
    a._motion.snap_to(11.0, 0.0)
    det.update_positions(agvs, sim_time=5.2)
    assert_eq("이동 후 stuck=0", round(det.stuck_duration("AGV_01", 5.2), 3), 0.0)


# ──────────────────────────────────────────────────────
# T2: head-on wait-for 사이클 탐지
# ──────────────────────────────────────────────────────

async def _test_headon_cycle_detection():
    print("\n[D2] head-on 2-AGV wait-for 사이클 탐지")
    g = _make_corridor_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()

    # 두 AGV: A1 은 A→B→C, A2 는 C→B→A. B 에서 충돌 유도.
    a1 = AGV("AGV_01", bus, g, sched)
    a2 = AGV("AGV_02", bus, g, sched)
    _place_agv(a1, "B")
    _place_agv(a2, "B")
    # 동일 노드 B 에 둘 다 놓는 건 부자연스러우니 시뮬레이트 구성:
    # A1: 현재 A, 다음 B (e_AB)
    # A2: 현재 C, 다음 B (e_BC 역방향)
    # 이미 상대방이 e_AB / e_BC 의 반대 방향 entry 를 예약했다고 가정.
    _place_agv(a1, "A")
    _place_agv(a2, "C")
    a1._path = ["A", "B", "C"]
    a1._path_index = 0
    a1._pending_edge_src = "A"
    a1._pending_edge_dst = "B"
    a2._path = ["C", "B", "A"]
    a2._path_index = 0
    a2._pending_edge_src = "C"
    a2._pending_edge_dst = "B"

    # WAITING_RESERVATION 강제
    a1._fsm.force(AGVState.IDLE)
    a1._fsm.force(AGVState.WAITING_RESERVATION)
    a2._fsm.force(AGVState.IDLE)
    a2._fsm.force(AGVState.WAITING_RESERVATION)

    # 상대방 진행 방향 예약을 미리 채워 둠
    # A2 가 B→A 방향 예약 → A1 의 A→B 가 head-on 으로 차단됨
    ok2 = await sched.reserve_edge(
        "B", "A", "AGV_02", start_time=0.0, end_time=100.0
    )
    assert_true("A2: B→A 예약 성공", ok2)
    # A1 이 B→C 예약 → A2 의 C→B 가 head-on 으로 차단됨
    ok1 = await sched.reserve_edge(
        "B", "C", "AGV_01", start_time=0.0, end_time=100.0
    )
    assert_true("A1: B→C 예약 성공", ok1)

    agvs = {a1.agv_id: a1, a2.agv_id: a2}
    det = DeadlockDetector(stuck_threshold_s=5.0)

    # stuck 임계 미만 → 사이클 없음
    det.update_positions(agvs, sim_time=0.0)
    cycles_early = det.detect(agvs, sched, sim_time=1.0)
    assert_eq("stuck 임계 전 cycles", cycles_early, [])

    # 시간 경과로 stuck 도달
    det.update_positions(agvs, sim_time=6.0)
    cycles = det.detect(agvs, sched, sim_time=6.0)
    assert_true("사이클 1개 이상 검출", len(cycles) >= 1)
    members = set(cycles[0])
    assert_eq("사이클 멤버 == {A1, A2}", members, {"AGV_01", "AGV_02"})


def test_headon_cycle_detection():
    run(_test_headon_cycle_detection())


# ──────────────────────────────────────────────────────
# T3: 해소 — 낮은 우선순위 AGV reroute 시도
# ──────────────────────────────────────────────────────

async def _test_resolve_picks_lowest_priority():
    print("\n[D3] resolve() 가 fleet priority 낮은 AGV 선택")
    g = _make_corridor_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()

    # 두 fleet: F1=priority 1 (우선), F2=priority 5 (양보)
    f_hi = Fleet(id="F1", graph_idx=0, priority=1)
    f_lo = Fleet(id="F2", graph_idx=0, priority=5)
    a1 = AGV("AGV_01", bus, g, sched, fleet=f_hi)
    a2 = AGV("AGV_02", bus, g, sched, fleet=f_lo)
    _place_agv(a1, "A")
    _place_agv(a2, "C")
    a1._path = ["A", "B", "C"]
    a1._path_index = 0
    a1._pending_edge_src = "A"
    a1._pending_edge_dst = "B"
    a2._path = ["C", "B", "A"]
    a2._path_index = 0
    a2._pending_edge_src = "C"
    a2._pending_edge_dst = "B"
    a1._fsm.force(AGVState.WAITING_RESERVATION)
    a2._fsm.force(AGVState.WAITING_RESERVATION)
    await sched.reserve_edge("B", "A", "AGV_02", 0.0, 100.0)
    await sched.reserve_edge("B", "C", "AGV_01", 0.0, 100.0)

    agvs = {a1.agv_id: a1, a2.agv_id: a2}
    det = DeadlockDetector(stuck_threshold_s=5.0)
    det.update_positions(agvs, sim_time=0.0)
    det.update_positions(agvs, sim_time=6.0)
    cycles = det.detect(agvs, sched, sim_time=6.0)
    assert_true("사이클 검출", len(cycles) == 1)
    res = await det.resolve(cycles[0], agvs, sched, sim_time=6.0)
    assert_eq("victim = 낮은 priority AGV", res["agv_id"], "AGV_02")
    assert_true("action ∈ {backup,reroute,alert}",
                res["action"] in ("backup", "reroute", "alert"))


def test_resolve_picks_lowest_priority():
    run(_test_resolve_picks_lowest_priority())


# ──────────────────────────────────────────────────────
# T4: step() 통합 payload
# ──────────────────────────────────────────────────────

async def _test_step_payload_contains_required_fields():
    print("\n[D4] step() 반환 payload 필수 필드")
    g = _make_corridor_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    a1 = AGV("AGV_01", bus, g, sched)
    a2 = AGV("AGV_02", bus, g, sched)
    _place_agv(a1, "A")
    _place_agv(a2, "C")
    a1._path = ["A", "B", "C"]; a1._path_index = 0
    a1._pending_edge_src = "A"; a1._pending_edge_dst = "B"
    a2._path = ["C", "B", "A"]; a2._path_index = 0
    a2._pending_edge_src = "C"; a2._pending_edge_dst = "B"
    a1._fsm.force(AGVState.WAITING_RESERVATION)
    a2._fsm.force(AGVState.WAITING_RESERVATION)
    await sched.reserve_edge("B", "A", "AGV_02", 0.0, 100.0)
    await sched.reserve_edge("B", "C", "AGV_01", 0.0, 100.0)

    agvs = {a1.agv_id: a1, a2.agv_id: a2}
    det = DeadlockDetector(stuck_threshold_s=5.0)
    # 첫 step: anchor 만 잡힘
    p0 = await det.step(agvs, sched, sim_time=0.0)
    assert_true("필드 deadlock_detected", "deadlock_detected" in p0)
    assert_true("필드 deadlock_groups", "deadlock_groups" in p0)
    assert_true("필드 deadlock_count_total", "deadlock_count_total" in p0)
    assert_true("필드 deadlock_alert", "deadlock_alert" in p0)
    assert_eq("초기 detected", p0["deadlock_detected"], False)

    # stuck 임계 후
    p1 = await det.step(agvs, sched, sim_time=6.0)
    assert_eq("detected=True", p1["deadlock_detected"], True)
    assert_true("count_total >= 1", p1["deadlock_count_total"] >= 1)
    assert_true("groups 비어있지 않음", len(p1["deadlock_groups"]) >= 1)


def test_step_payload_contains_required_fields():
    run(_test_step_payload_contains_required_fields())


# ──────────────────────────────────────────────────────
# T5: SimulationEngine 통합 — last_deadlock_payload 노출
# ──────────────────────────────────────────────────────

async def _test_engine_exposes_deadlock_payload_each_tick():
    print("\n[D5] SimulationEngine 매 tick payload 노출")
    g = _make_corridor_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    engine = SimulationEngine(g, sched)
    a1 = AGV("AGV_01", bus, g, sched)
    a2 = AGV("AGV_02", bus, g, sched)
    _place_agv(a1, "A")
    _place_agv(a2, "C")
    a1._path = ["A", "B", "C"]; a1._path_index = 0
    a1._pending_edge_src = "A"; a1._pending_edge_dst = "B"
    a2._path = ["C", "B", "A"]; a2._path_index = 0
    a2._pending_edge_src = "C"; a2._pending_edge_dst = "B"
    a1._fsm.force(AGVState.WAITING_RESERVATION)
    a2._fsm.force(AGVState.WAITING_RESERVATION)
    await sched.reserve_edge("B", "A", "AGV_02", 0.0, 100.0)
    await sched.reserve_edge("B", "C", "AGV_01", 0.0, 100.0)
    engine.register_agv(a1)
    engine.register_agv(a2)

    # 인공적으로 sim_time 진행시키며 tick 만 직접 호출.
    # (engine.run 은 task_generator/AGV start 없이 호출하면 stuck 시간만 진행.)
    dt = 0.5
    for _ in range(20):  # 10s
        await engine._tick(dt)
        engine.sim_time += dt
        # 매 tick 후 두 AGV 가 움직이지 못하도록 강제 WAITING 유지
        for a in (a1, a2):
            a._fsm.force(AGVState.WAITING_RESERVATION)
            n = g.nodes[a.current_node_id]
            a._motion.snap_to(n.x, n.y)

    p = engine.last_deadlock_payload
    assert_eq("engine.last_deadlock_payload.deadlock_detected", p["deadlock_detected"], True)
    assert_true("engine.last_deadlock_payload.count_total >= 1",
                p["deadlock_count_total"] >= 1)
    # _build_analytics 에서 deadlock_position_count 노출 확인
    kpi = engine._build_analytics()
    assert_true("kpi.deadlock_position_count >= 1",
                kpi.get("deadlock_position_count", 0) >= 1)


def test_engine_exposes_deadlock_payload_each_tick():
    run(_test_engine_exposes_deadlock_payload_each_tick())


# ──────────────────────────────────────────────────────
# 실행
# ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [
        test_anchor_tracks_position_and_stuck_duration,
        test_headon_cycle_detection,
        test_resolve_picks_lowest_priority,
        test_step_payload_contains_required_fields,
        test_engine_exposes_deadlock_payload_each_tick,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*40}")
    print(f"결과: {passed} passed / {failed} failed")
    sys.exit(0 if failed == 0 else 1)
