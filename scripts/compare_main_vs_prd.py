"""PR-D 변경 전후 처리량 비교 (단일 시뮬 내).

scheduler 인자 유무로 두 케이스:
  case A — TaskGenerator(scheduler=None)   = main 동작
  case B — TaskGenerator(scheduler=sched)  = PR-D 동작
"""
from __future__ import annotations
import asyncio, os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.adapters.bus.adapters import LocalMemoryBus
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.agv.fsm import AGVState
from src.domain.map.topology_generator import MapTopologyGenerator
from src.domain.reservation.scheduler import TimeWindowScheduler


async def one_run(type_code: str, with_capacity: bool, seed: int = 42):
    random.seed(seed)
    g = MapTopologyGenerator().generate(type_code)
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    gen = TaskGenerator(
        g, bus, task_interval_s=5.0,
        scheduler=(sched if with_capacity else None),
    )
    engine = SimulationEngine(g, sched, task_generator=gen)
    chargers = [n.node_id for n in g.get_chargers()]
    for i in range(12):
        agv = AGV(f"AGV_{i+1:03d}", bus, g, sched)
        agv.current_node_id = chargers[i % len(chargers)]
        agv.physics.x = g.nodes[agv.current_node_id].x
        agv.physics.y = g.nodes[agv.current_node_id].y
        engine.register_agv(agv)

    overlap_events = 0          # PROC/CHRG 2+
    overlap_max = 0
    any_overlap_events = 0      # 모든 state 2+ (단순 위치 기준)
    orig_tick = engine._tick
    async def watch_tick(dt, _orig=orig_tick):
        await _orig(dt)
        counts: dict[str, list[str]] = {}
        any_counts: dict[str, list[str]] = {}
        for a in engine.agvs.values():
            if a.current_node_id:
                any_counts.setdefault(a.current_node_id, []).append(a.agv_id)
            if a._fsm.state in (AGVState.PROCESSING, AGVState.CHARGING):
                counts.setdefault(a.current_node_id, []).append(a.agv_id)
        nonlocal overlap_events, overlap_max, any_overlap_events
        for nid, ids in counts.items():
            if len(ids) > 1:
                overlap_events += 1
                overlap_max = max(overlap_max, len(ids))
        for nid, ids in any_counts.items():
            if len(ids) > 1:
                any_overlap_events += 1
    engine._tick = watch_tick

    results = await engine.run(600.0)
    diag = gen._diagnostics
    return {
        "orders_published": diag.orders_published,
        "tasks_completed":  results.get("tasks_completed", 0),
        "station_capacity_reject": diag.station_capacity_reject,
        "PROC/CHRG_overlap":  overlap_events,
        "any_overlap_ticks":  any_overlap_events,
        "headon_total": engine.scheduler.get_headon_summary()["headon_total"],
    }


async def main():
    for type_code in ("A", "B", "C", "D", "E"):
        a = await one_run(type_code, with_capacity=False)
        b = await one_run(type_code, with_capacity=True)
        print(f"=== Type {type_code} ===")
        keys = ["orders_published","tasks_completed","station_capacity_reject",
                "PROC/CHRG_overlap","any_overlap_ticks","headon_total"]
        print(f"  {'metric':<28}  {'main':>10}  {'PR-D':>10}")
        for k in keys:
            print(f"  {k:<28}  {a[k]:>10}  {b[k]:>10}")


if __name__ == "__main__":
    asyncio.run(main())
