"""PR-D 효과 측정 — station capacity 거부 + station 중복 점유 통계.

main 대비:
  - station_capacity_reject 가 발생하는가 (sanity)
  - 어떤 sim_time 에서도 같은 STATION/HP 에 2대 이상 머무름 0건 확인
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


async def main():
    type_codes = (sys.argv[1],) if len(sys.argv) > 1 else ("A",)
    for type_code in type_codes:
        random.seed(42)
        g = MapTopologyGenerator().generate(type_code)
        bus = LocalMemoryBus()
        sched = TimeWindowScheduler()
        gen = TaskGenerator(g, bus, task_interval_s=5.0, scheduler=sched)
        engine = SimulationEngine(g, sched, task_generator=gen)
        # debug — 거부 시 무슨 노드 누가 reserve?
        orig_check = gen._is_node_capacity_blocked
        first_reject_logged = [False]
        def watch(nid, requester):
            blocked = orig_check(nid, requester)
            if blocked and not first_reject_logged[0]:
                holders = [(r.agv_id, round(r.start_time,2), round(r.end_time,2))
                           for r in sched._reservations.get(nid, [])
                           if not r.released and r.agv_id != requester]
                print(f"  DBG t≈{engine.sim_time:.2f} requester={requester} node={nid} holders={holders}")
                first_reject_logged[0] = True
            return blocked
        gen._is_node_capacity_blocked = watch
        chargers = [n.node_id for n in g.get_chargers()]
        for i in range(12):
            agv = AGV(f"AGV_{i+1:03d}", bus, g, sched)
            agv.current_node_id = chargers[i % len(chargers)]
            agv.physics.x = g.nodes[agv.current_node_id].x
            agv.physics.y = g.nodes[agv.current_node_id].y
            engine.register_agv(agv)

        # 각 tick 마다 PROCESSING/CHARGING 노드 중복 발생 검사
        overlap_events = 0
        orig_tick = engine._tick
        async def watch_tick(dt, _orig=orig_tick):
            await _orig(dt)
            counts: dict[str, list[str]] = {}
            for a in engine.agvs.values():
                if a._fsm.state in (AGVState.PROCESSING, AGVState.CHARGING):
                    counts.setdefault(a.current_node_id, []).append(a.agv_id)
            nonlocal overlap_events
            for nid, ids in counts.items():
                if len(ids) > 1:
                    overlap_events += 1
        engine._tick = watch_tick

        results = await engine.run(600.0)
        diag = gen._diagnostics

        print(f"--- Type {type_code} ---")
        print(f"  tasks_dispatched      = {diag.tasks_dispatched}")
        print(f"  orders_published      = {diag.orders_published}")
        print(f"  demands_completed     = {diag.demands_completed}")
        print(f"  tasks_completed       = {results.get('tasks_completed')}")
        print(f"  station_capacity_reject = {diag.station_capacity_reject}")
        print(f"  station overlap ticks (PROC/CHRG 2+) = {overlap_events}")


if __name__ == "__main__":
    asyncio.run(main())
