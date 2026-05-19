"""
"대기" 상태인데 demand_id=None + path[-1] 살아있는 stale state 재현.

Type A 시뮬 600s. 종료 시점에 AGV 별 상태 dump 해서
state==WAITING_RESERVATION + _current_demand_id is None + _path 살아있음
케이스가 있는지 확인.
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
from src.domain.map.graph import MapGraph
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler


async def main() -> None:
    import random
    seed = int(os.environ.get("SEED", "42"))
    random.seed(seed)
    print(f"seed={seed}")
    # Type A 토폴로지 — 단방향 순환
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

    await engine.run(duration_s=600.0)

    print("─" * 60)
    print("종료 시점 AGV state dump:")
    print("─" * 60)
    stuck: list[tuple[str, dict]] = []
    for aid, agv in engine.agvs.items():
        info = {
            "state": agv._fsm.state.value,
            "current": agv.current_node_id,
            "target": agv.target_node_id,
            "demand_id": agv._current_demand_id,
            "pickup": agv._current_pickup_node_id,
            "dropoff": agv._current_dropoff_node_id,
            "path_index": agv._path_index,
            "path_len": len(agv._path),
            "path_last": agv._path[-1] if agv._path else None,
            "pending": (agv._pending_edge_src, agv._pending_edge_dst),
            "retry": agv.collision_retry_count,
            "restart_delay": round(agv._restart_delay_remaining, 2),
        }
        if (
            agv._fsm.state == AGVState.WAITING_RESERVATION
            and agv._current_demand_id is None
            and agv._path
            and agv._path_index < len(agv._path)
        ):
            stuck.append((aid, info))
        elif agv._fsm.state == AGVState.WAITING_RESERVATION:
            print(f"  {aid}: WAITING but not stuck-pattern — {info}")

    print(f"\n[stuck pattern 후보: {len(stuck)}개]")
    for aid, info in stuck:
        print(f"  {aid}:")
        for k, v in info.items():
            print(f"      {k}={v}")


if __name__ == "__main__":
    asyncio.run(main())
