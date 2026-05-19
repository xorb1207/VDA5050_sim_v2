from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class PlaybackTraceRecorder:
    def __init__(self, graph, sample_interval_s: float = 0.5) -> None:
        self._graph = graph
        self.sample_interval_s = max(0.05, sample_interval_s)
        self._next_sample_time = 0.0
        self.snapshots: list[dict] = []
        self.events: list[dict] = []

    def record_event(self, kind: str, sim_time: float, **payload) -> None:
        event = {"t": round(sim_time, 4), "kind": kind}
        event.update(payload)
        self.events.append(event)

    def sample(self, sim_time: float, agvs: dict, scheduler) -> None:
        if sim_time + 1e-9 < self._next_sample_time:
            return
        self._next_sample_time = sim_time + self.sample_interval_s
        active_edge_by_agv: dict[str, list[str]] = {}
        for edge_key, reservations in getattr(scheduler, "_edge_reservations", {}).items():
            for reservation in reservations:
                if reservation.released:
                    continue
                if reservation.start_time <= sim_time <= reservation.end_time:
                    active_edge_by_agv.setdefault(reservation.agv_id, []).append(edge_key)
        snapshot = {
            "t": round(sim_time, 4),
            "agvs": [],
            "waiting_for": dict(getattr(scheduler, "_waiting_for", {})),
        }
        for agv in agvs.values():
            planned_edge_keys: list[str] = []
            node_chain: list[str] = []
            if getattr(agv, "current_node_id", None):
                node_chain.append(agv.current_node_id)
            if getattr(agv, "target_node_id", None):
                node_chain.append(agv.target_node_id)
            path = list(getattr(agv, "_path", []) or [])
            path_index = int(getattr(agv, "_path_index", 0) or 0)
            if path:
                tail = path[path_index:]
                if node_chain and tail and tail[0] == node_chain[-1]:
                    tail = tail[1:]
                node_chain.extend(tail[:5])
            for src_id, dst_id in zip(node_chain, node_chain[1:]):
                planned_edge_keys.append(f"{src_id}__{dst_id}")
            current_edge_key = (
                f"{agv.current_node_id}__{agv.target_node_id}"
                if getattr(agv, "current_node_id", None) and getattr(agv, "target_node_id", None)
                else ""
            )
            # 빨강 차단 edge는 "다른 AGV에 의해 막혔다"는 의미만 표현한다.
            # 가감속/재출발 지연(_restart_delay_remaining) 같은 자체 대기는 표시하지 않는다.
            pending_src = getattr(agv, "_pending_edge_src", None)
            pending_dst = getattr(agv, "_pending_edge_dst", None)
            blocking_agv_id = snapshot["waiting_for"].get(agv.agv_id, "")
            collision_retry_count = int(getattr(agv, "collision_retry_count", 0) or 0)
            actually_blocked = bool(blocking_agv_id) or collision_retry_count > 0
            if not actually_blocked:
                blocked_edge_key = ""
            elif pending_src and pending_dst:
                blocked_edge_key = f"{pending_src}__{pending_dst}"
            elif blocking_agv_id and agv.state.value == "WAITING_RESERVATION":
                next_hop = None
                if path and path_index < len(path):
                    candidate = path[path_index]
                    if candidate and candidate != agv.current_node_id:
                        next_hop = candidate
                    elif path_index + 1 < len(path):
                        next_hop = path[path_index + 1]
                if agv.current_node_id and next_hop:
                    blocked_edge_key = f"{agv.current_node_id}__{next_hop}"
                else:
                    blocked_edge_key = ""
            else:
                blocked_edge_key = ""
            goal_node_id = path[-1] if path else (getattr(agv, "target_node_id", "") or "")
            pickup_node_id = getattr(agv, "_current_pickup_node_id", None) or ""
            dropoff_node_id = getattr(agv, "_current_dropoff_node_id", None) or ""
            phase = ""
            immediate_goal = goal_node_id
            if pickup_node_id and dropoff_node_id:
                upcoming = set(path[path_index:])
                if pickup_node_id in upcoming:
                    phase = "pickup"
                    immediate_goal = pickup_node_id
                else:
                    phase = "dropoff"
                    immediate_goal = dropoff_node_id
            detour_via_siding = ""
            for nid in path[path_index:]:
                if not nid or nid == goal_node_id or nid == immediate_goal:
                    continue
                if nid.startswith("SD_"):
                    detour_via_siding = nid
                    break
            snapshot["agvs"].append({
                "agv_id": agv.agv_id,
                "state": agv.state.value,
                "x": round(agv.physics.x, 3),
                "y": round(agv.physics.y, 3),
                "heading": round(agv.physics.heading, 3),
                "speed": round(agv.physics.speed, 3),
                "battery_pct": round(getattr(agv, "_battery_pct", 0.0), 2),
                "current_node": agv.current_node_id,
                "target_node": agv.target_node_id,
                "goal_node": goal_node_id,
                "pickup_node": pickup_node_id,
                "dropoff_node": dropoff_node_id,
                "phase": phase,
                "immediate_goal": immediate_goal,
                "detour_via": detour_via_siding,
                "current_edge_key": current_edge_key,
                "planned_edge_keys": planned_edge_keys,
                "reserved_edge_keys": active_edge_by_agv.get(agv.agv_id, []),
                "blocked_edge_key": blocked_edge_key,
                "blocking_agv": snapshot["waiting_for"].get(agv.agv_id, ""),
                "current_demand_id": getattr(agv, "_current_demand_id", "") or "",
            })
        self.snapshots.append(snapshot)

    def build_trace(self, duration_s: float, extra_meta: Optional[dict] = None) -> dict:
        meta = {
            "duration_s": round(duration_s, 3),
            "sample_interval_s": self.sample_interval_s,
        }
        if extra_meta:
            meta.update(extra_meta)
        return {
            "meta": meta,
            "map": self._serialize_map(),
            "snapshots": self.snapshots,
            "events": self.events,
        }

    def save_json(self, path: Path, duration_s: float) -> None:
        path.write_text(
            json.dumps(self.build_trace(duration_s), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _serialize_map(self) -> dict:
        nodes = [
            {
                "node_id": node.node_id,
                "x": round(node.x, 3),
                "y": round(node.y, 3),
                "role": node.role.value,
                "is_charger": node.is_charger,
                "is_parking_spot": node.is_parking_spot,
            }
            for node in self._graph.nodes.values()
        ]
        edges = []
        for edge_id, edge in self._graph.edges.items():
            src = self._graph.nodes.get(edge.start_node_id)
            dst = self._graph.nodes.get(edge.end_node_id)
            if src is None or dst is None:
                continue
            edges.append({
                "edge_id": edge_id,
                "edge_key": f"{edge.start_node_id}__{edge.end_node_id}",
                "start_node_id": edge.start_node_id,
                "end_node_id": edge.end_node_id,
                "x1": round(src.x, 3),
                "y1": round(src.y, 3),
                "x2": round(dst.x, 3),
                "y2": round(dst.y, 3),
                "corridor": edge.corridor,
                "access_type": edge.access_type,
                "width_m": edge.width_m,
                "bidirectional": bool(getattr(edge, "bidirectional", False)),
            })
        return {"nodes": nodes, "edges": edges}


def build_playback_html(trace: dict) -> str:
    trace_json = json.dumps(trace, ensure_ascii=False)
    html = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Playback</title>
  <style>
    :root {
      color-scheme: light;
      /* Surfaces */
      --bg: #f4f6f9;
      --surface: #ffffff;
      --surface-2: #fafbfd;
      --surface-3: #f1f4f8;
      --panel: var(--surface);
      /* Ink */
      --ink: #0f172a;
      --text: var(--ink);
      --muted: #64748b;
      --muted-2: #94a3b8;
      /* Border */
      --border: #e3e8ef;
      --border-strong: #cbd3df;
      /* Edge state colors */
      --edge: #d3dae4;
      --edge-plan: #8bb4ff;
      --edge-reserved: #2459d1;
      --edge-active: #0f9d58;
      --edge-blocked: #c0392b;
      /* Node colors */
      --node-work: #0f9d58;
      --node-charger: #1f6feb;
      --node-siding: #e0a000;
      /* Accents */
      --accent: #2563eb;
      --accent-soft: #e6efff;
      --success: #0f9d58;
      --success-soft: #e6f7ee;
      --warn: #c77700;
      --warn-soft: #fff3df;
      --danger: #c0392b;
      --danger-soft: #fde8e7;
      /* Type */
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Pretendard", "Apple SD Gothic Neo", sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
      /* Shadows */
      --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.05);
      --shadow-md: 0 1px 3px rgba(15, 23, 42, 0.05), 0 6px 18px -10px rgba(15, 23, 42, 0.10);
      /* Radii */
      --radius-sm: 6px;
      --radius: 10px;
      --radius-lg: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--font-sans);
      font-size: 13px;
      line-height: 1.5;
      letter-spacing: -0.005em;
      background: var(--bg);
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    h1, h2, h3 { letter-spacing: -0.015em; margin: 0; }
    .page {
      max-width: 1480px;
      margin: 0 auto;
      padding: 20px 18px 32px;
      display: grid;
      gap: 14px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px 16px;
      box-shadow: var(--shadow-sm);
    }
    .top { display: grid; gap: 12px; }
    .header-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      align-items: end;
      justify-content: space-between;
    }
    .header-title { display: grid; gap: 2px; }
    .header-eyebrow {
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      font-weight: 600;
    }
    .header-title h1 {
      font-size: 22px;
      line-height: 1.1;
      font-weight: 700;
    }
    .topology-headline-line {
      margin: 4px 0 0 0;
      color: var(--accent);
      font-size: 13px;
      font-weight: 500;
    }
    .topology-headline-line:empty { display: none; }
    .topology-meta-chips {
      margin-top: 6px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .topology-meta-chips:empty { display: none; }
    .topology-meta-chips .meta-chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--surface-3);
      font-size: 11px;
      color: var(--muted);
    }
    .topology-meta-chips .meta-chip strong {
      color: var(--ink);
      font-family: var(--font-mono);
      font-weight: 600;
    }
    .topology-meta-chips .meta-chip.type-tag {
      background: var(--accent-soft);
      color: #1d4ed8;
      font-weight: 700;
    }
    .kpi-strip {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .kpi-chip {
      display: inline-flex;
      align-items: baseline;
      gap: 6px;
      padding: 6px 10px;
      background: var(--surface-3);
      border-radius: 999px;
      font-size: 12px;
      color: var(--muted);
    }
    .kpi-chip strong {
      color: var(--ink);
      font-family: var(--font-mono);
      font-weight: 600;
      font-size: 12.5px;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 10;
      padding: 10px 12px;
      background: rgba(255,255,255,0.92);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      backdrop-filter: saturate(140%) blur(10px);
      -webkit-backdrop-filter: saturate(140%) blur(10px);
      box-shadow: var(--shadow-sm);
    }
    .toolbar-group {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding-left: 10px;
      border-left: 1px solid var(--border);
    }
    .toolbar-group:first-of-type { padding-left: 0; border-left: 0; }
    .toolbar-group .meta { font-size: 11px; }
    .time-pill {
      display: inline-flex;
      align-items: baseline;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--surface-3);
      font-family: var(--font-mono);
      font-size: 13px;
      font-weight: 600;
      color: var(--ink);
      letter-spacing: 0.01em;
      white-space: nowrap;
    }
    .time-pill .meta { font-family: var(--font-sans); font-weight: 500; color: var(--muted); }
    .playback-stage { display: grid; gap: 10px; }
    .map-stage { display: grid; gap: 10px; }
    button {
      height: 30px;
      padding: 0 12px;
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: var(--radius-sm);
      color: var(--ink);
      font-family: inherit;
      font-size: 12.5px;
      font-weight: 500;
      cursor: pointer;
      transition: background 120ms, border-color 120ms, color 120ms, box-shadow 120ms;
    }
    button:hover { background: var(--surface-3); border-color: var(--border-strong); }
    button:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px var(--accent-soft), 0 0 0 3px var(--accent);
    }
    .play-toggle {
      min-width: 76px;
      font-weight: 600;
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .play-toggle:hover { background: #1a253b; border-color: #1a253b; }
    .play-toggle.is-playing { background: var(--surface); color: var(--ink); border-color: var(--border-strong); font-weight: 500; }
    .play-toggle.is-playing:hover { background: var(--surface-3); }
    select {
      height: 30px;
      padding: 0 26px 0 10px;
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: var(--radius-sm);
      color: var(--ink);
      font: inherit;
      font-size: 12.5px;
      appearance: none;
      -webkit-appearance: none;
      background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'><path fill='%2364748b' d='M3 4.5l3 3 3-3z'/></svg>");
      background-repeat: no-repeat;
      background-position: right 8px center;
      cursor: pointer;
    }
    .speed-btn { padding: 0 10px; min-width: 40px; font-variant-numeric: tabular-nums; }
    .speed-btn.active {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .speed-btn.active:hover { background: #1d4ed8; border-color: #1d4ed8; }
    /* Heatmap toggle 활성 상태 */
    #heatmap-toggle.active {
      background: #c0392b;
      color: #fff;
      border-color: #c0392b;
    }
    /* Traffic toggle 활성 상태 (T-70 / GAP-C) — 사고 히트맵과 색조 구분 */
    #traffic-toggle.active {
      background: #4338ca;
      color: #fff;
      border-color: #4338ca;
    }
    /* Collision toggle 활성 상태 */
    #collision-toggle.active {
      background: #f59e0b;
      color: #fff;
      border-color: #f59e0b;
    }
    /* Heatmap 범례 (활성 시만 표시) */
    .heatmap-legend {
      display: none;
      align-items: center;
      gap: 8px;
      font-size: 11.5px;
      color: var(--muted);
      padding: 4px 10px;
      background: var(--surface-2);
      border-radius: 6px;
      margin-left: 8px;
    }
    .heatmap-legend.active { display: inline-flex; }
    .heatmap-legend .gradient {
      width: 80px; height: 6px; border-radius: 3px;
      background: linear-gradient(90deg, #fde6e2 0%, #f5a695 50%, #c0392b 100%);
    }
    /* Traffic 범례 — heatmap 과 동일 구조, gradient 색조만 다르게 */
    .traffic-legend {
      display: none;
      align-items: center;
      gap: 8px;
      font-size: 11.5px;
      color: var(--muted);
      padding: 4px 10px;
      background: var(--surface-2);
      border-radius: 6px;
      margin-left: 8px;
    }
    .traffic-legend.active { display: inline-flex; }
    .traffic-legend .gradient {
      width: 80px; height: 6px; border-radius: 3px;
      background: linear-gradient(90deg, #e0e7ff 0%, #818cf8 50%, #4338ca 100%);
    }
    /* Range slider polish */
    input[type=range] {
      -webkit-appearance: none;
      width: min(540px, 100%);
      height: 6px;
      background: transparent;
      cursor: pointer;
    }
    input[type=range]::-webkit-slider-runnable-track {
      height: 6px;
      border-radius: 999px;
      background: var(--surface-3);
    }
    input[type=range]::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--accent);
      border: 2px solid #fff;
      margin-top: -4px;
      box-shadow: 0 0 0 1px var(--accent), 0 1px 2px rgba(15,23,42,0.2);
    }
    input[type=range]::-moz-range-track {
      height: 6px;
      border-radius: 999px;
      background: var(--surface-3);
    }
    input[type=range]::-moz-range-thumb {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--accent);
      border: 2px solid #fff;
      box-shadow: 0 0 0 1px var(--accent), 0 1px 2px rgba(15,23,42,0.2);
    }
    /* Timeline event markers — 슬라이더 위에 사고 시점 점 표시 */
    .slider-wrap {
      position: relative;
      width: min(540px, 100%);
      display: inline-block;
      vertical-align: middle;
    }
    .slider-wrap input[type=range] { display: block; }
    .event-markers {
      position: absolute;
      left: 0; right: 0;
      top: -4px;            /* 슬라이더 트랙 위로 살짝 띄움 */
      height: 8px;
      pointer-events: none; /* 마커가 슬라이더 드래그 막지 않도록. 마커 자체는 다시 enable */
    }
    .event-marker {
      position: absolute;
      width: 4px;
      height: 8px;
      border-radius: 1px;
      transform: translateX(-50%);
      pointer-events: auto;
      cursor: pointer;
      opacity: 0.85;
      transition: opacity 0.15s, transform 0.15s;
    }
    .event-marker:hover {
      opacity: 1;
      transform: translateX(-50%) scaleY(1.4);
    }
    .event-marker[data-kind="headon_block"]      { background: #c0392b; }
    .event-marker[data-kind="section_conflict"]  { background: #c77700; }
    .event-marker[data-kind="followon_block"]    { background: #b85450; }
    .event-marker[data-kind="deadlock_resolved"] { background: #6c3aa6; }
    .event-marker[data-kind="reroute"]           { background: #1f6feb; }
    .event-marker[data-kind="reroute_via_siding"]{ background: #4f8df0; }
    .hint {
      color: var(--muted);
      font-size: 11.5px;
    }
    .lower-layout {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }
    .main-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 16px;
      align-items: start;
    }
    .side-stack {
      display: flex;
      flex-direction: column;
      gap: 16px;
      position: sticky;
      top: 76px;
      height: calc(100vh - 96px);
      min-height: 0;
    }
    #agv-detail-panel[hidden] { display: none; }
    #agv-detail-panel { flex: 0 0 30%; }
    #incident-panel { flex: 0 0 32%; }
    #event-panel { flex: 1 1 0; }
    .side-stack.has-focus #incident-panel { flex: 0 0 26%; }
    .side-stack.has-focus #event-panel { flex: 1 1 0; }
    .agv-detail-body {
      display: grid;
      gap: 8px;
      font-size: 12.5px;
      overflow: auto;
      min-height: 0;
      padding-right: 4px;
    }
    .agv-detail-body .row {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 0;
      border-bottom: 1px dashed var(--border);
    }
    .agv-detail-body .row:last-of-type { border-bottom: 0; }
    .agv-detail-body .row .key { color: var(--muted); font-size: 11.5px; }
    .agv-detail-body .row .val-mono { font-family: var(--font-mono); font-size: 12px; }
    .state-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.01em;
      background: var(--surface-3);
      color: var(--muted);
    }
    .state-pill.s-navigating { background: var(--accent-soft); color: #1d4ed8; }
    .state-pill.s-processing { background: var(--success-soft); color: #0b6e3f; }
    .state-pill.s-charging { background: #ddeafe; color: #1d4ed8; }
    .state-pill.s-waiting { background: var(--danger-soft); color: var(--danger); }
    .state-pill.s-idle { background: var(--surface-3); color: var(--muted); }
    .depth-bars { display: grid; gap: 3px; }
    .depth-row {
      display: grid;
      grid-template-columns: 56px 1fr;
      gap: 8px;
      align-items: center;
      font-size: 11.5px;
    }
    .depth-row .bar {
      height: 6px;
      background: linear-gradient(90deg, #2459d1 0%, #2459d1 var(--bar), var(--surface-3) var(--bar));
      border-radius: 999px;
    }
    .depth-row .label { color: var(--muted); font-family: var(--font-mono); font-size: 10.5px; }
    .chain-row {
      display: flex;
      gap: 6px;
      align-items: center;
      font-size: 11.5px;
      flex-wrap: wrap;
    }
    .chain-row .badge {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 1px 8px;
      background: var(--surface);
      font-family: var(--font-mono);
      font-size: 11px;
    }
    .chain-row .arrow { color: var(--muted-2); font-size: 11px; }
    .chain-row.cycle .badge { border-color: var(--danger); color: var(--danger); background: var(--danger-soft); }
    .side-stack .panel {
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
      padding: 12px 14px;
    }
    .side-stack .event-list,
    .side-stack .incident-list {
      max-height: none;
      flex: 1 1 auto;
      min-height: 0;
    }
    .map-panel { padding: 0; overflow: hidden; }
    .map-shell {
      border-top: 1px solid var(--border);
      background: #fbfcfe;
    }
    svg {
      width: 100%;
      height: 760px;
      background: linear-gradient(180deg, #fcfdff 0%, #f7f9fc 100%);
      display: block;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      font-size: 11.5px;
      color: var(--muted);
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
      box-shadow: 0 0 0 1px rgba(15, 23, 42, 0.04);
    }
    .swatch-ring {
      border-radius: 50%;
      background: #fff;
      border: 2px solid #94a3b8;
      box-shadow: none;
    }
    .legend-divider {
      width: 1px;
      align-self: stretch;
      background: var(--border);
      margin: 0 2px;
    }
    .event-list, .incident-list {
      display: grid;
      gap: 6px;
      max-height: 280px;
      overflow: auto;
      padding-right: 4px;
    }
    .event-item, .incident-item {
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 8px 10px;
      background: var(--surface-2);
      font-size: 12.5px;
      line-height: 1.45;
      transition: border-color 100ms, background 100ms, box-shadow 100ms;
    }
    .event-item { font-family: inherit; }
    .event-item .event-head { font-weight: 600; color: var(--ink); }
    .event-item .event-key { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); }
    .incident-item {
      cursor: pointer;
    }
    .incident-item:hover {
      border-color: var(--accent);
      background: var(--accent-soft);
      box-shadow: var(--shadow-sm);
    }
    .meta { color: var(--muted); font-size: 11.5px; }
    .agv-label { font-size: 10px; fill: var(--ink); font-weight: 600; paint-order: stroke; stroke: rgba(255,255,255,0.85); stroke-width: 2px; }
    .section-title {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 10px;
    }
    .section-title h2 {
      font-size: 13px;
      font-weight: 700;
      letter-spacing: -0.005em;
    }
    .map-topline {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
    }
    .incident-item.current {
      border-color: var(--accent);
      background: #eef5ff;
    }
    .subtle {
      color: var(--muted);
      font-size: 12px;
    }
    @media (max-width: 1180px) {
      .main-layout { grid-template-columns: 1fr; }
      .side-stack {
        position: static;
        max-height: none;
      }
      .side-stack > .panel:nth-child(1),
      .side-stack > .panel:nth-child(2) { max-height: none; }
      .side-stack .event-list,
      .side-stack .incident-list { max-height: 320px; }
    }
    @media (max-width: 980px) {
      .lower-layout { grid-template-columns: 1fr; }
      .kpi { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      svg { height: 520px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <section class="panel top">
      <div class="header-row">
        <div class="header-title">
          <div class="header-eyebrow" id="topology-eyebrow">Playback Trace</div>
          <h1 id="topology-title">시뮬레이션 재생</h1>
          <p id="topology-headline" class="topology-headline-line"></p>
          <div id="topology-meta-chips" class="topology-meta-chips"></div>
        </div>
        <div class="kpi-strip" id="kpi-strip">
          <span class="kpi-chip">스냅샷 <strong id="snapshot-count">—</strong></span>
          <span class="kpi-chip">이벤트 <strong id="event-count">—</strong></span>
          <span class="kpi-chip">간격 <strong id="sample-interval">—</strong></span>
          <span class="kpi-chip">사고/병목 <strong id="incident-count">—</strong></span>
        </div>
      </div>
      <div class="playback-stage">
        <div class="toolbar">
          <div class="toolbar-group">
            <button id="play-toggle" class="play-toggle" type="button">재생</button>
          </div>
          <div class="toolbar-group" style="flex: 1 1 320px;">
            <div class="slider-wrap">
              <input id="time-slider" type="range" min="0" max="0" step="1" value="0" />
              <div id="event-markers" class="event-markers"></div>
            </div>
            <span class="time-pill"><span class="meta">t</span><span id="time-label">0.00s</span></span>
          </div>
          <div class="toolbar-group">
            <span class="meta">배속</span>
            <button class="speed-btn active" data-speed="1" type="button">1.0x</button>
            <button class="speed-btn" data-speed="2" type="button">2.0x</button>
            <button class="speed-btn" data-speed="5" type="button">5.0x</button>
          </div>
          <div class="toolbar-group">
            <span class="meta">AGV</span>
            <select id="agv-focus">
              <option value="">전체</option>
            </select>
            <button id="zoom-reset-btn" type="button">줌 초기화</button>
            <button id="heatmap-toggle" type="button" title="엣지별 사고 누적 히트맵 토글">🔥 히트맵</button>
            <button id="traffic-toggle" type="button" title="엣지별 AGV 통과 횟수 트래픽 히트맵 (사고 히트맵과 동시 활성 불가)">🚦 트래픽</button>
            <button id="collision-toggle" type="button" title="실시간 충돌 의심 마커 토글 (같은 노드/엣지 점유)">⚠ 충돌</button>
          </div>
        </div>
        <div class="hint">
          맵: 휠 확대/축소, 드래그 이동, 더블클릭 줌 초기화. AGV 클릭 시 포커스. 우측 사고 묶음 클릭 시 해당 시점으로 점프합니다.
          <span class="heatmap-legend" id="heatmap-legend">
            누적 사고 강도
            <span class="gradient"></span>
            <span id="heatmap-max-label">최대 —</span>
          </span>
          <span class="traffic-legend" id="traffic-legend">
            통과 횟수
            <span class="gradient"></span>
            <span id="traffic-max-label">최대 —</span>
          </span>
        </div>
      </div>
    </section>

    <section class="main-layout">
      <section class="panel map-panel">
        <div class="map-topline">
          <div class="legend">
            <span class="legend-item"><span class="swatch" style="background:#d3dae4"></span>기본 통로</span>
            <span class="legend-item"><span class="swatch" style="background:#8bb4ff"></span>계획</span>
            <span class="legend-item"><span class="swatch" style="background:#2459d1"></span>예약</span>
            <span class="legend-item"><span class="swatch" style="background:#0f9d58"></span>주행</span>
            <span class="legend-item"><span class="swatch" style="background:#c0392b"></span>차단/대기</span>
            <span class="legend-divider"></span>
            <span class="legend-item"><span class="swatch" style="background:#0f9d58"></span>작업(ST)</span>
            <span class="legend-item"><span class="swatch" style="background:#1f6feb"></span>충전(CH)</span>
            <span class="legend-item"><span class="swatch" style="background:#e0a000"></span>사이딩(SD)</span>
            <span class="legend-item"><span class="swatch swatch-ring"></span>홀딩(HP)</span>
            <span class="legend-item"><span class="swatch" style="background:#3a4555"></span>AGV</span>
          </div>
        </div>
        <div class="map-shell">
          <svg id="map" viewBox="-30 -10 1060 720"></svg>
        </div>
      </section>
      <aside class="side-stack" id="side-stack">
        <div class="panel" id="agv-detail-panel" hidden>
          <div class="section-title">
            <h2 style="margin:0;" id="agv-detail-title">AGV 상세</h2>
            <span class="meta">포커스 AGV</span>
          </div>
          <div id="agv-detail-body" class="agv-detail-body"></div>
        </div>
        <div class="panel" id="incident-panel">
          <div class="section-title">
            <h2 style="margin:0;">대표 사고 묶음</h2>
            <span class="meta">연속 이벤트 압축</span>
          </div>
          <div id="incident-list" class="incident-list"></div>
        </div>
        <div class="panel" id="event-panel">
          <div class="section-title">
            <h2 style="margin:0;">이벤트 로그</h2>
            <span class="meta">최근 20개</span>
          </div>
          <div id="event-list" class="event-list"></div>
        </div>
      </aside>
    </section>
  </div>
  <script>
    const trace = __TRACE_JSON__;
    const snapshots = trace.snapshots || [];
    const events = trace.events || [];
    const map = trace.map || { nodes: [], edges: [] };
    const incidentKinds = new Set(['headon_block', 'section_conflict', 'followon_block', 'deadlock_resolved', 'reroute', 'reroute_via_siding']);
    const incidents = events.filter(event => incidentKinds.has(event.kind));
    const agvIds = Array.from(new Set(snapshots.flatMap(snapshot => (snapshot.agvs || []).map(agv => agv.agv_id)))).sort();
    const nodeIndex = new Map((map.nodes || []).map(node => [node.node_id, node]));
    let index = 0;
    let timer = null;
    let speed = 1;
    let focusedAgvId = '';
    let zoomScale = 1;
    let zoomPanX = 0;
    let zoomPanY = 0;
    let heatmapMode = false;
    let trafficMode = false;     // 🚦 트래픽 (엣지 통과 횟수) — heatmap 과 mutually exclusive
    let collisionMode = false;   // ⚠ 충돌 의심 마커 토글
    // 엣지별 누적 사고 카운트 — heatmap mode 에서 색조로 표시.
    // 사고 종류: headon_block, section_conflict, followon_block (실제 충돌만 가산).
    // reroute / deadlock_resolved 는 결과적 사건이라 가산 제외.
    const HEATMAP_KINDS = new Set(['headon_block', 'section_conflict', 'followon_block']);
    const heatmapCounts = (() => {
      const m = new Map();
      for (const ev of events) {
        if (!HEATMAP_KINDS.has(ev.kind)) continue;
        const k = ev.edge_key || '';
        if (!k) continue;
        m.set(k, (m.get(k) || 0) + 1);
      }
      return m;
    })();
    const heatmapMax = (() => {
      let mx = 0;
      for (const v of heatmapCounts.values()) if (v > mx) mx = v;
      return mx;
    })();
    // 엣지별 통과 횟수 — edge_enter 이벤트 누적 (T-70 / GAP-C).
    // playback 은 trace 가 완성된 상태라 한 번만 집계해 두면 됨.
    const trafficCounts = (() => {
      const m = new Map();
      for (const ev of events) {
        if (ev.kind !== 'edge_enter') continue;
        const k = ev.edge_key || '';
        if (!k) continue;
        m.set(k, (m.get(k) || 0) + 1);
      }
      return m;
    })();
    const trafficMax = (() => {
      let mx = 0;
      for (const v of trafficCounts.values()) if (v > mx) mx = v;
      return mx;
    })();
    let isDragging = false;
    let dragStart = null;
    // Pre-compute one-way edge keys (no reverse counterpart) on main corridors.
    const _edgeKeySet = new Set((map.edges || []).map(e => e.edge_key));
    const directionMarkerCorridors = new Set(['north', 'center', 'south', 'bay']);
    const oneWayEdges = (map.edges || []).filter(e => {
      if (!directionMarkerCorridors.has(e.corridor)) return false;
      if (e.access_type) return false;
      const reverseKey = `${e.end_node_id}__${e.start_node_id}`;
      return !_edgeKeySet.has(reverseKey);
    });
    function immediateGoalNodeId(agv) {
      // The leg the AGV is actually heading toward right now: pickup during
      // pickup phase, dropoff after pickup completes. Falls back to goal_node
      // (= path[-1]) for non-order travel (idle return / charging).
      if (agv.immediate_goal) return agv.immediate_goal;
      if (agv.goal_node) return agv.goal_node;
      if (agv.target_node) return agv.target_node;
      const keys = agv.planned_edge_keys || [];
      if (!keys.length) return '';
      const parts = (keys[keys.length - 1] || '').split('__');
      return parts[1] || '';
    }
    function destinationNodeId(agv) {
      return immediateGoalNodeId(agv);
    }
    function describeGoal(goalId) {
      if (!goalId) return '';
      if (goalId.startsWith('ST_')) return '→' + goalId;
      if (goalId.startsWith('HP_')) return '→휴식 ' + goalId;
      if (goalId.startsWith('CH_')) return '→충전 ' + goalId;
      return '→' + goalId;
    }
    function destinationLabel(agv) {
      const target = immediateGoalNodeId(agv);
      let base = describeGoal(target);
      if (!base) return '';
      if (agv.phase === 'pickup') base += ' [픽업]';
      else if (agv.phase === 'dropoff') base += ' [드롭]';
      if (agv.detour_via) base += ' (via ' + agv.detour_via + ')';
      return base;
    }
    function agvCaption(agv, includeStateForCluster) {
      const state = agv.state;
      if (state === 'NAVIGATING') {
        const dest = destinationLabel(agv);
        return dest ? (agv.agv_id + ' ' + dest) : agv.agv_id;
      }
      if (state === 'WAITING_RESERVATION') return agv.agv_id + ' (대기)';
      if (state === 'PROCESSING') return agv.agv_id + ' (작업중)';
      if (state === 'CHARGING') return agv.agv_id + ' (충전중)';
      if (state === 'IDLE') return includeStateForCluster ? agv.agv_id : agv.agv_id + ' (IDLE)';
      return agv.agv_id + ' (' + state + ')';
    }
    let highlightedIncident = null; // { edge_key, agv_id, until }
    let highlightTimer = null;
    const HIGHLIGHT_DURATION_MS = 4500;
    function clearHighlight() {
      highlightedIncident = null;
      if (highlightTimer) { clearTimeout(highlightTimer); highlightTimer = null; }
      render();
    }
    function setHighlight(edgeKey, agvId, opts) {
      const meta = opts || {};
      highlightedIncident = {
        edge_key: edgeKey || '',
        agv_id: agvId || '',
        chain: meta.chain || [],
        cycle: !!meta.cycle,
        until: Date.now() + HIGHLIGHT_DURATION_MS,
      };
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(clearHighlight, HIGHLIGHT_DURATION_MS);
    }
    function chainAtTime(t, agvId) {
      // Find snapshot closest to t and compute blocking chain rooted at agvId.
      if (!agvId) return { chain: [], cycle: false };
      const intv = trace.meta.sample_interval_s || 0.5;
      const idx = Math.min(snapshots.length - 1, Math.max(0, Math.round(t / intv)));
      return buildBlockingChainFor(snapshots[idx] || { agvs: [] }, agvId);
    }

    const minX = Math.min(...map.nodes.map(n => n.x), 0);
    const maxX = Math.max(...map.nodes.map(n => n.x), 1);
    const minY = Math.min(...map.nodes.map(n => n.y), 0);
    const maxY = Math.max(...map.nodes.map(n => n.y), 1);

    function sx(x) {
      return 60 + ((x - minX) / Math.max(maxX - minX, 1)) * 880;
    }
    function sy(y) {
      return 620 - ((y - minY) / Math.max(maxY - minY, 1)) * 540;
    }
    function currentTime() {
      return (snapshots[index] && snapshots[index].t) || 0;
    }
    function setIndexFromTime(targetTime) {
      let closest = 0;
      for (let i = 0; i < snapshots.length; i += 1) {
        if ((snapshots[i].t || 0) >= targetTime) {
          closest = i;
          break;
        }
        closest = i;
      }
      index = closest;
      render();
    }
    function eventLabel(kind) {
      const labels = {
        headon_block: '정면 교행 충돌 차단',
        section_conflict: '구간 충돌',
        followon_block: '동일 방향 추종 차단',
        wait_start: '대기 시작',
        reroute: '재경로',
        reroute_via_siding: '사이딩 우회 후보',
        reroute_siding_applied: '사이딩 우회 적용',
        demand_completed: '수요 완료',
        charging_start: '충전 시작',
        charging_complete: '충전 완료',
        processing_start: '작업 시작',
        edge_enter: '엣지 진입',
        edge_exit: '엣지 이탈',
        deadlock_resolved: '데드락 해소',
        order_received: '오더 수신'
      };
      return labels[kind] || kind;
    }
    function filteredEventAgv(event) {
      return !focusedAgvId || event.agv_id === focusedAgvId || event.blocking_agv === focusedAgvId;
    }
    function describeEvent(event) {
      const parts = [];
      if (event.agv_id) parts.push(event.agv_id);
      parts.push(eventLabel(event.kind));
      if (event.edge_key) parts.push(event.edge_key);
      if (event.section_key) parts.push(event.section_key);
      if (event.siding_id) parts.push(`siding=${event.siding_id}`);
      if (event.blocking_agv) parts.push(`block=${event.blocking_agv}`);
      return parts.join(' / ');
    }
    function buildIncidentGroups(sourceEvents) {
      const sorted = [...sourceEvents].sort((a, b) => (a.t || 0) - (b.t || 0));
      const groups = [];
      for (const event of sorted) {
        const last = groups[groups.length - 1];
        const sameKey = last
          && last.kind === event.kind
          && (last.edge_key || '') === (event.edge_key || '')
          && (last.section_key || '') === (event.section_key || '')
          && (last.agv_id || '') === (event.agv_id || '')
          && ((event.t || 0) - last.end_t) <= 2.5;
        if (sameKey) {
          last.end_t = event.t || last.end_t;
          last.count += 1;
          last.events.push(event);
        } else {
          groups.push({
            kind: event.kind,
            agv_id: event.agv_id || '',
            edge_key: event.edge_key || '',
            section_key: event.section_key || '',
            start_t: event.t || 0,
            end_t: event.t || 0,
            count: 1,
            events: [event],
          });
        }
      }
      return groups.slice(-60).reverse();
    }
    function populateAgvFocusOptions() {
      const select = document.getElementById('agv-focus');
      select.innerHTML = '<option value="">전체</option>' + agvIds.map(agvId => `<option value="${agvId}">${agvId}</option>`).join('');
    }
    function svgPointFromClient(svg, clientX, clientY) {
      const rect = svg.getBoundingClientRect();
      const viewBox = svg.viewBox.baseVal;
      return {
        x: viewBox.x + ((clientX - rect.left) / rect.width) * viewBox.width,
        y: viewBox.y + ((clientY - rect.top) / rect.height) * viewBox.height,
      };
    }
    function svgDeltaFromClient(svg, dx, dy) {
      const rect = svg.getBoundingClientRect();
      const viewBox = svg.viewBox.baseVal;
      return {
        x: (dx / rect.width) * viewBox.width,
        y: (dy / rect.height) * viewBox.height,
      };
    }
    function resetZoom() {
      zoomScale = 1;
      zoomPanX = 0;
      zoomPanY = 0;
    }
    function isAnchoredOnNode(agv) {
      if (!agv.current_node) return false;
      const node = nodeIndex.get(agv.current_node);
      if (!node) return false;
      return Math.abs((agv.x || 0) - node.x) < 0.01 && Math.abs((agv.y || 0) - node.y) < 0.01;
    }
    function agvDisplayPoints(agvs) {
      const grouped = new Map();
      for (const agv of agvs) {
        const anchored = isAnchoredOnNode(agv);
        const bucketKey = anchored
          ? `node:${agv.current_node || agv.agv_id}`
          : `coord:${Math.round((agv.x || 0) * 10) / 10}:${Math.round((agv.y || 0) * 10) / 10}`;
        if (!grouped.has(bucketKey)) grouped.set(bucketKey, []);
        grouped.get(bucketKey).push({ agv, anchored });
      }
      const positioned = [];
      for (const entries of grouped.values()) {
        const count = entries.length;
        entries.forEach((entry, idx) => {
          // Single AGV in bucket: render exactly on the edge/node (no offset).
          // Only spread when multiple AGVs share the same anchor.
          const spread = count <= 1 ? 0 : (idx - (count - 1) / 2);
          const ox = count <= 1 ? 0 : (entry.anchored ? spread * 14 : spread * 10);
          const oy = count <= 1 ? 0 : (entry.anchored ? -10 - Math.abs(spread) * 4 : 8 + spread * 3);
          // Label offset: stack vertically when bucketed; single AGV gets a fixed callout up-right.
          const labelOx = 12;
          const labelOy = count <= 1 ? -12 : (-12 + (idx - (count - 1) / 2) * 12);
          positioned.push({
            agv: entry.agv,
            anchored: entry.anchored,
            ox,
            oy,
            labelOx,
            labelOy,
            bucketIndex: idx,
            bucketCount: count,
          });
        });
      }
      // Cross-bucket label collision: when single-bucket items share a y band,
      // stagger their labels above/below alternately so they don't fight each other.
      const Y_BAND = 22;
      const X_PROXIMITY = 110;
      const singles = positioned.filter(it => it.bucketCount <= 1);
      const bands = new Map();
      for (const item of singles) {
        const cy = sy(item.agv.y);
        const bandKey = Math.round(cy / Y_BAND);
        if (!bands.has(bandKey)) bands.set(bandKey, []);
        bands.get(bandKey).push(item);
      }
      for (const items of bands.values()) {
        if (items.length <= 1) continue;
        items.sort((a, b) => a.agv.x - b.agv.x);
        let lastBelow = -Infinity;
        let lastAbove = -Infinity;
        for (const item of items) {
          const cx = sx(item.agv.x);
          const aboveBusy = cx - lastAbove < X_PROXIMITY;
          const belowBusy = cx - lastBelow < X_PROXIMITY;
          if (aboveBusy && !belowBusy) {
            item.labelOy = 18;
            lastBelow = cx;
          } else if (!aboveBusy) {
            // default: above
            lastAbove = cx;
          } else {
            // both busy — push further below to avoid overlap
            item.labelOy = 30;
            lastBelow = cx;
          }
        }
      }
      return positioned;
    }
    function agvShapeMarkup(cx, cy, color, anchored, faded) {
      const opacity = faded ? 0.18 : 1.0;
      if (anchored) {
        // 마커 크기 축소: r 8→5 (faded 6→4) — 시각 사이즈가 실 robot에 가까워지도록
        return `<circle cx="${cx}" cy="${cy}" r="${faded ? 4 : 5}" fill="${color}" opacity="${opacity}" />`;
      }
      return '';
    }
    // ── AGV 상태별 색상 매핑 ────────────────────────────────────
    // 우선순위: ERROR > CHARGING > 작업중(demand) > WAITING > NAVIGATING(빈손) > IDLE
    function lightenHex(hex, amount) {
      let h = (hex || '#3a4555').replace('#', '');
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      if (h.length !== 6) return hex;
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      let hh = 0, s = 0, l = (max + min) / 510;
      if (max !== min) {
        const d = max - min;
        s = l > 0.5 ? d / (510 - max - min) : d / (max + min);
        if (max === r) hh = ((g - b) / d + (g < b ? 6 : 0));
        else if (max === g) hh = ((b - r) / d + 2);
        else hh = ((r - g) / d + 4);
        hh /= 6;
      }
      l = Math.min(1, l + amount);
      const hue2rgb = (p, q, t) => {
        if (t < 0) t += 1; if (t > 1) t -= 1;
        if (t < 1 / 6) return p + (q - p) * 6 * t;
        if (t < 1 / 2) return q;
        if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
        return p;
      };
      const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
      const p = 2 * l - q;
      const nr = Math.round(hue2rgb(p, q, hh + 1 / 3) * 255);
      const ng = Math.round(hue2rgb(p, q, hh) * 255);
      const nb = Math.round(hue2rgb(p, q, hh - 1 / 3) * 255);
      return '#' + [nr, ng, nb].map(c => c.toString(16).padStart(2, '0')).join('');
    }
    function agvDisplayColor(agv, baseColor) {
      const base = baseColor || '#3a4555';
      const state = (agv && agv.state) || '';
      if (state === 'ERROR') return '#e74c3c';
      if (state === 'CHARGING') return '#9b59b6';
      const demandId = (agv && agv.current_demand_id) || '';
      const hasJob = !!demandId;
      if (hasJob && (state === 'NAVIGATING' || state === 'PROCESSING')) {
        return demandId.startsWith('manual_') ? '#e67e22' : '#2980b9';
      }
      if (state === 'WAITING_RESERVATION') return '#f39c12';
      if (state === 'NAVIGATING') return lightenHex(base, 0.3);
      return base;
    }
    function agvArrowMarkup(cx, cy, color, heading, faded) {
      const opacity = faded ? 0.18 : 1.0;
      // 화살표 크기도 축소 (본래 1.15 → 0.65). follow-on 시 시각 겹침 방지.
      const scale = faded ? 0.5 : 0.65;
      const cos = Math.cos(heading || 0);
      const sin = Math.sin(heading || 0);
      const rotatePoint = (px, py) => {
        const rx = cx + (px * cos - py * sin) * scale;
        const ry = cy - (px * sin + py * cos) * scale;
        return `${rx},${ry}`;
      };
      const points = [
        rotatePoint(14, 0),
        rotatePoint(-8, -9),
        rotatePoint(-1, 0),
        rotatePoint(-8, 9),
      ].join(' ');
      return `<polygon points="${points}" fill="${color}" opacity="${opacity}" />`;
    }
    function renderMap() {
      const svg = document.getElementById('map');
      const snapshot = snapshots[index] || { agvs: [] };
      const visibleAgvs = (snapshot.agvs || []).filter(agv => !focusedAgvId || agv.agv_id === focusedAgvId);
      const fadedAgvs = (snapshot.agvs || []).filter(agv => focusedAgvId && agv.agv_id !== focusedAgvId);
      const visibleDisplay = agvDisplayPoints(visibleAgvs);
      const fadedDisplay = agvDisplayPoints(fadedAgvs);
      const edgeStyles = new Map();
      const applyEdgeStyle = (edgeKey, patch) => {
        if (!edgeKey) return;
        const prev = edgeStyles.get(edgeKey) || {
          stroke: '#c6d0db',
          width: 2,
          opacity: 1,
          dash: '',
        };
        edgeStyles.set(edgeKey, { ...prev, ...patch });
      };
      // 히트맵 모드: 다른 path 스타일 그리기 전에 베이스로 색을 깔아놓는다.
      // log 스케일로 노멀라이즈해서 1회 짜리 사건도 식별 가능하게.
      if (heatmapMode && heatmapMax > 0) {
        const logMax = Math.log(heatmapMax + 1);
        for (const [edgeKey, count] of heatmapCounts) {
          const t = Math.log(count + 1) / Math.max(logMax, 0.0001); // 0..1
          // 그라데이션: #fde6e2 (옅음) → #c0392b (진함)
          // R,G,B 보간
          const lerp = (a, b) => Math.round(a + (b - a) * t);
          const r = lerp(0xfd, 0xc0);
          const g = lerp(0xe6, 0x39);
          const b = lerp(0xe2, 0x2b);
          const stroke = `rgb(${r},${g},${b})`;
          const width = 3 + t * 5;  // 3 → 8
          applyEdgeStyle(edgeKey, { stroke, width, opacity: 0.92, dash: '' });
        }
      }
      // 트래픽 모드 (T-70): edge_enter 누적 횟수를 파랑~보라 그라데이션으로 표시.
      // heatmap 과 mutually exclusive (둘 다 활성될 일 없음, 클릭 핸들러가 보장).
      if (trafficMode && trafficMax > 0) {
        const logMax = Math.log(trafficMax + 1);
        for (const [edgeKey, count] of trafficCounts) {
          const t = Math.log(count + 1) / Math.max(logMax, 0.0001);
          // #e0e7ff (옅은 라벤더) → #4338ca (인디고)
          const lerp = (a, b) => Math.round(a + (b - a) * t);
          const r = lerp(0xe0, 0x43);
          const g = lerp(0xe7, 0x38);
          const b = lerp(0xff, 0xca);
          applyEdgeStyle(edgeKey, { stroke: `rgb(${r},${g},${b})`, width: 3 + t * 5, opacity: 0.92, dash: '' });
        }
      }
      // ★ 히트맵/트래픽 모드일 때는 AGV 오버레이 (planned/reserved/current/blocked) skip.
      //   누적 패턴에 집중하는 뷰 — AGV planned 색이 베이스 색을 덮어 가리지 않게.
      if (!heatmapMode && !trafficMode) {
        for (const agv of visibleAgvs) {
          const planned = agv.planned_edge_keys || [];
          planned.forEach((edgeKey, depth) => {
            const t = Math.min(depth, 8) / 8;
            const opacity = 0.95 - t * 0.65;
            applyEdgeStyle(edgeKey, { stroke: '#8bb4ff', width: 3, opacity, dash: '6 5' });
          });
          const reserved = agv.reserved_edge_keys || [];
          reserved.forEach((edgeKey, depth) => {
            const t = Math.min(depth, 6) / 6;
            const opacity = 1.0 - t * 0.45;
            const width = 4 - t * 1.2;
            applyEdgeStyle(edgeKey, { stroke: '#2459d1', width, opacity, dash: '' });
          });
          if (agv.current_edge_key) {
            applyEdgeStyle(agv.current_edge_key, { stroke: '#0f9d58', width: 6, opacity: 1, dash: '' });
          }
          if (agv.blocked_edge_key && agv.state === 'WAITING_RESERVATION') {
            applyEdgeStyle(agv.blocked_edge_key, { stroke: '#c0392b', width: 6, opacity: 1, dash: '' });
          }
        }
      }
      const labelScale = 1 / Math.max(zoomScale, 0.0001);
      svg.innerHTML = `
        <g id="viewport" transform="translate(${zoomPanX} ${zoomPanY}) scale(${zoomScale})">
          ${map.edges.map(edge => {
            const style = edgeStyles.get(edge.edge_key) || {
              stroke: '#c6d0db',
              width: 2,
              opacity: 1,
              dash: '',
            };
            return `<line x1="${sx(edge.x1)}" y1="${sy(edge.y1)}" x2="${sx(edge.x2)}" y2="${sy(edge.y2)}" stroke="${style.stroke}" stroke-width="${style.width / zoomScale}" stroke-opacity="${style.opacity}" stroke-dasharray="${style.dash}" stroke-linecap="round" vector-effect="non-scaling-stroke" />`;
          }).join('')}
          ${oneWayEdges.map(edge => {
            const mx = sx((edge.x1 + edge.x2) / 2);
            const my = sy((edge.y1 + edge.y2) / 2);
            const dx = sx(edge.x2) - sx(edge.x1);
            const dy = sy(edge.y2) - sy(edge.y1);
            const len = Math.hypot(dx, dy) || 1;
            const ux = dx / len, uy = dy / len;
            // Chevron: 8px long arrowhead, 5px wide, drawn in counter-scaled group.
            const tipX = 6, baseX = -2, halfY = 4;
            const tip = `${tipX},0`;
            const upper = `${baseX},${-halfY}`;
            const lower = `${baseX},${halfY}`;
            const angle = Math.atan2(uy, ux) * 180 / Math.PI;
            return `<g transform="translate(${mx} ${my}) scale(${labelScale}) rotate(${angle})"><polygon points="${tip} ${upper} ${lower}" fill="#9aa6b6" opacity="0.85" /></g>`;
          }).join('')}
          ${map.nodes.map(node => {
            const id = node.node_id || '';
            const isCharger = node.is_charger || node.role === 'charger';
            const isWork = node.role === 'work';
            const isSiding = node.role === 'siding' || id.startsWith('SD_');
            const isHolding = id.startsWith('HP_');
            const isAccess = id.startsWith('SA_') || id.startsWith('CA_') || id.startsWith('HA_');
            const cx = sx(node.x), cy = sy(node.y);
            let inner;
            if (isCharger) {
              inner = `<rect x="-5" y="-5" width="10" height="10" rx="1" fill="#1f6feb" opacity="0.95" />`;
            } else if (isWork) {
              inner = `<circle r="5" fill="#0f9d58" opacity="0.95" />`;
            } else if (isSiding) {
              inner = `<circle r="4" fill="#e0a000" opacity="0.95" />`;
            } else if (isHolding) {
              inner = `<circle r="4" fill="#fff" stroke="#8b98a8" stroke-width="${1.5 / zoomScale}" vector-effect="non-scaling-stroke" />`;
            } else {
              const radius = isAccess ? 2.5 : 3;
              inner = `<circle r="${radius}" fill="#8b98a8" opacity="0.7" />`;
            }
            return `<g transform="translate(${cx} ${cy}) scale(${labelScale})">${inner}</g>`;
          }).join('')}
          ${fadedDisplay.map(item => {
            const agv = item.agv;
            const cx = sx(agv.x) + item.ox;
            const cy = sy(agv.y) + item.oy;
            const fadedColor = agvDisplayColor(agv, '#3a4555');
            const inner = item.anchored
              ? agvShapeMarkup(0, 0, fadedColor, true, true)
              : agvArrowMarkup(0, 0, fadedColor, agv.heading, true);
            return `<g transform="translate(${cx} ${cy}) scale(${labelScale})">${inner}</g>`;
          }).join('')}
          ${visibleDisplay.map(item => {
            const agv = item.agv;
            const cx = sx(agv.x) + item.ox;
            const cy = sy(agv.y) + item.oy;
            const labelX = cx + item.labelOx;
            const labelY = cy + item.labelOy;
            const labelText = item.bucketCount > 1
              ? agvCaption(agv, true)
              : agvCaption(agv, false);
            const visColor = agvDisplayColor(agv, '#3a4555');
            const inner = item.anchored
              ? agvShapeMarkup(0, 0, visColor, true, false)
              : agvArrowMarkup(0, 0, visColor, agv.heading, false);
            // Invisible 18px hit-circle (counter-scaled) so clicks land reliably.
            return `
              <g class="agv-hit" data-agv-id="${agv.agv_id}" style="cursor: pointer;" transform="translate(${cx} ${cy}) scale(${labelScale})">
                <circle r="18" fill="transparent" />
                ${inner}
              </g>
              <text class="agv-label" data-agv-id="${agv.agv_id}" style="cursor: pointer;" transform="translate(${labelX} ${labelY}) scale(${labelScale})">${labelText}</text>
            `;
          }).join('')}
          ${(() => {
            // Focus AGV target ring + connector (bay confusion fix C, path detail).
            if (!focusedAgvId) return '';
            const agv = (snapshot.agvs || []).find(a => a.agv_id === focusedAgvId);
            if (!agv) return '';
            const destId = destinationNodeId(agv);
            if (!destId) return '';
            const node = nodeIndex.get(destId);
            if (!node) return '';
            const ax = sx(agv.x), ay = sy(agv.y);
            const tx = sx(node.x), ty = sy(node.y);
            const ring = `<g transform="translate(${tx} ${ty}) scale(${labelScale})"><circle r="14" fill="none" stroke="#1f6feb" stroke-width="2" stroke-dasharray="4 3" /></g>`;
            const line = `<line x1="${ax}" y1="${ay}" x2="${tx}" y2="${ty}" stroke="#1f6feb" stroke-width="2" stroke-opacity="0.55" stroke-dasharray="6 5" vector-effect="non-scaling-stroke" />`;
            return line + ring;
          })()}
          ${(() => {
            if (!highlightedIncident || Date.now() >= highlightedIncident.until) return '';
            const overlays = [];
            if (highlightedIncident.edge_key) {
              const edge = map.edges.find(e => e.edge_key === highlightedIncident.edge_key);
              if (edge) {
                overlays.push(`<line x1="${sx(edge.x1)}" y1="${sy(edge.y1)}" x2="${sx(edge.x2)}" y2="${sy(edge.y2)}" stroke="#ff6b35" stroke-width="9" stroke-opacity="0.85" stroke-linecap="round" vector-effect="non-scaling-stroke"><animate attributeName="stroke-opacity" values="0.95;0.35;0.95" dur="1.1s" repeatCount="indefinite" /></line>`);
              }
            }
            const ringIds = (highlightedIncident.chain && highlightedIncident.chain.length)
              ? highlightedIncident.chain
              : (highlightedIncident.agv_id ? [highlightedIncident.agv_id] : []);
            for (const id of ringIds) {
              const agv = (snapshot.agvs || []).find(a => a.agv_id === id);
              if (!agv) continue;
              const cx = sx(agv.x), cy = sy(agv.y);
              const color = highlightedIncident.cycle ? '#c0392b' : '#ff6b35';
              overlays.push(`<g transform="translate(${cx} ${cy}) scale(${labelScale})"><circle r="16" fill="none" stroke="${color}" stroke-width="3"><animate attributeName="r" values="14;22;14" dur="1.1s" repeatCount="indefinite" /><animate attributeName="stroke-opacity" values="1;0.4;1" dur="1.1s" repeatCount="indefinite" /></circle></g>`);
            }
            return overlays.join('');
          })()}
          ${(() => {
            // ⚠ 실시간 충돌 의심 마커 (collisionMode 토글 시만)
            if (!collisionMode) return '';
            const collisions = detectCollisions(snapshot);
            if (collisions.length === 0) return '';
            return collisions.map(c => {
              const color = c.type === 'node' ? '#dc2626' : '#f59e0b';
              const label = c.type === 'node' ? '⚠' : '!';
              return `<g transform="translate(${c.x} ${c.y}) scale(${labelScale})">
                <circle r="14" fill="${color}" opacity="0.25" />
                <circle r="9" fill="${color}" opacity="0.85">
                  <animate attributeName="r" values="9;13;9" dur="0.8s" repeatCount="indefinite" />
                </circle>
                <text y="3.5" text-anchor="middle" fill="#fff" font-size="11" font-weight="700">${label}</text>
              </g>`;
            }).join('');
          })()}
        </g>
      `;
    }
    function renderIncidents() {
      const current = currentTime();
      const filteredIncidents = incidents.filter(event => filteredEventAgv(event));
      const groups = buildIncidentGroups(filteredIncidents);
      document.getElementById('incident-count').textContent = String(groups.length);
      document.getElementById('incident-list').innerHTML = groups.map(group => {
        const head = group.events[0] || {};
        const blockingKinds = new Set(['headon_block', 'section_conflict', 'followon_block', 'deadlock_resolved']);
        let chainBadge = '';
        if (head.agv_id && blockingKinds.has(group.kind)) {
          const { chain, cycle } = chainAtTime(group.start_t, head.agv_id);
          if (chain.length >= 2) {
            chainBadge = `<span class="subtle" style="${cycle ? 'color:#c0392b;font-weight:600;' : ''}">${cycle ? 'cycle ' : ''}chain depth=${chain.length}</span>`;
          }
        }
        return `
        <div class="incident-item ${current >= group.start_t && current <= group.end_t ? 'current' : ''}"
             data-time="${group.start_t}"
             data-edge-key="${head.edge_key || ''}"
             data-agv-id="${head.agv_id || ''}">
          <div class="section-title" style="margin:0 0 6px 0;">
            <strong>${eventLabel(group.kind)}</strong>
            <span class="subtle">${group.count}회 ${chainBadge}</span>
          </div>
          <div style="margin-top:4px;">${describeEvent(head)}</div>
          <div class="meta">t=${group.start_t.toFixed(2)}s ~ ${group.end_t.toFixed(2)}s ${current >= group.start_t && current <= group.end_t ? '· 현재 시점 인접' : ''}</div>
        </div>
      `;
      }).join('');
      document.querySelectorAll('.incident-item').forEach(el => {
        el.addEventListener('click', () => {
          pause();
          const t = Number(el.dataset.time || 0);
          setIndexFromTime(t);
          const agvId = el.dataset.agvId || '';
          const { chain, cycle } = agvId ? chainAtTime(t, agvId) : { chain: [], cycle: false };
          setHighlight(el.dataset.edgeKey || '', agvId, { chain, cycle });
          render();
        });
      });
    }
    function renderEvents() {
      const now = currentTime();
      const visible = events
        .filter(filteredEventAgv)
        .filter(e => (e.t || 0) <= now)
        .slice(-20)
        .reverse();
      document.getElementById('event-list').innerHTML = visible.map(event => `
        <div class="event-item">
          <div><strong>${describeEvent(event)}</strong></div>
          <div class="meta">t=${(event.t || 0).toFixed(2)}s</div>
        </div>
      `).join('');
    }
    function render() {
      document.getElementById('snapshot-count').textContent = snapshots.length.toLocaleString();
      document.getElementById('event-count').textContent = events.length.toLocaleString();
      document.getElementById('sample-interval').textContent = `${trace.meta.sample_interval_s.toFixed(2)}s`;
      document.getElementById('time-slider').max = Math.max(snapshots.length - 1, 0);
      document.getElementById('time-slider').value = index;
      document.getElementById('time-label').textContent = `${currentTime().toFixed(2)}s`;
      const toggle = document.getElementById('play-toggle');
      if (toggle) {
        const playing = !!timer;
        toggle.textContent = playing ? '일시정지' : '재생';
        toggle.classList.toggle('is-playing', playing);
      }
      renderMap();
      renderIncidents();
      renderEvents();
      renderAgvDetail();
      renderEventMarkers();
    }
    // ── 타임라인 사고 마커 ───────────────────────────────
    // 슬라이더 위에 사고 시점을 색깔 점으로 찍어 시간대별 군집 즉시 인지.
    // 클릭 시 해당 시점으로 점프(+해당 사고 group의 첫 사건을 강조).
    let _eventMarkersBuiltKey = '';
    function renderEventMarkers() {
      const container = document.getElementById('event-markers');
      if (!container) return;
      const duration = (trace.meta && trace.meta.duration_s) || (snapshots.length * (trace.meta.sample_interval_s || 0.5));
      // AGV 필터 반영 (전체 또는 focus AGV의 사고만)
      const filtered = incidents.filter(filteredEventAgv);
      const groups = buildIncidentGroups(filtered);
      // 캐시: 같은 데이터셋이면 다시 안 그림
      const key = focusedAgvId + '|' + groups.length + '|' + (groups[0] ? groups[0].start_t : 0) + '|' + (groups[groups.length-1] ? groups[groups.length-1].start_t : 0);
      if (key === _eventMarkersBuiltKey) return;
      _eventMarkersBuiltKey = key;
      if (!duration || groups.length === 0) {
        container.innerHTML = '';
        return;
      }
      container.innerHTML = groups.map(group => {
        const pct = Math.max(0, Math.min(100, (group.start_t / duration) * 100));
        const title = `${eventLabel(group.kind)} · t=${group.start_t.toFixed(2)}s · ${group.count}회`;
        return `<div class="event-marker" data-kind="${group.kind}" data-time="${group.start_t}" data-edge-key="${group.edge_key || ''}" data-agv-id="${group.agv_id || ''}" style="left:${pct}%" title="${title}"></div>`;
      }).join('');
      container.querySelectorAll('.event-marker').forEach(el => {
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          pause();
          const t = Number(el.dataset.time || 0);
          setIndexFromTime(t);
          const agvId = el.dataset.agvId || '';
          const { chain, cycle } = agvId ? chainAtTime(t, agvId) : { chain: [], cycle: false };
          setHighlight(el.dataset.edgeKey || '', agvId, { chain, cycle });
          render();
        });
      });
    }
    function buildBlockingChainFor(snapshot, startAgvId) {
      const map = new Map();
      for (const a of (snapshot.agvs || [])) {
        if (a.blocking_agv) map.set(a.agv_id, a.blocking_agv);
      }
      const chain = [];
      const seen = new Set();
      let cur = startAgvId;
      let cycle = false;
      while (cur) {
        if (seen.has(cur)) { cycle = true; chain.push(cur); break; }
        seen.add(cur);
        chain.push(cur);
        cur = map.get(cur) || '';
      }
      return { chain, cycle };
    }
    function renderAgvDetail() {
      const panel = document.getElementById('agv-detail-panel');
      const stack = document.getElementById('side-stack');
      if (!focusedAgvId) {
        panel.hidden = true;
        stack.classList.remove('has-focus');
        return;
      }
      const snapshot = snapshots[index] || { agvs: [] };
      const agv = (snapshot.agvs || []).find(a => a.agv_id === focusedAgvId);
      if (!agv) {
        panel.hidden = true;
        stack.classList.remove('has-focus');
        return;
      }
      panel.hidden = false;
      stack.classList.add('has-focus');
      document.getElementById('agv-detail-title').textContent = agv.agv_id + ' 상세';
      const dest = destinationNodeId(agv);
      const detourRow = agv.detour_via
        ? `<div class="row"><span class="key">우회</span><span style="color:#c77700;">via ${agv.detour_via}</span></div>`
        : '';
      const orderRow = (agv.pickup_node || agv.dropoff_node)
        ? (() => {
            const isPickup = agv.phase === 'pickup';
            const isDropoff = agv.phase === 'dropoff';
            const pickupBadge = `<span class="badge" ${isPickup ? 'style="border-color:#0f9d58;color:#0f9d58;font-weight:600;"' : ''}>${agv.pickup_node || '—'}</span>`;
            const dropoffBadge = `<span class="badge" ${isDropoff ? 'style="border-color:#0f9d58;color:#0f9d58;font-weight:600;"' : ''}>${agv.dropoff_node || '—'}</span>`;
            return `<div class="row"><span class="key">주문</span></div><div class="chain-row">픽업 ${pickupBadge}<span class="arrow">→</span>드롭 ${dropoffBadge}</div>`;
          })()
        : '<div class="meta">현재 주문 없음</div>';
      const reserved = agv.reserved_edge_keys || [];
      const planned = agv.planned_edge_keys || [];
      const reservedDepth = reserved.length;
      const plannedDepth = planned.length;
      const maxDepth = Math.max(reservedDepth, plannedDepth, 1);
      const reservedRows = reserved.slice(0, 6).map((key, i) => {
        const pct = ((reservedDepth - i) / maxDepth) * 100;
        return `<div class="depth-row"><span class="label">res ${i+1}</span><span class="bar" style="--bar: ${pct.toFixed(1)}%"></span></div>`;
      }).join('');
      const plannedRows = planned.slice(0, 6).map((key, i) => {
        const pct = ((plannedDepth - i) / maxDepth) * 100;
        return `<div class="depth-row"><span class="label">plan ${i+1}</span><span class="bar" style="--bar: ${pct.toFixed(1)}%; background: linear-gradient(90deg, #8bb4ff 0%, #8bb4ff ${pct.toFixed(1)}%, #e6ecf5 ${pct.toFixed(1)}%);"></span></div>`;
      }).join('');
      const blocking = agv.blocking_agv ? buildBlockingChainFor(snapshot, agv.agv_id) : null;
      const chainHtml = blocking && blocking.chain.length > 1
        ? `<div class="chain-row ${blocking.cycle ? 'cycle' : ''}">${blocking.chain.map(id => `<span class="badge">${id}</span>`).join('<span class="arrow">→</span>')}</div>`
        : '<div class="meta">현재 대기 체인 없음</div>';
      const stateMap = {
        NAVIGATING: ['s-navigating', '주행'],
        PROCESSING: ['s-processing', '작업'],
        WAITING_RESERVATION: ['s-waiting', '대기'],
        CHARGING: ['s-charging', '충전'],
        IDLE: ['s-idle', 'IDLE'],
      };
      const [statePillCls, stateLabel] = stateMap[agv.state] || ['s-idle', agv.state];
      document.getElementById('agv-detail-body').innerHTML = `
        <div class="row"><span class="key">상태</span><span class="state-pill ${statePillCls}">${stateLabel}</span></div>
        <div class="row"><span class="key">현재</span><span class="val-mono">${agv.current_node || '—'}</span></div>
        <div class="row"><span class="key">다음</span><span class="val-mono">${agv.target_node || '—'}</span></div>
        <div class="row"><span class="key">즉시 목적</span><span><span class="val-mono">${dest || '—'}</span>${agv.phase ? ' <span class="meta">· ' + (agv.phase === 'pickup' ? '픽업' : '드롭') + ' 단계</span>' : ''}</span></div>
        ${orderRow}
        ${detourRow}
        <div class="row"><span class="key">배터리</span><span><span class="val-mono">${(agv.battery_pct || 0).toFixed(1)}%</span></span></div>
        <div class="row"><span class="key">예약 / 계획</span><span class="val-mono">${reservedDepth} / ${plannedDepth} hop</span></div>
        ${reservedRows ? `<div class="depth-bars">${reservedRows}</div>` : ''}
        ${plannedRows ? `<div class="depth-bars">${plannedRows}</div>` : ''}
        <div class="row"><span class="key">대기 체인</span></div>
        ${chainHtml}
      `;
    }
    function play() {
      if (timer) return;
      timer = setInterval(() => {
        if (index >= snapshots.length - 1) {
          pause();
          return;
        }
        index += 1;
        render();
      }, Math.max(20, 120 / speed));
    }
    function pause() {
      if (!timer) return;
      clearInterval(timer);
      timer = null;
    }
    document.getElementById('play-toggle').addEventListener('click', () => {
      if (timer) pause(); else play();
      render();
    });
    document.getElementById('time-slider').addEventListener('input', (e) => {
      index = Number(e.target.value);
      render();
    });
    document.querySelectorAll('.speed-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        speed = Number(btn.dataset.speed || '1');
        document.querySelectorAll('.speed-btn').forEach(node => node.classList.remove('active'));
        btn.classList.add('active');
        if (timer) {
          pause();
          play();
        }
      });
    });
    document.getElementById('agv-focus').addEventListener('change', (e) => {
      focusedAgvId = e.target.value || '';
      render();
    });
    document.getElementById('heatmap-toggle').addEventListener('click', () => {
      heatmapMode = !heatmapMode;
      // mutually exclusive: heatmap 켜면 traffic 자동 끔
      if (heatmapMode && trafficMode) {
        trafficMode = false;
        document.getElementById('traffic-toggle').classList.remove('active');
        document.getElementById('traffic-legend').classList.remove('active');
      }
      const btn = document.getElementById('heatmap-toggle');
      btn.classList.toggle('active', heatmapMode);
      const legend = document.getElementById('heatmap-legend');
      legend.classList.toggle('active', heatmapMode);
      const lab = document.getElementById('heatmap-max-label');
      if (lab) {
        lab.textContent = heatmapMax > 0 ? `최대 ${heatmapMax}회` : '데이터 없음';
      }
      render();
    });
    document.getElementById('traffic-toggle').addEventListener('click', () => {
      trafficMode = !trafficMode;
      // mutually exclusive: traffic 켜면 heatmap 자동 끔 (혼동 방지)
      if (trafficMode && heatmapMode) {
        heatmapMode = false;
        document.getElementById('heatmap-toggle').classList.remove('active');
        document.getElementById('heatmap-legend').classList.remove('active');
      }
      document.getElementById('traffic-toggle').classList.toggle('active', trafficMode);
      const legend = document.getElementById('traffic-legend');
      legend.classList.toggle('active', trafficMode);
      const lab = document.getElementById('traffic-max-label');
      if (lab) {
        lab.textContent = trafficMax > 0 ? `최대 ${trafficMax}회` : '데이터 없음';
      }
      render();
    });
    document.getElementById('collision-toggle').addEventListener('click', () => {
      collisionMode = !collisionMode;
      document.getElementById('collision-toggle').classList.toggle('active', collisionMode);
      render();
    });

    // ── 실시간 충돌 의심 감지 ─────────────────────────────────
    // 같은 노드에 둘 이상 anchored 또는 같은 엣지에 둘 이상 NAVIGATING + 거리 가까움.
    // 결과: [{type:'node'|'edge', key, agv_ids:[...], x, y}]
    function detectCollisions(snapshot) {
      const out = [];
      const byNode = new Map();   // node_id → [agv_id, ...]
      const byEdge = new Map();   // edge_key → [agv]
      for (const a of (snapshot.agvs || [])) {
        if (isAnchoredOnNode(a) && a.current_node) {
          if (!byNode.has(a.current_node)) byNode.set(a.current_node, []);
          byNode.get(a.current_node).push(a);
        } else if (a.current_edge_key) {
          if (!byEdge.has(a.current_edge_key)) byEdge.set(a.current_edge_key, []);
          byEdge.get(a.current_edge_key).push(a);
        }
      }
      // 같은 노드 anchored 둘 이상 = 진성 충돌
      for (const [nid, list] of byNode.entries()) {
        if (list.length < 2) continue;
        // charger 노드에 IDLE/CHARGING 다중 점유는 OK
        const allChargingOrIdle = list.every(a => a.state === 'CHARGING' || a.state === 'IDLE');
        const isCharger = (nodeIndex.get(nid) || {}).is_charger ||
                          (nodeIndex.get(nid) || {}).role === 'charger';
        if (allChargingOrIdle && isCharger) continue;
        const node = nodeIndex.get(nid);
        if (!node) continue;
        out.push({type:'node', key:nid, agv_ids:list.map(a=>a.agv_id),
                  x:sx(node.x), y:sy(node.y)});
      }
      // 같은 엣지 위 둘 이상 + 거리 매우 가까움 (1m 미만) = 진성 의심
      for (const [ek, list] of byEdge.entries()) {
        if (list.length < 2) continue;
        for (let i = 0; i < list.length; i++) {
          for (let j = i+1; j < list.length; j++) {
            const a = list[i], b = list[j];
            const dist = Math.hypot(a.x - b.x, a.y - b.y);
            if (dist < 1.0) {  // 1m 이내
              out.push({type:'edge', key:ek, agv_ids:[a.agv_id, b.agv_id],
                        x:sx((a.x + b.x)/2), y:sy((a.y + b.y)/2)});
            }
          }
        }
      }
      return out;
    }
    document.getElementById('zoom-reset-btn').addEventListener('click', () => {
      resetZoom();
      render();
    });
    document.getElementById('map').addEventListener('wheel', (e) => {
      e.preventDefault();
      const svg = e.currentTarget;
      const before = svgPointFromClient(svg, e.clientX, e.clientY);
      const nextScale = Math.min(6, Math.max(0.6, zoomScale * (e.deltaY < 0 ? 1.12 : 0.9)));
      const scaleRatio = nextScale / zoomScale;
      zoomScale = nextScale;
      zoomPanX = before.x - (before.x - zoomPanX) * scaleRatio;
      zoomPanY = before.y - (before.y - zoomPanY) * scaleRatio;
      render();
    }, { passive: false });
    document.getElementById('map').addEventListener('pointerdown', (e) => {
      isDragging = true;
      dragStart = { x: e.clientX, y: e.clientY, panX: zoomPanX, panY: zoomPanY };
    });
    document.getElementById('map').addEventListener('pointermove', (e) => {
      if (!isDragging || !dragStart) return;
      const svg = e.currentTarget;
      const delta = svgDeltaFromClient(svg, e.clientX - dragStart.x, e.clientY - dragStart.y);
      zoomPanX = dragStart.panX + delta.x;
      zoomPanY = dragStart.panY + delta.y;
      render();
    });
    document.getElementById('map').addEventListener('pointerup', () => {
      isDragging = false;
      dragStart = null;
    });
    document.getElementById('map').addEventListener('pointerleave', () => {
      isDragging = false;
      dragStart = null;
    });
    // AGV click → toggle focus on clicked AGV.
    document.getElementById('map').addEventListener('click', (e) => {
      const hit = e.target.closest('[data-agv-id]');
      if (!hit) return;
      const agvId = hit.dataset.agvId;
      const select = document.getElementById('agv-focus');
      // Toggle: clicking the already-focused AGV clears focus.
      const next = focusedAgvId === agvId ? '' : agvId;
      focusedAgvId = next;
      select.value = next;
      render();
    });
    // Double-click empty map area → reset zoom/pan. Skip when clicking an AGV.
    document.getElementById('map').addEventListener('dblclick', (e) => {
      if (e.target.closest('[data-agv-id]')) return;
      resetZoom();
      render();
    });
    function renderTopologyMeta() {
      const meta = trace.meta || {};
      const desc = meta.description || {};
      const ttype = meta.topology_type || '';
      const variant = meta.topology_variant || '';
      const eyebrow = ttype
        ? (ttype === variant ? `Type ${ttype}` : `Type ${ttype} · ${variant}`)
        : 'Playback Trace';
      document.getElementById('topology-eyebrow').textContent = eyebrow;
      const titleEl = document.getElementById('topology-title');
      if (variant && variant !== ttype) {
        titleEl.textContent = `${variant} 시뮬레이션`;
      } else if (ttype) {
        titleEl.textContent = `Type ${ttype} 시뮬레이션`;
      }
      const headlineEl = document.getElementById('topology-headline');
      headlineEl.textContent = desc.headline || '';
      const chipBox = document.getElementById('topology-meta-chips');
      const chips = [];
      if (ttype) chips.push(`<span class="meta-chip type-tag">Type ${ttype}</span>`);
      if (desc.lanes) chips.push(`<span class="meta-chip">차선 <strong>${desc.lanes}</strong></span>`);
      if (desc.direction) chips.push(`<span class="meta-chip">방향 <strong>${desc.direction}</strong></span>`);
      if (desc.conflict) chips.push(`<span class="meta-chip">충돌 <strong>${desc.conflict}</strong></span>`);
      if (meta.n_agv) chips.push(`<span class="meta-chip">AGV <strong>${meta.n_agv}</strong></span>`);
      if (meta.random_seed !== undefined) chips.push(`<span class="meta-chip">seed <strong>${meta.random_seed}</strong></span>`);
      if (meta.demand_count) chips.push(`<span class="meta-chip">demands <strong>${meta.demand_count}</strong></span>`);
      if (meta.duration_s) chips.push(`<span class="meta-chip">duration <strong>${meta.duration_s}s</strong></span>`);
      chipBox.innerHTML = chips.join('');
      if (ttype) {
        document.title = `Type ${ttype} · ${variant || ttype} — Playback`;
      }
    }
    renderTopologyMeta();
    populateAgvFocusOptions();
    render();
  </script>
</body>
</html>
"""
    return html.replace("__TRACE_JSON__", trace_json)


def build_live_html(default_params: dict | None = None) -> str:  # noqa: E501
    """playback.html 의 4-pane 렌더러를 재사용하되, 데이터를 WS tick 으로 실시간 공급.
    default_params: {topology, agv_count, speed, duration}
    """
    dp = default_params or {}
    topo = dp.get("topology", "A")
    agv_count = int(dp.get("agv_count", 12))
    speed = float(dp.get("speed", 2.0))
    duration = int(dp.get("duration", 600))

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FAB AMR Live Sim</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f9; --surface: #ffffff; --surface-2: #fafbfd; --surface-3: #f1f4f8;
      --panel: var(--surface);
      --ink: #0f172a; --text: var(--ink); --muted: #64748b; --muted-2: #94a3b8;
      --border: #e3e8ef; --border-strong: #cbd3df;
      --edge: #d3dae4; --edge-plan: #8bb4ff; --edge-reserved: #2459d1;
      --edge-active: #0f9d58; --edge-blocked: #c0392b;
      --node-work: #0f9d58; --node-charger: #1f6feb; --node-siding: #e0a000;
      --accent: #2563eb; --accent-soft: #e6efff;
      --success: #0f9d58; --success-soft: #e6f7ee;
      --warn: #c77700; --warn-soft: #fff3df;
      --danger: #c0392b; --danger-soft: #fde8e7;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Pretendard", "Apple SD Gothic Neo", sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
      --shadow-sm: 0 1px 2px rgba(15,23,42,0.05);
      --shadow-md: 0 1px 3px rgba(15,23,42,0.05),0 6px 18px -10px rgba(15,23,42,0.10);
      --radius-sm: 6px; --radius: 10px; --radius-lg: 14px;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:var(--font-sans); font-size:13px; line-height:1.5;
      letter-spacing:-0.005em; background:var(--bg); color:var(--ink);
      -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale; }}
    h1,h2,h3 {{ letter-spacing:-0.015em; margin:0; }}
    .page {{ max-width:1480px; margin:0 auto; padding:20px 18px 32px; display:grid; gap:14px; }}
    .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius);
      padding:14px 16px; box-shadow:var(--shadow-sm); }}
    .top {{ display:grid; gap:12px; }}
    .header-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; align-items:end; justify-content:space-between; }}
    .header-title {{ display:grid; gap:2px; }}
    .header-eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:0.06em; text-transform:uppercase; font-weight:600; }}
    .header-title h1 {{ font-size:22px; line-height:1.1; font-weight:700; }}
    .kpi-strip {{ display:inline-flex; flex-wrap:wrap; gap:6px; }}
    .kpi-chip {{ display:inline-flex; align-items:baseline; gap:6px; padding:6px 10px;
      background:var(--surface-3); border-radius:999px; font-size:12px; color:var(--muted); }}
    .kpi-chip strong {{ color:var(--ink); font-family:var(--font-mono); font-weight:600; font-size:12.5px; }}
    /* Live params form */
    .live-params {{ display:flex; flex-wrap:wrap; gap:10px 20px; align-items:center; padding:10px 0 4px; }}
    .param-group {{ display:flex; align-items:center; gap:8px; }}
    .param-label {{ color:var(--muted); font-size:11.5px; white-space:nowrap; }}
    .param-value {{ font-family:var(--font-mono); font-size:12px; color:var(--ink); min-width:26px; text-align:right; }}
    .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center;
      position:sticky; top:0; z-index:10; padding:10px 12px;
      background:rgba(255,255,255,0.92); border:1px solid var(--border);
      border-radius:var(--radius); backdrop-filter:saturate(140%) blur(10px);
      -webkit-backdrop-filter:saturate(140%) blur(10px); box-shadow:var(--shadow-sm); }}
    .toolbar-group {{ display:inline-flex; align-items:center; gap:6px;
      padding-left:10px; border-left:1px solid var(--border); }}
    .toolbar-group:first-of-type {{ padding-left:0; border-left:0; }}
    .toolbar-group .meta {{ font-size:11px; }}
    .time-pill {{ display:inline-flex; align-items:baseline; gap:4px; padding:4px 10px;
      border-radius:999px; background:var(--surface-3); font-family:var(--font-mono);
      font-size:13px; font-weight:600; color:var(--ink); letter-spacing:0.01em; white-space:nowrap; }}
    .time-pill .meta {{ font-family:var(--font-sans); font-weight:500; color:var(--muted); }}
    .playback-stage {{ display:grid; gap:10px; }}
    .map-stage {{ display:grid; gap:10px; }}
    button {{ height:30px; padding:0 12px; border:1px solid var(--border);
      background:var(--surface); border-radius:var(--radius-sm); color:var(--ink);
      font-family:inherit; font-size:12.5px; font-weight:500; cursor:pointer;
      transition:background 120ms,border-color 120ms,color 120ms,box-shadow 120ms; }}
    button:hover {{ background:var(--surface-3); border-color:var(--border-strong); }}
    button:disabled {{ opacity:0.4; cursor:not-allowed; }}
    button:focus-visible {{ outline:none; box-shadow:0 0 0 2px var(--accent-soft),0 0 0 3px var(--accent); }}
    .run-btn {{ font-weight:600; background:var(--ink); color:#fff; border-color:var(--ink); }}
    .run-btn:hover {{ background:#1a253b; border-color:#1a253b; }}
    .run-btn:disabled {{ background:var(--ink); color:#fff; }}
    .play-toggle {{ min-width:76px; font-weight:600; background:var(--surface); color:var(--ink); border-color:var(--border-strong); }}
    .play-toggle.is-playing {{ background:var(--surface-3); }}
    select {{ height:30px; padding:0 26px 0 10px; border:1px solid var(--border);
      background:var(--surface); border-radius:var(--radius-sm); color:var(--ink);
      font:inherit; font-size:12.5px; appearance:none; -webkit-appearance:none;
      background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'><path fill='%2364748b' d='M3 4.5l3 3 3-3z'/></svg>");
      background-repeat:no-repeat; background-position:right 8px center; cursor:pointer; }}
    .speed-btn {{ padding:0 10px; min-width:40px; font-variant-numeric:tabular-nums; }}
    .speed-btn.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
    .speed-btn.active:hover {{ background:#1d4ed8; border-color:#1d4ed8; }}
    #heatmap-toggle.active {{ background:#c0392b; color:#fff; border-color:#c0392b; }}
    #traffic-toggle.active {{ background:#4338ca; color:#fff; border-color:#4338ca; }}
    #collision-toggle.active {{ background:#f59e0b; color:#fff; border-color:#f59e0b; }}
    #block-toggle.active {{ background:#dc2626; color:#fff; border-color:#dc2626; }}
    #manual-job-toggle.active {{ background:#059669; color:#fff; border-color:#059669; }}
    /* 차단 모드 ON 일 때 SVG 엣지 hit 영역 시각 강조 */
    .block-mode svg .edge-hit {{ cursor:pointer; }}
    .block-mode svg .edge-hit:hover {{ stroke:#f59e0b; stroke-opacity:0.9; }}
    /* GAP-B: 수동 Job 모드 */
    #manual-job-panel {{ display:none; color:#059669; font-weight:600; margin-left:6px; align-items:center; gap:4px; }}
    #manual-job-panel.active {{ display:inline-flex; }}
    #mj-pickup-badge {{ padding:1px 7px; border-radius:4px; background:#d1fae5;
      color:#065f46; font-weight:600; font-variant-numeric:tabular-nums; }}
    #manual-job-panel .mj-hint {{ color:var(--muted); font-weight:400; margin-left:4px; }}
    .mj-mode svg .node-hit {{ cursor:crosshair; }}
    .mj-mode svg .node-hit:hover {{ fill:rgba(5,150,105,0.18); }}
    /* 수동 Job 모드: IDLE AGV 강조 ring (할당 가능 후보). */
    .agv-idle-mj-ring {{ fill:none; stroke:#059669; stroke-width:2.5; stroke-dasharray:4 3; }}
    /* 방금 수동 Job 할당된 AGV 강조 — 노란 펄스 ring. */
    .agv-just-assigned-ring {{ fill:none; stroke:#f59e0b; stroke-width:3; }}
    /* 최근 할당 패널 — toast 가 사라진 후에도 누가 받았는지 추적용 */
    #mj-last-assigned {{ display:none; align-items:center; gap:6px; padding:4px 10px;
      background:#fef3c7; color:#92400e; border:1px solid #fcd34d;
      border-radius:var(--radius-sm); font-size:12px; margin-left:6px; cursor:pointer; }}
    #mj-last-assigned.active {{ display:inline-flex; }}
    #mj-last-assigned:hover {{ background:#fde68a; }}
    svg .node-hit {{ fill:transparent; pointer-events:auto; }}
    svg .node-pickup-ring {{ fill:none; stroke:#059669; stroke-width:2.5;
      vector-effect:non-scaling-stroke; opacity:0.9; }}
    .mj-toast {{ position:fixed; left:50%; bottom:34px; transform:translateX(-50%) translateY(20px);
      padding:10px 18px; border-radius:8px; background:#065f46; color:#fff;
      font-size:13px; font-weight:600; letter-spacing:0.01em;
      box-shadow:0 6px 20px rgba(15,23,42,0.25); opacity:0;
      transition:opacity 0.25s, transform 0.25s; pointer-events:none; z-index:9999;
      max-width:min(560px,90vw); }}
    .mj-toast.show {{ opacity:1; transform:translateX(-50%) translateY(0); }}
    .mj-toast.warn {{ background:#a16207; }}
    .mj-toast.err {{ background:#b91c1c; }}
    .heatmap-legend {{ display:none; align-items:center; gap:8px; font-size:11.5px;
      color:var(--muted); padding:4px 10px; background:var(--surface-2); border-radius:6px; margin-left:8px; }}
    .heatmap-legend.active {{ display:inline-flex; }}
    .heatmap-legend .gradient {{ width:80px; height:6px; border-radius:3px;
      background:linear-gradient(90deg,#fde6e2 0%,#f5a695 50%,#c0392b 100%); }}
    .traffic-legend {{ display:none; align-items:center; gap:8px; font-size:11.5px;
      color:var(--muted); padding:4px 10px; background:var(--surface-2); border-radius:6px; margin-left:8px; }}
    .traffic-legend.active {{ display:inline-flex; }}
    .traffic-legend .gradient {{ width:80px; height:6px; border-radius:3px;
      background:linear-gradient(90deg,#e0e7ff 0%,#818cf8 50%,#4338ca 100%); }}
    input[type=range] {{ -webkit-appearance:none; width:min(420px,100%); height:6px;
      background:transparent; cursor:pointer; }}
    input[type=range]::-webkit-slider-runnable-track {{ height:6px; border-radius:999px; background:var(--surface-3); }}
    input[type=range]::-webkit-slider-thumb {{ -webkit-appearance:none; appearance:none;
      width:14px; height:14px; border-radius:50%; background:var(--accent); border:2px solid #fff;
      margin-top:-4px; box-shadow:0 0 0 1px var(--accent),0 1px 2px rgba(15,23,42,0.2); }}
    input[type=range]::-moz-range-track {{ height:6px; border-radius:999px; background:var(--surface-3); }}
    input[type=range]::-moz-range-thumb {{ width:14px; height:14px; border-radius:50%;
      background:var(--accent); border:2px solid #fff;
      box-shadow:0 0 0 1px var(--accent),0 1px 2px rgba(15,23,42,0.2); }}
    .slider-wrap {{ position:relative; width:min(420px,100%); display:inline-block; vertical-align:middle; }}
    .slider-wrap input[type=range] {{ display:block; }}
    .event-markers {{ position:absolute; left:0; right:0; top:-4px; height:8px; pointer-events:none; }}
    .event-marker {{ position:absolute; width:4px; height:8px; border-radius:1px;
      transform:translateX(-50%); pointer-events:auto; cursor:pointer; opacity:0.85;
      transition:opacity 0.15s,transform 0.15s; }}
    .event-marker:hover {{ opacity:1; transform:translateX(-50%) scaleY(1.4); }}
    .event-marker[data-kind="headon_block"]      {{ background:#c0392b; }}
    .event-marker[data-kind="section_conflict"]  {{ background:#c77700; }}
    .event-marker[data-kind="followon_block"]    {{ background:#b85450; }}
    .event-marker[data-kind="deadlock_resolved"] {{ background:#6c3aa6; }}
    .event-marker[data-kind="reroute"]           {{ background:#1f6feb; }}
    .event-marker[data-kind="reroute_via_siding"]{{ background:#4f8df0; }}
    .hint {{ color:var(--muted); font-size:11.5px; }}
    .live-badge {{ display:inline-flex; align-items:center; gap:5px; padding:3px 9px;
      border-radius:999px; font-size:11px; font-weight:700; letter-spacing:0.04em;
      background:var(--danger-soft); color:var(--danger); border:1px solid #fdb8b4;
      opacity:0; transition:opacity 0.3s; }}
    .live-badge.active {{ opacity:1; }}
    .live-dot {{ width:6px; height:6px; border-radius:50%; background:var(--danger);
      animation:pulse 1.2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
    /* 데드락 KPI chip: count>0 일 때 빨강 강조 */
    .kpi-chip.deadlock-hot {{ background:var(--danger-soft); color:var(--danger);
      border:1px solid #fdb8b4; }}
    .kpi-chip.deadlock-hot strong {{ color:var(--danger); }}
    /* 화면 상단 alert 배너 — deadlock_alert=true 시 표시 */
    .deadlock-alert {{ display:none; padding:10px 16px; margin:0 0 12px 0;
      background:#fff1ef; color:#8a1f1f; border:1px solid #f5a99c;
      border-left:4px solid var(--danger); border-radius:var(--radius-sm);
      font-size:13px; font-weight:600; align-items:center; gap:10px;
      box-shadow:var(--shadow-sm); animation:dl-pulse 1.4s infinite; }}
    .deadlock-alert.active {{ display:flex; }}
    .deadlock-alert .dl-icon {{ font-size:18px; }}
    .deadlock-alert .dl-detail {{ font-weight:400; font-size:12px; color:#5d2222;
      font-family:var(--font-mono); }}
    @keyframes dl-pulse {{ 0%,100%{{box-shadow:0 0 0 0 rgba(192,57,43,0.0)}}
      50%{{box-shadow:0 0 0 6px rgba(192,57,43,0.15)}} }}
    .lower-layout {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; align-items:start; }}
    .main-layout {{ display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:16px; align-items:start; }}
    .side-stack {{ display:flex; flex-direction:column; gap:16px; position:sticky;
      top:76px; height:calc(100vh - 96px); min-height:0; }}
    #agv-detail-panel[hidden] {{ display:none; }}
    #agv-detail-panel {{ flex:0 0 30%; }}
    #incident-panel {{ flex:0 0 32%; }}
    #event-panel {{ flex:1 1 0; }}
    .side-stack.has-focus #incident-panel {{ flex:0 0 26%; }}
    .side-stack.has-focus #event-panel {{ flex:1 1 0; }}
    .agv-detail-body {{ display:grid; gap:8px; font-size:12.5px; overflow:auto; min-height:0; padding-right:4px; }}
    .agv-detail-body .row {{ display:flex; justify-content:space-between; gap:8px; padding:4px 0;
      border-bottom:1px dashed var(--border); }}
    .agv-detail-body .row:last-of-type {{ border-bottom:0; }}
    .agv-detail-body .row .key {{ color:var(--muted); font-size:11.5px; }}
    .agv-detail-body .row .val-mono {{ font-family:var(--font-mono); font-size:12px; }}
    .state-pill {{ display:inline-flex; align-items:center; gap:4px; padding:2px 8px;
      border-radius:999px; font-size:11px; font-weight:600; letter-spacing:0.01em;
      background:var(--surface-3); color:var(--muted); }}
    .state-pill.s-navigating {{ background:var(--accent-soft); color:#1d4ed8; }}
    .state-pill.s-processing {{ background:var(--success-soft); color:#0b6e3f; }}
    .state-pill.s-charging {{ background:#ddeafe; color:#1d4ed8; }}
    .state-pill.s-waiting {{ background:var(--danger-soft); color:var(--danger); }}
    .state-pill.s-idle {{ background:var(--surface-3); color:var(--muted); }}
    .depth-bars {{ display:grid; gap:3px; }}
    .depth-row {{ display:grid; grid-template-columns:56px 1fr; gap:8px; align-items:center; font-size:11.5px; }}
    .depth-row .bar {{ height:6px; background:linear-gradient(90deg,#2459d1 0%,#2459d1 var(--bar),var(--surface-3) var(--bar)); border-radius:999px; }}
    .depth-row .label {{ color:var(--muted); font-family:var(--font-mono); font-size:10.5px; }}
    .chain-row {{ display:flex; gap:6px; align-items:center; font-size:11.5px; flex-wrap:wrap; }}
    .chain-row .badge {{ border:1px solid var(--border); border-radius:999px; padding:1px 8px;
      background:var(--surface); font-family:var(--font-mono); font-size:11px; }}
    .chain-row .arrow {{ color:var(--muted-2); font-size:11px; }}
    .chain-row.cycle .badge {{ border-color:var(--danger); color:var(--danger); background:var(--danger-soft); }}
    .side-stack .panel {{ display:flex; flex-direction:column; min-height:0; overflow:hidden; padding:12px 14px; }}
    .side-stack .event-list, .side-stack .incident-list {{ max-height:none; flex:1 1 auto; min-height:0; }}
    .map-panel {{ padding:0; overflow:hidden; }}
    .map-shell {{ border-top:1px solid var(--border); background:#fbfcfe; }}
    svg {{ width:100%; height:760px; background:linear-gradient(180deg,#fcfdff 0%,#f7f9fc 100%); display:block; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:8px 14px; font-size:11.5px; color:var(--muted); }}
    .legend-item {{ display:inline-flex; align-items:center; gap:5px; }}
    .swatch {{ width:10px; height:10px; border-radius:2px; box-shadow:0 0 0 1px rgba(15,23,42,0.04); }}
    .swatch-ring {{ border-radius:50%; background:#fff; border:2px solid #94a3b8; box-shadow:none; }}
    .legend-divider {{ width:1px; align-self:stretch; background:var(--border); margin:0 2px; }}
    .event-list, .incident-list {{ display:grid; gap:6px; max-height:280px; overflow:auto; padding-right:4px; }}
    .event-item, .incident-item {{ border:1px solid var(--border); border-radius:var(--radius-sm);
      padding:8px 10px; background:var(--surface-2); font-size:12.5px; line-height:1.45;
      transition:border-color 100ms,background 100ms,box-shadow 100ms; }}
    .event-item {{ font-family:inherit; }}
    .event-item .event-head {{ font-weight:600; color:var(--ink); }}
    .event-item .event-key {{ font-family:var(--font-mono); font-size:11.5px; color:var(--muted); }}
    .incident-item {{ cursor:pointer; }}
    .incident-item:hover {{ border-color:var(--accent); background:var(--accent-soft); box-shadow:var(--shadow-sm); }}
    .meta {{ color:var(--muted); font-size:11.5px; }}
    .agv-label {{ font-size:10px; fill:var(--ink); font-weight:600; paint-order:stroke;
      stroke:rgba(255,255,255,0.85); stroke-width:2px; }}
    .section-title {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom:10px; }}
    .section-title h2 {{ font-size:13px; font-weight:700; letter-spacing:-0.005em; }}
    .map-topline {{ display:flex; justify-content:space-between; align-items:center; gap:10px; padding:10px 14px; }}
    .pill {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px;
      border:1px solid var(--border); border-radius:999px; background:#fff; color:var(--muted); font-size:12px; }}
    .incident-item.current {{ border-color:var(--accent); background:#eef5ff; }}
    .subtle {{ color:var(--muted); font-size:12px; }}
    @media (max-width:1180px) {{
      .main-layout {{ grid-template-columns:1fr; }}
      .side-stack {{ position:static; max-height:none; }}
      .side-stack .event-list, .side-stack .incident-list {{ max-height:320px; }}
    }}
    @media (max-width:980px) {{
      svg {{ height:520px; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="panel top">
      <div class="header-row">
        <div class="header-title">
          <div class="header-eyebrow">FAB AMR Simulator — Live</div>
          <h1>실시간 시뮬레이션</h1>
        </div>
        <div class="kpi-strip">
          <span class="kpi-chip">처리량 <strong id="live-kpi-tasks">—</strong></span>
          <span class="kpi-chip">가동률 <strong id="live-kpi-util">—</strong></span>
          <span class="kpi-chip">정면충돌 <strong id="live-kpi-headon">—</strong></span>
          <span class="kpi-chip">평균대기 <strong id="live-kpi-wait">—</strong></span>
          <span class="kpi-chip" id="live-kpi-deadlock-chip">데드락 <strong id="live-kpi-deadlock">0</strong></span>
          <span class="kpi-chip">스냅샷 <strong id="snapshot-count">—</strong></span>
          <span class="kpi-chip">이벤트 <strong id="event-count">—</strong></span>
        </div>
      </div>
      <!-- 데드락 alert 배너: backup/reroute 모두 실패한 진성 데드락 발생 시 표시 -->
      <div class="deadlock-alert" id="deadlock-alert">
        <span class="dl-icon">⛔</span>
        <span>데드락 발생 — operator 개입 필요</span>
        <span class="dl-detail" id="deadlock-alert-detail"></span>
      </div>
      <!-- F1a: fleet 별 KPI 카드 (multi-fleet 일 때만 표시) -->
      <div id="fleet-kpi-row" class="kpi-strip" style="display:none; gap:8px; padding-top:4px; border-top:1px dashed var(--border);"></div>
      <!-- 파라미터 폼 -->
      <div class="live-params" id="live-params">
        <div class="param-group">
          <span class="param-label">토폴로지</span>
          <select id="live-topology">
            <option value="A"{' selected' if topo == 'A' else ''}>Type A — 단방향 순환</option>
            <option value="B"{' selected' if topo == 'B' else ''}>Type B — 양방향+사이딩</option>
            <option value="C"{' selected' if topo == 'C' else ''}>Type C — 단방향 대형</option>
            <option value="D"{' selected' if topo == 'D' else ''}>Type D — 이중 루프</option>
            <option value="E"{' selected' if topo == 'E' else ''}>Type E — 크립 양방향</option>
            <!-- 업로드된 임포트 맵은 fetch /imported-maps 로 동적 채워짐 -->
          </select>
          <button type="button" id="upload-map-btn"
            style="height:30px; padding:0 10px; margin-left:6px; border:1px dashed var(--border-strong);
                   background:transparent; border-radius:var(--radius-sm); color:var(--muted);
                   font-size:12px; cursor:pointer;">📂 외부 맵 업로드</button>
          <button type="button" id="edit-map-btn" disabled
            style="height:30px; padding:0 10px; margin-left:4px; border:1px solid var(--border);
                   background:var(--surface); border-radius:var(--radius-sm); color:var(--ink);
                   font-size:12px; cursor:pointer;"
            title="선택된 임포트 맵을 Map Editor 에서 열기">🛠 Editor</button>
          <input type="file" id="upload-map-input" accept=".json"
            style="display:none;" multiple>
        </div>
        <div class="param-group" id="agv-count-group">
          <span class="param-label">AGV</span>
          <input type="range" id="live-agv-count" min="4" max="24" value="{agv_count}"
            oninput="document.getElementById('live-agv-val').textContent=this.value" style="width:100px">
          <span class="param-value" id="live-agv-val">{agv_count}</span>
          <span class="param-label">대</span>
        </div>
        <!-- F1a: 임포트 맵에 fleets 가 있을 때만 표시 -->
        <div class="param-group" id="fleet-slider-group" style="display:none; gap:14px;"></div>
        <div class="param-group">
          <span class="param-label">시뮬 속도</span>
          <select id="live-sim-speed">
            <option value="1.0">1×</option>
            <option value="2.0"{' selected' if abs(speed - 2.0) < 0.1 else ''}>2×</option>
            <option value="5.0"{' selected' if abs(speed - 5.0) < 0.1 else ''}>5×</option>
            <option value="10.0"{' selected' if abs(speed - 10.0) < 0.1 else ''}>10×</option>
          </select>
        </div>
        <div class="param-group">
          <span class="param-label">시뮬 시간</span>
          <input type="range" id="live-duration" min="60" max="7200" step="60" value="{duration}"
            oninput="document.getElementById('live-dur-val').textContent=fmtDur(this.value)" style="width:120px">
          <span class="param-value" id="live-dur-val">{_fmt_dur(duration)}</span>
        </div>
        <div class="param-group">
          <span class="param-label">잡 주기</span>
          <input type="range" id="live-task-interval" min="1" max="30" step="1" value="5"
            oninput="document.getElementById('live-task-val').textContent=this.value+'s'" style="width:100px">
          <span class="param-value" id="live-task-val">5s</span>
        </div>
      </div>
      <div class="playback-stage">
        <div class="toolbar">
          <div class="toolbar-group">
            <button id="run-btn" class="run-btn" type="button">▶ 실행</button>
            <button id="stop-btn" type="button" disabled>■ 중단</button>
            <button id="reset-btn" type="button">↺ 초기화</button>
            <span class="live-badge" id="live-badge"><span class="live-dot"></span>LIVE</span>
          </div>
          <div class="toolbar-group" style="flex: 1 1 320px;">
            <button id="play-toggle" class="play-toggle" type="button">재생</button>
            <div class="slider-wrap">
              <input id="time-slider" type="range" min="0" max="0" step="1" value="0" />
              <div id="event-markers" class="event-markers"></div>
            </div>
            <span class="time-pill"><span class="meta">t</span><span id="time-label">0.00s</span></span>
          </div>
          <div class="toolbar-group">
            <label class="meta" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;">
              <input type="checkbox" id="auto-follow" checked> 라이브 추적
            </label>
          </div>
          <div class="toolbar-group">
            <span class="meta">배속</span>
            <button class="speed-btn active" data-speed="1" type="button">1×</button>
            <button class="speed-btn" data-speed="2" type="button">2×</button>
            <button class="speed-btn" data-speed="5" type="button">5×</button>
          </div>
          <div class="toolbar-group">
            <span class="meta">AGV</span>
            <select id="agv-focus"><option value="">전체</option></select>
            <button id="zoom-reset-btn" type="button">줌 초기화</button>
            <button id="heatmap-toggle" type="button" title="히트맵 토글">🔥 히트맵</button>
            <button id="traffic-toggle" type="button" title="엣지별 AGV 통과 횟수 트래픽 히트맵 (사고 히트맵과 동시 활성 불가)">🚦 트래픽</button>
            <button id="collision-toggle" type="button" title="실시간 충돌 의심 마커">⚠ 충돌</button>
            <button id="block-toggle" type="button" title="엣지 클릭 차단/해제 — 영향 AGV 가 즉시 reroute">⛔ 차단</button>
            <button id="manual-job-toggle" type="button" title="노드 두 개 클릭(pickup → dropoff)으로 수동 demand 발행. ESC 로 취소">📋 수동 Job</button>
          </div>
        </div>
        <div class="hint">
          맵: 휠 확대/축소, 드래그 이동, 더블클릭 줌 초기화. AGV 클릭 시 포커스. 우측 사고 묶음 클릭 시 해당 시점으로 점프.
          <span id="block-mode-hint" style="display:none; color:var(--danger); font-weight:600; margin-left:6px;">
            ⛔ 차단 모드: 엣지 클릭 → 차단/해제
          </span>
          <span id="manual-job-panel">
            📋 수동 Job — <span id="mj-state-label">pickup 노드 선택</span>
            <span id="mj-pickup-badge" style="display:none;"></span>
            <span id="mj-idle-count" class="mj-hint"></span>
            <span class="mj-hint">ESC 취소</span>
          </span>
          <span id="mj-last-assigned" title="클릭하면 해당 AGV 포커스 / 다시 클릭 해제">
            📍 방금 할당: <strong id="mj-last-assigned-agv">—</strong>
            <span id="mj-last-assigned-route" style="font-family:var(--font-mono); font-size:11px; color:#78350f;"></span>
          </span>
          <span class="heatmap-legend" id="heatmap-legend">
            누적 사고 강도
            <span class="gradient"></span>
            <span id="heatmap-max-label">최대 —</span>
          </span>
          <span class="traffic-legend" id="traffic-legend">
            통과 횟수
            <span class="gradient"></span>
            <span id="traffic-max-label">최대 —</span>
          </span>
        </div>
      </div>
    </section>

    <section class="main-layout">
      <section class="panel map-panel">
        <div class="map-topline">
          <div class="legend">
            <span class="legend-item"><span class="swatch" style="background:#d3dae4"></span>기본 통로</span>
            <span class="legend-item"><span class="swatch" style="background:#8bb4ff"></span>계획</span>
            <span class="legend-item"><span class="swatch" style="background:#2459d1"></span>예약</span>
            <span class="legend-item"><span class="swatch" style="background:#0f9d58"></span>주행</span>
            <span class="legend-item"><span class="swatch" style="background:#c0392b"></span>차단</span>
            <span class="legend-divider"></span>
            <span class="legend-item"><span class="swatch" style="background:#0f9d58"></span>작업(ST)</span>
            <span class="legend-item"><span class="swatch" style="background:#1f6feb"></span>충전(CH)</span>
            <span class="legend-item"><span class="swatch" style="background:#e0a000"></span>사이딩(SD)</span>
            <span class="legend-item"><span class="swatch swatch-ring"></span>홀딩(HP)</span>
            <span class="legend-divider" id="fleet-legend-divider" style="display:none"></span>
            <span id="fleet-legend" style="display:inline-flex; gap:10px;"></span>
          </div>
          <span id="live-sim-time" class="meta"></span>
        </div>
        <div class="map-shell">
          <svg id="map" viewBox="-30 -10 1060 720"></svg>
        </div>
      </section>
      <aside class="side-stack" id="side-stack">
        <div class="panel" id="agv-detail-panel" hidden>
          <div class="section-title">
            <h2 style="margin:0;" id="agv-detail-title">AGV 상세</h2>
            <span class="meta">포커스 AGV</span>
          </div>
          <div id="agv-detail-body" class="agv-detail-body"></div>
        </div>
        <div class="panel" id="incident-panel">
          <div class="section-title">
            <h2 style="margin:0;">대표 사고 묶음</h2>
            <span class="meta">연속 이벤트 압축</span>
          </div>
          <div id="incident-list" class="incident-list"></div>
        </div>
        <div class="panel" id="event-panel">
          <div class="section-title">
            <h2 style="margin:0;">이벤트 로그</h2>
            <span class="meta" id="incident-count"></span>
          </div>
          <div id="event-list" class="event-list"></div>
        </div>
      </aside>
    </section>
  </div>

  <div id="mj-toast" class="mj-toast"></div>

  <script>
    // ── 시간 포맷 헬퍼 (파라미터 폼용) ─────────────────────────
    function fmtDur(s) {{
      s = Number(s);
      if (s >= 3600) return (s/3600).toFixed(1) + 'h';
      return Math.round(s/60) + 'min';
    }}

    // ── LIVE 모드 트레이스 (빈 상태에서 시작) ────────────────────
    const trace = {{
      meta: {{ sample_interval_s: 0.5, duration_s: {duration} }},
      map: {{ nodes: [], edges: [] }},
      snapshots: [],
      events: [],
    }};
    const snapshots = trace.snapshots;
    const events = trace.events;
    let map = trace.map;

    // ── 사고 필터 ─────────────────────────────────────────────────
    const incidentKinds = new Set(['headon_block','section_conflict','followon_block','deadlock_resolved','reroute','reroute_via_siding']);
    function getIncidents() {{ return events.filter(e => incidentKinds.has(e.kind)); }}

    // ── 동적으로 재계산되는 맵 파생 상태 ─────────────────────────
    let agvIds = [];
    let nodeIndex = new Map();
    let minX = 0, maxX = 1, minY = 0, maxY = 1;
    let oneWayEdges = [];
    const directionMarkerCorridors = new Set(['north','center','south','bay']);
    let heatmapCounts = new Map();
    let heatmapMax = 0;
    // T-70: edge_enter 누적 통과 횟수 (트래픽 히트맵 source)
    let trafficCounts = new Map();
    let trafficMax = 0;

    function rebuildMapState() {{
      map = trace.map;
      nodeIndex = new Map((map.nodes || []).map(n => [n.node_id, n]));
      if (map.nodes && map.nodes.length) {{
        const xs = map.nodes.map(n => n.x);
        const ys = map.nodes.map(n => n.y);
        minX = Math.min(...xs); maxX = Math.max(...xs);
        minY = Math.min(...ys); maxY = Math.max(...ys);
      }}
      const edgeKeySet = new Set((map.edges || []).map(e => e.edge_key));
      oneWayEdges = (map.edges || []).filter(e => {{
        if (!directionMarkerCorridors.has(e.corridor)) return false;
        if (e.access_type) return false;
        return !edgeKeySet.has(`${{e.end_node_id}}__${{e.start_node_id}}`);
      }});
    }}

    function rebuildHeatmap() {{
      heatmapCounts = new Map();
      const HEAT = new Set(['headon_block','section_conflict','followon_block']);
      for (const ev of events) {{
        if (!HEAT.has(ev.kind)) continue;
        const k = ev.edge_key || '';
        if (!k) continue;
        heatmapCounts.set(k, (heatmapCounts.get(k) || 0) + 1);
      }}
      heatmapMax = 0;
      for (const v of heatmapCounts.values()) if (v > heatmapMax) heatmapMax = v;
    }}

    // T-70: edge_enter 이벤트를 누적해 엣지별 통과 횟수 집계. tick 마다 새 이벤트가
    // 들어오므로 onWsTick 에서 호출 (heatmap 과 같은 패턴).
    function rebuildTraffic() {{
      trafficCounts = new Map();
      for (const ev of events) {{
        if (ev.kind !== 'edge_enter') continue;
        const k = ev.edge_key || '';
        if (!k) continue;
        trafficCounts.set(k, (trafficCounts.get(k) || 0) + 1);
      }}
      trafficMax = 0;
      for (const v of trafficCounts.values()) if (v > trafficMax) trafficMax = v;
      // 활성 상태면 legend max 라벨도 갱신
      if (trafficMode) {{
        const lab = document.getElementById('traffic-max-label');
        if (lab) lab.textContent = trafficMax > 0 ? `최대 ${{trafficMax}}회` : '데이터 없음';
      }}
    }}

    // ── 플레이백 UI 상태 ─────────────────────────────────────────
    let index = 0;
    let timer = null;
    let playbackSpeed = 1;
    let focusedAgvId = '';
    let zoomScale = 1, zoomPanX = 0, zoomPanY = 0;
    let heatmapMode = false;
    let trafficMode = false;
    let collisionMode = false;
    // GAP-A: ⛔ 차단 모드 + 사용자가 차단한 엣지 키 집합 (서버 상태와 미러)
    let blockMode = false;
    let userBlockedEdges = new Set();
    let isDragging = false, dragStart = null;
    let highlightedIncident = null, highlightTimer = null;
    const HIGHLIGHT_DURATION_MS = 4500;

    // ── LIVE 연결 상태 ────────────────────────────────────────────
    let runId = null;
    let liveWs = null;
    let liveStreaming = false;
    let autoFollow = true;

    // ── KPI 표시 ─────────────────────────────────────────────────
    function updateLiveKpi(kpi) {{
      const fmt = (v, d=1) => v != null ? Number(v).toFixed(d) : '—';
      const trend = (t) => {{
        if (t == null || Math.abs(t) < 0.5) return '';
        return t > 0 ? ' ↑' : ' ↓';
      }};
      const tr = kpi.trends || {{}};
      document.getElementById('live-kpi-tasks').textContent = fmt(kpi.tasksPerHr) + '/h' + trend(tr.tasksPerHr);
      document.getElementById('live-kpi-util').textContent = fmt((kpi.utilization||0)*100,0) + '%' + trend(tr.utilization);
      document.getElementById('live-kpi-headon').textContent = fmt(kpi.headOn,0) + '회';
      document.getElementById('live-kpi-wait').textContent = fmt(kpi.avgWait) + 's' + trend(tr.avgWait);
      // F1a: fleet 별 KPI 카드
      updateFleetKpiCards(kpi.by_fleet || null);
    }}

    // ── 데드락 표시 ─────────────────────────────────────────────
    // 매 tick payload 의 deadlock_* 필드 → KPI chip + 알림 배너 + AGV 사이클 강조.
    let _lastDeadlockCycleKey = '';  // 같은 사이클 반복 강조 방지
    function updateDeadlock(msg) {{
      const total = msg.deadlock_count_total || 0;
      const detected = !!msg.deadlock_detected;
      const alert = !!msg.deadlock_alert;
      const groups = Array.isArray(msg.deadlock_groups) ? msg.deadlock_groups : [];

      // KPI chip: 누적 카운트 + count>0 일 때 빨강 강조
      const chip = document.getElementById('live-kpi-deadlock-chip');
      const num = document.getElementById('live-kpi-deadlock');
      if (num) num.textContent = String(total);
      if (chip) chip.classList.toggle('deadlock-hot', total > 0);

      // 사이클 멤버 강조 — 새 사이클이 감지된 tick 에만.
      const cycleKey = groups.map(g => g.slice().sort().join(',')).sort().join('|');
      if (detected && groups.length && cycleKey !== _lastDeadlockCycleKey) {{
        _lastDeadlockCycleKey = cycleKey;
        const cycle = groups[0];
        // 사이클 첫 멤버를 anchor 로 highlight + chain 에 전체 멤버
        setHighlight('', cycle[0], {{ chain: cycle, cycle: true }});
        // 합성 이벤트로 이벤트 패널 / 사고 카운터 에 노출
        const synth = {{
          t: (snapshots.length && snapshots[snapshots.length-1].t) || 0,
          kind: 'deadlock_resolved',
          agv_id: cycle[0],
          cycle: cycle,
          deadlock_total: total,
        }};
        events.push(synth);
        _eventMarkersBuiltKey = '';
      }} else if (!detected) {{
        _lastDeadlockCycleKey = '';
      }}

      // alert 배너 — backup/reroute 모두 실패 시 빨강 깜빡임
      const banner = document.getElementById('deadlock-alert');
      const detail = document.getElementById('deadlock-alert-detail');
      if (banner) banner.classList.toggle('active', alert);
      if (detail && alert && groups.length) {{
        detail.textContent = `cycle=[${{groups[0].join(', ')}}]`;
      }} else if (detail && !alert) {{
        detail.textContent = '';
      }}
    }}

    // ── F1a: fleet 색/카드 상태 ────────────────────────────────
    let currentFleets = []; // [{{id, color, graph_idx, count, agv_ids}}]
    let fleetById = new Map();
    let fleetByAgvId = new Map();
    function setCurrentFleets(fleets) {{
      currentFleets = Array.isArray(fleets) ? fleets : [];
      fleetById = new Map(currentFleets.map(f => [f.id, f]));
      fleetByAgvId = new Map();
      for (const fl of currentFleets) {{
        for (const aid of (fl.agv_ids || [])) {{
          fleetByAgvId.set(aid, fl);
        }}
      }}
      renderFleetLegend();
      // 카드 row 초기화 (값은 tick 받기 전까지 — 표시)
      const row = document.getElementById('fleet-kpi-row');
      const isMulti = currentFleets.length > 1
        || (currentFleets.length === 1 && currentFleets[0].id !== 'default');
      if (!isMulti) {{
        row.style.display = 'none';
        row.innerHTML = '';
        return;
      }}
      row.style.display = 'inline-flex';
      row.innerHTML = currentFleets.map(fl => `
        <span class="kpi-chip" data-fleet="${{fl.id}}" style="border:1px solid ${{fl.color}}33;">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${{fl.color}};margin-right:4px;"></span>
          ${{fl.id}}
          · 처리량 <strong class="fk-tasks">—</strong>
          · 가동률 <strong class="fk-util">—</strong>
        </span>`).join('');
    }}
    function updateFleetKpiCards(byFleet) {{
      if (!byFleet) return;
      const fmt = (v, d=1) => v != null ? Number(v).toFixed(d) : '—';
      for (const fid of Object.keys(byFleet)) {{
        const card = document.querySelector(`#fleet-kpi-row [data-fleet="${{fid}}"]`);
        if (!card) continue;
        const k = byFleet[fid] || {{}};
        const tasksEl = card.querySelector('.fk-tasks');
        const utilEl = card.querySelector('.fk-util');
        if (tasksEl) tasksEl.textContent = fmt(k.tasksPerHr) + '/h';
        if (utilEl) utilEl.textContent = fmt((k.utilization||0)*100, 0) + '%';
      }}
    }}
    function renderFleetLegend() {{
      const el = document.getElementById('fleet-legend');
      const div = document.getElementById('fleet-legend-divider');
      const isMulti = currentFleets.length > 1
        || (currentFleets.length === 1 && currentFleets[0].id !== 'default');
      if (!el) return;
      if (!isMulti) {{ el.innerHTML = ''; if (div) div.style.display='none'; return; }}
      if (div) div.style.display = 'inline-block';
      el.innerHTML = currentFleets.map(fl => `
        <span class="legend-item">
          <span class="swatch" style="background:${{fl.color}}"></span>
          ${{fl.id}}
        </span>`).join('');
    }}
    function fleetColorOfAgv(agv) {{
      const fid = agv.fleet_id || '';
      if (fid) {{
        const fl = fleetById.get(fid);
        if (fl && fl.color) return fl.color;
      }}
      const fl = fleetByAgvId.get(agv.agv_id);
      return (fl && fl.color) ? fl.color : '#3a4555';
    }}
    window.__fleetColorOfAgv = fleetColorOfAgv;  // renderMap 에서 사용
    // F1a: imported map 의 fleets 로 슬라이더 빌드
    function buildFleetSliders(fleets) {{
      const sliderGroup = document.getElementById('fleet-slider-group');
      const agvGroup = document.getElementById('agv-count-group');
      if (!fleets || fleets.length === 0) {{
        sliderGroup.style.display = 'none';
        sliderGroup.innerHTML = '';
        agvGroup.style.display = '';
        return;
      }}
      // multi-fleet (imported) → fleet 별 슬라이더 표시, 단일 AGV 슬라이더 숨김
      agvGroup.style.display = 'none';
      sliderGroup.style.display = 'inline-flex';
      sliderGroup.innerHTML = fleets.map((fl, idx) => {{
        const color = fl.color || '#3a4555';
        const def = Math.max(1, Number(fl.count || 1));
        return `
          <span class="param-group" data-fleet-slider="${{fl.id}}" style="gap:6px;">
            <span class="param-label" style="display:inline-flex;align-items:center;gap:4px;">
              <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${{color}};"></span>
              ${{fl.id}}
            </span>
            <input type="range" min="0" max="12" step="1" value="${{def}}"
              class="fleet-count-input" data-fleet="${{fl.id}}" style="width:80px"
              oninput="this.nextElementSibling.textContent=this.value">
            <span class="param-value">${{def}}</span>
          </span>`;
      }}).join('');
    }}
    function getAgvCountByFleet() {{
      const inputs = document.querySelectorAll('.fleet-count-input');
      if (inputs.length === 0) return null;
      const m = {{}};
      inputs.forEach(inp => {{ m[inp.dataset.fleet] = Number(inp.value); }});
      return m;
    }}

    // ── 라이브 실행 ───────────────────────────────────────────────
    async function doRun() {{
      const topoSel = document.getElementById('live-topology').value;
      // 업로드된 임포트 맵은 옵션 value 가 "imported:<id>" 형식
      let topology = topoSel, importedMapId = null;
      if (topoSel.startsWith('imported:')) {{
        importedMapId = topoSel.slice('imported:'.length);
        topology = 'A'; // 무시되지만 placeholder
      }}
      const agvCount = Number(document.getElementById('live-agv-count').value);
      const simSpeed = Number(document.getElementById('live-sim-speed').value);
      const duration = Number(document.getElementById('live-duration').value);
      const taskIntervalS = Number(document.getElementById('live-task-interval').value);

      // 기존 연결 종료 + 상태 초기화
      if (liveWs) {{ try {{ liveWs.close(); }} catch(e) {{}} liveWs = null; }}
      snapshots.length = 0;
      events.length = 0;
      agvIds = [];
      index = 0;
      heatmapCounts = new Map(); heatmapMax = 0;
      trafficCounts = new Map(); trafficMax = 0;
      _eventMarkersBuiltKey = '';
      // GAP-A: 새 sim 시작 → 차단 엣지 초기화 (서버도 init 시 리셋)
      userBlockedEdges = new Set();
      trace.meta.duration_s = duration;
      trace.meta.sample_interval_s = 0.5;

      document.getElementById('run-btn').disabled = true;
      document.getElementById('stop-btn').disabled = false;
      document.getElementById('live-params').style.opacity = '0.5';
      document.getElementById('live-params').style.pointerEvents = 'none';
      document.getElementById('live-badge').classList.remove('active');

      // F1a: 임포트 맵에 fleet 슬라이더가 있으면 fleet 별 count 전달
      const agvCountByFleet = importedMapId ? getAgvCountByFleet() : null;
      try {{
        const resp = await fetch('/init', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ topology, agvCount, speed: simSpeed, duration, blockedEdges: [],
                                  taskIntervalS, importedMapId, agvCountByFleet }}),
        }});
        if (!resp.ok) throw new Error('init failed: ' + resp.status + ' ' + await resp.text());
        const data = await resp.json();
        runId = data.runId;
        trace.map = data.map;
        rebuildMapState();
        populateAgvFocusOptions();
        // F1a: /init 응답의 fleets 로 색/legend/카드 초기화
        setCurrentFleets(data.fleets || []);

        // WS 연결
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        liveWs = new WebSocket(proto + '//' + location.host + data.wsUrl);
        liveWs.onmessage = onWsTick;
        liveWs.onclose = onWsClose;
        liveStreaming = true;
        document.getElementById('live-badge').classList.add('active');
        render();
      }} catch (err) {{
        alert('시뮬레이션 시작 실패: ' + err.message);
        document.getElementById('run-btn').disabled = false;
        document.getElementById('stop-btn').disabled = true;
        document.getElementById('live-params').style.opacity = '';
        document.getElementById('live-params').style.pointerEvents = '';
      }}
    }}

    function onWsTick(ev) {{
      let msg;
      try {{ msg = JSON.parse(ev.data); }} catch(e) {{ return; }}
      if (msg.type === 'tick') {{
        const snap = msg.snapshot;
        if (!snap) return;
        snapshots.push(snap);
        if (msg.new_events && msg.new_events.length) {{
          events.push(...msg.new_events);
          rebuildHeatmap();
          rebuildTraffic();
          _eventMarkersBuiltKey = '';
        }}
        if (msg.kpi) updateLiveKpi(msg.kpi);
        // 데드락 필드: KPI chip + 사이클 강조 + alert 배너
        updateDeadlock(msg);
        // 수동 Job 모드 IDLE AGV 카운트 갱신
        if (manualJobMode) updateManualJobPanel(snap);
        // AGV 목록이 생기면 드롭다운 채우기
        if (snap.agvs && snap.agvs.length && agvIds.length === 0) {{
          agvIds = snap.agvs.map(a => a.agv_id).sort();
          populateAgvFocusOptions();
        }}
        if (autoFollow) index = snapshots.length - 1;
        // 맵 시뮬 시간 표시
        const simT = document.getElementById('live-sim-time');
        if (simT) simT.textContent = `sim t = ${{(snap.t||0).toFixed(1)}}s / ${{trace.meta.duration_s}}s`;
        render();
      }} else if (msg.type === 'end') {{
        liveStreaming = false;
        document.getElementById('live-badge').classList.remove('active');
        document.getElementById('run-btn').disabled = false;
        document.getElementById('stop-btn').disabled = true;
        document.getElementById('live-params').style.opacity = '';
        document.getElementById('live-params').style.pointerEvents = '';
        render();
      }}
    }}

    function onWsClose() {{
      if (liveStreaming) {{
        liveStreaming = false;
        document.getElementById('live-badge').classList.remove('active');
        document.getElementById('run-btn').disabled = false;
        document.getElementById('stop-btn').disabled = true;
        document.getElementById('live-params').style.opacity = '';
        document.getElementById('live-params').style.pointerEvents = '';
      }}
    }}

    async function doStop() {{
      if (!runId) return;
      try {{
        await fetch('/control', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ runId, action: 'stop' }}),
        }});
      }} catch(e) {{}}
    }}

    function doReset() {{
      if (liveWs) {{ try {{ liveWs.close(); }} catch(e) {{}} liveWs = null; }}
      snapshots.length = 0; events.length = 0;
      agvIds = []; index = 0; runId = null; liveStreaming = false;
      heatmapCounts = new Map(); heatmapMax = 0;
      trafficCounts = new Map(); trafficMax = 0;
      _eventMarkersBuiltKey = '';
      trace.meta.duration_s = Number(document.getElementById('live-duration').value);
      document.getElementById('run-btn').disabled = false;
      document.getElementById('stop-btn').disabled = true;
      document.getElementById('live-params').style.opacity = '';
      document.getElementById('live-params').style.pointerEvents = '';
      document.getElementById('live-badge').classList.remove('active');
      document.getElementById('live-sim-time').textContent = '';
      document.getElementById('live-kpi-tasks').textContent = '—';
      document.getElementById('live-kpi-util').textContent = '—';
      document.getElementById('live-kpi-headon').textContent = '—';
      document.getElementById('live-kpi-wait').textContent = '—';
      // 데드락 KPI / alert 초기화
      const dlNum = document.getElementById('live-kpi-deadlock');
      if (dlNum) dlNum.textContent = '0';
      const dlChip = document.getElementById('live-kpi-deadlock-chip');
      if (dlChip) dlChip.classList.remove('deadlock-hot');
      const dlBanner = document.getElementById('deadlock-alert');
      if (dlBanner) dlBanner.classList.remove('active');
      _lastDeadlockCycleKey = '';
      // F1a: fleet 카드/legend 도 초기화 (단일 default 로 돌림)
      setCurrentFleets([]);
      populateAgvFocusOptions();
      render();
    }}

    // ── 공유 헬퍼 함수 ────────────────────────────────────────────
    function clearHighlight() {{
      highlightedIncident = null;
      if (highlightTimer) {{ clearTimeout(highlightTimer); highlightTimer = null; }}
      render();
    }}
    function setHighlight(edgeKey, agvId, opts) {{
      const meta = opts || {{}};
      highlightedIncident = {{
        edge_key: edgeKey || '', agv_id: agvId || '',
        chain: meta.chain || [], cycle: !!meta.cycle,
        until: Date.now() + HIGHLIGHT_DURATION_MS,
      }};
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(clearHighlight, HIGHLIGHT_DURATION_MS);
    }}
    function chainAtTime(t, agvId) {{
      if (!agvId) return {{ chain: [], cycle: false }};
      const intv = trace.meta.sample_interval_s || 0.5;
      const idx = Math.min(snapshots.length - 1, Math.max(0, Math.round(t / intv)));
      return buildBlockingChainFor(snapshots[idx] || {{ agvs: [] }}, agvId);
    }}
    function sx(x) {{ return 60 + ((x - minX) / Math.max(maxX - minX, 1)) * 880; }}
    function sy(y) {{ return 620 - ((y - minY) / Math.max(maxY - minY, 1)) * 540; }}
    function currentTime() {{ return (snapshots[index] && snapshots[index].t) || 0; }}
    function setIndexFromTime(targetTime) {{
      let closest = 0;
      for (let i = 0; i < snapshots.length; i++) {{
        if ((snapshots[i].t || 0) >= targetTime) {{ closest = i; break; }}
        closest = i;
      }}
      index = closest;
      render();
    }}
    function eventLabel(kind) {{
      const labels = {{
        headon_block:'정면 교행 충돌 차단', section_conflict:'구간 충돌',
        followon_block:'동일 방향 추종 차단', wait_start:'대기 시작',
        reroute:'재경로', reroute_via_siding:'사이딩 우회 후보',
        reroute_siding_applied:'사이딩 우회 적용', demand_completed:'수요 완료',
        charging_start:'충전 시작', charging_complete:'충전 완료',
        processing_start:'작업 시작', edge_enter:'엣지 진입',
        edge_exit:'엣지 이탈', deadlock_resolved:'데드락 해소',
        order_received:'오더 수신',
      }};
      return labels[kind] || kind;
    }}
    function filteredEventAgv(event) {{
      return !focusedAgvId || event.agv_id === focusedAgvId || event.blocking_agv === focusedAgvId;
    }}
    function describeEvent(event) {{
      const parts = [];
      if (event.agv_id) parts.push(event.agv_id);
      parts.push(eventLabel(event.kind));
      if (event.edge_key) parts.push(event.edge_key);
      if (event.section_key) parts.push(event.section_key);
      if (event.siding_id) parts.push(`siding=${{event.siding_id}}`);
      if (event.blocking_agv) parts.push(`block=${{event.blocking_agv}}`);
      return parts.join(' / ');
    }}
    function buildIncidentGroups(sourceEvents) {{
      const sorted = [...sourceEvents].sort((a,b) => (a.t||0)-(b.t||0));
      const groups = [];
      for (const event of sorted) {{
        const last = groups[groups.length-1];
        const sameKey = last && last.kind === event.kind
          && (last.edge_key||'') === (event.edge_key||'')
          && (last.section_key||'') === (event.section_key||'')
          && (last.agv_id||'') === (event.agv_id||'')
          && ((event.t||0)-last.end_t) <= 2.5;
        if (sameKey) {{
          last.end_t = event.t || last.end_t;
          last.count += 1;
          last.events.push(event);
        }} else {{
          groups.push({{ kind:event.kind, agv_id:event.agv_id||'', edge_key:event.edge_key||'',
            section_key:event.section_key||'', start_t:event.t||0, end_t:event.t||0,
            count:1, events:[event] }});
        }}
      }}
      return groups.slice(-60).reverse();
    }}
    function populateAgvFocusOptions() {{
      const select = document.getElementById('agv-focus');
      select.innerHTML = '<option value="">전체</option>'
        + agvIds.map(id => `<option value="${{id}}">${{id}}</option>`).join('');
    }}
    function svgPointFromClient(svg, clientX, clientY) {{
      const rect = svg.getBoundingClientRect();
      const vb = svg.viewBox.baseVal;
      return {{ x: vb.x + ((clientX-rect.left)/rect.width)*vb.width,
               y: vb.y + ((clientY-rect.top)/rect.height)*vb.height }};
    }}
    function svgDeltaFromClient(svg, dx, dy) {{
      const rect = svg.getBoundingClientRect();
      const vb = svg.viewBox.baseVal;
      return {{ x:(dx/rect.width)*vb.width, y:(dy/rect.height)*vb.height }};
    }}
    function resetZoom() {{ zoomScale=1; zoomPanX=0; zoomPanY=0; }}
    function isAnchoredOnNode(agv) {{
      if (!agv.current_node) return false;
      const node = nodeIndex.get(agv.current_node);
      if (!node) return false;
      return Math.abs((agv.x||0)-node.x)<0.01 && Math.abs((agv.y||0)-node.y)<0.01;
    }}
    function agvDisplayPoints(agvs) {{
      const grouped = new Map();
      for (const agv of agvs) {{
        const anchored = isAnchoredOnNode(agv);
        const bucketKey = anchored
          ? `node:${{agv.current_node||agv.agv_id}}`
          : `coord:${{Math.round((agv.x||0)*10)/10}}:${{Math.round((agv.y||0)*10)/10}}`;
        if (!grouped.has(bucketKey)) grouped.set(bucketKey, []);
        grouped.get(bucketKey).push({{agv, anchored}});
      }}
      const positioned = [];
      for (const entries of grouped.values()) {{
        const count = entries.length;
        entries.forEach((entry, idx) => {{
          const spread = count<=1 ? 0 : (idx-(count-1)/2);
          const ox = count<=1 ? 0 : (entry.anchored ? spread*14 : spread*10);
          const oy = count<=1 ? 0 : (entry.anchored ? -10-Math.abs(spread)*4 : 8+spread*3);
          const labelOx = 12;
          const labelOy = count<=1 ? -12 : (-12+(idx-(count-1)/2)*12);
          positioned.push({{agv:entry.agv, anchored:entry.anchored, ox, oy, labelOx, labelOy,
            bucketIndex:idx, bucketCount:count}});
        }});
      }}
      const Y_BAND=22, X_PROXIMITY=110;
      const singles = positioned.filter(it=>it.bucketCount<=1);
      const bands = new Map();
      for (const item of singles) {{
        const cy = sy(item.agv.y);
        const bandKey = Math.round(cy/Y_BAND);
        if (!bands.has(bandKey)) bands.set(bandKey, []);
        bands.get(bandKey).push(item);
      }}
      for (const items of bands.values()) {{
        if (items.length<=1) continue;
        items.sort((a,b)=>a.agv.x-b.agv.x);
        let lastBelow=-Infinity, lastAbove=-Infinity;
        for (const item of items) {{
          const cx=sx(item.agv.x);
          const aboveBusy=cx-lastAbove<X_PROXIMITY;
          const belowBusy=cx-lastBelow<X_PROXIMITY;
          if (aboveBusy && !belowBusy) {{ item.labelOy=18; lastBelow=cx; }}
          else if (!aboveBusy) {{ lastAbove=cx; }}
          else {{ item.labelOy=30; lastBelow=cx; }}
        }}
      }}
      return positioned;
    }}
    function agvShapeMarkup(cx,cy,color,anchored,faded) {{
      const opacity = faded ? 0.18 : 1.0;
      if (anchored) return `<circle cx="${{cx}}" cy="${{cy}}" r="${{faded?4:5}}" fill="${{color}}" opacity="${{opacity}}" />`;
      return '';
    }}
    function agvArrowMarkup(cx,cy,color,heading,faded) {{
      const opacity = faded ? 0.18 : 1.0;
      const scale = faded ? 0.5 : 0.65;
      const cos = Math.cos(heading||0), sin = Math.sin(heading||0);
      const rp = (px,py) => `${{cx+(px*cos-py*sin)*scale}},${{cy-(px*sin+py*cos)*scale}}`;
      const points = [rp(14,0),rp(-8,-9),rp(-1,0),rp(-8,9)].join(' ');
      return `<polygon points="${{points}}" fill="${{color}}" opacity="${{opacity}}" />`;
    }}
    // ── AGV 상태별 색상 매핑 ────────────────────────────────────
    // 우선순위: ERROR > CHARGING > 작업중(demand) > WAITING > NAVIGATING(빈손) > IDLE
    function lightenHex(hex, amount) {{
      let h = (hex || '#3a4555').replace('#', '');
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      if (h.length !== 6) return hex;
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      let hh = 0, s = 0, l = (max + min) / 510;
      if (max !== min) {{
        const d = max - min;
        s = l > 0.5 ? d / (510 - max - min) : d / (max + min);
        if (max === r) hh = ((g - b) / d + (g < b ? 6 : 0));
        else if (max === g) hh = ((b - r) / d + 2);
        else hh = ((r - g) / d + 4);
        hh /= 6;
      }}
      l = Math.min(1, l + amount);
      const hue2rgb = (p, q, t) => {{
        if (t < 0) t += 1; if (t > 1) t -= 1;
        if (t < 1 / 6) return p + (q - p) * 6 * t;
        if (t < 1 / 2) return q;
        if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
        return p;
      }};
      const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
      const p = 2 * l - q;
      const nr = Math.round(hue2rgb(p, q, hh + 1 / 3) * 255);
      const ng = Math.round(hue2rgb(p, q, hh) * 255);
      const nb = Math.round(hue2rgb(p, q, hh - 1 / 3) * 255);
      return '#' + [nr, ng, nb].map(c => c.toString(16).padStart(2, '0')).join('');
    }}
    function agvDisplayColor(agv, baseColor) {{
      const base = baseColor || '#3a4555';
      const state = (agv && agv.state) || '';
      if (state === 'ERROR') return '#e74c3c';
      if (state === 'CHARGING') return '#9b59b6';
      const demandId = (agv && agv.current_demand_id) || '';
      const hasJob = !!demandId;
      if (hasJob && (state === 'NAVIGATING' || state === 'PROCESSING')) {{
        return demandId.startsWith('manual_') ? '#e67e22' : '#2980b9';
      }}
      if (state === 'WAITING_RESERVATION') return '#f39c12';
      if (state === 'NAVIGATING') return lightenHex(base, 0.3);
      return base;
    }}
    window.__agvDisplayColor = agvDisplayColor;
    function immediateGoalNodeId(agv) {{
      if (agv.immediate_goal) return agv.immediate_goal;
      if (agv.goal_node) return agv.goal_node;
      if (agv.target_node) return agv.target_node;
      const keys = agv.planned_edge_keys || [];
      if (!keys.length) return '';
      const parts = (keys[keys.length-1]||'').split('__');
      return parts[1]||'';
    }}
    function destinationNodeId(agv) {{ return immediateGoalNodeId(agv); }}
    function describeGoal(goalId) {{
      if (!goalId) return '';
      if (goalId.startsWith('ST_')) return '→'+goalId;
      if (goalId.startsWith('HP_')) return '→휴식 '+goalId;
      if (goalId.startsWith('CH_')) return '→충전 '+goalId;
      return '→'+goalId;
    }}
    function destinationLabel(agv) {{
      let base = describeGoal(immediateGoalNodeId(agv));
      if (!base) return '';
      if (agv.phase==='pickup') base+=' [픽업]';
      else if (agv.phase==='dropoff') base+=' [드롭]';
      if (agv.detour_via) base+=' (via '+agv.detour_via+')';
      return base;
    }}
    function agvCaption(agv, includeState) {{
      const state = agv.state;
      if (state==='NAVIGATING') {{
        const dest=destinationLabel(agv);
        return dest ? (agv.agv_id+' '+dest) : agv.agv_id;
      }}
      if (state==='WAITING_RESERVATION') return agv.agv_id+' (대기)';
      if (state==='PROCESSING') return agv.agv_id+' (작업중)';
      if (state==='CHARGING') return agv.agv_id+' (충전중)';
      if (state==='IDLE') return includeState ? agv.agv_id : agv.agv_id+' (IDLE)';
      return agv.agv_id+' ('+state+')';
    }}
    function renderMap() {{
      const svg = document.getElementById('map');
      if (!map.nodes || !map.nodes.length) {{ svg.innerHTML=''; return; }}
      const snapshot = snapshots[index] || {{ agvs:[] }};
      const visibleAgvs = (snapshot.agvs||[]).filter(agv=>!focusedAgvId||agv.agv_id===focusedAgvId);
      const fadedAgvs = (snapshot.agvs||[]).filter(agv=>focusedAgvId&&agv.agv_id!==focusedAgvId);
      const visibleDisplay = agvDisplayPoints(visibleAgvs);
      const fadedDisplay = agvDisplayPoints(fadedAgvs);
      const edgeStyles = new Map();
      const applyEdgeStyle = (edgeKey, patch) => {{
        if (!edgeKey) return;
        const prev = edgeStyles.get(edgeKey) || {{stroke:'#c6d0db',width:2,opacity:1,dash:''}};
        edgeStyles.set(edgeKey, {{...prev,...patch}});
      }};
      if (heatmapMode && heatmapMax>0) {{
        const logMax=Math.log(heatmapMax+1);
        for (const [edgeKey,count] of heatmapCounts) {{
          const t=Math.log(count+1)/Math.max(logMax,0.0001);
          const lerp=(a,b)=>Math.round(a+(b-a)*t);
          const stroke=`rgb(${{lerp(0xfd,0xc0)}},${{lerp(0xe6,0x39)}},${{lerp(0xe2,0x2b)}})`;
          applyEdgeStyle(edgeKey,{{stroke,width:3+t*5,opacity:0.92,dash:''}});
        }}
      }}
      // T-70: 트래픽 모드 — edge_enter 누적 횟수 파랑~보라 그라데이션. heatmap 과 mutually exclusive.
      if (trafficMode && trafficMax>0) {{
        const logMax=Math.log(trafficMax+1);
        for (const [edgeKey,count] of trafficCounts) {{
          const t=Math.log(count+1)/Math.max(logMax,0.0001);
          const lerp=(a,b)=>Math.round(a+(b-a)*t);
          const stroke=`rgb(${{lerp(0xe0,0x43)}},${{lerp(0xe7,0x38)}},${{lerp(0xff,0xca)}})`;
          applyEdgeStyle(edgeKey,{{stroke,width:3+t*5,opacity:0.92,dash:''}});
        }}
      }}
      // ★ 히트맵/트래픽 모드일 때 AGV 오버레이 skip (누적 패턴 전용 뷰)
      if (!heatmapMode && !trafficMode) {{
        for (const agv of visibleAgvs) {{
          const planned=agv.planned_edge_keys||[];
          planned.forEach((edgeKey,depth)=>{{
            const t=Math.min(depth,8)/8;
            applyEdgeStyle(edgeKey,{{stroke:'#8bb4ff',width:3,opacity:0.95-t*0.65,dash:'6 5'}});
          }});
          const reserved=agv.reserved_edge_keys||[];
          reserved.forEach((edgeKey,depth)=>{{
            const t=Math.min(depth,6)/6;
            applyEdgeStyle(edgeKey,{{stroke:'#2459d1',width:4-t*1.2,opacity:1.0-t*0.45,dash:''}});
          }});
          if (agv.current_edge_key) applyEdgeStyle(agv.current_edge_key,{{stroke:'#0f9d58',width:6,opacity:1,dash:''}});
          if (agv.blocked_edge_key && agv.state==='WAITING_RESERVATION')
            applyEdgeStyle(agv.blocked_edge_key,{{stroke:'#c0392b',width:6,opacity:1,dash:''}});
        }}
      }}
      // GAP-A: 사용자 차단 엣지는 빨간 점선 + ⛔ 마커 (최상위 우선)
      for (const ek of userBlockedEdges) {{
        applyEdgeStyle(ek,{{stroke:'#c0392b',width:5,opacity:0.95,dash:'4 4'}});
      }}
      const labelScale=1/Math.max(zoomScale,0.0001);
      svg.innerHTML=`
        <g id="viewport" transform="translate(${{zoomPanX}} ${{zoomPanY}}) scale(${{zoomScale}})">
          ${{(map.edges||[]).map(edge=>{{
            const style=edgeStyles.get(edge.edge_key)||{{stroke:'#c6d0db',width:2,opacity:1,dash:''}};
            const blocked = userBlockedEdges.has(edge.edge_key);
            // 차단된 엣지는 시각 강조 (그 위에 ⛔ 마커도 추가). 클릭 hit 영역은
            // 별도 투명 stroke 로 두꺼워 모드 OFF 일 때 클릭 인터랙션 방해 X (pointer-events 제어).
            const hit = `<line class="edge-hit" data-edge-key="${{edge.edge_key}}" x1="${{sx(edge.x1)}}" y1="${{sy(edge.y1)}}" x2="${{sx(edge.x2)}}" y2="${{sy(edge.y2)}}" stroke="transparent" stroke-width="${{14/zoomScale}}" stroke-linecap="round" pointer-events="${{blockMode?'stroke':'none'}}" vector-effect="non-scaling-stroke" />`;
            const main = `<line x1="${{sx(edge.x1)}}" y1="${{sy(edge.y1)}}" x2="${{sx(edge.x2)}}" y2="${{sy(edge.y2)}}" stroke="${{style.stroke}}" stroke-width="${{style.width/zoomScale}}" stroke-opacity="${{style.opacity}}" stroke-dasharray="${{style.dash}}" stroke-linecap="round" vector-effect="non-scaling-stroke" pointer-events="none" />`;
            return main + hit;
          }}).join('')}}
          ${{Array.from(userBlockedEdges).map(ek=>{{
            const edge=(map.edges||[]).find(e=>e.edge_key===ek);
            if (!edge) return '';
            const mx=sx((edge.x1+edge.x2)/2), my=sy((edge.y1+edge.y2)/2);
            return `<g transform="translate(${{mx}} ${{my}}) scale(${{labelScale}})" pointer-events="none">
              <circle r="9" fill="#fff" stroke="#c0392b" stroke-width="1.5" />
              <text y="3.5" text-anchor="middle" font-size="11" font-weight="700" fill="#c0392b">⛔</text>
            </g>`;
          }}).join('')}}
          ${{oneWayEdges.map(edge=>{{
            const mx=sx((edge.x1+edge.x2)/2), my=sy((edge.y1+edge.y2)/2);
            const dx=sx(edge.x2)-sx(edge.x1), dy=sy(edge.y2)-sy(edge.y1);
            const len=Math.hypot(dx,dy)||1;
            const ux=dx/len, uy=dy/len;
            const angle=Math.atan2(uy,ux)*180/Math.PI;
            return `<g transform="translate(${{mx}} ${{my}}) scale(${{labelScale}}) rotate(${{angle}})"><polygon points="6,0 -2,-4 -2,4" fill="#9aa6b6" opacity="0.85" /></g>`;
          }}).join('')}}
          ${{(map.nodes||[]).map(node=>{{
            const id=node.node_id||'';
            const isCharger=node.is_charger||node.role==='charger';
            const isWork=node.role==='work';
            const isSiding=node.role==='siding'||id.startsWith('SD_');
            const isHolding=id.startsWith('HP_');
            const isAccess=id.startsWith('SA_')||id.startsWith('CA_')||id.startsWith('HA_');
            const cx=sx(node.x), cy=sy(node.y);
            let inner;
            if (isCharger) inner=`<rect x="-5" y="-5" width="10" height="10" rx="1" fill="#1f6feb" opacity="0.95" />`;
            else if (isWork) inner=`<circle r="5" fill="#0f9d58" opacity="0.95" />`;
            else if (isSiding) inner=`<circle r="4" fill="#e0a000" opacity="0.95" />`;
            else if (isHolding) inner=`<circle r="4" fill="#fff" stroke="#8b98a8" stroke-width="${{1.5/zoomScale}}" vector-effect="non-scaling-stroke" />`;
            else {{ const radius=isAccess?2.5:3; inner=`<circle r="${{radius}}" fill="#8b98a8" opacity="0.7" />`; }}
            // 수동 Job 모드 시 pickup 으로 선택된 노드는 초록 ring 강조
            const isPickupSel = window.__mjPickup === id;
            const pickupRing = isPickupSel
              ? `<circle r="11" class="node-pickup-ring"><animate attributeName="r" values="9;13;9" dur="1.2s" repeatCount="indefinite" /></circle>`
              : '';
            // 노드 클릭용 hit 영역 — 평소엔 투명, 수동 모드에선 hover 가능
            const hit = `<circle r="10" class="node-hit" data-node-id="${{id}}" />`;
            return `<g transform="translate(${{cx}} ${{cy}}) scale(${{labelScale}})">${{pickupRing}}${{inner}}${{hit}}</g>`;
          }}).join('')}}
          ${{fadedDisplay.map(item=>{{
            const agv=item.agv, cx=sx(agv.x)+item.ox, cy=sy(agv.y)+item.oy;
            const baseColor = (window.__fleetColorOfAgv ? window.__fleetColorOfAgv(agv) : '#3a4555');
            const color = agvDisplayColor(agv, baseColor);
            const inner=item.anchored?agvShapeMarkup(0,0,color,true,true):agvArrowMarkup(0,0,color,agv.heading,true);
            return `<g transform="translate(${{cx}} ${{cy}}) scale(${{labelScale}})">${{inner}}</g>`;
          }}).join('')}}
          ${{visibleDisplay.map(item=>{{
            const agv=item.agv, cx=sx(agv.x)+item.ox, cy=sy(agv.y)+item.oy;
            const labelX=cx+item.labelOx, labelY=cy+item.labelOy;
            const labelText=item.bucketCount>1?agvCaption(agv,true):agvCaption(agv,false);
            const baseColor = (window.__fleetColorOfAgv ? window.__fleetColorOfAgv(agv) : '#3a4555');
            const color = agvDisplayColor(agv, baseColor);
            const inner=item.anchored?agvShapeMarkup(0,0,color,true,false):agvArrowMarkup(0,0,color,agv.heading,false);
            return `
              <g class="agv-hit" data-agv-id="${{agv.agv_id}}" style="cursor:pointer;" transform="translate(${{cx}} ${{cy}}) scale(${{labelScale}})">
                <circle r="18" fill="transparent" />
                ${{inner}}
              </g>
              <text class="agv-label" data-agv-id="${{agv.agv_id}}" style="cursor:pointer;" transform="translate(${{labelX}} ${{labelY}}) scale(${{labelScale}})">${{labelText}}</text>`;
          }}).join('')}}
          ${{(()=>{{
            if (!focusedAgvId) return '';
            const agv=(snapshot.agvs||[]).find(a=>a.agv_id===focusedAgvId);
            if (!agv) return '';
            const destId=destinationNodeId(agv);
            if (!destId) return '';
            const node=nodeIndex.get(destId);
            if (!node) return '';
            const ax=sx(agv.x), ay=sy(agv.y), tx=sx(node.x), ty=sy(node.y);
            return `<line x1="${{ax}}" y1="${{ay}}" x2="${{tx}}" y2="${{ty}}" stroke="#1f6feb" stroke-width="2" stroke-opacity="0.55" stroke-dasharray="6 5" vector-effect="non-scaling-stroke" />
                    <g transform="translate(${{tx}} ${{ty}}) scale(${{labelScale}})"><circle r="14" fill="none" stroke="#1f6feb" stroke-width="2" stroke-dasharray="4 3" /></g>`;
          }})()}}
          ${{(()=>{{
            if (!highlightedIncident || Date.now()>=highlightedIncident.until) return '';
            const overlays=[];
            if (highlightedIncident.edge_key) {{
              const edge=(map.edges||[]).find(e=>e.edge_key===highlightedIncident.edge_key);
              if (edge) overlays.push(`<line x1="${{sx(edge.x1)}}" y1="${{sy(edge.y1)}}" x2="${{sx(edge.x2)}}" y2="${{sy(edge.y2)}}" stroke="#ff6b35" stroke-width="9" stroke-opacity="0.85" stroke-linecap="round" vector-effect="non-scaling-stroke"><animate attributeName="stroke-opacity" values="0.95;0.35;0.95" dur="1.1s" repeatCount="indefinite" /></line>`);
            }}
            const ringIds=(highlightedIncident.chain&&highlightedIncident.chain.length)?highlightedIncident.chain:(highlightedIncident.agv_id?[highlightedIncident.agv_id]:[]);
            for (const id of ringIds) {{
              const agv=(snapshot.agvs||[]).find(a=>a.agv_id===id);
              if (!agv) continue;
              const color=highlightedIncident.cycle?'#c0392b':'#ff6b35';
              overlays.push(`<g transform="translate(${{sx(agv.x)}} ${{sy(agv.y)}}) scale(${{labelScale}})"><circle r="16" fill="none" stroke="${{color}}" stroke-width="3"><animate attributeName="r" values="14;22;14" dur="1.1s" repeatCount="indefinite" /><animate attributeName="stroke-opacity" values="1;0.4;1" dur="1.1s" repeatCount="indefinite" /></circle></g>`);
            }}
            return overlays.join('');
          }})()}}
          ${{(()=>{{
            // ⚠ 실시간 충돌 의심 마커
            if (!collisionMode) return '';
            const collisions = detectCollisions(snapshot);
            if (collisions.length === 0) return '';
            return collisions.map(c => {{
              const color = c.type === 'node' ? '#dc2626' : '#f59e0b';
              const label = c.type === 'node' ? '⚠' : '!';
              return `<g transform="translate(${{c.x}} ${{c.y}}) scale(${{labelScale}})">
                <circle r="14" fill="${{color}}" opacity="0.25" />
                <circle r="9" fill="${{color}}" opacity="0.85">
                  <animate attributeName="r" values="9;13;9" dur="0.8s" repeatCount="indefinite" />
                </circle>
                <text y="3.5" text-anchor="middle" fill="#fff" font-size="11" font-weight="700">${{label}}</text>
              </g>`;
            }}).join('');
          }})()}}
          ${{(()=>{{
            // 📋 수동 Job 모드: IDLE AGV 둘레 초록 dashed ring (할당 후보)
            if (!manualJobMode) return '';
            return (snapshot.agvs||[]).filter(a=>a.state==='IDLE').map(a => {{
              const cx=sx(a.x), cy=sy(a.y);
              return `<g transform="translate(${{cx}} ${{cy}}) scale(${{labelScale}})">
                <circle r="13" class="agv-idle-mj-ring">
                  <animate attributeName="r" values="11;15;11" dur="1.6s" repeatCount="indefinite" />
                </circle>
              </g>`;
            }}).join('');
          }})()}}
          ${{(()=>{{
            // 📍 방금 할당된 AGV — 노란 펄스 ring (focus 와 별개 시각 강조)
            const a = window.__mjJustAssigned;
            if (!a || Date.now() >= a.until_ms) return '';
            const agv = (snapshot.agvs||[]).find(x=>x.agv_id===a.agv_id);
            if (!agv) return '';
            const cx=sx(agv.x), cy=sy(agv.y);
            return `<g transform="translate(${{cx}} ${{cy}}) scale(${{labelScale}})">
              <circle r="18" class="agv-just-assigned-ring">
                <animate attributeName="r" values="16;24;16" dur="1.0s" repeatCount="indefinite" />
                <animate attributeName="stroke-opacity" values="1;0.4;1" dur="1.0s" repeatCount="indefinite" />
              </circle>
            </g>`;
          }})()}}
        </g>`;
    }}

    function renderIncidents() {{
      const current=currentTime();
      const incidents=getIncidents();
      const groups=buildIncidentGroups(incidents.filter(filteredEventAgv));
      document.getElementById('incident-count').textContent = groups.length ? groups.length+'개 사고' : '';
      document.getElementById('incident-list').innerHTML=groups.map(group=>{{
        const head=group.events[0]||{{}};
        const blockingKinds=new Set(['headon_block','section_conflict','followon_block','deadlock_resolved']);
        let chainBadge='';
        if (head.agv_id && blockingKinds.has(group.kind)) {{
          const {{chain,cycle}}=chainAtTime(group.start_t,head.agv_id);
          if (chain.length>=2) chainBadge=`<span class="subtle" style="${{cycle?'color:#c0392b;font-weight:600;':''}}">${{cycle?'cycle ':''}}chain depth=${{chain.length}}</span>`;
        }}
        return `
          <div class="incident-item ${{current>=group.start_t&&current<=group.end_t?'current':''}}"
               data-time="${{group.start_t}}" data-edge-key="${{head.edge_key||''}}" data-agv-id="${{head.agv_id||''}}">
            <div class="section-title" style="margin:0 0 6px 0;">
              <strong>${{eventLabel(group.kind)}}</strong>
              <span class="subtle">${{group.count}}회 ${{chainBadge}}</span>
            </div>
            <div style="margin-top:4px;">${{describeEvent(head)}}</div>
            <div class="meta">t=${{group.start_t.toFixed(2)}}s ~ ${{group.end_t.toFixed(2)}}s</div>
          </div>`;
      }}).join('');
      document.querySelectorAll('.incident-item').forEach(el=>{{
        el.addEventListener('click',()=>{{
          pause();
          const t=Number(el.dataset.time||0);
          setIndexFromTime(t);
          const agvId=el.dataset.agvId||'';
          const {{chain,cycle}}=agvId?chainAtTime(t,agvId):{{chain:[],cycle:false}};
          setHighlight(el.dataset.edgeKey||'',agvId,{{chain,cycle}});
          render();
        }});
      }});
    }}

    function renderEvents() {{
      const now=currentTime();
      const visible=events.filter(filteredEventAgv).filter(e=>(e.t||0)<=now).slice(-20).reverse();
      document.getElementById('event-list').innerHTML=visible.map(event=>`
        <div class="event-item">
          <div><strong>${{describeEvent(event)}}</strong></div>
          <div class="meta">t=${{(event.t||0).toFixed(2)}}s</div>
        </div>`).join('');
    }}

    function render() {{
      document.getElementById('snapshot-count').textContent=snapshots.length.toLocaleString();
      document.getElementById('event-count').textContent=events.length.toLocaleString();
      document.getElementById('time-slider').max=Math.max(snapshots.length-1,0);
      document.getElementById('time-slider').value=index;
      document.getElementById('time-label').textContent=`${{currentTime().toFixed(2)}}s`;
      const toggle=document.getElementById('play-toggle');
      if (toggle) {{
        const playing=!!timer;
        toggle.textContent=playing?'일시정지':'재생';
        toggle.classList.toggle('is-playing',playing);
      }}
      renderMap();
      renderIncidents();
      renderEvents();
      renderAgvDetail();
      renderEventMarkers();
    }}

    let _eventMarkersBuiltKey='';
    function renderEventMarkers() {{
      const container=document.getElementById('event-markers');
      if (!container) return;
      const duration=(trace.meta&&trace.meta.duration_s)||(snapshots.length*(trace.meta.sample_interval_s||0.5));
      const incidents=getIncidents();
      const filtered=incidents.filter(filteredEventAgv);
      const groups=buildIncidentGroups(filtered);
      const key=focusedAgvId+'|'+groups.length+'|'+(groups[0]?groups[0].start_t:0)+'|'+(groups[groups.length-1]?groups[groups.length-1].start_t:0);
      if (key===_eventMarkersBuiltKey) return;
      _eventMarkersBuiltKey=key;
      if (!duration||groups.length===0) {{ container.innerHTML=''; return; }}
      container.innerHTML=groups.map(group=>{{
        const pct=Math.max(0,Math.min(100,(group.start_t/duration)*100));
        const title=`${{eventLabel(group.kind)}} · t=${{group.start_t.toFixed(2)}}s · ${{group.count}}회`;
        return `<div class="event-marker" data-kind="${{group.kind}}" data-time="${{group.start_t}}" data-edge-key="${{group.edge_key||''}}" data-agv-id="${{group.agv_id||''}}" style="left:${{pct}}%" title="${{title}}"></div>`;
      }}).join('');
      container.querySelectorAll('.event-marker').forEach(el=>{{
        el.addEventListener('click',(e)=>{{
          e.stopPropagation();
          pause();
          const t=Number(el.dataset.time||0);
          setIndexFromTime(t);
          const agvId=el.dataset.agvId||'';
          const {{chain,cycle}}=agvId?chainAtTime(t,agvId):{{chain:[],cycle:false}};
          setHighlight(el.dataset.edgeKey||'',agvId,{{chain,cycle}});
          render();
        }});
      }});
    }}

    function buildBlockingChainFor(snapshot, startAgvId) {{
      const m=new Map();
      for (const a of (snapshot.agvs||[])) {{
        if (a.blocking_agv) m.set(a.agv_id,a.blocking_agv);
      }}
      const chain=[], seen=new Set();
      let cur=startAgvId, cycle=false;
      while (cur) {{
        if (seen.has(cur)) {{ cycle=true; chain.push(cur); break; }}
        seen.add(cur); chain.push(cur);
        cur=m.get(cur)||'';
      }}
      return {{chain,cycle}};
    }}

    function renderAgvDetail() {{
      const panel=document.getElementById('agv-detail-panel');
      const stack=document.getElementById('side-stack');
      if (!focusedAgvId) {{ panel.hidden=true; stack.classList.remove('has-focus'); return; }}
      const snapshot=snapshots[index]||{{agvs:[]}};
      const agv=(snapshot.agvs||[]).find(a=>a.agv_id===focusedAgvId);
      if (!agv) {{ panel.hidden=true; stack.classList.remove('has-focus'); return; }}
      panel.hidden=false; stack.classList.add('has-focus');
      document.getElementById('agv-detail-title').textContent=agv.agv_id+' 상세';
      const dest=destinationNodeId(agv);
      const detourRow=agv.detour_via?`<div class="row"><span class="key">우회</span><span style="color:#c77700;">via ${{agv.detour_via}}</span></div>`:'';
      const orderRow=(agv.pickup_node||agv.dropoff_node)?(() => {{
        const isPickup=agv.phase==='pickup', isDropoff=agv.phase==='dropoff';
        const pb=`<span class="badge" ${{isPickup?'style="border-color:#0f9d58;color:#0f9d58;font-weight:600;"':''}}>${{agv.pickup_node||'—'}}</span>`;
        const db=`<span class="badge" ${{isDropoff?'style="border-color:#0f9d58;color:#0f9d58;font-weight:600;"':''}}>${{agv.dropoff_node||'—'}}</span>`;
        return `<div class="row"><span class="key">주문</span></div><div class="chain-row">픽업 ${{pb}}<span class="arrow">→</span>드롭 ${{db}}</div>`;
      }})():'<div class="meta">현재 주문 없음</div>';
      const reserved=agv.reserved_edge_keys||[], planned=agv.planned_edge_keys||[];
      const maxDepth=Math.max(reserved.length,planned.length,1);
      const reservedRows=reserved.slice(0,6).map((k,i)=>{{
        const pct=((reserved.length-i)/maxDepth)*100;
        return `<div class="depth-row"><span class="label">res ${{i+1}}</span><span class="bar" style="--bar: ${{pct.toFixed(1)}}%"></span></div>`;
      }}).join('');
      const plannedRows=planned.slice(0,6).map((k,i)=>{{
        const pct=((planned.length-i)/maxDepth)*100;
        return `<div class="depth-row"><span class="label">plan ${{i+1}}</span><span class="bar" style="--bar: ${{pct.toFixed(1)}}%; background: linear-gradient(90deg, #8bb4ff 0%, #8bb4ff ${{pct.toFixed(1)}}%, #e6ecf5 ${{pct.toFixed(1)}}%);"></span></div>`;
      }}).join('');
      const blocking=agv.blocking_agv?buildBlockingChainFor(snapshot,agv.agv_id):null;
      const chainHtml=blocking&&blocking.chain.length>1
        ?`<div class="chain-row ${{blocking.cycle?'cycle':''}}">${{blocking.chain.map(id=>`<span class="badge">${{id}}</span>`).join('<span class="arrow">→</span>')}}</div>`
        :'<div class="meta">현재 대기 체인 없음</div>';
      const stateMap={{NAVIGATING:['s-navigating','주행'],PROCESSING:['s-processing','작업'],
        WAITING_RESERVATION:['s-waiting','대기'],CHARGING:['s-charging','충전'],IDLE:['s-idle','IDLE']}};
      const [cls,label]=stateMap[agv.state]||['s-idle',agv.state];
      document.getElementById('agv-detail-body').innerHTML=`
        <div class="row"><span class="key">상태</span><span class="state-pill ${{cls}}">${{label}}</span></div>
        <div class="row"><span class="key">현재</span><span class="val-mono">${{agv.current_node||'—'}}</span></div>
        <div class="row"><span class="key">다음</span><span class="val-mono">${{agv.target_node||'—'}}</span></div>
        <div class="row"><span class="key">즉시 목적</span><span><span class="val-mono">${{dest||'—'}}</span>${{agv.phase?' <span class="meta">· '+(agv.phase==='pickup'?'픽업':'드롭')+' 단계</span>':''}}</span></div>
        ${{orderRow}}${{detourRow}}
        <div class="row"><span class="key">배터리</span><span class="val-mono">${{(agv.battery_pct||0).toFixed(1)}}%</span></div>
        <div class="row"><span class="key">예약 / 계획</span><span class="val-mono">${{reserved.length}} / ${{planned.length}} hop</span></div>
        ${{reservedRows?`<div class="depth-bars">${{reservedRows}}</div>`:''}}
        ${{plannedRows?`<div class="depth-bars">${{plannedRows}}</div>`:''}}
        <div class="row"><span class="key">대기 체인</span></div>
        ${{chainHtml}}`;
    }}

    function play() {{
      if (timer) return;
      if (liveStreaming && autoFollow) return; // 라이브 중엔 자동 추적이 play 역할
      timer=setInterval(()=>{{
        if (index>=snapshots.length-1) {{ pause(); return; }}
        index+=1; render();
      }}, Math.max(20,120/playbackSpeed));
    }}
    function pause() {{
      if (!timer) return;
      clearInterval(timer); timer=null;
    }}

    // ── 외부 맵 업로드 ───────────────────────────────────────────
    // F1a: imported map id → fleets[] 매핑 (슬라이더 빌드용)
    const importedFleetsById = {{}};
    async function refreshImportedMaps() {{
      try {{
        const resp = await fetch('/imported-maps');
        if (!resp.ok) return;
        const list = await resp.json();
        const sel = document.getElementById('live-topology');
        // 기존 imported 옵션 제거
        const opts = Array.from(sel.querySelectorAll('option'));
        for (const o of opts) {{
          if (o.value.startsWith('imported:')) o.remove();
        }}
        // 새로 추가
        if (list.length > 0) {{
          const sep = document.createElement('option');
          sep.disabled = true;
          sep.textContent = '── Imported Maps ──';
          sel.appendChild(sep);
        }}
        for (const m of list) {{
          const opt = document.createElement('option');
          opt.value = 'imported:' + m.id;
          opt.textContent = `📂 ${{m.name}} (${{m.stats.nodes}}n/${{m.stats.edges}}e)`;
          sel.appendChild(opt);
          importedFleetsById[m.id] = m.fleets || [];
        }}
      }} catch(e) {{ /* server 안 떠있을 수도 — 무시 */ }}
    }}

    document.getElementById('upload-map-btn').addEventListener('click', ()=>{{
      document.getElementById('upload-map-input').click();
    }});
    document.getElementById('upload-map-input').addEventListener('change', async (e)=>{{
      const files = Array.from(e.target.files || []);
      if (files.length === 0) return;
      // 파일 분류: 일반 JSON (nodes/links) vs *.edit.json
      let mapFile = null, editsFile = null;
      for (const f of files) {{
        if (f.name.endsWith('.edit.json')) editsFile = f;
        else mapFile = f;
      }}
      if (!mapFile) {{
        alert('원본 맵 JSON (nodes/links) 파일을 선택해 주세요. .edit.json 만으로는 업로드 불가.');
        return;
      }}
      try {{
        const mapText = await mapFile.text();
        const mapJson = JSON.parse(mapText);
        let editsJson = null;
        if (editsFile) {{
          editsJson = JSON.parse(await editsFile.text());
        }}
        const stem = mapFile.name.replace(/\\.json$/, '');
        const resp = await fetch('/upload-map', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{ name: stem, map_json: mapJson, edits_json: editsJson }}),
        }});
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        await refreshImportedMaps();
        // 자동으로 새 옵션 선택
        const sel = document.getElementById('live-topology');
        sel.value = 'imported:' + data.importedMapId;
        const editPart = editsJson ? ' (+edits)' : '';
        alert(`업로드 성공: ${{data.name}}${{editPart}}\\n  ${{data.stats.nodes}} nodes / ${{data.stats.edges}} edges\\n  chargers ${{data.stats.chargers}} · stations ${{data.stats.stations}}`);
      }} catch (err) {{
        alert('업로드 실패: ' + err.message);
      }}
      e.target.value = ''; // input 리셋
    }});
    // 토폴로지 드롭다운 change → "🛠 Editor" 버튼 활성 토글 + F1a fleet 슬라이더 표시
    function updateEditorBtn() {{
      const sel = document.getElementById('live-topology').value;
      const btn = document.getElementById('edit-map-btn');
      btn.disabled = !sel.startsWith('imported:');
      // F1a: imported map fleet 정의로 슬라이더 갱신 (기본 단일-fleet 케이스는 슬라이더 숨김)
      if (sel.startsWith('imported:')) {{
        const id = sel.slice('imported:'.length);
        const fleets = importedFleetsById[id] || [];
        buildFleetSliders(fleets);
      }} else {{
        buildFleetSliders([]);
      }}
    }}
    document.getElementById('live-topology').addEventListener('change', updateEditorBtn);
    document.getElementById('edit-map-btn').addEventListener('click', ()=>{{
      const sel = document.getElementById('live-topology').value;
      if (!sel.startsWith('imported:')) return;
      const id = sel.slice('imported:'.length);
      window.open(`/edit/${{id}}`, '_blank');
    }});

    // 페이지 로드 시 한 번 채움 (이미 업로드된 맵이 있을 수도)
    refreshImportedMaps().then(updateEditorBtn);

    // ── 이벤트 핸들러 ─────────────────────────────────────────────
    document.getElementById('run-btn').addEventListener('click', doRun);
    document.getElementById('stop-btn').addEventListener('click', doStop);
    document.getElementById('reset-btn').addEventListener('click', doReset);
    document.getElementById('auto-follow').addEventListener('change', (e)=>{{
      autoFollow=e.target.checked;
      if (autoFollow && liveStreaming) {{ index=snapshots.length-1; render(); }}
    }});
    document.getElementById('play-toggle').addEventListener('click',()=>{{
      if (liveStreaming && autoFollow) return;
      if (timer) pause(); else play();
      render();
    }});
    document.getElementById('time-slider').addEventListener('input',(e)=>{{
      autoFollow=false;
      document.getElementById('auto-follow').checked=false;
      index=Number(e.target.value);
      render();
    }});
    document.querySelectorAll('.speed-btn').forEach(btn=>{{
      btn.addEventListener('click',()=>{{
        playbackSpeed=Number(btn.dataset.speed||'1');
        document.querySelectorAll('.speed-btn').forEach(n=>n.classList.remove('active'));
        btn.classList.add('active');
        if (timer) {{ pause(); play(); }}
      }});
    }});
    document.getElementById('agv-focus').addEventListener('change',(e)=>{{
      focusedAgvId=e.target.value||''; render();
    }});
    document.getElementById('heatmap-toggle').addEventListener('click',()=>{{
      heatmapMode=!heatmapMode;
      // mutually exclusive: heatmap 켜면 traffic 자동 끔 (혼동 방지)
      if (heatmapMode && trafficMode) {{
        trafficMode = false;
        document.getElementById('traffic-toggle').classList.remove('active');
        document.getElementById('traffic-legend').classList.remove('active');
      }}
      document.getElementById('heatmap-toggle').classList.toggle('active',heatmapMode);
      const legend=document.getElementById('heatmap-legend');
      legend.classList.toggle('active',heatmapMode);
      const lab=document.getElementById('heatmap-max-label');
      if (lab) lab.textContent=heatmapMax>0?`최대 ${{heatmapMax}}회`:'데이터 없음';
      render();
    }});
    document.getElementById('traffic-toggle').addEventListener('click',()=>{{
      trafficMode=!trafficMode;
      // mutually exclusive: traffic 켜면 heatmap 자동 끔
      if (trafficMode && heatmapMode) {{
        heatmapMode = false;
        document.getElementById('heatmap-toggle').classList.remove('active');
        document.getElementById('heatmap-legend').classList.remove('active');
      }}
      document.getElementById('traffic-toggle').classList.toggle('active',trafficMode);
      const legend=document.getElementById('traffic-legend');
      legend.classList.toggle('active',trafficMode);
      const lab=document.getElementById('traffic-max-label');
      if (lab) lab.textContent=trafficMax>0?`최대 ${{trafficMax}}회`:'데이터 없음';
      render();
    }});
    document.getElementById('collision-toggle').addEventListener('click',()=>{{
      collisionMode=!collisionMode;
      document.getElementById('collision-toggle').classList.toggle('active',collisionMode);
      render();
    }});
    // GAP-A: ⛔ 차단 모드 토글
    document.getElementById('block-toggle').addEventListener('click',()=>{{
      blockMode=!blockMode;
      document.getElementById('block-toggle').classList.toggle('active',blockMode);
      const hint=document.getElementById('block-mode-hint');
      if (hint) hint.style.display = blockMode ? 'inline' : 'none';
      document.body.classList.toggle('block-mode', blockMode);
      render();
    }});
    // GAP-A: 엣지 클릭 → /block-edge POST (차단 모드 ON 일 때만 동작)
    async function toggleEdgeBlock(edgeKey) {{
      if (!runId || !edgeKey) return;
      const willBlock = !userBlockedEdges.has(edgeKey);
      try {{
        const resp = await fetch('/block-edge', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ runId, edge_id: edgeKey, blocked: willBlock }}),
        }});
        if (!resp.ok) {{
          console.error('block-edge failed', resp.status);
          return;
        }}
        const data = await resp.json();
        userBlockedEdges = new Set(data.currently_blocked || []);
        render();
      }} catch (e) {{
        console.error('block-edge error', e);
      }}
    }}
    // ── 실시간 충돌 의심 감지 (같은 노드/엣지 점유) ──
    function detectCollisions(snapshot) {{
      const out = [];
      const byNode = new Map(), byEdge = new Map();
      for (const a of (snapshot.agvs||[])) {{
        if (isAnchoredOnNode(a) && a.current_node) {{
          if (!byNode.has(a.current_node)) byNode.set(a.current_node, []);
          byNode.get(a.current_node).push(a);
        }} else if (a.current_edge_key) {{
          if (!byEdge.has(a.current_edge_key)) byEdge.set(a.current_edge_key, []);
          byEdge.get(a.current_edge_key).push(a);
        }}
      }}
      for (const [nid, list] of byNode.entries()) {{
        if (list.length < 2) continue;
        const allChargingOrIdle = list.every(a => a.state === 'CHARGING' || a.state === 'IDLE');
        const node = nodeIndex.get(nid);
        const isCharger = node && (node.is_charger || node.role === 'charger');
        if (allChargingOrIdle && isCharger) continue;
        if (!node) continue;
        out.push({{type:'node', x:sx(node.x), y:sy(node.y), agv_ids:list.map(a=>a.agv_id)}});
      }}
      for (const [ek, list] of byEdge.entries()) {{
        if (list.length < 2) continue;
        for (let i = 0; i < list.length; i++) {{
          for (let j = i+1; j < list.length; j++) {{
            const a = list[i], b = list[j];
            if (Math.hypot(a.x - b.x, a.y - b.y) < 1.0) {{
              out.push({{type:'edge', x:sx((a.x+b.x)/2), y:sy((a.y+b.y)/2),
                         agv_ids:[a.agv_id, b.agv_id]}});
            }}
          }}
        }}
      }}
      return out;
    }}
    document.getElementById('zoom-reset-btn').addEventListener('click',()=>{{ resetZoom(); render(); }});
    document.getElementById('map').addEventListener('wheel',(e)=>{{
      e.preventDefault();
      const svg=e.currentTarget;
      const before=svgPointFromClient(svg,e.clientX,e.clientY);
      const nextScale=Math.min(6,Math.max(0.6,zoomScale*(e.deltaY<0?1.12:0.9)));
      const ratio=nextScale/zoomScale;
      zoomScale=nextScale;
      zoomPanX=before.x-(before.x-zoomPanX)*ratio;
      zoomPanY=before.y-(before.y-zoomPanY)*ratio;
      render();
    }},{{passive:false}});
    document.getElementById('map').addEventListener('pointerdown',(e)=>{{
      isDragging=true;
      dragStart={{x:e.clientX,y:e.clientY,panX:zoomPanX,panY:zoomPanY}};
    }});
    document.getElementById('map').addEventListener('pointermove',(e)=>{{
      if (!isDragging||!dragStart) return;
      const delta=svgDeltaFromClient(e.currentTarget,e.clientX-dragStart.x,e.clientY-dragStart.y);
      zoomPanX=dragStart.panX+delta.x; zoomPanY=dragStart.panY+delta.y;
      render();
    }});
    document.getElementById('map').addEventListener('pointerup',()=>{{ isDragging=false; dragStart=null; }});
    document.getElementById('map').addEventListener('pointerleave',()=>{{ isDragging=false; dragStart=null; }});
    document.getElementById('map').addEventListener('click',(e)=>{{
      // GAP-A: 차단 모드 ON → AGV 포커스 대신 엣지 클릭으로 라우팅
      if (blockMode) {{
        const edgeHit=e.target.closest('[data-edge-key]');
        if (edgeHit) {{
          e.stopPropagation();
          toggleEdgeBlock(edgeHit.dataset.edgeKey);
          return;
        }}
        return; // 토글 ON 일 때 엣지 외 클릭은 무시 (AGV 포커스 비활성)
      }}
      // GAP-B: 수동 Job 모드 ON → 노드 클릭으로 pickup/dropoff 선택
      if (manualJobMode) {{
        const nodeHit=e.target.closest('[data-node-id]');
        if (nodeHit) {{
          e.stopPropagation();
          handleManualJobNodeClick(nodeHit.dataset.nodeId);
        }}
        return; // 노드 외 클릭은 무시 (AGV 포커스 비활성)
      }}
      const hit=e.target.closest('[data-agv-id]');
      if (!hit) return;
      const agvId=hit.dataset.agvId;
      const next=focusedAgvId===agvId?'':agvId;
      focusedAgvId=next;
      document.getElementById('agv-focus').value=next;
      render();
    }});

    // ── GAP-B: 수동 Job UI ───────────────────────────────────────
    let manualJobMode = false;
    window.__mjPickup = null;  // renderMap 가 참조 (전역 노출)
    // 방금 할당된 AGV 추적 — toast 가 사라진 후에도 식별 가능. render() 가 참조.
    window.__mjJustAssigned = null;  // {{ agv_id, demand_id, pickup, dropoff, until_ms }}
    let mjJustAssignedTimer = null;
    const mjToast = document.getElementById('mj-toast');
    let mjToastTimer = null;
    function showToast(msg, kind) {{
      mjToast.textContent = msg;
      mjToast.className = 'mj-toast show' + (kind ? ' ' + kind : '');
      if (mjToastTimer) clearTimeout(mjToastTimer);
      // dispatch 성공 토스트는 좀 더 오래 보여줌
      const dur = (kind === 'err' || kind === 'warn') ? 2800 : 4500;
      mjToastTimer = setTimeout(()=>{{ mjToast.className='mj-toast'; }}, dur);
    }}
    function updateManualJobPanel(snapshot) {{
      const panel = document.getElementById('manual-job-panel');
      const label = document.getElementById('mj-state-label');
      const badge = document.getElementById('mj-pickup-badge');
      const idleCnt = document.getElementById('mj-idle-count');
      if (!manualJobMode) {{
        panel.classList.remove('active');
        return;
      }}
      panel.classList.add('active');
      if (window.__mjPickup) {{
        label.textContent = 'dropoff 노드 선택 →';
        badge.style.display = 'inline-block';
        badge.textContent = window.__mjPickup;
      }} else {{
        label.textContent = 'pickup 노드 선택';
        badge.style.display = 'none';
      }}
      // 사용 가능한 IDLE AGV 수 — 후보가 0개면 미리 알림
      const snap = snapshot || (snapshots.length ? snapshots[snapshots.length-1] : null);
      if (snap && idleCnt) {{
        const idle = (snap.agvs||[]).filter(a => a.state === 'IDLE').length;
        idleCnt.textContent = `· IDLE AGV ${{idle}}대`;
        idleCnt.style.color = idle === 0 ? '#b91c1c' : 'var(--muted)';
      }} else if (idleCnt) {{
        idleCnt.textContent = '';
      }}
    }}
    function updateLastAssignedPanel() {{
      const el = document.getElementById('mj-last-assigned');
      const a = window.__mjJustAssigned;
      const agvEl = document.getElementById('mj-last-assigned-agv');
      const rtEl = document.getElementById('mj-last-assigned-route');
      if (!a) {{
        el.classList.remove('active');
        return;
      }}
      el.classList.add('active');
      agvEl.textContent = a.agv_id;
      rtEl.textContent = a.pickup && a.dropoff ? ` · ${{a.pickup}} → ${{a.dropoff}}` : '';
    }}
    function resetManualPickup() {{
      window.__mjPickup = null;
      updateManualJobPanel();
      render();
    }}
    function setManualJobMode(on) {{
      manualJobMode = !!on;
      window.__mjPickup = null;
      document.body.classList.toggle('mj-mode', manualJobMode);
      const btn = document.getElementById('manual-job-toggle');
      btn.classList.toggle('active', manualJobMode);
      const lastSnap = snapshots.length ? snapshots[snapshots.length-1] : null;
      updateManualJobPanel(lastSnap);
      if (manualJobMode && lastSnap) {{
        const idle = (lastSnap.agvs||[]).filter(a => a.state === 'IDLE').length;
        if (idle === 0) {{
          showToast('현재 IDLE AGV 없음 — 시뮬 진행 후 다시 시도', 'warn');
        }}
      }}
      render();
    }}
    async function handleManualJobNodeClick(nodeId) {{
      if (!runId) {{
        showToast('시뮬레이션 미실행 — 먼저 ▶ 실행', 'err');
        return;
      }}
      if (!nodeId) return;
      // 같은 노드 또 클릭 → pickup 취소
      if (window.__mjPickup === nodeId) {{
        resetManualPickup();
        showToast('pickup 선택 취소', 'warn');
        return;
      }}
      if (!window.__mjPickup) {{
        // 첫 클릭 → pickup 후보
        window.__mjPickup = nodeId;
        updateManualJobPanel();
        render();
        return;
      }}
      // 두 번째 클릭 → dropoff 확정 → POST
      const pickup = window.__mjPickup;
      const dropoff = nodeId;
      try {{
        const resp = await fetch('/manual-job', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ pickup_node: pickup, dropoff_node: dropoff, runId }}),
        }});
        const data = await resp.json().catch(()=>({{}}));
        if (!resp.ok) {{
          showToast('발행 실패: ' + (data.detail || resp.status), 'err');
        }} else if (data.status === 'dispatched') {{
          showToast(`📋 ${{data.agv_id}} 에게 ${{pickup}} → ${{dropoff}} 할당 (demand ${{data.demand_id}})`);
          // 자동 포커스 — 우측 상세 패널에 그 AGV 표시. 사용자가 명시적으로 다른
          // AGV 클릭하기 전까지 유지됨.
          focusedAgvId = data.agv_id;
          // agv-focus dropdown 동기화
          const focusSel = document.getElementById('agv-focus');
          if (focusSel) focusSel.value = data.agv_id;
          // 노란 ring 강조 8초 — render 함수가 window.__mjJustAssigned 참조해 그림
          window.__mjJustAssigned = {{
            agv_id: data.agv_id,
            demand_id: data.demand_id,
            pickup, dropoff,
            until_ms: Date.now() + 8000,
          }};
          updateLastAssignedPanel();
          if (mjJustAssignedTimer) clearTimeout(mjJustAssignedTimer);
          mjJustAssignedTimer = setTimeout(() => {{
            window.__mjJustAssigned = null;
            updateLastAssignedPanel();
            render();
          }}, 8000);
          render();
        }} else if (data.status === 'pending') {{
          showToast(`📋 ${{data.demand_id}} pending — ${{data.reason || ''}}`, 'warn');
        }} else {{
          // dispatched=false: 흔히 IDLE AGV 없거나 capability 미일치
          const why = data.reason || data.status || 'unknown';
          const hint = (why.includes('no_idle_agv') || why.includes('agv_not_idle'))
            ? ' (배정 가능한 IDLE AGV 없음 — 시뮬 잠시 진행 후 재시도)' : '';
          showToast('거부: ' + why + hint, 'err');
        }}
      }} catch (err) {{
        showToast('네트워크 오류: ' + err.message, 'err');
      }}
      resetManualPickup();
    }}
    // 최근 할당 패널 클릭 — 포커스 토글
    document.getElementById('mj-last-assigned').addEventListener('click', () => {{
      const a = window.__mjJustAssigned;
      if (!a) return;
      focusedAgvId = (focusedAgvId === a.agv_id) ? '' : a.agv_id;
      const focusSel = document.getElementById('agv-focus');
      if (focusSel) focusSel.value = focusedAgvId;
      render();
    }});
    document.getElementById('manual-job-toggle').addEventListener('click', ()=>{{
      setManualJobMode(!manualJobMode);
    }});
    document.addEventListener('keydown', (e)=>{{
      if (e.key === 'Escape' && manualJobMode) {{
        if (window.__mjPickup) {{
          resetManualPickup();
          showToast('pickup 선택 취소', 'warn');
        }} else {{
          setManualJobMode(false);
          showToast('수동 Job 모드 종료', 'warn');
        }}
      }}
    }});
    document.getElementById('map').addEventListener('dblclick',(e)=>{{
      if (e.target.closest('[data-agv-id]')) return;
      resetZoom(); render();
    }});

    // 초기 렌더 (빈 맵)
    render();
  </script>
</body>
</html>"""


def _fmt_dur(s: int) -> str:
    """초 → 사람이 읽기 쉬운 문자열. (파라미터 폼 초기 표시용)"""
    if s >= 3600:
        return f"{s/3600:.1f}h"
    return f"{s//60}min"
