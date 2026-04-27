from __future__ import annotations

import json
from pathlib import Path


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
            pending_src = getattr(agv, "_pending_edge_src", None)
            pending_dst = getattr(agv, "_pending_edge_dst", None)
            blocking_agv_id = snapshot["waiting_for"].get(agv.agv_id, "")
            if pending_src and pending_dst:
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
                "current_edge_key": current_edge_key,
                "planned_edge_keys": planned_edge_keys,
                "reserved_edge_keys": active_edge_by_agv.get(agv.agv_id, []),
                "blocked_edge_key": blocked_edge_key,
                "blocking_agv": snapshot["waiting_for"].get(agv.agv_id, ""),
            })
        self.snapshots.append(snapshot)

    def build_trace(self, duration_s: float) -> dict:
        return {
            "meta": {
                "duration_s": round(duration_s, 3),
                "sample_interval_s": self.sample_interval_s,
            },
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
      --bg: #f3f5f8;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #5a6877;
      --border: #d8dee7;
      --edge: #c6d0db;
      --edge-plan: #8bb4ff;
      --edge-reserved: #2459d1;
      --edge-active: #0f9d58;
      --edge-blocked: #c0392b;
      --node-work: #0f9d58;
      --node-charger: #1f6feb;
      --accent: #1f6feb;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .page {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px;
      display: grid;
      gap: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .top { display: grid; gap: 12px; }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 10;
      padding: 12px;
      background: rgba(255,255,255,0.96);
      border: 1px solid var(--border);
      border-radius: 8px;
      backdrop-filter: blur(8px);
    }
    .playback-stage {
      display: grid;
      gap: 10px;
    }
    .map-stage {
      display: grid;
      gap: 10px;
    }
    button {
      height: 32px;
      padding: 0 12px;
      border: 1px solid var(--border);
      background: #fff;
      border-radius: 6px;
      cursor: pointer;
    }
    select {
      height: 32px;
      padding: 0 10px;
      border: 1px solid var(--border);
      background: #fff;
      border-radius: 6px;
    }
    .speed-group {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      padding-left: 8px;
      border-left: 1px solid var(--border);
    }
    .focus-group {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding-left: 8px;
      border-left: 1px solid var(--border);
    }
    .speed-btn.active {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }
    input[type=range] { width: min(620px, 100%); }
    .hint {
      color: var(--muted);
      font-size: 12px;
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
      font-size: 13px;
      overflow: auto;
      min-height: 0;
    }
    .agv-detail-body .row {
      display: flex;
      justify-content: space-between;
      gap: 8px;
    }
    .agv-detail-body .row .key { color: var(--muted); }
    .depth-bars { display: grid; gap: 3px; }
    .depth-row {
      display: grid;
      grid-template-columns: 64px 1fr;
      gap: 8px;
      align-items: center;
      font-size: 12px;
    }
    .depth-row .bar {
      height: 8px;
      background: linear-gradient(90deg, #2459d1 0%, #2459d1 var(--bar), #e6ecf5 var(--bar));
      border-radius: 4px;
    }
    .depth-row .label { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .chain-row {
      display: flex;
      gap: 6px;
      align-items: center;
      font-size: 12px;
      flex-wrap: wrap;
    }
    .chain-row .badge {
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 6px;
      background: #fff;
    }
    .chain-row .arrow { color: var(--muted); }
    .chain-row.cycle .badge { border-color: #c0392b; color: #c0392b; }
    .side-stack .panel {
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
    }
    .side-stack .event-list,
    .side-stack .incident-list {
      max-height: none;
      flex: 1 1 auto;
      min-height: 0;
    }
    .map-shell {
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: #fcfdff;
    }
    svg {
      width: 100%;
      height: 760px;
      background: #fcfdff;
      display: block;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      font-size: 12px;
      color: var(--muted);
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
    }
    .swatch-ring {
      border-radius: 50%;
      background: #fff;
      border: 2px solid #8b98a8;
    }
    .legend-divider {
      width: 1px;
      align-self: stretch;
      background: var(--border);
      margin: 0 4px;
    }
    .event-list, .incident-list {
      display: grid;
      gap: 8px;
      max-height: 280px;
      overflow: auto;
    }
    .event-item, .incident-item {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      background: #fbfcfe;
      font-size: 13px;
      line-height: 1.45;
    }
    .incident-item {
      cursor: pointer;
    }
    .incident-item:hover {
      border-color: var(--accent);
      background: #f4f8ff;
    }
    .meta { color: var(--muted); font-size: 12px; }
    .kpi {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .kpi .panel { padding: 12px; }
    .agv-label { font-size: 10px; fill: #18202a; font-weight: 600; }
    .section-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
    }
    .map-topline {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
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
      <div>
        <div class="meta">Playback Trace</div>
        <h1 style="margin: 0; font-size: 28px;">시뮬레이션 재생</h1>
      </div>
      <div class="playback-stage">
        <div class="toolbar">
          <button id="play-btn">재생</button>
          <button id="pause-btn">일시정지</button>
          <input id="time-slider" type="range" min="0" max="0" step="1" value="0" />
          <strong id="time-label">t=0.0s</strong>
          <div class="speed-group">
            <span class="meta">배속</span>
            <button class="speed-btn active" data-speed="1">1.0x</button>
            <button class="speed-btn" data-speed="2">2.0x</button>
            <button class="speed-btn" data-speed="5">5.0x</button>
          </div>
          <div class="focus-group">
            <span class="meta">AGV 포커스</span>
            <select id="agv-focus">
              <option value="">전체</option>
            </select>
            <button id="zoom-reset-btn" type="button">줌 초기화</button>
          </div>
        </div>
        <div class="hint">지도 위에서 마우스 휠로 확대/축소하고 드래그로 이동. 우측 사고 묶음 항목을 클릭하면 해당 시점으로 점프합니다.</div>
      </div>
    </section>

    <section class="kpi">
      <div class="panel"><div class="meta">총 스냅샷</div><strong id="snapshot-count"></strong></div>
      <div class="panel"><div class="meta">총 이벤트</div><strong id="event-count"></strong></div>
      <div class="panel"><div class="meta">샘플 간격</div><strong id="sample-interval"></strong></div>
      <div class="panel"><div class="meta">사고/병목 포인트</div><strong id="incident-count"></strong></div>
    </section>

    <section class="main-layout">
      <section class="panel map-panel">
        <div class="map-topline">
          <div class="legend">
            <span class="legend-item"><span class="swatch" style="background:#c6d0db"></span>기본 통로</span>
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
          <svg id="map" viewBox="0 0 1000 700"></svg>
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
    function destinationNodeId(agv) {
      if (agv.target_node) return agv.target_node;
      const keys = agv.planned_edge_keys || [];
      if (!keys.length) return '';
      const parts = (keys[keys.length - 1] || '').split('__');
      return parts[1] || '';
    }
    function destinationLabel(agv) {
      // Used for labels during transit so "this AGV is heading where" is visible.
      const keys = agv.planned_edge_keys || [];
      const finalNode = keys.length
        ? ((keys[keys.length - 1] || '').split('__')[1] || '')
        : (agv.target_node || '');
      if (!finalNode) return '';
      if (finalNode.startsWith('ST_')) return '→' + finalNode;
      if (finalNode.startsWith('HP_')) return '→휴식 ' + finalNode;
      if (finalNode.startsWith('CH_')) return '→충전 ' + finalNode;
      if (finalNode.startsWith('SD_')) return '→사이딩';
      // generic — shows pass-through target (often a bay/main waypoint)
      return '→' + finalNode;
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
        x: ((clientX - rect.left) / rect.width) * viewBox.width,
        y: ((clientY - rect.top) / rect.height) * viewBox.height,
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
        return `<circle cx="${cx}" cy="${cy}" r="${faded ? 6 : 8}" fill="${color}" opacity="${opacity}" />`;
      }
      return '';
    }
    function agvArrowMarkup(cx, cy, color, heading, faded) {
      const opacity = faded ? 0.18 : 1.0;
      const scale = faded ? 0.9 : 1.15;
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
            return `
              <g transform="translate(${cx} ${cy}) scale(${labelScale})">${inner}</g>
              <text class="agv-label" transform="translate(${labelX} ${labelY}) scale(${labelScale})">${labelText}</text>
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
      document.getElementById('snapshot-count').textContent = String(snapshots.length);
      document.getElementById('event-count').textContent = String(events.length);
      document.getElementById('sample-interval').textContent = `${trace.meta.sample_interval_s.toFixed(2)}s`;
      document.getElementById('time-slider').max = Math.max(snapshots.length - 1, 0);
      document.getElementById('time-slider').value = index;
      document.getElementById('time-label').textContent = `t=${currentTime().toFixed(2)}s`;
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
      document.getElementById('agv-detail-body').innerHTML = `
        <div class="row"><span class="key">상태</span><strong>${agv.state}</strong></div>
        <div class="row"><span class="key">현재</span><span>${agv.current_node || '—'}</span></div>
        <div class="row"><span class="key">목적</span><span>${dest || '—'}</span></div>
        <div class="row"><span class="key">배터리</span><span>${(agv.battery_pct || 0).toFixed(1)}%</span></div>
        <div class="row"><span class="key">예약 깊이</span><span>${reservedDepth} hop</span></div>
        <div class="row"><span class="key">계획 깊이</span><span>${plannedDepth} hop</span></div>
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
    document.getElementById('play-btn').addEventListener('click', play);
    document.getElementById('pause-btn').addEventListener('click', pause);
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
    populateAgvFocusOptions();
    render();
  </script>
</body>
</html>
"""
    return html.replace("__TRACE_JSON__", trace_json)
