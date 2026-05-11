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
          <button data-mode="single">Single <span class="hint-key">N</span></button>
        </div>
        <div style="font-size:10.5px; color:var(--muted); margin-top:6px;">
          숫자키 2~5 누르면 자동으로 Stamp 모드로 전환됩니다
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
    let mode = "paint";              // paint / stamp / single
    let stampTool = "inspect";       // inspect / station / charger / holding / siding / reset
    let paintTrajectoryButton = -1;  // 진행 중 trajectory 의 마우스 버튼 (0=좌, 2=우)
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

    // ── 검증 패널 ───────────────────────────────────────────────
    document.getElementById("vstat-cc").textContent = report.connected_components;
    document.getElementById("vstat-iso").textContent = report.isolated_nodes.length;
    document.getElementById("vstat-de").textContent = report.dead_end_nodes.length;
    const vlist = document.getElementById("validation-list");
    if (report.warnings.length === 0) {
      vlist.innerHTML = `<div class="warn-item info">✓ 검증 통과</div>`;
    } else {
      vlist.innerHTML = report.warnings.map(w =>
        `<div class="warn-item ${w.severity}"><strong>[${w.severity}]</strong> ${w.message}</div>`
      ).join("");
    }

    // ── 모드 토글 ───────────────────────────────────────────────
    function setMode(newMode) {
      mode = newMode;
      document.querySelectorAll("#mode-toggle button").forEach(b =>
        b.classList.toggle("active", b.dataset.mode === newMode));
      document.getElementById("paint-options").style.display = (newMode === "paint") ? "" : "none";
      // Paint 모드에서 다른 모드로 가면 진행 중인 trajectory cancel
      if (newMode !== "paint") { paintTrajectory = null; render(); }
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
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      const stampMap = {"1":"inspect","2":"station","3":"charger","4":"holding","5":"siding","0":"reset"};
      if (stampMap[e.key]) {
        selectStamp(stampMap[e.key]);
        // ★ 자동 모드 전환: stamp 도구를 누르면 Stamp 모드로 가야 클릭이 stamp 가 됨
        setMode("stamp");
        e.preventDefault();
        return;
      }
      if (e.key === "p" || e.key === "P") { setMode("paint"); e.preventDefault(); return; }
      if (e.key === "s" || e.key === "S") { setMode("stamp"); e.preventDefault(); return; }
      if (e.key === "n" || e.key === "N") { setMode("single"); e.preventDefault(); return; }
      if (e.key === "Escape") {
        selectStamp("inspect");
        setMode("paint");
        paintTrajectory = null;
        render();
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
        const hoverRing = (n.id === hoveredNodeId)
          ? `<circle class="node-hover" r="11" fill="none" />` : "";
        // Click target r=22 (이전 r=14) — 클릭 미스 줄임. transparent 라 시각 영향 없음.
        return `<g data-node-id="${n.id}" style="cursor:${stampTool === 'inspect' ? 'default' : 'pointer'}"
                   transform="translate(${cx} ${cy}) scale(${labelScale})">
          <circle r="22" fill="transparent" /> <!-- expanded click target -->
          ${hoverRing}
          ${shape}
        </g>`;
      }).join("");

      // Paint trajectory preview (드래그 중에만)
      const trajSvg = (paintTrajectory && paintTrajectory.length >= 2)
        ? `<polyline class="traj-preview" points="${paintTrajectory.map(p => `${p.x},${p.y}`).join(' ')}" />`
        : "";

      svg.innerHTML = `<g transform="translate(${panX} ${panY}) scale(${zoomScale})">
        ${edgesSvg}${nodesSvg}${trajSvg}
      </g>`;
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
      // 좌(0) / 우(2) 만 처리. 가운데 버튼은 무시.
      if (e.button !== 0 && e.button !== 2) return;
      const onNode = !!e.target.closest("[data-node-id]");
      const svg = e.currentTarget;
      const pt = clientToSvg(svg, e.clientX, e.clientY);

      // ── 분기 ──────────────────────────────────────────────
      // 1) Space 누르고 있으면 → 무조건 pan (어느 모드든, 좌클릭만)
      // 2) Paint 모드 → trajectory 그리기 시작 (좌=단방향, 우=양방향)
      // 3) Stamp 모드 + 노드 위 → drag 시작 안 함 (click/contextmenu 에서 처리)
      // 4) Stamp 모드 + 빈 공간 + 좌클릭 → pan
      if (spaceKeyDown && e.button === 0) {
        isPanning = true;
        panStart = {x: e.clientX, y: e.clientY, panX, panY};
        document.querySelector(".map-shell").classList.add("dragging");
        return;
      }
      if (mode === "paint") {
        // 좌/우 모두 trajectory. mouseup 에서 button 보고 단방/양방 결정.
        paintTrajectory = [{x: (pt.x - panX) / zoomScale, y: (pt.y - panY) / zoomScale}];
        paintTrajectoryButton = e.button;
        e.preventDefault();
        return;
      }
      // Stamp / Single 모드
      if (onNode) return; // 노드 클릭은 click / contextmenu 핸들러로 처리
      if (e.button !== 0) return; // 우클릭은 pan 안 함
      isPanning = true;
      panStart = {x: e.clientX, y: e.clientY, panX, panY};
      document.querySelector(".map-shell").classList.add("dragging");
    });

    document.getElementById("map").addEventListener("pointermove", (e) => {
      if (isPanning && panStart) {
        const svg = e.currentTarget;
        const rect = svg.getBoundingClientRect();
        panX = panStart.panX + (e.clientX - panStart.x) * (VW / rect.width);
        panY = panStart.panY + (e.clientY - panStart.y) * (VH / rect.height);
        render();
        return;
      }
      if (paintTrajectory) {
        const svg = e.currentTarget;
        const pt = clientToSvg(svg, e.clientX, e.clientY);
        // SVG 좌표를 viewport 좌표 (zoom/pan 적용 전) 로 환산
        paintTrajectory.push({x: (pt.x - panX) / zoomScale, y: (pt.y - panY) / zoomScale});
        render();
      }
    });

    document.getElementById("map").addEventListener("pointerup", (e) => {
      document.querySelector(".map-shell").classList.remove("dragging");
      if (isPanning) {
        isPanning = false; panStart = null;
        return;
      }
      if (paintTrajectory) {
        if (paintTrajectory.length >= 2) {
          // 마우스 버튼이 곧 방향: 좌(0)=단방향, 우(2)=양방향
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
        const isBidir = (paintTrajectoryButton === 2);
        applyPaint(paintTrajectory, isBidir, false);
      }
      paintTrajectory = null;
      paintTrajectoryButton = -1;
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

    // ── 노드 클릭 (Stamp 모드일 때만 stamp 적용) ───────────────
    // Paint 모드일 땐 click 이벤트 무시 — pointerdown/up 에서 trajectory 처리됨
    document.getElementById("map").addEventListener("click", (e) => {
      if (mode === "paint") return;
      const nodeHit = e.target.closest("[data-node-id]");
      if (!nodeHit) return;
      if (stampTool === "inspect") return;
      const n = nodeById.get(nodeHit.dataset.nodeId);
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
