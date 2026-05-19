from __future__ import annotations

import asyncio
import random

from src.application.scenario.scenario import Scenario
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.map.graph import MapGraph
from src.domain.reservation.scheduler import TimeWindowScheduler
from src.domain.agv.agv import AGV
from src.adapters.bus.adapters import LocalMemoryBus


class ScenarioRunner:
    """
    Scenario 객체 → 엔진 조립 → 실행 → KPI 반환.
    단일 시나리오 실행과 batch 실행 모두 이 클래스를 통해.
    """

    @staticmethod
    async def run(scenario: Scenario) -> dict:
        random.seed(scenario.random_seed)

        bus   = LocalMemoryBus()
        await bus.connect()

        # 맵 로드 (one_way는 engine에서 처리)
        graph = MapGraph.from_json(scenario.map_file)
        sched = TimeWindowScheduler()

        # task_generator: scenario task_profile 반영
        tp = scenario.task_profile
        task_gen = TaskGenerator(
            graph=graph,
            bus=bus,
            task_interval_s=tp.interval_seconds,
            scheduler=sched,
        )

        engine = SimulationEngine(
            graph=graph,
            scheduler=sched,
            task_generator=task_gen,
            policy=scenario.traffic_policy,
        )

        # AGV 등록
        charger_nodes = [
            nid for nid, n in graph.nodes.items()
            if n.role.value == "charger"
        ]

        for i in range(scenario.fleet_size):
            # initial_nodes YAML 지정 우선, 없으면 charger 순환
            if scenario.initial_nodes and i < len(scenario.initial_nodes):
                start = scenario.initial_nodes[i]
            elif charger_nodes:
                start = charger_nodes[i % len(charger_nodes)]
            else:
                start = next(iter(graph.nodes))

            agv = AGV(
                agv_id=f"AGV_{i+1:03d}",
                bus=bus,
                graph=graph,
                scheduler=sched,
                policy=scenario.traffic_policy,
            )
            if start in graph.nodes:
                agv.current_node_id = start
                agv._motion.snap_to(
                    graph.nodes[start].x,
                    graph.nodes[start].y,
                )
            engine.register_agv(agv)

        result = await engine.run(duration_s=float(scenario.runtime_seconds))

        # 메타 정보 추가
        result["scenario_name"]  = scenario.name
        result["fleet_size"]     = scenario.fleet_size
        result["lane_mode"]      = scenario.traffic_policy.lane_mode
        result["lane_count"]     = scenario.traffic_policy.lane_count
        result["reservation_mode"] = scenario.traffic_policy.reservation_mode
        result["lookahead_depth"]  = scenario.traffic_policy.lookahead_depth
        result["random_seed"]    = scenario.random_seed

        await bus.disconnect()
        return result

    @classmethod
    def run_sync(cls, scenario: Scenario) -> dict:
        """동기 래퍼 — batch runner / 테스트용."""
        return asyncio.run(cls.run(scenario))
