"""
데드락 감지기 smoke test.

실측 FAB 맵 + 20 AGV / 600s 시뮬 (T20 와 동일 셋업, 시간만 단축).
DeadlockDetector 의 누적 카운트, alert 빈도, resolution 분포를 출력해
실제 시뮬에서 detector 가 비정상 남발하지 않는지 + 의미 있는 데드락이
발생하면 알림이 떨어지는지 확인한다.
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
from src.domain.map.graph import MapGraph
from src.domain.reservation.scheduler import TimeWindowScheduler


async def main() -> None:
    graph = MapGraph.from_rmf_yaml("maps/fab_nav_graph.yaml")
    sched = TimeWindowScheduler()
    bus = LocalMemoryBus()
    gen = TaskGenerator(graph, bus, task_interval_s=5.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    # 충전소들에 AGV 분산 배치
    chargers = [n.node_id for n in graph.get_chargers()]
    start_nodes = (chargers * ((20 + len(chargers) - 1) // len(chargers)))[:20]
    for i, charger in enumerate(start_nodes, 1):
        agv = AGV(f"AGV_{i:03d}", bus, graph, sched, max_speed_mps=1.5)
        agv.current_node_id = charger
        engine.register_agv(agv)

    resolutions: list[dict] = []
    alert_ticks = 0
    detected_ticks = 0
    # detector.step 안에서 resolve 가 호출되므로, 우리는 매 push 시점에 last_payload 만 모니터링.
    # detector 내부 resolution 을 따로 모으기 위해 step() 을 monkey-patch.
    orig_step = engine.deadlock_detector.step

    async def wrapped_step(agvs, scheduler, sim_time):
        nonlocal alert_ticks, detected_ticks
        p = await orig_step(agvs, scheduler, sim_time)
        if p["deadlock_detected"]:
            detected_ticks += 1
            if p["deadlock_resolutions"]:
                resolutions.extend(p["deadlock_resolutions"])
        if p["deadlock_alert"]:
            alert_ticks += 1
        return p

    engine.deadlock_detector.step = wrapped_step

    result = await engine.run(duration_s=600.0)

    print("─" * 50)
    print(f"sim_time              = {result.get('sim_time_s')}")
    print(f"deadlock_count_total  = {engine.deadlock_detector.total_count}")
    print(f"deadlock_position_kpi = {result.get('deadlock_position_count')}")
    print(f"deadlock_count_kpi    = {result.get('deadlock_count')}")
    print(f"detected_ticks        = {detected_ticks} / 30000")
    print(f"alert_ticks           = {alert_ticks}")
    print(f"resolutions_logged    = {len(resolutions)}")
    if resolutions:
        from collections import Counter
        actions = Counter(r["action"] for r in resolutions)
        successes = sum(1 for r in resolutions if r["success"])
        print(f"  by action: {dict(actions)}")
        print(f"  successes: {successes} / {len(resolutions)}")
    print("─" * 50)


if __name__ == "__main__":
    asyncio.run(main())
