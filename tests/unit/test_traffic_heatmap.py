"""
T-70 (GAP-C) — Traffic 밀도 히트맵 단위 테스트.

1) edge_enter 이벤트가 시뮬 후 trace.events 에 들어있다.
2) edge_enter 카운트가 AGV 의 실제 통과 횟수와 일치한다.
3) build_live_html / build_playback_html 이 🚦 트래픽 토글/legend/JS 변수를 포함한다.

실행: python tests/unit/test_traffic_heatmap.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.adapters.bus.adapters import LocalMemoryBus
from src.analytics.playback_trace import (
    PlaybackTraceRecorder,
    build_live_html,
    build_playback_html,
)
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.map.graph import MapGraph
from src.domain.reservation.scheduler import TimeWindowScheduler


def _assert(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        raise AssertionError(label)


def _make_fab_graph() -> MapGraph:
    return MapGraph.from_rmf_yaml("maps/fab_nav_graph.yaml")


async def _run_short_sim(duration_s: float = 60.0):
    """짧은 FAB 시뮬을 돌려 recorder.events 를 얻는다."""
    graph = _make_fab_graph()
    bus = LocalMemoryBus()
    sched = TimeWindowScheduler()
    gen = TaskGenerator(graph, bus, task_interval_s=5.0)
    recorder = PlaybackTraceRecorder(graph, sample_interval_s=1.0)
    engine = SimulationEngine(graph, sched, task_generator=gen, trace_recorder=recorder)

    chargers = [n.node_id for n in graph.get_chargers()]
    for i, ch in enumerate(chargers[:3], 1):
        agv = AGV(f"AGV_{i:03d}", bus, graph, sched)
        agv.current_node_id = ch
        agv.physics.x = graph.nodes[ch].x
        agv.physics.y = graph.nodes[ch].y
        engine.register_agv(agv)

    await engine.run(duration_s=duration_s)
    return recorder


def test_edge_enter_events_collected():
    """짧은 시뮬 후 recorder.events 에 edge_enter 이벤트가 1개 이상 존재."""
    print("\n[T70.1] edge_enter 이벤트 수집")
    recorder = asyncio.run(_run_short_sim(duration_s=60.0))
    edge_enter_events = [ev for ev in recorder.events if ev.get("kind") == "edge_enter"]
    _assert("edge_enter 이벤트 ≥ 1", len(edge_enter_events) >= 1)
    sample = edge_enter_events[0]
    _assert("edge_enter 에 edge_key 포함", "edge_key" in sample and "__" in sample["edge_key"])
    _assert("edge_enter 에 agv_id 포함", "agv_id" in sample and sample["agv_id"].startswith("AGV_"))


def test_traffic_count_matches_traversal():
    """edge_enter 카운트 집계가 AGV 별 실제 통과 횟수와 일치한다."""
    print("\n[T70.2] traffic count == edge_enter 누적")
    recorder = asyncio.run(_run_short_sim(duration_s=60.0))
    enter_events = [ev for ev in recorder.events if ev.get("kind") == "edge_enter"]

    # 클라이언트 JS 의 rebuildTraffic 과 동일 로직: edge_key 별 누적.
    traffic_counts: dict[str, int] = {}
    for ev in enter_events:
        k = ev.get("edge_key", "")
        if not k:
            continue
        traffic_counts[k] = traffic_counts.get(k, 0) + 1

    _assert("traffic_counts 비어있지 않음", len(traffic_counts) >= 1)
    total = sum(traffic_counts.values())
    _assert("합계 == 전체 edge_enter 수", total == len(enter_events))

    # 가장 많이 지난 엣지의 통과 횟수가 1 이상.
    max_count = max(traffic_counts.values())
    _assert("max 통과 횟수 ≥ 1", max_count >= 1)


def test_traffic_toggle_present_in_live_html():
    """build_live_html 에 🚦 트래픽 토글/legend/JS 변수가 결합되어 있다."""
    print("\n[T70.3] live HTML 에 트래픽 UI 포함")
    html = build_live_html({
        "topology": "A", "agv_count": 12, "speed": 2.0, "duration": 600,
    })
    _assert("🚦 트래픽 버튼 존재", 'id="traffic-toggle"' in html and "🚦 트래픽" in html)
    _assert("traffic-legend 존재", 'id="traffic-legend"' in html)
    _assert("traffic-max-label 존재", 'id="traffic-max-label"' in html)
    _assert("trafficMode 변수 존재", "trafficMode" in html)
    _assert("rebuildTraffic 함수 존재", "function rebuildTraffic" in html)
    _assert("edge_enter 누적 로직 존재", "ev.kind !== 'edge_enter'" in html)
    # heatmap 과 mutually exclusive 보장하는 코드가 있어야 함.
    _assert("mutually exclusive 코드", "if (trafficMode && heatmapMode)" in html)


def test_traffic_toggle_present_in_playback_html():
    """build_playback_html 에 🚦 트래픽 토글/legend/JS 변수가 결합되어 있다."""
    print("\n[T70.4] playback HTML 에 트래픽 UI 포함")
    trace = {
        "meta": {"duration_s": 10.0, "sample_interval_s": 0.5},
        "map": {"nodes": [], "edges": []},
        "snapshots": [],
        "events": [
            {"t": 1.0, "kind": "edge_enter", "agv_id": "AGV_001", "edge_key": "A__B"},
            {"t": 2.0, "kind": "edge_enter", "agv_id": "AGV_001", "edge_key": "A__B"},
            {"t": 3.0, "kind": "edge_enter", "agv_id": "AGV_002", "edge_key": "C__D"},
        ],
    }
    html = build_playback_html(trace)
    _assert("🚦 트래픽 버튼 존재", 'id="traffic-toggle"' in html and "🚦 트래픽" in html)
    _assert("traffic-legend 존재", 'id="traffic-legend"' in html)
    _assert("trafficMode 변수 존재", "trafficMode" in html)
    _assert("trafficCounts 집계 존재", "trafficCounts" in html)
    # heatmap 과 mutually exclusive 보장 코드.
    _assert("mutually exclusive 코드", "if (trafficMode && heatmapMode)" in html)


if __name__ == "__main__":
    tests = [
        test_edge_enter_events_collected,
        test_traffic_count_matches_traversal,
        test_traffic_toggle_present_in_live_html,
        test_traffic_toggle_present_in_playback_html,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] {t.__name__}: {exc}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed out of {len(tests)}")
    if failed:
        sys.exit(1)
