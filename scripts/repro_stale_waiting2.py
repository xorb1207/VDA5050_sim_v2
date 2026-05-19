"""
Stale WAITING 재현 v2 — _clear_demand_context, _on_order_received,
_navigate_to_next_node 호출 시점을 trace 해서 stuck AGV 가 어떻게 만들어지는지 추적.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.adapters.bus.adapters import LocalMemoryBus
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.agv.fsm import AGVState
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler


# 추적할 AGV id 들
WATCH: set[str] = set()
TRACE: list[tuple[float, str, str, dict]] = []


def install_hooks(engine: SimulationEngine) -> None:
    """주요 메서드에 후킹 — 호출 시 trace 기록."""
    for aid, agv in engine.agvs.items():
        # _on_order_received
        orig_on_order = agv._on_order_received
        async def _trace_order(payload, _orig=orig_on_order, _aid=aid, _agv=agv):
            t = engine.sim_time
            TRACE.append((t, _aid, "order_recv_pre", {
                "state": _agv._fsm.state.value,
                "demand_id": _agv._current_demand_id,
                "path_last": _agv._path[-1] if _agv._path else None,
                "payload_demandId": payload.get("demandId"),
                "payload_nodes_count": len(payload.get("nodes", [])),
            }))
            await _orig(payload)
            TRACE.append((t, _aid, "order_recv_post", {
                "state": _agv._fsm.state.value,
                "demand_id": _agv._current_demand_id,
                "path_last": _agv._path[-1] if _agv._path else None,
                "pickup": _agv._current_pickup_node_id,
                "dropoff": _agv._current_dropoff_node_id,
            }))
        agv._on_order_received = _trace_order

        # _clear_demand_context
        orig_clear = agv._clear_demand_context
        def _trace_clear(_orig=orig_clear, _aid=aid, _agv=agv):
            t = engine.sim_time
            TRACE.append((t, _aid, "clear_demand", {
                "state": _agv._fsm.state.value,
                "was_demand_id": _agv._current_demand_id,
                "path_last": _agv._path[-1] if _agv._path else None,
                "current": _agv.current_node_id,
                "path_index": _agv._path_index,
            }))
            _orig()
        agv._clear_demand_context = _trace_clear


async def main() -> None:
    graph = MapTopologyGenerator().generate("A")
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    gen = TaskGenerator(graph, bus, task_interval_s=5.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    chargers = [n.node_id for n in graph.get_chargers()]
    n_agv = 12
    start_nodes = (chargers * ((n_agv + len(chargers) - 1) // len(chargers)))[:n_agv]
    for i, charger in enumerate(start_nodes, 1):
        agv = AGV(f"AGV_{i:03d}", bus, graph, sched, max_speed_mps=1.5)
        agv.current_node_id = charger
        engine.register_agv(agv)

    # AGV start() 가 _on_order_received subscribe 하므로, run 직전에 hook
    install_hooks(engine)

    await engine.run(duration_s=600.0)

    # stuck AGV 찾기
    stuck: list[str] = []
    for aid, agv in engine.agvs.items():
        if (
            agv._fsm.state == AGVState.WAITING_RESERVATION
            and agv._current_demand_id is None
            and agv._path
            and agv._path_index < len(agv._path)
        ):
            stuck.append(aid)

    print(f"stuck AGVs: {stuck}")
    print()
    # stuck AGV 의 trace 만 출력 (마지막 30 이벤트)
    for aid in stuck:
        print(f"━━━━━ {aid} trace (last 30) ━━━━━")
        events = [e for e in TRACE if e[1] == aid]
        for t, _, kind, info in events[-30:]:
            print(f"  t={t:7.2f}  {kind:20s}  {info}")
        # 최종 상태
        agv = engine.agvs[aid]
        print(f"  ✓ FINAL: state={agv._fsm.state.value}  demand_id={agv._current_demand_id}  "
              f"current={agv.current_node_id}  path[0..3]={agv._path[:3]}  "
              f"path[-3..]={agv._path[-3:]}  path_index={agv._path_index}/{len(agv._path)}  "
              f"pending=({agv._pending_edge_src},{agv._pending_edge_dst})")
        print()


if __name__ == "__main__":
    asyncio.run(main())
