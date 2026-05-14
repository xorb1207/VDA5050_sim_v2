"""
JobDispatcher + TaskGenerator manual 모드 + 신규 KPI 키 통합 테스트.
실행: python -m pytest tests/integration/test_dispatch.py -v
또는: python tests/integration/test_dispatch.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.adapters.bus.adapters import LocalMemoryBus
from src.adapters.job_api import JobApi
from src.application.dispatch import JobDispatcher
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.map.graph import MapGraph
from src.domain.reservation.scheduler import TimeWindowScheduler


def make_graph() -> MapGraph:
    return MapGraph.from_json("maps/sample_fab.json")


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


def _seat_agv(graph: MapGraph, bus, sched, agv_id: str, node_id: str) -> AGV:
    agv = AGV(agv_id, bus, graph, sched)
    agv.current_node_id = node_id
    node = graph.nodes[node_id]
    agv.physics.x = node.x
    agv.physics.y = node.y
    return agv


def _pick_two_work_nodes(graph: MapGraph) -> tuple[str, str]:
    from src.domain.map.graph import NodeRole
    work = [
        nid for nid, n in graph.nodes.items()
        if n.role == NodeRole.WORK or n.is_parking_spot
    ]
    for a in work:
        for b in work:
            if a != b and graph.get_path(a, b):
                return a, b
    raise RuntimeError("no routeable work pair")


# ─────────────────────────────────────────────
# T-D1: dispatch → 올바른 AMR에 order 발행
# ─────────────────────────────────────────────

async def _test_dispatch_assigns_job_to_correct_amr():
    print("\n[T-D1] dispatch 성공 시 지정 AMR에 order 발행")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()

    start, dest = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    agvs = {"AMR-001": agv}

    received = []
    async def cap(payload): received.append(payload)
    await bus.subscribe("uagv/v2/NEXT/AMR-001/order", cap)

    dispatcher = JobDispatcher(graph, bus, agvs)
    result = await dispatcher.dispatch("AMR-001", dest, sim_time=0.0)

    assert_eq("status", result.status, "success")
    assert_true("job_id 발급", result.job_id is not None)
    assert_true("ETA > 0", (result.estimated_arrival_s or 0.0) > 0.0)
    assert_eq("order 1건 발행", len(received), 1)
    assert_eq("destination 도착 node", received[0]["nodes"][-1]["nodeId"], dest)


def test_dispatch_assigns_job_to_correct_amr():
    run(_test_dispatch_assigns_job_to_correct_amr())


# ─────────────────────────────────────────────
# T-D2: AMR이 busy면 reject
# ─────────────────────────────────────────────

async def _test_dispatch_fails_when_amr_busy():
    print("\n[T-D2] AMR이 busy 상태면 reject")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    start, dest = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    await agv.start()  # order 토픽 구독 + state machine 활성
    agvs = {"AMR-001": agv}
    dispatcher = JobDispatcher(graph, bus, agvs)

    # 1차 dispatch 성공 → 메시지 버스가 _on_order_received를 동기적으로 호출,
    # AMR이 NAVIGATING/WAITING_RESERVATION으로 진입.
    first = await dispatcher.dispatch("AMR-001", dest, sim_time=0.0)
    assert_eq("1차 status", first.status, "success")

    second = await dispatcher.dispatch("AMR-001", dest, sim_time=0.1)
    assert_eq("2차 status", second.status, "amr_busy")
    assert_true("reason 채워짐", second.reason is not None)


def test_dispatch_fails_when_amr_busy():
    run(_test_dispatch_fails_when_amr_busy())


# ─────────────────────────────────────────────
# T-D3: 알 수 없는 destination
# ─────────────────────────────────────────────

async def _test_dispatch_fails_when_node_not_found():
    print("\n[T-D3] 존재하지 않는 destination → node_not_found")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    start, _ = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    dispatcher = JobDispatcher(graph, bus, {"AMR-001": agv})

    result = await dispatcher.dispatch("AMR-001", "node_does_not_exist")
    assert_eq("status", result.status, "node_not_found")
    assert_eq("job_id 미발급", result.job_id, None)


def test_dispatch_fails_when_node_not_found():
    run(_test_dispatch_fails_when_node_not_found())


# ─────────────────────────────────────────────
# T-D4: 알 수 없는 AMR
# ─────────────────────────────────────────────

async def _test_dispatch_fails_when_amr_not_found():
    print("\n[T-D4] 등록되지 않은 AMR → amr_not_found")
    graph = make_graph()
    bus = LocalMemoryBus()
    dispatcher = JobDispatcher(graph, bus, {})
    _, dest = _pick_two_work_nodes(graph)
    result = await dispatcher.dispatch("AMR-ghost", dest)
    assert_eq("status", result.status, "amr_not_found")


def test_dispatch_fails_when_amr_not_found():
    run(_test_dispatch_fails_when_amr_not_found())


# ─────────────────────────────────────────────
# T-D5: manual 모드는 자동 발행 안 함
# ─────────────────────────────────────────────

async def _test_manual_mode_no_auto_generation():
    print("\n[T-D5] manual 모드: TaskGenerator.step()이 order를 발행하지 않음")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    start, _ = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    agvs = {"AMR-001": agv}

    received = []
    async def cap(payload): received.append(payload)
    await bus.subscribe("uagv/v2/NEXT/AMR-001/order", cap)

    gen = TaskGenerator(graph, bus, task_interval_s=0.0, mode="manual")
    # interval 0 + manual: 평소라면 매 tick 발행되어야 하는데 manual은 무시.
    for t in range(5):
        await gen.step(sim_time=float(t), agvs=agvs)

    assert_eq("manual 모드에서 order 0건", len(received), 0)
    assert_eq("diagnostics.dispatch_mode", gen.diagnostics["dispatch_mode"], "manual")


def test_manual_mode_no_auto_generation():
    run(_test_manual_mode_no_auto_generation())


# ─────────────────────────────────────────────
# T-D6: auto 모드는 기존 동작 유지
# ─────────────────────────────────────────────

async def _test_auto_mode_still_publishes():
    print("\n[T-D6] auto 모드 (기존 default): order 발행 유지")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    start, _ = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    agvs = {"AMR-001": agv}

    received = []
    async def cap(payload): received.append(payload)
    await bus.subscribe("uagv/v2/NEXT/AMR-001/order", cap)

    gen = TaskGenerator(graph, bus, task_interval_s=0.0)  # 기본 auto
    await gen.step(sim_time=0.0, agvs=agvs)
    assert_eq("auto 모드 1건 발행", len(received), 1)
    assert_eq("diagnostics.dispatch_mode", gen.diagnostics["dispatch_mode"], "auto")


def test_auto_mode_still_publishes():
    run(_test_auto_mode_still_publishes())


# ─────────────────────────────────────────────
# T-D7: JobApi dict-in/out 스키마
# ─────────────────────────────────────────────

async def _test_job_api_schema():
    print("\n[T-D7] JobApi: dict-in / dict-out 스키마 고정")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    start, dest = _pick_two_work_nodes(graph)
    agv = _seat_agv(graph, bus, sched, "AMR-001", start)
    api = JobApi(JobDispatcher(graph, bus, {"AMR-001": agv}))

    ok = await api.dispatch({
        "amr_id": "AMR-001",
        "destination_node_id": dest,
    })
    for key in ("job_id", "status", "estimated_arrival_s", "reason"):
        assert_true(f"응답 키 '{key}' 존재", key in ok)
    assert_eq("status=success", ok["status"], "success")

    miss = await api.dispatch({})  # empty
    assert_eq("빈 요청 → amr_not_found", miss["status"], "amr_not_found")
    assert_eq("빈 요청 → job_id null", miss["job_id"], None)


def test_job_api_schema():
    run(_test_job_api_schema())


# ─────────────────────────────────────────────
# T-D8: 신규 KPI 키 존재 확인
# ─────────────────────────────────────────────

async def _test_new_kpi_fields_exposed():
    print("\n[T-D8] 신규 KPI 키 노출: max_wait_time_s / deadlock_count / node_contention_rate")
    graph = make_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    gen = TaskGenerator(graph, bus, task_interval_s=10.0)
    engine = SimulationEngine(graph, sched, task_generator=gen)

    for i, ch in enumerate(["node_charger_01", "node_charger_02"], 1):
        agv = _seat_agv(graph, bus, sched, f"AGV_00{i}", ch)
        engine.register_agv(agv)

    results = await engine.run(duration_s=60.0)
    for key in ("max_wait_time_s", "deadlock_count", "node_contention_rate"):
        assert_true(f"KPI 키 '{key}' 노출", key in results)
    assert_true(
        "max_wait_time_s >= 0",
        isinstance(results["max_wait_time_s"], (int, float)) and results["max_wait_time_s"] >= 0.0,
    )
    assert_true(
        "node_contention_rate ∈ [0,1]",
        0.0 <= results["node_contention_rate"] <= 1.0,
    )
    assert_true(
        "deadlock_count == deadlock_or_stall_count",
        results["deadlock_count"] == results["deadlock_or_stall_count"],
    )


def test_new_kpi_fields_exposed():
    run(_test_new_kpi_fields_exposed())


if __name__ == "__main__":
    import traceback
    tests = [
        test_dispatch_assigns_job_to_correct_amr,
        test_dispatch_fails_when_amr_busy,
        test_dispatch_fails_when_node_not_found,
        test_dispatch_fails_when_amr_not_found,
        test_manual_mode_no_auto_generation,
        test_auto_mode_still_publishes,
        test_job_api_schema,
        test_new_kpi_fields_exposed,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"결과: {passed} passed / {failed} failed")
    sys.exit(0 if failed == 0 else 1)
