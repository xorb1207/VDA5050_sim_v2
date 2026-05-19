"""
실 시뮬레이션 e2e — 강제 head-on 시나리오에서 detector 가 감지 + 해소하는지.

A — B — C 좁은 양방향 코리도 + 곁가지 D(A 인근)
  A ─── B ─── C
  │
  D

AGV_01 (priority=1): A → C 이동 시도
AGV_02 (priority=5): C → A 이동 시도

두 AGV 가 B 직전 엣지를 서로 점유 → head-on. detector 가 5s 후 감지 → 우선순위 낮은 AGV_02 가 backup(C 위치 유지 못 함, 곁가지 없음) 실패 → reroute (대체 경로 없음) → alert. priority 1 AGV_01 은 그대로.

대신 D 곁가지를 A 에 연결해서 AGV_01 이 backup-via-D 로 해소할 수 있는 토폴로지를 시도. AGV_02 우선순위가 낮으면 AGV_02 가 victim, 그러나 AGV_02 의 path 는 C→B→A 라 backup 할 prev 가 없음(현재 노드가 path[0]). AGV_01 의 path 는 A→B→C 인데 path_index=1 이면 prev=A. backup 으로 A→D 우회.

따라서 priority 를 반대로 (AGV_02 가 higher priority = 1) 해서 AGV_01 이 victim 이 되어 backup 가능하게.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.adapters.bus.adapters import LocalMemoryBus
from src.application.engine.simulation_engine import SimulationEngine
from src.domain.agv.agv import AGV
from src.domain.fleet import Fleet
from src.domain.map.graph import Edge, MapGraph, Node, NodeRole
from src.domain.reservation.scheduler import TimeWindowScheduler


def make_graph() -> MapGraph:
    g = MapGraph()
    g._add_node(Node("A", 0.0, 0.0))
    g._add_node(Node("B", 5.0, 0.0))
    g._add_node(Node("C", 10.0, 0.0))
    g._add_node(Node("D", 0.0, 5.0))  # 곁가지 (A 위쪽)
    g._add_node(Node("E", 5.0, 5.0))  # D-E
    g._add_node(Node("F", 10.0, 5.0)) # E-F (C 위쪽)
    # main corridor (1차선 양방향)
    g._add_edge(Edge("e_AB", "A", "B", bidirectional=True, max_speed=1.0, width_m=1.5))
    g._add_edge(Edge("e_BC", "B", "C", bidirectional=True, max_speed=1.0, width_m=1.5))
    # bypass corridor: A-D-E-F-C
    g._add_edge(Edge("e_AD", "A", "D", bidirectional=True, max_speed=1.0, width_m=1.5))
    g._add_edge(Edge("e_DE", "D", "E", bidirectional=True, max_speed=1.0, width_m=1.5))
    g._add_edge(Edge("e_EF", "E", "F", bidirectional=True, max_speed=1.0, width_m=1.5))
    g._add_edge(Edge("e_CF", "C", "F", bidirectional=True, max_speed=1.0, width_m=1.5))
    return g


async def main() -> None:
    graph = make_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    engine = SimulationEngine(graph, sched)

    # F1 priority 5(낮음), F2 priority 1(높음)
    # AGV_01 in F1 (낮은 우선순위 → victim 후보), starts A, goes A→B→C
    # AGV_02 in F2 (높음), starts C, goes C→B→A
    f_lo = Fleet(id="F1", graph_idx=0, priority=5)
    f_hi = Fleet(id="F2", graph_idx=0, priority=1)
    a1 = AGV("AGV_01", bus, graph, sched, max_speed_mps=1.0, fleet=f_lo)
    a2 = AGV("AGV_02", bus, graph, sched, max_speed_mps=1.0, fleet=f_hi)

    # 직접 path 부여 + 시작 노드 스냅 (task_generator 없이 deterministic)
    n_A = graph.nodes["A"]; n_C = graph.nodes["C"]
    a1.current_node_id = "A"
    a1._motion.snap_to(n_A.x, n_A.y)
    a1._path = ["A", "B", "C"]
    a1._path_index = 0
    a2.current_node_id = "C"
    a2._motion.snap_to(n_C.x, n_C.y)
    a2._path = ["C", "B", "A"]
    a2._path_index = 0
    engine.register_agv(a1)
    engine.register_agv(a2)

    # AGV.start 는 bus 구독만 함 — order 없이도 _tick 안에서 path 따라 navigation 시도 가능?
    # 실제로는 _on_order_received 에서만 _navigate_to_next_node 가 트리거됨.
    # 따라서 첫 _navigate_to_next_node 를 강제 호출.
    await a1.start()
    await a2.start()
    await a1._navigate_to_next_node(0.0)
    await a2._navigate_to_next_node(0.0)

    resolutions: list[dict] = []
    orig_step = engine.deadlock_detector.step

    async def wrapped(agvs, scheduler, sim_time):
        p = await orig_step(agvs, scheduler, sim_time)
        if p["deadlock_resolutions"]:
            for r in p["deadlock_resolutions"]:
                r["t"] = round(sim_time, 2)
                resolutions.append(r)
        return p

    engine.deadlock_detector.step = wrapped

    # 30 초 시뮬레이트
    dt = 1.0 / engine.TICK_RATE_HZ
    for step in range(int(30.0 / dt)):
        await engine._tick(dt)
        engine.sim_time += dt

    print("─" * 50)
    print(f"sim_time end           = {engine.sim_time:.1f}")
    print(f"AGV_01 final node      = {a1.current_node_id} (path_idx {a1._path_index}/{len(a1._path)})")
    print(f"AGV_02 final node      = {a2.current_node_id} (path_idx {a2._path_index}/{len(a2._path)})")
    print(f"AGV_01 state           = {a1._fsm.state.value}")
    print(f"AGV_02 state           = {a2._fsm.state.value}")
    print(f"deadlock_count_total   = {engine.deadlock_detector.total_count}")
    print(f"resolutions ({len(resolutions)}):")
    for r in resolutions:
        print(f"  t={r['t']:.2f}  agv={r['agv_id']}  action={r['action']}  success={r['success']}")
    print(f"AGV_01 path now        = {a1._path}")
    print(f"AGV_02 path now        = {a2._path}")
    print(f"_reroute_count: a1={a1._reroute_count}  a2={a2._reroute_count}")
    print("─" * 50)


if __name__ == "__main__":
    asyncio.run(main())
