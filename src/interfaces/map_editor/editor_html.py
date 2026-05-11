"""
editor_html.py — Map Editor HTML 페이지 생성기.

임포트된 ImportedMap 을 self-contained HTML 페이지로 export. 사용자가 브라우저에서:
  · 방향성 편집 (Paint 모드 + 키 조합)
  · 노드 역할 마킹 (Stamp 도구 + 숫자키 + 우클릭)
  · 다중 선택 (Shift+박스)
  · 저장 (다운로드로 *.edit.json)

Phase 1 (현재): UI 골격 + 맵 시각화 + 도구 패널 표시 + 도구 선택 인터랙션 (인덱스만 변경)
Phase 2 (다음): Paint 인터랙션 — drag trajectory → 엣지 매칭 → 방향 변경
Phase 3 (다음): Stamp 적용 — 클릭 시 노드 역할 변경
Phase 4: 저장 / Undo
"""
from __future__ import annotations

import json

from src.domain.map.external_importer import ImportedMap


def build_editor_html(imported: ImportedMap, title: str = "Map Editor") -> str:
    """ImportedMap → self-contained HTML 페이지 문자열.

    페이지는 외부 의존성 없음 (모든 JS 인라인). 폐쇄망에서도 그대로 동작.
    """
    # ── 데이터 직렬화 (JS 가 그대로 사용) ──────────────────────────
    nodes_payload = [{
        "id": n.node_id,
        "x": n.x,
        "y": n.y,
        "name": n.name,
        "role": n.inferred_role,
        "is_charger": n.inferred_is_charger,
        "is_holding": n.inferred_is_holding,
        "degree_in": n.degree_in,
        "degree_out": n.degree_out,
    } for n in imported.nodes]

    edges_payload = [{
        "id": e.edge_id,
        "src": e.src,
        "dst": e.dst,
        "bidir": e.inferred_bidirectional,
        "corridor": e.inferred_corridor,
        "access": e.inferred_access_type,
    } for e in imported.edges]

    report = imported.report
    report_payload = {
        "node_count": report.node_count,
        "edge_count_raw": report.edge_count_raw,
        "edge_count_after_merge": report.edge_count_after_merge,
        "bidirectional_count": report.bidirectional_count,
        "inferred_chargers": report.inferred_chargers,
        "inferred_stations": report.inferred_stations,
        "inferred_holding": report.inferred_holding,
        "connected_components": report.connected_components,
        "isolated_nodes": report.isolated_nodes[:20],
        "dead_end_nodes": report.dead_end_nodes[:20],
        "corridor_stats": report.corridor_stats,
        "warnings": [{"severity": w.severity, "code": w.code, "message": w.message}
                     for w in report.warnings],
    }

    payload = {
        "title": title,
        "nodes": nodes_payload,
        "edges": edges_payload,
        "report": report_payload,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    return _TEMPLATE.replace("__PAYLOAD_JSON__", payload_json)


# ────────────────────────────────────────────────────────────────────
# HTML 템플릿 — playback.html 의 light theme 동일 룩
# ────────────────────────────────────────────────────────────────────
_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Map Editor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f9; --surface: #ffffff; --surface-2: #fafbfd; --surface-3: #f1f4f8;
      --ink: #0f172a; --muted: #64748b; --muted-2: #94a3b8;
      --border: #e3e8ef; --border-strong: #cbd3df;
      --edge: #c6d0db; --edge-bidir: #2459d1; --edge-unidir: #0f9d58;
      --accent: #2563eb; --accent-soft: #e6efff;
      --success: #0f9d58; --warn: #c77700; --danger: #c0392b;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Pretendard", "Apple SD Gothic Neo", sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
      --shadow-sm: 0 1px 2px rgba(15,23,42,0.05);
      --radius-sm: 6px; --radius: 10px;
      /* 노드 역할 색 (도장 도구와 일치) */
      --role-standard: #8b98a8;
      --role-station: #0f9d58;
      --role-charger: #1f6feb;
      --role-holding: #94a3b8;
      --role-siding: #e0a000;
    }
    * { box-sizing: border-box; }
    body { margin:0; font-family:var(--font-sans); font-size:13px;
      background:var(--bg); color:var(--ink); height:100vh; overflow:hidden;
      -webkit-font-smoothing:antialiased; }
    h1, h2, h3 { margin:0; letter-spacing:-0.015em; }
    button { font-family:inherit; cursor:pointer; }
    button:disabled { cursor:not-allowed; opacity:0.4; }

    .layout {
      display: grid;
      grid-template-columns: 1fr 320px;
      height: 100vh;
    }
    .map-area { display:flex; flex-direction:column; min-width:0; }
    .map-toolbar {
      display:flex; align-items:center; gap:10px;
      padding:10px 14px;
      background:var(--surface);
      border-bottom:1px solid var(--border);
    }
    .map-toolbar h1 { font-size:14px; font-weight:700; }
    .map-toolbar .stat-chip {
      display:inline-flex; gap:4px; align-items:baseline;
      padding:3px 9px; border-radius:999px;
      background:var(--surface-3); font-size:11px; color:var(--muted);
    }
    .map-toolbar .stat-chip strong {
      color:var(--ink); font-family:var(--font-mono); font-weight:600;
    }
    .map-toolbar .spacer { flex:1; }
    .map-toolbar button {
      height:28px; padding:0 12px; border:1px solid var(--border);
      background:var(--surface); border-radius:var(--radius-sm);
      color:var(--ink); font-size:12px; font-weight:500;
    }
    .map-toolbar button:hover { background:var(--surface-3); border-color:var(--border-strong); }
    .map-toolbar button.primary {
      background:var(--ink); color:#fff; border-color:var(--ink);
    }
    .map-toolbar button.primary:hover { background:#1a253b; }

    .map-shell {
      flex:1; min-height:0;
      background:linear-gradient(180deg, #fcfdff 0%, #f7f9fc 100%);
      position:relative; overflow:hidden;
    }
    svg#map { width:100%; height:100%; display:block; }

    .side {
      border-left:1px solid var(--border);
      background:var(--surface);
      display:flex; flex-direction:column;
      overflow:auto;
    }
    .panel { padding:14px 16px; border-bottom:1px solid var(--border); }
    .panel h2 {
      font-size:11px; font-weight:700; letter-spacing:0.05em; text-transform:uppercase;
      color:var(--muted); margin-bottom:10px;
    }

    /* 모드 토글 */
    .mode-toggle { display:flex; gap:4px; padding:3px; background:var(--surface-3); border-radius:8px; }
    .mode-toggle button {
      flex:1; height:30px; border:0; background:transparent; color:var(--muted);
      font-size:12px; font-weight:600; border-radius:5px;
    }
    .mode-toggle button.active { background:var(--surface); color:var(--ink); box-shadow:var(--shadow-sm); }

    .hint-key {
      display:inline-block; padding:1px 6px; font-family:var(--font-mono); font-size:10px;
      background:var(--surface-3); border-radius:3px; color:var(--muted-2);
      vertical-align:middle; margin-left:6px;
    }

    /* Stamp 도구 그리드 */
    .stamp-grid { display:grid; grid-template-columns: repeat(3, 1fr); gap:6px; }
    .stamp-btn {
      display:flex; flex-direction:column; align-items:center; gap:3px;
      padding:9px 6px; border:1px solid var(--border);
      background:var(--surface); border-radius:var(--radius-sm);
      font-size:11px; color:var(--ink); cursor:pointer;
    }
    .stamp-btn:hover { border-color:var(--border-strong); background:var(--surface-3); }
    .stamp-btn.active {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-soft) inset;
    }
    .stamp-btn .icon { font-size:16px; line-height:1; }
    .stamp-btn .key {
      font-family: var(--font-mono); font-size:9.5px; color:var(--muted-2);
    }

    /* Info / 검증 패널 */
    .stat-row {
      display:flex; justify-content:space-between; padding:4px 0;
      font-size:12px; border-bottom:1px dashed var(--border);
    }
    .stat-row:last-of-type { border-bottom:0; }
    .stat-row .k { color:var(--muted); }
    .stat-row .v { font-family:var(--font-mono); font-weight:600; }
    .warn-item {
      padding:6px 8px; margin-bottom:4px; border-radius:5px;
      border-left:3px solid var(--muted); background:var(--surface-2);
      font-size:11.5px;
    }
    .warn-item.error { border-left-color:var(--danger); background:#fde8e7; }
    .warn-item.warn { border-left-color:var(--warn); background:#fff3df; }
    .warn-item.info { border-left-color:var(--accent); background:var(--accent-soft); }

    /* Inspector */
    .inspector-empty {
      padding:14px 16px; color:var(--muted); font-size:12px; font-style:italic;
    }
    .inspector-row {
      display:flex; justify-content:space-between; gap:8px;
      padding:5px 0; font-size:12px;
    }
    .inspector-row .k { color:var(--muted); }
    .inspector-row .v { font-family:var(--font-mono); }

    /* Build 도구 패널 */
    .build-tools { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
    .build-tools button {
      height: 36px; border: 1px solid var(--border);
      background: var(--surface); border-radius: var(--radius-sm);
      font-size: 12px; font-weight: 500; color: var(--ink); cursor: pointer;
      display: flex; align-items: center; justify-content: center; gap: 4px;
    }
    .build-tools button:hover { background: var(--surface-3); border-color: var(--border-strong); }
    .build-tools button.active {
      background: var(--accent-soft); color: var(--accent); border-color: var(--accent);
    }
    /* 선택 박스 */
    .select-box {
      fill: rgba(37,99,235,0.10);
      stroke: var(--accent);
      stroke-width: 1.5;
      stroke-dasharray: 4 3;
      pointer-events: none;
    }
    /* 선택된 노드 강조 */
    .node-selected { stroke: var(--accent); stroke-width: 3; opacity: 0.9; }
    /* Add Edge 진행 중 첫 노드 */
    .node-edge-start { stroke: var(--warn); stroke-width: 3; opacity: 0.9; }
    /* Add Edge preview line (커서 따라가는 가이드) */
    .edge-preview {
      stroke: var(--accent); stroke-width: 2; stroke-dasharray: 6 4;
      opacity: 0.6; pointer-events: none;
    }

    /* Paint trajectory preview (드래그 중 시각 피드백) */
    .traj-preview {
      fill: none;
      stroke: var(--accent);
      stroke-width: 3;
      stroke-dasharray: 6 4;
      stroke-linecap: round;
      stroke-linejoin: round;
      opacity: 0.7;
      pointer-events: none;
    }
    /* Pan 모드 (Space 키) 시 커서 */
    .map-shell.pan-mode { cursor: grab; }
    .map-shell.pan-mode.dragging { cursor: grabbing; }
    /* 노드 hover 강조 */
    .node-hover { stroke: var(--accent); stroke-width: 2; opacity: 0.5; }

    /* 토스트 (Phase 4 저장 알림 등) */
    .toast {
      position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
      padding:10px 18px; background:var(--ink); color:#fff;
      border-radius:999px; font-size:12.5px; font-weight:500;
      opacity:0; transition:opacity 0.2s; z-index:2000;
    }
    .toast.show { opacity:1; }
  </style>
</head>
<body>
  <div class="layout">
    <!-- 좌측: 맵 + 툴바 -->
    <div class="map-area">
      <div class="map-toolbar">
        <h1>Map Editor</h1>
        <span class="stat-chip">nodes <strong id="stat-nodes">—</strong></span>
        <span class="stat-chip">edges <strong id="stat-edges">—</strong></span>
        <span class="stat-chip">bidir <strong id="stat-bidir">—</strong></span>
        <span class="stat-chip" id="chip-components">component <strong id="stat-components">—</strong></span>
        <span class="spacer"></span>
        <button id="btn-reset-view" title="줌/팬 초기화">⌂ Reset View</button>
        <button id="btn-save">💾 Save</button>
        <button id="btn-save-run" class="primary">▶ Save &amp; Run</button>
      </div>
      <div class="map-shell">
        <svg id="map"></svg>
      </div>
    </div>

    <!-- 우측 패널: 도구 / 인스펙터 -->
    <aside class="side">
      <!-- 모드 토글 -->
      <div class="panel">
        <h2>Edit Mode</h2>
        <div class="mode-toggle" id="mode-toggle">
          <button data-mode="paint" class="active">Paint <span class="hint-key">P</span></button>
          <button data-mode="stamp">Stamp <span class="hint-key">S</span></button>
          <button data-mode="build">Build <span class="hint-key">B</span></button>
        </div>
        <div style="font-size:10.5px; color:var(--muted); margin-top:6px;">
          숫자키 2~5 = Stamp 자동 전환 · Ctrl/Cmd+Z = Undo · Shift+드래그 = 다중 선택
        </div>

        <!-- Build 모드 옵션 -->
        <div id="build-options" style="display:none; margin-top:12px;">
          <div class="build-tools">
            <button data-build="node" class="active">➕ Node <span class="hint-key">N</span></button>
            <button data-build="edge">↔ Edge <span class="hint-key">E</span></button>
            <button data-build="delete">🗑 Delete <span class="hint-key">D</span></button>
          </div>
          <div style="font-size:10.5px; color:var(--muted); margin-top:8px; line-height:1.5;">
            · <strong>Node</strong>: 빈 공간 클릭 → 새 노드<br>
            · <strong>Edge</strong>: 노드 A → 노드 B 순차 클릭 (좌=단방향, 우=양방향)<br>
            · <strong>Delete</strong>: 노드/엣지 클릭 → 삭제 (Shift+박스 = 일괄 삭제)
          </div>
        </div>

        <!-- Paint 모드 옵션 — 마우스 버튼으로 방향 결정 (토글 UI 불필요) -->
        <div id="paint-options" style="margin-top:12px; font-size:11.5px; color:var(--muted); line-height:1.6;">
          드래그로 화살표 쫙 → 가까운 엣지 일괄 방향
          <ul style="margin:6px 0 0 0; padding-left:18px;">
            <li><strong style="color:var(--edge-unidir)">좌클릭 드래그</strong> → 단방향 (→)</li>
            <li><strong style="color:var(--edge-bidir)">우클릭 드래그</strong> → 양방향 (↔)</li>
            <li><strong>Alt + 드래그</strong> → 단방향 역방향 강제 (←)</li>
            <li><strong>Space + 드래그</strong> → 일시 pan</li>
          </ul>
        </div>
      </div>

      <!-- Stamp 도구 -->
      <div class="panel">
        <h2>Node Role Stamp</h2>
        <div class="stamp-grid" id="stamp-grid">
          <button class="stamp-btn active" data-stamp="inspect">
            <span class="icon">👁</span><span>Inspect</span><span class="key">1</span>
          </button>
          <button class="stamp-btn" data-stamp="station">
            <span class="icon" style="color:var(--role-station)">●</span><span>Station</span><span class="key">2</span>
          </button>
          <button class="stamp-btn" data-stamp="charger">
            <span class="icon" style="color:var(--role-charger)">■</span><span>Charger</span><span class="key">3</span>
          </button>
          <button class="stamp-btn" data-stamp="holding">
            <span class="icon">○</span><span>Holding</span><span class="key">4</span>
          </button>
          <button class="stamp-btn" data-stamp="siding">
            <span class="icon" style="color:var(--role-siding)">◆</span><span>Siding</span><span class="key">5</span>
          </button>
          <button class="stamp-btn" data-stamp="reset">
            <span class="icon">↺</span><span>Reset</span><span class="key">0</span>
          </button>
        </div>
        <div class="hint" style="font-size:11px; color:var(--muted); margin-top:8px;">
          숫자키로 빠른 전환 · Shift+드래그로 영역 선택 일괄 적용
        </div>
      </div>

      <!-- Inspector / 선택 정보 -->
      <div class="panel">
        <h2>Inspector</h2>
        <div id="inspector" class="inspector-empty">
          노드 또는 엣지 위에 마우스를 올리면 정보 표시
        </div>
      </div>

      <!-- 검증 리포트 -->
      <div class="panel">
        <h2>Validation</h2>
        <div id="validation-list"></div>
        <div style="margin-top:10px;">
          <div class="stat-row"><span class="k">Connected components</span><span class="v" id="vstat-cc">—</span></div>
          <div class="stat-row"><span class="k">Isolated nodes</span><span class="v" id="vstat-iso">—</span></div>
          <div class="stat-row"><span class="k">Dead-end nodes</span><span class="v" id="vstat-de">—</span></div>
        </div>
      </div>
    </aside>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    // ── 데이터 로드 ─────────────────────────────────────────────
    const PAYLOAD = __PAYLOAD_JSON__;
    const nodes = PAYLOAD.nodes;            // 편집 대상 (mutable)
    const edges = PAYLOAD.edges;            // 편집 대상 (mutable)
    const report = PAYLOAD.report;
    const nodeById = new Map(nodes.map(n => [n.id, n]));

    // ── 편집 상태 ───────────────────────────────────────────────
    let mode = "paint";              // paint / stamp / build
    let stampTool = "inspect";       // inspect / station / charger / holding / siding / reset
    let buildTool = "node";          // node / edge / delete
    let paintTrajectoryButton = -1;  // 진행 중 trajectory 의 마우스 버튼 (0=좌, 2=우)

    // Add Edge 진행 중: 첫 번째로 클릭된 노드 (두 번째 클릭 시 엣지 확정)
    let edgeStartNodeId = "";
    let edgeStartButton = 0;     // 첫 클릭 시 마우스 버튼 (좌/우)
    let edgePreviewClient = null;  // 커서 위치 (preview line 그리기용)

    // 다중 선택 상태
    let selectionBox = null;     // {x1,y1,x2,y2} (SVG 좌표) — Shift+드래그 중일 때만
    let selectedNodeIds = new Set();   // 박스로 선택된 노드들 (Stamp 일괄 적용용)

    // ID 자동 생성 (새 노드용)
    let newNodeCounter = 9000;
    function nextNodeId() {
      // 기존 ID 와 충돌 안 하게
      while (nodeById.has(`N${++newNodeCounter}`)) {}
      return `N${newNodeCounter}`;
    }
    let newEdgeCounter = 9000;
    function nextEdgeId() {
      const existing = new Set(edges.map(e => e.id));
      while (existing.has(`L${++newEdgeCounter}`)) {}
      return `L${newEdgeCounter}`;
    }

    // ── Undo / Redo history stack ───────────────────────────────
    const history = { past: [], future: [], capacity: 50 };
    function snapshot() {
      return {
        nodes: nodes.map(n => ({...n})),
        edges: edges.map(e => ({...e})),
      };
    }
    function applySnapshot(s) {
      nodes.length = 0;
      for (const n of s.nodes) nodes.push({...n});
      edges.length = 0;
      for (const e of s.edges) edges.push({...e});
      nodeById.clear();
      for (const n of nodes) nodeById.set(n.id, n);
      selectedNodeIds.clear();
      edgeStartNodeId = "";
      render();
    }
    function pushHistory() {
      history.past.push(snapshot());
      if (history.past.length > history.capacity) history.past.shift();
      history.future.length = 0;
    }
    function undo() {
      if (history.past.length === 0) { showToast("Nothing to undo"); return; }
      history.future.push(snapshot());
      applySnapshot(history.past.pop());
      showToast("Undo");
    }
    function redo() {
      if (history.future.length === 0) { showToast("Nothing to redo"); return; }
      history.past.push(snapshot());
      applySnapshot(history.future.pop());
      showToast("Redo");
    }
    let zoomScale = 1, panX = 0, panY = 0;
    let isPanning = false, panStart = null;
    let hoveredNodeId = "", hoveredEdgeId = "";

    // Paint 모드 상태
    let paintTrajectory = null;   // [{x,y}, ...] in SVG coords (drag 중)
    let spaceKeyDown = false;     // Space 누르고 있는 동안 pan 모드 override

    // ── 좌표 fit ────────────────────────────────────────────────
    // 노드 좌표 분포를 SVG viewport (1000 × 700) 에 맞춤
    const PAD = 40;
    const VW = 1000, VH = 700;
    const xs = nodes.map(n => n.x);
    const ys = nodes.map(n => n.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const rangeX = Math.max(maxX - minX, 1), rangeY = Math.max(maxY - minY, 1);
    const scaleFit = Math.min((VW - 2*PAD) / rangeX, (VH - 2*PAD) / rangeY);
    // 좌측 상단 원점 SVG vs y-up 데이터 좌표 변환
    function sx(x) { return PAD + (x - minX) * scaleFit; }
    function sy(y) { return VH - PAD - (y - minY) * scaleFit; }

    // ── 통계 칩 채우기 ──────────────────────────────────────────
    document.getElementById("stat-nodes").textContent = report.node_count;
    document.getElementById("stat-edges").textContent =
      `${report.edge_count_raw} → ${report.edge_count_after_merge}`;
    document.getElementById("stat-bidir").textContent = report.bidirectional_count;
    document.getElementById("stat-components").textContent = report.connected_components;
    if (report.connected_components > 1) {
      document.getElementById("chip-components").style.background = "#fde8e7";
      document.getElementById("chip-components").style.color = "var(--danger)";
    }

    // ── 검증 패널 (실시간 재계산) ───────────────────────────────
    // 정적 import report 가 아니라 매번 nodes/edges 현재 상태로 다시 계산.
    // 사용자가 charger 마킹하거나 방향 바꾸면 즉시 반영됨.
    function recomputeValidation() {
      // 1) degree 재계산 (방향 변경 반영)
      for (const n of nodes) { n.degree_in = 0; n.degree_out = 0; }
      for (const e of edges) {
        const a = nodeById.get(e.src), b = nodeById.get(e.dst);
        if (!a || !b) continue;
        a.degree_out += 1;
        b.degree_in += 1;
        if (e.bidir) {
          a.degree_in += 1;
          b.degree_out += 1;
        }
      }
      // 2) connected components (weakly)
      const parent = new Map(nodes.map(n => [n.id, n.id]));
      const find = (x) => { while (parent.get(x) !== x) { parent.set(x, parent.get(parent.get(x))); x = parent.get(x); } return x; };
      const union = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent.set(ra, rb); };
      for (const e of edges) {
        if (parent.has(e.src) && parent.has(e.dst)) union(e.src, e.dst);
      }
      const cc = new Set(nodes.map(n => find(n.id))).size;
      // 3) isolated / dead-end
      const isolated = nodes.filter(n => n.degree_in === 0 && n.degree_out === 0);
      const deadEnds = nodes.filter(n =>
        n.degree_in > 0 && n.degree_out === 0 && !n.is_charger && n.role !== "holding"
      );
      // 4) chargers / stations count
      const chargerCount = nodes.filter(n => n.is_charger || n.role === "charger").length;
      const stationCount = nodes.filter(n => n.role === "station" || n.role === "work").length;
      const holdingCount = nodes.filter(n => n.is_holding || n.role === "holding" || n.role === "holding_candidate").length;

      // ── 패널 업데이트 ──
      document.getElementById("vstat-cc").textContent = cc;
      document.getElementById("vstat-iso").textContent = isolated.length;
      document.getElementById("vstat-de").textContent = deadEnds.length;

      const warnings = [];
      // error: 시뮬 불가능 수준
      if (isolated.length > 0) {
        warnings.push({sev: "error", msg: `${isolated.length} 개 노드가 어떤 링크에도 연결 안 됨`});
      }
      if (cc > 1) {
        warnings.push({sev: "error", msg: `그래프가 ${cc} 개 단편으로 나뉨 (AGV 도달 불가 구간)`});
      }
      // warn: 시뮬은 가능하지만 문제 잠재
      if (deadEnds.length > 0) {
        const sample = deadEnds.slice(0, 3).map(n => n.id).join(", ");
        warnings.push({sev: "warn", msg: `${deadEnds.length} 개 dead-end (들어오기만 가능): ${sample}${deadEnds.length > 3 ? ' …' : ''}`});
      }
      if (chargerCount === 0) {
        warnings.push({sev: "warn", msg: `Charger 미지정 (배터리 모드 사용 시 필요). Stamp [3] 으로 지정`});
      }
      // 통계 정보 (warning 아닌 좋은 상태도 표시)
      const okMsg = [];
      if (chargerCount > 0) okMsg.push(`✓ ${chargerCount} chargers`);
      if (stationCount > 0) okMsg.push(`✓ ${stationCount} stations`);
      if (holdingCount > 0) okMsg.push(`✓ ${holdingCount} holding`);

      const vlist = document.getElementById("validation-list");
      let html = "";
      if (warnings.length === 0 && okMsg.length === 0) {
        html = `<div class="warn-item info">✓ 검증 통과 (아직 노드 역할 미지정)</div>`;
      } else {
        html = warnings.map(w => `<div class="warn-item ${w.sev}"><strong>[${w.sev}]</strong> ${w.msg}</div>`).join("");
        if (okMsg.length > 0) {
          html += `<div class="warn-item info" style="font-size:11px; color:var(--success);">${okMsg.join(' · ')}</div>`;
        }
      }
      document.getElementById("validation-list").innerHTML = html;
    }

    // ── 모드 토글 ───────────────────────────────────────────────
    function setMode(newMode) {
      mode = newMode;
      document.querySelectorAll("#mode-toggle button").forEach(b =>
        b.classList.toggle("active", b.dataset.mode === newMode));
      document.getElementById("paint-options").style.display = (newMode === "paint") ? "" : "none";
      document.getElementById("build-options").style.display = (newMode === "build") ? "" : "none";
      // 모드 전환 시 진행 중 인터랙션 모두 cancel
      paintTrajectory = null;
      edgeStartNodeId = "";
      edgePreviewClient = null;
      selectedNodeIds.clear();
      selectionBox = null;
      render();
    }
    document.getElementById("mode-toggle").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn) return;
      setMode(btn.dataset.mode);
    });

    // ── Stamp 도구 선택 ─────────────────────────────────────────
    function selectStamp(tool) {
      stampTool = tool;
      document.querySelectorAll(".stamp-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.stamp === tool)
      );
    }
    document.getElementById("stamp-grid").addEventListener("click", (e) => {
      const btn = e.target.closest(".stamp-btn");
      if (!btn) return;
      selectStamp(btn.dataset.stamp);
    });

    // 키보드 단축키:
    //   숫자키 1~5 / 0 → Stamp 도구 선택 + Stamp 모드 자동 전환
    //   P/S/N 키     → Paint / Stamp / Single 모드 직접 전환
    //   Space        → 누르고 있는 동안 pan 모드 (어느 모드든)
    //   Shift / Alt  → Paint drag 시 임시 override (handler 안에서 e.shiftKey/altKey 체크)
    //   Escape       → Inspect 도구 + Paint 모드 (안전 상태로)
    function selectBuildTool(tool) {
      buildTool = tool;
      document.querySelectorAll(".build-tools button").forEach(b =>
        b.classList.toggle("active", b.dataset.build === tool));
      edgeStartNodeId = ""; // 도구 바꾸면 진행 중 Add Edge cancel
      render();
    }
    document.addEventListener("click", (e) => {
      const buildBtn = e.target.closest(".build-tools button[data-build]");
      if (buildBtn) selectBuildTool(buildBtn.dataset.build);
    });

    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      // ── Undo / Redo: Ctrl/Cmd+Z / Ctrl/Cmd+Shift+Z / Ctrl+Y ──
      if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        if (e.shiftKey) redo(); else undo();
        e.preventDefault();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "y" || e.key === "Y")) {
        redo(); e.preventDefault(); return;
      }
      // ── 숫자키: Stamp 자동 전환 ─────────────────────────────
      const stampMap = {"1":"inspect","2":"station","3":"charger","4":"holding","5":"siding","0":"reset"};
      if (stampMap[e.key]) {
        selectStamp(stampMap[e.key]);
        setMode("stamp");
        e.preventDefault();
        return;
      }
      // ── 모드 단축키 ──────────────────────────────────────────
      if (e.key === "p" || e.key === "P") { setMode("paint"); e.preventDefault(); return; }
      if (e.key === "s" || e.key === "S") { setMode("stamp"); e.preventDefault(); return; }
      if (e.key === "b" || e.key === "B") { setMode("build"); e.preventDefault(); return; }
      // ── Build 도구 단축키 (Build 모드일 때만) ──────────────
      if (mode === "build") {
        if (e.key === "n" || e.key === "N") { selectBuildTool("node"); e.preventDefault(); return; }
        if (e.key === "e" || e.key === "E") { selectBuildTool("edge"); e.preventDefault(); return; }
        if (e.key === "d" || e.key === "D") { selectBuildTool("delete"); e.preventDefault(); return; }
      }
      if (e.key === "Escape") {
        selectStamp("inspect");
        setMode("paint");
        return;
      }
      if (e.code === "Space" && !spaceKeyDown) {
        spaceKeyDown = true;
        document.querySelector(".map-shell").classList.add("pan-mode");
        e.preventDefault();
      }
    });
    document.addEventListener("keyup", (e) => {
      if (e.code === "Space") {
        spaceKeyDown = false;
        document.querySelector(".map-shell").classList.remove("pan-mode");
      }
    });

    // ── 맵 렌더링 ───────────────────────────────────────────────
    // Phase 1: 정적 렌더링 (편집 결과를 즉시 반영하지만 인터랙션은 stub)
    function render() {
      const svg = document.getElementById("map");
      svg.setAttribute("viewBox", `0 0 ${VW} ${VH}`);
      const labelScale = 1 / Math.max(zoomScale, 0.001);

      const edgesSvg = edges.map(e => {
        const a = nodeById.get(e.src), b = nodeById.get(e.dst);
        if (!a || !b) return "";
        const x1 = sx(a.x), y1 = sy(a.y), x2 = sx(b.x), y2 = sy(b.y);
        const stroke = e.bidir ? "var(--edge-bidir)" : "var(--edge-unidir)";
        const width = 2;
        const dx = x2-x1, dy = y2-y1;
        const len = Math.hypot(dx, dy) || 1;
        const angle = Math.atan2(dy, dx) * 180 / Math.PI;
        // 화살촉:
        //  · 단방향 → 가운데 한 개 (forward)
        //  · 양방향 → 양 끝 가까이 두 개 (각각 안쪽을 보는 chevron, 색깔이 같아도 양방향임이 한눈에)
        let arrow = "";
        if (e.bidir) {
          // 양 끝 30% / 70% 지점에 화살촉. node 와 겹치지 않게.
          const p1x = x1 + dx*0.3, p1y = y1 + dy*0.3;
          const p2x = x1 + dx*0.7, p2y = y1 + dy*0.7;
          // p1 의 화살촉은 src(x1,y1) 방향 (=뒤쪽), p2 는 dst(x2,y2) 방향 (=앞쪽)
          arrow =
            `<g transform="translate(${p1x} ${p1y}) scale(${labelScale}) rotate(${angle + 180})">
              <polygon points="5,0 -3,-3 -3,3" fill="${stroke}" opacity="0.95" /></g>` +
            `<g transform="translate(${p2x} ${p2y}) scale(${labelScale}) rotate(${angle})">
              <polygon points="5,0 -3,-3 -3,3" fill="${stroke}" opacity="0.95" /></g>`;
        } else {
          const mx = (x1+x2)/2, my = (y1+y2)/2;
          arrow = `<g transform="translate(${mx} ${my}) scale(${labelScale}) rotate(${angle})">
            <polygon points="6,0 -3,-4 -3,4" fill="${stroke}" opacity="0.95" /></g>`;
        }
        return `<g data-edge-id="${e.id}">
          <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                stroke="${stroke}" stroke-width="${width/zoomScale}"
                stroke-linecap="round" vector-effect="non-scaling-stroke" />
          ${arrow}
        </g>`;
      }).join("");

      const nodesSvg = nodes.map(n => {
        const cx = sx(n.x), cy = sy(n.y);
        const color = roleColor(n.role, n.is_charger, n.is_holding);
        const shape = roleShape(n.role, n.is_charger, n.is_holding, color);
        const rings = [];
        if (n.id === hoveredNodeId) rings.push(`<circle class="node-hover" r="11" fill="none" />`);
        if (selectedNodeIds.has(n.id)) rings.push(`<circle class="node-selected" r="13" fill="none" />`);
        if (n.id === edgeStartNodeId) rings.push(`<circle class="node-edge-start" r="14" fill="none" />`);
        return `<g data-node-id="${n.id}" style="cursor:${stampTool === 'inspect' && mode !== 'build' ? 'default' : 'pointer'}"
                   transform="translate(${cx} ${cy}) scale(${labelScale})">
          <circle r="22" fill="transparent" />
          ${rings.join('')}
          ${shape}
        </g>`;
      }).join("");

      // Paint trajectory preview
      const trajSvg = (paintTrajectory && paintTrajectory.length >= 2)
        ? `<polyline class="traj-preview" points="${paintTrajectory.map(p => `${p.x},${p.y}`).join(' ')}" />`
        : "";

      // Selection box (Shift+드래그)
      let boxSvg = "";
      if (selectionBox) {
        const xLo = Math.min(selectionBox.x1, selectionBox.x2);
        const yLo = Math.min(selectionBox.y1, selectionBox.y2);
        const w = Math.abs(selectionBox.x2 - selectionBox.x1);
        const h = Math.abs(selectionBox.y2 - selectionBox.y1);
        boxSvg = `<rect class="select-box" x="${xLo}" y="${yLo}" width="${w}" height="${h}" />`;
      }

      // Add Edge preview line (첫 노드 ~ 커서)
      let edgePrevSvg = "";
      if (mode === "build" && buildTool === "edge" && edgeStartNodeId && edgePreviewClient) {
        const src = nodeById.get(edgeStartNodeId);
        if (src) {
          edgePrevSvg = `<line class="edge-preview" x1="${sx(src.x)}" y1="${sy(src.y)}"
            x2="${edgePreviewClient.x}" y2="${edgePreviewClient.y}"
            vector-effect="non-scaling-stroke" />`;
        }
      }

      svg.innerHTML = `<g transform="translate(${panX} ${panY}) scale(${zoomScale})">
        ${edgesSvg}${nodesSvg}${edgePrevSvg}${trajSvg}${boxSvg}
      </g>`;
      // 사용자 액션 마다 validation 자동 재계산
      recomputeValidation();
    }

    function roleColor(role, isCharger, isHolding) {
      if (isCharger || role === "charger") return "var(--role-charger)";
      if (role === "station" || role === "work") return "var(--role-station)";
      if (isHolding || role === "holding" || role === "holding_candidate") return "var(--role-holding)";
      if (role === "siding") return "var(--role-siding)";
      return "var(--role-standard)";
    }
    function roleShape(role, isCharger, isHolding, color) {
      if (isCharger || role === "charger")
        return `<rect x="-5" y="-5" width="10" height="10" rx="1" fill="${color}" />`;
      if (role === "station" || role === "work")
        return `<circle r="5" fill="${color}" />`;
      if (isHolding || role === "holding" || role === "holding_candidate")
        return `<circle r="4" fill="#fff" stroke="${color}" stroke-width="2" />`;
      if (role === "siding")
        return `<polygon points="0,-5 5,0 0,5 -5,0" fill="${color}" />`;
      return `<circle r="3" fill="${color}" opacity="0.7" />`;
    }

    // ── Hover (인스펙터 + 노드 하이라이트) ──────────────────────
    document.getElementById("map").addEventListener("mousemove", (e) => {
      const nodeHit = e.target.closest("[data-node-id]");
      const edgeHit = e.target.closest("[data-edge-id]");
      const prevHoverNode = hoveredNodeId;
      if (nodeHit) {
        hoveredNodeId = nodeHit.dataset.nodeId;
        const n = nodeById.get(hoveredNodeId);
        showInspectorNode(n);
      } else if (edgeHit) {
        hoveredNodeId = "";
        const ed = edges.find(x => x.id === edgeHit.dataset.edgeId);
        showInspectorEdge(ed);
      } else {
        hoveredNodeId = "";
        document.getElementById("inspector").innerHTML =
          '<div class="inspector-empty">노드 또는 엣지 위에 마우스를 올리면 정보 표시</div>';
      }
      // 노드 hover ring 만 재렌더 (drag 중 아닐 때만)
      if (prevHoverNode !== hoveredNodeId && !paintTrajectory && !isPanning) {
        render();
      }
    });

    function showInspectorNode(n) {
      const role = n.role || "standard";
      const extras = [];
      if (n.is_charger) extras.push("charger");
      if (n.is_holding) extras.push("holding");
      document.getElementById("inspector").innerHTML = `
        <div class="inspector-row"><span class="k">type</span><span class="v">📍 NODE</span></div>
        <div class="inspector-row"><span class="k">id</span><span class="v">${n.id}</span></div>
        <div class="inspector-row"><span class="k">name</span><span class="v">${n.name}</span></div>
        <div class="inspector-row"><span class="k">position</span><span class="v">(${n.x.toFixed(1)}, ${n.y.toFixed(1)})</span></div>
        <div class="inspector-row"><span class="k">role</span><span class="v">${role}${extras.length ? ' · ' + extras.join(', ') : ''}</span></div>
        <div class="inspector-row"><span class="k">degree</span><span class="v">in ${n.degree_in} / out ${n.degree_out}</span></div>
      `;
    }
    function showInspectorEdge(ed) {
      const a = nodeById.get(ed.src), b = nodeById.get(ed.dst);
      document.getElementById("inspector").innerHTML = `
        <div class="inspector-row"><span class="k">type</span><span class="v">↔ EDGE</span></div>
        <div class="inspector-row"><span class="k">id</span><span class="v">${ed.id}</span></div>
        <div class="inspector-row"><span class="k">from → to</span><span class="v">${ed.src} → ${ed.dst}</span></div>
        <div class="inspector-row"><span class="k">direction</span><span class="v">${ed.bidir ? '↔ bidirectional' : '→ unidirectional'}</span></div>
        <div class="inspector-row"><span class="k">corridor</span><span class="v">${ed.corridor || '(unlabeled)'}</span></div>
        <div class="inspector-row"><span class="k">access</span><span class="v">${ed.access || '—'}</span></div>
      `;
    }

    // ── Pan / Zoom ─────────────────────────────────────────────
    document.getElementById("map").addEventListener("wheel", (e) => {
      e.preventDefault();
      const svg = e.currentTarget;
      const rect = svg.getBoundingClientRect();
      const cx = (e.clientX - rect.left) * (VW / rect.width);
      const cy = (e.clientY - rect.top) * (VH / rect.height);
      const nextScale = Math.min(8, Math.max(0.4, zoomScale * (e.deltaY < 0 ? 1.12 : 0.9)));
      const ratio = nextScale / zoomScale;
      panX = cx - (cx - panX) * ratio;
      panY = cy - (cy - panY) * ratio;
      zoomScale = nextScale;
      render();
    }, {passive: false});

    // ── 마우스 인터랙션 — 모드별 분기 ─────────────────────────
    //   pan 우선: Space 누른 상태면 모드와 무관하게 pan
    //   Paint 모드 + 빈 공간 드래그: trajectory 그리기
    //   Stamp/Single/Inspect 모드 + 빈 공간 드래그: pan
    //   노드 위에서 시작한 드래그: 노드 클릭 (mouseup) 우선, drag 무시
    // SVG screen pixel → viewBox 좌표 (preserveAspectRatio letterbox 정확히 처리)
    function clientToSvg(svg, clientX, clientY) {
      const pt = svg.createSVGPoint();
      pt.x = clientX; pt.y = clientY;
      const ctm = svg.getScreenCTM();
      if (!ctm) return {x: 0, y: 0};
      const inv = ctm.inverse();
      const local = pt.matrixTransform(inv);
      return {x: local.x, y: local.y};
    }

    document.getElementById("map").addEventListener("pointerdown", (e) => {
      if (e.button !== 0 && e.button !== 2) return;
      const onNode = !!e.target.closest("[data-node-id]");
      const onEdge = !onNode && !!e.target.closest("[data-edge-id]");
      const svg = e.currentTarget;
      const pt = clientToSvg(svg, e.clientX, e.clientY);
      const localX = (pt.x - panX) / zoomScale;
      const localY = (pt.y - panY) / zoomScale;

      // Space + 좌클릭 = pan (모드 무관)
      if (spaceKeyDown && e.button === 0) {
        isPanning = true;
        panStart = {x: e.clientX, y: e.clientY, panX, panY};
        document.querySelector(".map-shell").classList.add("dragging");
        return;
      }

      // Shift + 좌클릭 = 다중 선택 박스 (Stamp / Build 모드에서)
      if (e.shiftKey && e.button === 0 && (mode === "stamp" || mode === "build")) {
        selectionBox = {x1: localX, y1: localY, x2: localX, y2: localY};
        selectedNodeIds.clear();
        e.preventDefault();
        return;
      }

      if (mode === "paint") {
        paintTrajectory = [{x: localX, y: localY}];
        paintTrajectoryButton = e.button;
        e.preventDefault();
        return;
      }

      if (mode === "build") {
        // Build 도구별 분기 (pointerdown 즉시 액션)
        if (buildTool === "node") {
          // 빈 공간 좌클릭만 — 노드 추가
          if (!onNode && !onEdge && e.button === 0) {
            addNodeAt(localX, localY);
            render();
          }
          // Add Node 도구 + 노드 위 클릭은 무시 (또는 hint?)
          return;
        }
        if (buildTool === "edge") {
          if (onNode) {
            const nid = e.target.closest("[data-node-id]").dataset.nodeId;
            if (!edgeStartNodeId) {
              // 첫 노드 선택
              edgeStartNodeId = nid;
              edgeStartButton = e.button;
              edgePreviewClient = {x: localX, y: localY};
              showToast(`Edge: ${nid} → ?`);
              render();
            } else {
              // 두 번째 노드 → 엣지 생성
              const bidir = (edgeStartButton === 2) || (e.button === 2);
              addEdgeBetween(edgeStartNodeId, nid, bidir);
              edgeStartNodeId = "";
              edgePreviewClient = null;
              render();
            }
          }
          // 빈 공간 클릭 = 진행 중 cancel
          else if (edgeStartNodeId) {
            edgeStartNodeId = "";
            edgePreviewClient = null;
            showToast("Edge add cancelled");
            render();
          }
          return;
        }
        if (buildTool === "delete") {
          if (onNode) {
            deleteNode(e.target.closest("[data-node-id]").dataset.nodeId);
            render();
          } else if (onEdge) {
            deleteEdge(e.target.closest("[data-edge-id]").dataset.edgeId);
            render();
          } else if (e.button === 0) {
            // 빈 공간 좌클릭 = pan
            isPanning = true;
            panStart = {x: e.clientX, y: e.clientY, panX, panY};
            document.querySelector(".map-shell").classList.add("dragging");
          }
          return;
        }
      }

      // Stamp 모드
      if (onNode) return;
      if (e.button !== 0) return;
      isPanning = true;
      panStart = {x: e.clientX, y: e.clientY, panX, panY};
      document.querySelector(".map-shell").classList.add("dragging");
    });

    document.getElementById("map").addEventListener("pointermove", (e) => {
      const svg = e.currentTarget;
      if (isPanning && panStart) {
        const rect = svg.getBoundingClientRect();
        panX = panStart.panX + (e.clientX - panStart.x) * (VW / rect.width);
        panY = panStart.panY + (e.clientY - panStart.y) * (VH / rect.height);
        render();
        return;
      }
      const pt = clientToSvg(svg, e.clientX, e.clientY);
      const localX = (pt.x - panX) / zoomScale;
      const localY = (pt.y - panY) / zoomScale;

      // 다중 선택 박스 업데이트
      if (selectionBox) {
        selectionBox.x2 = localX;
        selectionBox.y2 = localY;
        // 실시간으로 박스 안 노드 마킹 (시각 피드백)
        selectedNodeIds.clear();
        const xLo = Math.min(selectionBox.x1, selectionBox.x2);
        const xHi = Math.max(selectionBox.x1, selectionBox.x2);
        const yLo = Math.min(selectionBox.y1, selectionBox.y2);
        const yHi = Math.max(selectionBox.y1, selectionBox.y2);
        for (const n of nodes) {
          const cx = sx(n.x), cy = sy(n.y);
          if (cx >= xLo && cx <= xHi && cy >= yLo && cy <= yHi) {
            selectedNodeIds.add(n.id);
          }
        }
        render();
        return;
      }

      // Paint trajectory 점 추가
      if (paintTrajectory) {
        paintTrajectory.push({x: localX, y: localY});
        render();
        return;
      }

      // Add Edge 진행 중 — preview line 갱신
      if (mode === "build" && buildTool === "edge" && edgeStartNodeId) {
        edgePreviewClient = {x: localX, y: localY};
        render();
        return;
      }
    });

    document.getElementById("map").addEventListener("pointerup", (e) => {
      document.querySelector(".map-shell").classList.remove("dragging");
      if (isPanning) {
        isPanning = false; panStart = null;
        return;
      }
      // 다중 선택 박스 종료 → 현재 모드/도구에 맞춰 일괄 적용
      if (selectionBox) {
        selectionBox = null;
        if (selectedNodeIds.size > 0) {
          // Stamp 모드: 현재 stamp 도구로 일괄
          if (mode === "stamp" && stampTool !== "inspect") {
            stampSelected(stampTool);
          }
          // Build + Delete: 일괄 삭제
          else if (mode === "build" && buildTool === "delete") {
            deleteSelected();
          }
          // 다른 도구는 선택 유지 (사용자가 다음 액션 결정)
        }
        render();
        return;
      }
      // Paint trajectory 종료
      if (paintTrajectory) {
        if (paintTrajectory.length >= 2) {
          pushHistory();
          const isBidir = (paintTrajectoryButton === 2);
          applyPaint(paintTrajectory, isBidir, e.altKey);
        }
        paintTrajectory = null;
        paintTrajectoryButton = -1;
        render();
      }
    });
    document.getElementById("map").addEventListener("pointerleave", (e) => {
      document.querySelector(".map-shell").classList.remove("dragging");
      if (paintTrajectory && paintTrajectory.length >= 2) {
        pushHistory();
        const isBidir = (paintTrajectoryButton === 2);
        applyPaint(paintTrajectory, isBidir, false);
      }
      paintTrajectory = null;
      paintTrajectoryButton = -1;
      selectionBox = null;
      isPanning = false; panStart = null;
      render();
    });

    // ── Paint trajectory → 가까운 엣지 일괄 방향 변경 ─────────
    function applyPaint(trajectory, forceBidir, altKey) {
      // forceBidir: 우클릭 드래그 였는지. true 면 양방향, false 면 단방향
      // altKey: 단방향일 때 역방향 강제 (양방향엔 의미 없음)
      const effectiveDir = forceBidir ? "bi" : "uni";
      const reverseUni = altKey;

      // trajectory 의 평균 방향 벡터 (시작점 → 끝점)
      const t0 = trajectory[0], tN = trajectory[trajectory.length - 1];
      const tdx = tN.x - t0.x, tdy = tN.y - t0.y;

      // 화면상 거리 임계값 (SVG 좌표 단위). zoom 무관하게 ~30px 화면 거리로.
      const HIT_RADIUS = 30 / zoomScale;

      let changedCount = 0;
      for (const e of edges) {
        const a = nodeById.get(e.src), b = nodeById.get(e.dst);
        if (!a || !b) continue;
        const x1 = sx(a.x), y1 = sy(a.y), x2 = sx(b.x), y2 = sy(b.y);
        // 엣지 중점이 trajectory 어느 segment 와 가까운가
        const mx = (x1+x2)/2, my = (y1+y2)/2;
        if (!nearTrajectory(mx, my, trajectory, HIT_RADIUS)) continue;

        // 엣지 방향 (src→dst) vs trajectory 방향 내적
        const edx = x2 - x1, edy = y2 - y1;
        const dot = tdx * edx + tdy * edy;

        if (effectiveDir === "bi") {
          e.bidir = true;
        } else {
          e.bidir = false;
          // 단방향: trajectory 와 같은 방향이면 src/dst 유지, 반대면 스왑
          const wantReversed = (dot < 0) ^ reverseUni;
          if (wantReversed) {
            const tmp = e.src; e.src = e.dst; e.dst = tmp;
          }
        }
        changedCount++;
      }
      if (changedCount > 0) {
        showToast(`${changedCount} 개 엣지 → ${effectiveDir === "bi" ? "양방향" : (reverseUni ? "역방향 단방향" : "단방향")}`);
      }
    }

    // 점 (px, py) 와 polyline segments 의 최소 거리 ≤ R 인가
    function nearTrajectory(px, py, trajectory, R) {
      for (let i = 0; i < trajectory.length - 1; i++) {
        const a = trajectory[i], b = trajectory[i + 1];
        if (pointSegmentDistance(px, py, a.x, a.y, b.x, b.y) <= R) return true;
      }
      return false;
    }
    function pointSegmentDistance(px, py, x1, y1, x2, y2) {
      const dx = x2 - x1, dy = y2 - y1;
      const len2 = dx*dx + dy*dy;
      if (len2 < 1e-9) return Math.hypot(px - x1, py - y1);
      let t = ((px - x1) * dx + (py - y1) * dy) / len2;
      t = Math.max(0, Math.min(1, t));
      const cx = x1 + t * dx, cy = y1 + t * dy;
      return Math.hypot(px - cx, py - cy);
    }

    // ── 좌클릭 (mouseup 직후) — Stamp / Build 모드 처리 ───────
    // Paint 모드는 pointerdown/up 에서 trajectory 처리됨
    document.getElementById("map").addEventListener("click", (e) => {
      if (mode === "paint") return;
      // Build 모드 click 은 pointerdown 에서 별도 처리 (좌/우 분기) → 여기선 skip
      if (mode === "build") return;
      // Stamp 모드
      const nodeHit = e.target.closest("[data-node-id]");
      if (!nodeHit) return;
      if (stampTool === "inspect") return;
      const n = nodeById.get(nodeHit.dataset.nodeId);
      pushHistory();
      applyStamp(n, stampTool);
      render();
      showToast(`${n.id} → ${stampTool}`);
    });

    function applyStamp(node, tool) {
      // 노드 role/플래그를 즉시 변경
      if (tool === "reset") {
        node.role = "standard"; node.is_charger = false; node.is_holding = false;
      } else if (tool === "station") {
        node.role = "station"; node.is_charger = false; node.is_holding = false;
      } else if (tool === "charger") {
        node.role = "charger"; node.is_charger = true; node.is_holding = false;
      } else if (tool === "holding") {
        node.role = "holding"; node.is_charger = false; node.is_holding = true;
      } else if (tool === "siding") {
        node.role = "siding"; node.is_charger = false; node.is_holding = false;
      }
    }

    // ── Build mutations (모두 호출 전 pushHistory()) ──────────
    // SVG 좌표 (panX/panY/zoomScale 적용 전) → 데이터 좌표 (x, y)
    function svgToData(sxv, syv) {
      const x = (sxv - PAD) / scaleFit + minX;
      const y = ((VH - PAD) - syv) / scaleFit + minY;
      return {x, y};
    }
    function addNodeAt(svgX, svgY) {
      pushHistory();
      const {x, y} = svgToData(svgX, svgY);
      const id = nextNodeId();
      const newNode = {
        id, x, y, name: id,
        role: "standard", is_charger: false, is_holding: false,
        degree_in: 0, degree_out: 0,
      };
      nodes.push(newNode);
      nodeById.set(id, newNode);
      showToast(`+ Node ${id} at (${x.toFixed(1)}, ${y.toFixed(1)})`);
    }
    function addEdgeBetween(srcId, dstId, bidir) {
      if (srcId === dstId) { showToast("동일 노드 — 엣지 생략"); return; }
      // 같은 방향 중복?
      if (edges.some(e => e.src === srcId && e.dst === dstId)) {
        // 이미 있으면 양/단 update
        const existing = edges.find(e => e.src === srcId && e.dst === dstId);
        if (existing.bidir !== bidir) {
          pushHistory();
          existing.bidir = bidir;
          showToast(`Edge ${srcId}→${dstId} → ${bidir ? '양방향' : '단방향'}`);
        } else {
          showToast(`이미 존재: ${srcId} → ${dstId}`);
        }
        return;
      }
      pushHistory();
      const id = nextEdgeId();
      edges.push({
        id, src: srcId, dst: dstId,
        bidir: !!bidir, corridor: "", access: "",
      });
      showToast(`+ Edge ${srcId} ${bidir ? '↔' : '→'} ${dstId}`);
    }
    function deleteNode(id) {
      pushHistory();
      const idx = nodes.findIndex(n => n.id === id);
      if (idx >= 0) nodes.splice(idx, 1);
      nodeById.delete(id);
      // 관련 엣지 모두 삭제
      const before = edges.length;
      for (let i = edges.length - 1; i >= 0; i--) {
        if (edges[i].src === id || edges[i].dst === id) edges.splice(i, 1);
      }
      showToast(`- Node ${id} (+ ${before - edges.length} edges)`);
    }
    function deleteEdge(id) {
      pushHistory();
      const idx = edges.findIndex(e => e.id === id);
      if (idx >= 0) {
        const e = edges[idx];
        edges.splice(idx, 1);
        showToast(`- Edge ${e.src} → ${e.dst}`);
      }
    }
    function deleteSelected() {
      if (selectedNodeIds.size === 0) return;
      pushHistory();
      const count = selectedNodeIds.size;
      let edgeRemoved = 0;
      for (const id of selectedNodeIds) {
        const idx = nodes.findIndex(n => n.id === id);
        if (idx >= 0) nodes.splice(idx, 1);
        nodeById.delete(id);
      }
      for (let i = edges.length - 1; i >= 0; i--) {
        if (selectedNodeIds.has(edges[i].src) || selectedNodeIds.has(edges[i].dst)) {
          edges.splice(i, 1);
          edgeRemoved++;
        }
      }
      selectedNodeIds.clear();
      showToast(`- ${count} nodes (+ ${edgeRemoved} edges)`);
    }
    function stampSelected(tool) {
      if (selectedNodeIds.size === 0) return;
      pushHistory();
      const count = selectedNodeIds.size;
      for (const id of selectedNodeIds) {
        const n = nodeById.get(id);
        if (n) applyStamp(n, tool);
      }
      selectedNodeIds.clear();
      showToast(`${count} nodes → ${tool}`);
    }

    // ── 우클릭: 브라우저 기본 메뉴 차단 + Stamp 모드 노드 우클릭 = Reset
    document.getElementById("map").addEventListener("contextmenu", (e) => {
      // Paint 모드 우클릭은 pointerdown 에서 trajectory 시작에 이미 사용 → 메뉴 무조건 막음
      e.preventDefault();
      if (mode !== "stamp") return;
      const nodeHit = e.target.closest("[data-node-id]");
      if (!nodeHit) return;
      const n = nodeById.get(nodeHit.dataset.nodeId);
      // 우클릭 = "되돌리기" → standard 로 reset
      applyStamp(n, "reset");
      render();
      showToast(`${n.id} → reset (standard)`);
    });

    // ── Reset View ──────────────────────────────────────────────
    document.getElementById("btn-reset-view").addEventListener("click", () => {
      zoomScale = 1; panX = 0; panY = 0; render();
    });

    // ── Save / Save & Run (Phase 4 에서 풀 구현, 지금은 stub) ───
    document.getElementById("btn-save").addEventListener("click", () => {
      const edits = exportEdits();
      downloadEdits(edits);
      showToast("Saved");
    });
    document.getElementById("btn-save-run").addEventListener("click", () => {
      showToast("Save & Run — Phase 4/5 에서 Quickrun 연결 예정");
    });

    function exportEdits() {
      // override-only 메타: 원본에서 바뀐 것만 기록
      const node_overrides = {};
      for (const n of nodes) {
        if (n.role !== "standard" || n.is_charger || n.is_holding) {
          node_overrides[n.id] = {role: n.role, is_charger: n.is_charger, is_holding: n.is_holding};
        }
      }
      const edge_overrides = {};
      for (const e of edges) {
        // Phase 2 에서 inferred vs current 비교해 기록 (지금은 모든 엣지 기록)
        edge_overrides[e.id] = {bidirectional: e.bidir, corridor: e.corridor};
      }
      return {node_overrides, edge_overrides, timestamp: new Date().toISOString()};
    }

    function downloadEdits(edits) {
      const blob = new Blob([JSON.stringify(edits, null, 2)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "map_edits.edit.json";
      a.click();
      URL.revokeObjectURL(url);
    }

    // ── Toast 알림 ──────────────────────────────────────────────
    let toastTimer = null;
    function showToast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
    }

    // ── 초기 렌더 ───────────────────────────────────────────────
    render();
    document.title = PAYLOAD.title;
  </script>
</body>
</html>
"""
