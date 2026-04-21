"""
vda5050_simulator 실행 진입점.
config.yaml 또는 --scenario YAML로 단일 시나리오 실행.
"""
from __future__ import annotations

import asyncio
import argparse
import yaml # type: ignore

from src.domain.map.graph import MapGraph
from src.domain.reservation.scheduler import TimeWindowScheduler
from src.domain.agv.agv import AGV
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator


async def run_from_config(config: dict) -> None:
    bus_type = config.get("bus", "memory")
    if bus_type == "mqtt":
        from src.adapters.bus.adapters import MQTTAdapter
        bus = MQTTAdapter(
            host=config["mqtt"]["host"],
            port=config["mqtt"].get("port", 1883),
        )
    else:
        from src.adapters.bus.adapters import LocalMemoryBus
        bus = LocalMemoryBus()

    await bus.connect()

    graph    = MapGraph.from_json(config["map"])
    sched    = TimeWindowScheduler()
    task_gen = TaskGenerator(
        graph=graph,
        bus=bus,
        task_interval_s=config.get("task_interval_s", 30.0),
    )
    engine = SimulationEngine(graph, sched, task_generator=task_gen)

    for agv_cfg in config.get("agvs", []):
        agv = AGV(
            agv_id=agv_cfg["id"],
            bus=bus,
            graph=graph,
            scheduler=sched,
        )
        start_node = agv_cfg.get("start_node")
        if start_node and start_node in graph.nodes:
            agv.current_node_id = start_node
            agv._motion.snap_to(
                graph.nodes[start_node].x,
                graph.nodes[start_node].y,
            )
        engine.register_agv(agv)

    results = await engine.run(duration_s=config.get("duration_s", 3600))
    print(results)
    await bus.disconnect()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", help="Scenario YAML file")
    parser.add_argument("--experiment", help="Experiment matrix YAML file")
    args = parser.parse_args()

    if args.experiment:
        from src.application.usecases.run_batch_experiments import run_batch
        await run_batch(args.experiment)
        return

    config_path = args.scenario or "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    await run_from_config(config)


if __name__ == "__main__":
    asyncio.run(main())