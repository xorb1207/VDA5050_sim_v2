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


def build_editor_html(
    imported: ImportedMap,
    title: str = "Map Editor",
    source_name: str = "imported_map",
    server_map_id: str | None = None,
) -> str:
    """ImportedMap → self-contained HTML 페이지 문자열.

    source_name: Save 시 다운로드 파일명 (예: "synthetic_plant") → "synthetic_plant.edit.json"
    server_map_id: 서버 메모리에 보관된 map id. 있으면 Save 시 서버에도 push 시도.
                   None 이면 다운로드만.
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
        "v_max": e.v_max,   # F1b-ux: per-edge 속도 제한 (None=미설정)
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
        "source_name": source_name,
        "server_map_id": server_map_id,   # null 이면 다운로드만, 있으면 server 갱신도
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
          <button data-mode="speed">Speed <span class="hint-key">V</span></button>
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
          <!-- F1e: Stamp 배치 정렬 옵션 -->
          <div style="margin-top:10px; padding:8px; background:var(--surface-3); border-radius:6px;">
            <label style="display:flex; gap:6px; align-items:center; font-size:11.5px;">
              <input type="checkbox" id="grid-snap-toggle" />
              <span>Grid snap</span>
              <input type="number" id="grid-size-input" value="10" min="0.5" step="0.5"
                     style="width:54px; margin-left:auto; padding:2px 4px;" />
              <span style="color:var(--muted); font-size:10.5px;">units</span>
            </label>
            <div style="font-size:10.5px; color:var(--muted); margin-top:6px; line-height:1.5;">
              · <strong>Shift + 클릭</strong>: 직전 노드의 X 또는 Y 축에 고정 (가까운 축)
            </div>
          </div>
          <div style="font-size:10.5px; color:var(--muted); margin-top:8px; line-height:1.5;">
            · <strong>Node</strong>: 빈 공간 클릭 → 새 노드<br>
            · <strong>Edge</strong>: 노드 A → 노드 B 순차 클릭 (좌=단방향, 우=양방향)<br>
            · <strong>Edge</strong>: edge 위 좌클릭 → 단↔양 토글<br>
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

        <!-- Speed 모드 옵션 -->
        <div id="speed-options" style="display:none; margin-top:12px; font-size:11.5px; color:var(--muted); line-height:1.6;">
          <strong style="color:var(--warn)">v_max 편집 모드</strong>
          <ul style="margin:6px 0 0 0; padding-left:18px;">
            <li>Edge 위 <strong>휠 스크롤</strong> → v_max ±0.1 m/s (즉시 적용)</li>
            <li>Edge 좌클릭 → 인스펙터 <strong>고정</strong> (마우스 떠도 +/-/입력 유지)</li>
            <li>빈 공간 스크롤 → 평소 zoom</li>
            <li>모든 edge 에 현재 v_max 값 표시</li>
            <li>Esc → Paint 모드로 복귀</li>
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
    const SOURCE_NAME = PAYLOAD.source_name || "imported_map";
    const SERVER_MAP_ID = PAYLOAD.server_map_id || null;
    const nodes = PAYLOAD.nodes;            // 편집 대상 (mutable)
    const edges = PAYLOAD.edges;            // 편집 대상 (mutable)
    const report = PAYLOAD.report;
    const nodeById = new Map(nodes.map(n => [n.id, n]));
    // 원본 상태 보존 (Save 시 diff 계산용)
    const ORIGINAL_NODES = new Map(PAYLOAD.nodes.map(n => [n.id, JSON.parse(JSON.stringify(n))]));
    const ORIGINAL_EDGES = new Map(PAYLOAD.edges.map(e => [e.id, JSON.parse(JSON.stringify(e))]));

    // ── 편집 상태 ───────────────────────────────────────────────
    let mode = "paint";              // paint / stamp / build / speed
    let pinnedEdgeId = "";           // Speed 모드: 클릭으로 고정된 edge (인스펙터 유지)
    let stampTool = "inspect";       // inspect / station / charger / holding / siding / reset
    let buildTool = "node";          // node / edge / delete
    let paintTrajectoryButton = -1;  // 진행 중 trajectory 의 마우스 버튼 (0=좌, 2=우)

    // Add Edge 진행 중: 첫 번째로 클릭된 노드 (두 번째 클릭 시 엣지 확정)
    let edgeStartNodeId = "";
    let edgeStartButton = 0;     // 첫 클릭 시 마우스 버튼 (좌/우)
    // F1e: Stamp 배치 정렬 상태
    let gridSnapEnabled = false;
    let gridSize = 10;           // data 좌표 단위
    let lastPlacedDataXY = null; // 마지막 추가된 노드 위치 (data 좌표)
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
      const speedOpts = document.getElementById("speed-options");
      if (speedOpts) speedOpts.style.display = (newMode === "speed") ? "" : "none";
      // Speed 모드 떠나면 pin 해제
      if (newMode !== "speed") pinnedEdgeId = "";
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

    // F1e: Grid snap UI 바인딩
    document.getElementById("grid-snap-toggle").addEventListener("change", (e) => {
      gridSnapEnabled = !!e.target.checked;
      showToast(`Grid snap ${gridSnapEnabled ? 'ON' : 'OFF'}`);
    });
    document.getElementById("grid-size-input").addEventListener("change", (e) => {
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v) && v > 0) gridSize = v;
      else e.target.value = gridSize;
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
      if (e.key === "v" || e.key === "V") { setMode("speed"); e.preventDefault(); return; }
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
        // F1b-ux: v_max 설정된 edge 는 amber, 일반 edge 는 기존 색
        const hasVMax = (e.v_max !== null && e.v_max !== undefined);
        const isActive = (e.id === hoveredEdgeId);
        const isPinned = (e.id === pinnedEdgeId);
        let stroke;
        if (hasVMax) stroke = "var(--warn)";              // 속도 제한 표시색 (amber)
        else if (e.bidir) stroke = "var(--edge-bidir)";
        else stroke = "var(--edge-unidir)";
        // 활성/hover/pinned 시 두께 증가
        const width = (isPinned ? 5 : (isActive ? 4 : 2));
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
        // F1c+ : invisible 두꺼운 hit line (화면 ~9px) — 줌인 상태에서도 edge hover 쉬움.
        // 실 visual line(2px) 위에 더 두꺼운 transparent stroke 을 깔고 pointer-events="stroke" 로
        // 클릭/호버 잡음. 시각 두께는 그대로 유지.
        const hitW = 9 / zoomScale;
        return `<g data-edge-id="${e.id}">
          <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                stroke="transparent" stroke-width="${hitW}"
                pointer-events="stroke" stroke-linecap="round" />
          <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                stroke="${stroke}" stroke-width="${width/zoomScale}"
                stroke-linecap="round" vector-effect="non-scaling-stroke"
                pointer-events="none" />
          ${arrow}
        </g>`;
      }).join("");

      // Speed 모드: 모든 edge 에 현재 v_max 값 라벨 표시 (전체 상태 한눈에)
      let vmaxLabelsSvg = "";
      if (mode === "speed") {
        vmaxLabelsSvg = edges.map(e => {
          const a = nodeById.get(e.src), b = nodeById.get(e.dst);
          if (!a || !b) return "";
          const mx = (sx(a.x) + sx(b.x)) / 2;
          const my = (sy(a.y) + sy(b.y)) / 2;
          const hasV = (e.v_max !== null && e.v_max !== undefined);
          const txt = hasV ? Number(e.v_max).toFixed(2) : "—";
          const fill = hasV ? "var(--warn)" : "var(--muted-2)";
          return `<g transform="translate(${mx} ${my}) scale(${labelScale})" style="pointer-events:none">
            <rect x="-14" y="-7" width="28" height="14" rx="3" fill="rgba(255,255,255,0.92)" stroke="${fill}" stroke-width="0.8" />
            <text x="0" y="3" text-anchor="middle" fill="${fill}" font-family="var(--font-mono)" font-size="9">${txt}</text>
          </g>`;
        }).join("");
      }

      // F1c: hit-test 반경을 world 좌표 기반으로 변환 + 화면 픽셀 범위 클램프.
      //   기본 22 world units(노드 간 fit scale 평균에 대응), 화면 픽셀로 6~28 사이 clamp.
      //   저줌 → 화면에서 hit area 작아짐 (정밀 선택), 고줌 → 화면 cap 28px (이웃 노드 충돌 방지).
      const HIT_RADIUS_WORLD = 22;
      const HIT_RADIUS_PX_MIN = 6;
      const HIT_RADIUS_PX_MAX = 28;
      const _wantPx = HIT_RADIUS_WORLD * zoomScale;
      const _pxClamped = Math.max(HIT_RADIUS_PX_MIN, Math.min(HIT_RADIUS_PX_MAX, _wantPx));
      const hitR = _pxClamped / zoomScale;  // viewBox 단위

      const nodesSvg = nodes.map(n => {
        const cx = sx(n.x), cy = sy(n.y);
        const color = roleColor(n.role, n.is_charger, n.is_holding);
        const shape = roleShape(n.role, n.is_charger, n.is_holding, color);
        const rings = [];
        if (n.id === hoveredNodeId) rings.push(`<circle class="node-hover" r="11" fill="none" />`);
        if (selectedNodeIds.has(n.id)) rings.push(`<circle class="node-selected" r="13" fill="none" />`);
        if (n.id === edgeStartNodeId) rings.push(`<circle class="node-edge-start" r="14" fill="none" />`);
        // outer g: translate only (hit circle = world unit). inner g: labelScale (visual = pixel constant).
        return `<g data-node-id="${n.id}" style="cursor:${stampTool === 'inspect' && mode !== 'build' ? 'default' : 'pointer'}"
                   transform="translate(${cx} ${cy})">
          <circle r="${hitR}" fill="transparent" />
          <g transform="scale(${labelScale})">
            ${rings.join('')}
            ${shape}
          </g>
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

      // Add Edge preview line (첫 노드 ~ 커서) + F1d: 방향 화살촉 미리보기
      // edgeStartButton 으로 단방향(좌=0) / 양방향(우=2) 시각 분기. 사용자가 손가락 떼기
      // 전에 어느 방향/유형으로 생성될지 즉시 보임.
      let edgePrevSvg = "";
      if (mode === "build" && buildTool === "edge" && edgeStartNodeId && edgePreviewClient) {
        const src = nodeById.get(edgeStartNodeId);
        if (src) {
          const x1 = sx(src.x), y1 = sy(src.y);
          const x2 = edgePreviewClient.x, y2 = edgePreviewClient.y;
          const pdx = x2 - x1, pdy = y2 - y1;
          const plen = Math.hypot(pdx, pdy) || 1;
          const pang = Math.atan2(pdy, pdx) * 180 / Math.PI;
          const isBidirPreview = (edgeStartButton === 2);
          let prevArrow = "";
          if (plen > 12) {  // 너무 짧으면 화살촉 생략 (잡음 방지)
            if (isBidirPreview) {
              const p1x = x1 + pdx*0.3, p1y = y1 + pdy*0.3;
              const p2x = x1 + pdx*0.7, p2y = y1 + pdy*0.7;
              prevArrow =
                `<g transform="translate(${p1x} ${p1y}) scale(${labelScale}) rotate(${pang + 180})">
                  <polygon points="5,0 -3,-3 -3,3" fill="var(--accent)" opacity="0.85" /></g>` +
                `<g transform="translate(${p2x} ${p2y}) scale(${labelScale}) rotate(${pang})">
                  <polygon points="5,0 -3,-3 -3,3" fill="var(--accent)" opacity="0.85" /></g>`;
            } else {
              // 단방향: 끝점 가까이 (src→cursor)
              const tx = x1 + pdx*0.85, ty = y1 + pdy*0.85;
              prevArrow =
                `<g transform="translate(${tx} ${ty}) scale(${labelScale}) rotate(${pang})">
                  <polygon points="6,0 -3,-4 -3,4" fill="var(--accent)" opacity="0.9" /></g>`;
            }
          }
          edgePrevSvg = `<line class="edge-preview" x1="${x1}" y1="${y1}"
            x2="${x2}" y2="${y2}"
            vector-effect="non-scaling-stroke" />${prevArrow}`;
        }
      }

      // F1b-ux: hover 시 v_max 툴팁 (hover edge 의 중간점에 텍스트)
      let vmaxTipSvg = "";
      if (hoveredEdgeId) {
        const he = edges.find(x => x.id === hoveredEdgeId);
        if (he) {
          const a = nodeById.get(he.src), b = nodeById.get(he.dst);
          if (a && b) {
            const mx = (sx(a.x) + sx(b.x)) / 2;
            const my = (sy(a.y) + sy(b.y)) / 2;
            const label = (he.v_max !== null && he.v_max !== undefined)
              ? `v_max ${Number(he.v_max).toFixed(2)} m/s`
              : "v_max — (Shift+scroll)";
            // labelScale = 1/zoomScale → 폰트는 항상 12px 보여짐
            vmaxTipSvg = `
              <g transform="translate(${mx} ${my - 14}) scale(${labelScale})">
                <rect x="-58" y="-10" width="116" height="18" rx="4"
                      fill="rgba(15,23,42,0.92)" />
                <text x="0" y="3" text-anchor="middle" fill="#fff"
                      font-family="var(--font-mono)" font-size="11"
                      style="pointer-events:none">${label}</text>
              </g>`;
          }
        }
      }

      svg.innerHTML = `<g transform="translate(${panX} ${panY}) scale(${zoomScale})">
        ${edgesSvg}${vmaxLabelsSvg}${nodesSvg}${edgePrevSvg}${trajSvg}${boxSvg}${vmaxTipSvg}
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

    // ── Hover (인스펙터 + 노드/엣지 하이라이트) ──────────────────
    // 인스펙터 grace: edge/노드에서 벗어나도 즉시 비우지 않고 400ms 유예. 그 사이
    // 사용자가 인스펙터 영역(우측 패널)으로 마우스를 옮기면 mouseenter 에서 pin → 유지.
    let _inspectorMouseOver = false;
    let _inspectorClearTimer = null;
    function _emptyInspectorNow() {
      document.getElementById("inspector").innerHTML =
        '<div class="inspector-empty">노드 또는 엣지 위에 마우스를 올리면 정보 표시</div>';
    }
    function scheduleInspectorClear() {
      if (_inspectorClearTimer) clearTimeout(_inspectorClearTimer);
      _inspectorClearTimer = setTimeout(() => {
        _inspectorClearTimer = null;
        if (!hoveredEdgeId && !hoveredNodeId && !_inspectorMouseOver) _emptyInspectorNow();
      }, 400);
    }
    function cancelInspectorClear() {
      if (_inspectorClearTimer) { clearTimeout(_inspectorClearTimer); _inspectorClearTimer = null; }
    }
    document.getElementById("inspector").addEventListener("mouseenter", () => {
      _inspectorMouseOver = true;
      cancelInspectorClear();
    });
    document.getElementById("inspector").addEventListener("mouseleave", () => {
      _inspectorMouseOver = false;
      // 인스펙터 밖으로 나가면 edge hover 가 아직 살아있으면 유지, 아니면 grace 후 비움
      if (!hoveredEdgeId && !hoveredNodeId) scheduleInspectorClear();
    });

    document.getElementById("map").addEventListener("mousemove", (e) => {
      const nodeHit = e.target.closest("[data-node-id]");
      const edgeHit = e.target.closest("[data-edge-id]");
      const prevHoverNode = hoveredNodeId;
      const prevHoverEdge = hoveredEdgeId;
      if (nodeHit) {
        const nid = nodeHit.dataset.nodeId;
        if (nid !== hoveredNodeId) {
          hoveredNodeId = nid;
          hoveredEdgeId = "";
          showInspectorNode(nodeById.get(nid));
        }
        cancelInspectorClear();
      } else if (edgeHit) {
        const eid = edgeHit.dataset.edgeId;
        if (eid !== hoveredEdgeId) {
          // edge 가 바뀐 경우에만 인스펙터 재생성 (input focus / 부분 입력값 보존)
          hoveredNodeId = "";
          hoveredEdgeId = eid;
          // pinned 상태에서는 hover 가 바뀌어도 인스펙터 유지 (pinned edge 정보 계속 표시)
          if (!pinnedEdgeId) showInspectorEdge(edges.find(x => x.id === eid));
        }
        cancelInspectorClear();
      } else {
        hoveredNodeId = "";
        hoveredEdgeId = "";
        // pinned 또는 인스펙터에 마우스 있는 동안은 비우지 않음. 빈 공간이면 grace 후 비움.
        if (!_inspectorMouseOver && !pinnedEdgeId) scheduleInspectorClear();
      }
      // hover 변화 시 재렌더 (drag 중 아닐 때만)
      const changed = (prevHoverNode !== hoveredNodeId) || (prevHoverEdge !== hoveredEdgeId);
      if (changed && !paintTrajectory && !isPanning) {
        render();
      }
    });
    // SVG 밖으로 나가면 visual hover 만 해제, 인스펙터는 grace (pinned 면 유지)
    document.getElementById("map").addEventListener("mouseleave", () => {
      const had = hoveredEdgeId || hoveredNodeId;
      hoveredEdgeId = "";
      hoveredNodeId = "";
      if (had && !paintTrajectory && !isPanning) {
        render();
        if (!_inspectorMouseOver && !pinnedEdgeId) scheduleInspectorClear();
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
      const curVal = (ed.v_max !== null && ed.v_max !== undefined)
        ? Number(ed.v_max).toFixed(2) : "";
      // F1b-ux (rev2): 정확 조작용 +/- 버튼 + number input + 해제 버튼.
      // scroll 은 보조 (부호 노이즈 디바이스에선 불안정).
      const vmaxEditor = `
        <div style="display:flex; align-items:center; gap:4px;">
          <button data-vmax-act="dec" title="−0.1"
                  style="width:24px; height:22px; padding:0; border:1px solid var(--border-strong);
                         background:var(--surface-2); border-radius:4px; cursor:pointer;">−</button>
          <input type="number" data-vmax-input id="vmax-input"
                 value="${curVal}" placeholder="—" min="0.1" max="1.5" step="0.1"
                 style="width:64px; padding:2px 4px; font-family:var(--font-mono); font-size:11px;
                        text-align:center; border:1px solid var(--border-strong); border-radius:4px;" />
          <button data-vmax-act="inc" title="+0.1"
                  style="width:24px; height:22px; padding:0; border:1px solid var(--border-strong);
                         background:var(--surface-2); border-radius:4px; cursor:pointer;">+</button>
          <span style="color:var(--muted); font-size:10.5px; margin-left:2px;">m/s</span>
          <button data-vmax-act="clear" title="해제(미설정으로)"
                  style="margin-left:auto; padding:2px 6px; font-size:10.5px; border:1px solid var(--border);
                         background:transparent; color:var(--muted); border-radius:4px; cursor:pointer;">×</button>
        </div>
        <div style="font-size:10.5px; color:var(--muted); margin-top:4px;">
          범위 0.1 ~ 1.5 m/s · Shift+scroll 도 가능
        </div>`;
      document.getElementById("inspector").innerHTML = `
        <div class="inspector-row"><span class="k">type</span><span class="v">↔ EDGE</span></div>
        <div class="inspector-row"><span class="k">id</span><span class="v">${ed.id}</span></div>
        <div class="inspector-row"><span class="k">from → to</span><span class="v">${ed.src} → ${ed.dst}</span></div>
        <div class="inspector-row"><span class="k">direction</span><span class="v">${ed.bidir ? '↔ bidirectional' : '→ unidirectional'}</span></div>
        <div class="inspector-row"><span class="k">corridor</span><span class="v">${ed.corridor || '(unlabeled)'}</span></div>
        <div class="inspector-row"><span class="k">access</span><span class="v">${ed.access || '—'}</span></div>
        <div class="inspector-row" style="flex-direction:column; align-items:stretch; gap:6px;">
          <span class="k">v_max</span>
          ${vmaxEditor}
        </div>
      `;
      // 버튼/input 핸들러 바인딩 (hover edge 가 살아있는 동안만 유효)
      const inspector = document.getElementById("inspector");
      const targetId = ed.id;
      function findEdge() { return edges.find(x => x.id === targetId); }
      inspector.querySelectorAll("[data-vmax-act]").forEach(btn => {
        btn.addEventListener("click", () => {
          const edge = findEdge(); if (!edge) return;
          const act = btn.dataset.vmaxAct;
          if (act === "clear") {
            if (edge.v_max == null) return;
            pushHistoryAndApply(edge, null);
          } else {
            const dir = (act === "inc") ? +1 : -1;
            const cur = (edge.v_max == null) ? 1.0 : Number(edge.v_max);
            const next = clampToStep(cur + dir * VMAX_STEP, VMAX_MIN, VMAX_MAX);
            if (edge.v_max == null || Math.abs(next - Number(edge.v_max)) > 1e-9) {
              pushHistoryAndApply(edge, next);
            }
          }
          showInspectorEdge(findEdge());
        });
      });
      const inputEl = inspector.querySelector("[data-vmax-input]");
      if (inputEl) {
        inputEl.addEventListener("change", () => {
          const edge = findEdge(); if (!edge) return;
          const v = inputEl.value.trim();
          if (v === "") {
            if (edge.v_max != null) pushHistoryAndApply(edge, null);
          } else {
            const num = parseFloat(v);
            if (!Number.isFinite(num)) { inputEl.value = (edge.v_max == null ? "" : Number(edge.v_max).toFixed(2)); return; }
            const clamped = Math.max(VMAX_MIN, Math.min(VMAX_MAX, num));
            const stepped = Math.round(clamped / VMAX_STEP) * VMAX_STEP;
            const next = Number(stepped.toFixed(2));
            if (edge.v_max == null || Math.abs(next - Number(edge.v_max)) > 1e-9) {
              pushHistoryAndApply(edge, next);
            }
          }
          showInspectorEdge(findEdge());
        });
      }
    }

    // ── Pan / Zoom ─────────────────────────────────────────────
    // F1b-ux (rev): scroll 기본 = zoom. v_max 편집은 Shift+scroll on hovered edge 로만.
    //   - 기본 scroll → zoom (어느 줌 레벨이든 항상)
    //   - Shift + scroll on hovered edge → v_max 0.1 ~ 1.5 자유 조절 (0.1 step)
    //     · 0.6 ↔ 0.7 경계 없음 — 전 범위 자유 이동
    //     · 미설정(null)이면 1.0 부터 시작
    const VMAX_STEP = 0.1;
    const VMAX_MIN = 0.1, VMAX_MAX = 1.5;
    function pushHistoryAndApply(edge, newVMax) {
      history.past.push(snapshot());
      history.future = [];
      edge.v_max = newVMax;
      render();
    }
    function clampToStep(v, lo, hi) {
      const stepped = Math.round(v / VMAX_STEP) * VMAX_STEP;
      return Math.min(hi, Math.max(lo, Number(stepped.toFixed(2))));
    }
    // Wheel 누적기 — 마우스(deltaMode=0 pixel ~100/click, mode=1 line ~3/click) 와
    // trackpad(mode=0 작은 값 ~±1~10 다발) 양쪽 모두 자연스럽게 1 tick씩 처리.
    //   1. deltaMode 정규화 (line/page → pixel)
    //   2. 누적값이 ±WHEEL_ACCUM_THRESHOLD 마다 1 tick
    //   3. 200ms 무동작 시 누적 리셋 (다른 swipe 동작과 섞이지 않게)
    let _wheelAccum = 0;
    let _wheelResetTimer = null;
    const WHEEL_ACCUM_THRESHOLD = 80;  // 마우스 한 click(~100px)에 1 tick, trackpad 짧은 swipe는 누적 후
    document.getElementById("map").addEventListener("wheel", (e) => {
      // Speed 모드 + edge hover → v_max 편집. modifier 의존 없음.
      // (Shift+scroll 방식은 키 release timing 문제로 폐기됨)
      if (mode === "speed" && hoveredEdgeId) {
        const edge = edges.find(x => x.id === hoveredEdgeId);
        if (edge) {
          e.preventDefault();
          // deltaMode 정규화
          let scaled = e.deltaY;
          if (e.deltaMode === 1) scaled *= 33;     // line → pixel
          else if (e.deltaMode === 2) scaled *= 400; // page → pixel
          _wheelAccum += scaled;
          if (_wheelResetTimer) clearTimeout(_wheelResetTimer);
          _wheelResetTimer = setTimeout(() => { _wheelAccum = 0; }, 200);
          let ticks = 0;
          while (Math.abs(_wheelAccum) >= WHEEL_ACCUM_THRESHOLD) {
            const s = Math.sign(_wheelAccum);
            ticks -= s;  // deltaY > 0 (아래로 굴림) → 감소
            _wheelAccum -= s * WHEEL_ACCUM_THRESHOLD;
          }
          if (ticks === 0) return;
          const cur = (edge.v_max == null) ? 1.0 : Number(edge.v_max);
          const next = clampToStep(cur + ticks * VMAX_STEP, VMAX_MIN, VMAX_MAX);
          if (edge.v_max == null || Math.abs(next - Number(edge.v_max)) > 1e-9) {
            pushHistoryAndApply(edge, next);
            showInspectorEdge(edge);
          }
          return;
        }
      }
      // default: zoom (모든 위치/줌 레벨)
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

    // 휠 버튼 클릭 시 브라우저 autoscroll 방지
    document.getElementById("map").addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });
    document.getElementById("map").addEventListener("pointerdown", (e) => {
      const onNode = !!e.target.closest("[data-node-id]");
      const onEdge = !onNode && !!e.target.closest("[data-edge-id]");
      const svg = e.currentTarget;
      const pt = clientToSvg(svg, e.clientX, e.clientY);
      const localX = (pt.x - panX) / zoomScale;
      const localY = (pt.y - panY) / zoomScale;

      // ★ 휠(가운데) 버튼 = 어떤 모드든 pan (Photoshop / IDE 표준)
      if (e.button === 1) {
        isPanning = true;
        panStart = {x: e.clientX, y: e.clientY, panX, panY};
        document.querySelector(".map-shell").classList.add("dragging");
        e.preventDefault();
        return;
      }
      if (e.button !== 0 && e.button !== 2) return;

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

      // Speed 모드: edge 좌클릭 → 인스펙터 pin (마우스 떠도 +/- 사용 유지)
      if (mode === "speed") {
        if (onEdge && e.button === 0) {
          const eid = e.target.closest("[data-edge-id]").dataset.edgeId;
          pinnedEdgeId = (pinnedEdgeId === eid) ? "" : eid;  // 같은 edge 재클릭 → unpin
          const ed = edges.find(x => x.id === eid);
          if (ed) showInspectorEdge(ed);
          render();
          return;
        }
        // 빈 공간 좌클릭 → pin 해제 + 평소 pan
        if (!onEdge && !onNode && e.button === 0) {
          if (pinnedEdgeId) { pinnedEdgeId = ""; render(); }
          isPanning = true;
          panStart = {x: e.clientX, y: e.clientY, panX, panY};
          document.querySelector(".map-shell").classList.add("dragging");
          return;
        }
        return;
      }

      if (mode === "build") {
        // Build 도구별 분기 (pointerdown 즉시 액션)
        if (buildTool === "node") {
          // 빈 공간 좌클릭만 — 노드 추가 (Shift = 직전 노드 축 고정)
          if (!onNode && !onEdge && e.button === 0) {
            addNodeAt(localX, localY, {shiftAxis: e.shiftKey});
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
          // F1d: edge 위 좌클릭 (그리는 중 아닐 때) = 단↔양방향 토글
          else if (onEdge && !edgeStartNodeId && e.button === 0) {
            const edgeId = e.target.closest("[data-edge-id]").dataset.edgeId;
            toggleEdgeBidir(edgeId);
            render();
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
    function addNodeAt(svgX, svgY, opts) {
      pushHistory();
      let {x, y} = svgToData(svgX, svgY);
      const shiftAxis = !!(opts && opts.shiftAxis);
      // F1e: grid snap (data 좌표 기준)
      if (gridSnapEnabled && gridSize > 0) {
        x = Math.round(x / gridSize) * gridSize;
        y = Math.round(y / gridSize) * gridSize;
      }
      // F1e: Shift = 직전 노드의 X 또는 Y 축 고정 (커서가 가까운 축으로)
      if (shiftAxis && lastPlacedDataXY) {
        const dx = Math.abs(x - lastPlacedDataXY.x);
        const dy = Math.abs(y - lastPlacedDataXY.y);
        if (dx < dy) x = lastPlacedDataXY.x;   // 수직 정렬 (X 고정)
        else         y = lastPlacedDataXY.y;   // 수평 정렬 (Y 고정)
      }
      const id = nextNodeId();
      const newNode = {
        id, x, y, name: id,
        role: "standard", is_charger: false, is_holding: false,
        degree_in: 0, degree_out: 0,
      };
      nodes.push(newNode);
      nodeById.set(id, newNode);
      lastPlacedDataXY = {x, y};
      const hint = (shiftAxis ? " [axis]" : "") + (gridSnapEnabled ? ` [grid ${gridSize}]` : "");
      showToast(`+ Node ${id} at (${x.toFixed(1)}, ${y.toFixed(1)})${hint}`);
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
    // F1d: 단↔양방향 토글
    function toggleEdgeBidir(edgeId) {
      const edge = edges.find(e => e.id === edgeId);
      if (!edge) return;
      pushHistory();
      edge.bidir = !edge.bidir;
      showToast(`Edge ${edge.src} ${edge.bidir ? '↔' : '→'} ${edge.dst}`);
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

    // ── Save / Save & Run ─────────────────────────────────────
    async function saveEdits(opts = {}) {
      const edits = exportEdits();
      // 1) 다운로드 (항상)
      if (opts.download !== false) downloadEdits(edits);
      // 2) 서버 메모리 갱신 (server_map_id 가 있을 때만)
      if (SERVER_MAP_ID) {
        try {
          const resp = await fetch(`/update-map/${SERVER_MAP_ID}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({edits}),
          });
          if (!resp.ok) throw new Error(await resp.text());
          const data = await resp.json();
          showToast(`Saved + applied (chargers=${data.stats.chargers}, stations=${data.stats.stations})`);
        } catch (err) {
          showToast(`Saved (download only) — server update 실패: ${err.message}`);
        }
      } else {
        showToast("Saved (file download)");
      }
    }
    document.getElementById("btn-save").addEventListener("click", () => { saveEdits(); });
    document.getElementById("btn-save-run").addEventListener("click", async () => {
      await saveEdits({download: false});  // 서버에 적용만 (다운로드 생략)
      if (SERVER_MAP_ID) {
        // Quickrun 페이지로 이동 — 이미 떠 있으면 새로고침으로 옵션 갱신됨
        window.location.href = "/";
      } else {
        showToast("Server 없음 — 먼저 ./run quickrun 으로 띄우세요");
      }
    });

    function exportEdits() {
      // 원본 vs 현재 diff 를 정확하게 기록.
      // 임포트 시 apply_edits(original, edits) 로 재현 가능한 형태.
      const currentNodeIds = new Set(nodes.map(n => n.id));
      const currentEdgeIds = new Set(edges.map(e => e.id));

      // 삭제된 항목 = 원본에 있었지만 현재 없는 것
      const deleted_node_ids = [];
      for (const id of ORIGINAL_NODES.keys()) {
        if (!currentNodeIds.has(id)) deleted_node_ids.push(id);
      }
      const deleted_edge_ids = [];
      for (const id of ORIGINAL_EDGES.keys()) {
        if (!currentEdgeIds.has(id)) deleted_edge_ids.push(id);
      }

      // 추가된 항목 = 원본에 없던 것 (Build 모드로 생성)
      const added_nodes = [];
      for (const n of nodes) {
        if (!ORIGINAL_NODES.has(n.id)) {
          added_nodes.push({
            id: n.id, x: n.x, y: n.y, name: n.name || n.id,
            role: n.role || "standard",
            is_charger: !!n.is_charger,
            is_holding: !!n.is_holding,
          });
        }
      }
      const added_edges = [];
      for (const e of edges) {
        if (!ORIGINAL_EDGES.has(e.id)) {
          const payload = {id: e.id, src: e.src, dst: e.dst, bidir: !!e.bidir};
          if (e.v_max !== null && e.v_max !== undefined) payload.v_max = e.v_max;
          added_edges.push(payload);
        }
      }

      // override = 원본에 있었지만 속성이 바뀐 것
      const node_overrides = {};
      for (const n of nodes) {
        const orig = ORIGINAL_NODES.get(n.id);
        if (!orig) continue;
        const diff = {};
        if ((n.role || "standard") !== (orig.role || "standard")) diff.role = n.role;
        if (!!n.is_charger !== !!orig.is_charger) diff.is_charger = !!n.is_charger;
        if (!!n.is_holding !== !!orig.is_holding) diff.is_holding = !!n.is_holding;
        if (Object.keys(diff).length > 0) node_overrides[n.id] = diff;
      }
      const edge_overrides = {};
      for (const e of edges) {
        const orig = ORIGINAL_EDGES.get(e.id);
        if (!orig) continue;
        const diff = {};
        if (!!e.bidir !== !!orig.bidir) diff.bidir = !!e.bidir;
        // 단방향에서 src/dst 가 스왑됐는지 (Paint Alt 또는 Build edge)
        if (e.src !== orig.src || e.dst !== orig.dst) {
          diff.src = e.src; diff.dst = e.dst;
        }
        // F1b-ux: v_max diff — null/undefined 도 unset 의도로 명시 기록
        const curV = (e.v_max === null || e.v_max === undefined) ? null : Number(e.v_max);
        const origV = (orig.v_max === null || orig.v_max === undefined) ? null : Number(orig.v_max);
        if (curV !== origV) diff.v_max = curV;
        if (Object.keys(diff).length > 0) edge_overrides[e.id] = diff;
      }

      return {
        format_version: 1,
        source: SOURCE_NAME,
        timestamp: new Date().toISOString(),
        counts: {
          deleted_nodes: deleted_node_ids.length,
          deleted_edges: deleted_edge_ids.length,
          added_nodes: added_nodes.length,
          added_edges: added_edges.length,
          overridden_nodes: Object.keys(node_overrides).length,
          overridden_edges: Object.keys(edge_overrides).length,
        },
        deleted_node_ids,
        deleted_edge_ids,
        added_nodes,
        added_edges,
        node_overrides,
        edge_overrides,
      };
    }

    function downloadEdits(edits) {
      const blob = new Blob([JSON.stringify(edits, null, 2)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${SOURCE_NAME}.edit.json`;
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
