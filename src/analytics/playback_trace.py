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
            <input id="time-slider" type="range" min="0" max="0" step="1" value="0" />
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
          </div>
        </div>
        <div class="hint">맵: 휠 확대/축소, 드래그 이동, 더블클릭 줌 초기화. AGV 클릭 시 포커스. 우측 사고 묶음 클릭 시 해당 시점으로 점프합니다.</div>
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
      for (const agv of visibleAgvs) {
        const planned = agv.planned_edge_keys || [];
        // Planned: depth-based opacity ramp so the immediate next hops stand out
        // and farther-out plans fade. Cap depth at 8 to avoid invisible tails.
        planned.forEach((edgeKey, depth) => {
          const t = Math.min(depth, 8) / 8;
          const opacity = 0.95 - t * 0.65; // 0.95 → 0.30
          applyEdgeStyle(edgeKey, { stroke: '#8bb4ff', width: 3, opacity, dash: '6 5' });
        });
        const reserved = agv.reserved_edge_keys || [];
        reserved.forEach((edgeKey, depth) => {
          const t = Math.min(depth, 6) / 6;
          const opacity = 1.0 - t * 0.45; // 1.0 → 0.55
          const width = 4 - t * 1.2;        // 4 → 2.8
          applyEdgeStyle(edgeKey, { stroke: '#2459d1', width, opacity, dash: '' });
        });
        if (agv.current_edge_key) {
          applyEdgeStyle(agv.current_edge_key, { stroke: '#0f9d58', width: 6, opacity: 1, dash: '' });
        }
        if (agv.blocked_edge_key && agv.state === 'WAITING_RESERVATION') {
          applyEdgeStyle(agv.blocked_edge_key, { stroke: '#c0392b', width: 6, opacity: 1, dash: '' });
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
            const inner = item.anchored
              ? agvShapeMarkup(0, 0, '#3a4555', true, true)
              : agvArrowMarkup(0, 0, '#3a4555', agv.heading, true);
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
            const inner = item.anchored
              ? agvShapeMarkup(0, 0, '#3a4555', true, false)
              : agvArrowMarkup(0, 0, '#3a4555', agv.heading, false);
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
