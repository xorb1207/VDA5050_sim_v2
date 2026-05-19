"""
강제 데드락 — backup/reroute 모두 실패 → alert=true 까지 확인.

Graph: A — B — C (linear bidirectional, no bypass).
  AGV_02 를 노드 B 에 미리 두고, 거기서 A 로 가려고 (path [B, A]) 시도.
  AGV_01 은 A 에서 B 로 가려고 (path [A, B]) 시도.
  ⇒ A→B 예약(AGV_01) ↔ B→A 예약 시도(AGV_02) head-on
  ⇒ AGV_01 이 B 에 도착해도 노드 B 는 AGV_02 가 점유 → 못 들어감
  ⇒ true deadlock.
  Backup 불가 (path_index=0 → prev 없음), reroute 불가 (단일 corridor).
  ⇒ alert=true 떨어져야 정상.
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
    # 양방향이지만 외길 — bypass 없음.
    g._add_edge(Edge("e_AB", "A", "B", bidirectional=True, max_speed=1.0, width_m=1.5))
    g._add_edge(Edge("e_BC", "B", "C", bidirectional=True, max_speed=1.0, width_m=1.5))
    return g


async def main() -> None:
    graph = make_graph()
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    engine = SimulationEngine(graph, sched)

    f_hi = Fleet(id="F1", graph_idx=0, priority=1)
    f_lo = Fleet(id="F2", graph_idx=0, priority=5)
    a1 = AGV("AGV_01", bus, graph, sched, max_speed_mps=1.0, fleet=f_hi)
    a2 = AGV("AGV_02", bus, graph, sched, max_speed_mps=1.0, fleet=f_lo)

    # 핵심: AGV_02 는 노드 B 에 머무른다. A 로 출발하려고 하지만 head-on 으로 막힘.
    n_A = graph.nodes["A"]; n_B = graph.nodes["B"]
    a1.current_node_id = "A"; a1._motion.snap_to(n_A.x, n_A.y)
    a2.current_node_id = "B"; a2._motion.snap_to(n_B.x, n_B.y)
    a1._path = ["A", "B"]; a1._path_index = 0
    a2._path = ["B", "A"]; a2._path_index = 0
    engine.register_agv(a1)
    engine.register_agv(a2)

    await a1.start(); await a2.start()
    # 양쪽 모두 첫 hop 시도 → A→B 예약(AGV_01) / B→A 예약 시도(AGV_02)
    await a1._navigate_to_next_node(0.0)
    await a2._navigate_to_next_node(0.0)

    print(f"t=0: a1 state={a1._fsm.state.value}  a2 state={a2._fsm.state.value}")
    print(f"      a1 pending=({a1._pending_edge_src},{a1._pending_edge_dst})  "
          f"a2 pending=({a2._pending_edge_src},{a2._pending_edge_dst})")
    print(f"      A→B reservations: {[r.agv_id for r in sched._edge_reservations.get('A__B', []) if not r.released]}")
    print(f"      B→A reservations: {[r.agv_id for r in sched._edge_reservations.get('B__A', []) if not r.released]}")

    # 매 tick payload 캡처
    detected_count = 0
    first_detected_t = None
    first_alert_t = None
    last_payload = None

    dt = 1.0 / engine.TICK_RATE_HZ
    for step in range(int(15.0 / dt)):
        await engine._tick(dt)
        engine.sim_time += dt
        p = engine.last_deadlock_payload
        if p["deadlock_detected"]:
            detected_count += 1
            if first_detected_t is None:
                first_detected_t = engine.sim_time
        if p["deadlock_alert"] and first_alert_t is None:
            first_alert_t = engine.sim_time
        last_payload = p

    print("─" * 50)
    print(f"sim_time end           = {engine.sim_time:.2f}s")
    print(f"detected_ticks         = {detected_count}")
    print(f"first_detected_at      = {first_detected_t}")
    print(f"first_alert_at         = {first_alert_t}")
    print(f"deadlock_count_total   = {engine.deadlock_detector.total_count}")
    print(f"last payload           = {last_payload}")
    print(f"a1 final state         = {a1._fsm.state.value}  pos=({a1._motion.state.x:.1f},{a1._motion.state.y:.1f})")
    print(f"a2 final state         = {a2._fsm.state.value}  pos=({a2._motion.state.x:.1f},{a2._motion.state.y:.1f})")
    print(f"a1 reroute_count       = {a1._reroute_count}")
    print(f"a2 reroute_count       = {a2._reroute_count}")


if __name__ == "__main__":
    asyncio.run(main())
