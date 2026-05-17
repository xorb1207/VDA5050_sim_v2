/* Quick Sim 프론트 어댑터.
 * - mapAdapter: backend /init 호출, 응답을 UI 가 쓰는 형태로 정규화.
 * - engineAdapter: WebSocket 으로 tick snapshot 구독.
 * - stateMapping: backend AGV state → UI 4종 (그대로 패스, backend 가 이미 매핑함).
 *
 * 목업이 기대하는 데이터 형태:
 *   nodes: [{id, x, y, type:"wp"|"station"|"charger"|"holding"|"siding", label}]
 *   edges: [{id, a, b, directed?}]
 *   idx:   {[node_id]: node}
 *
 * backend 가 주는 데이터:
 *   nodes: [{id, x, y, kind}]
 *   edges: [{id, src, dst, directed, corridor, speed}]
 */
(function () {
  function backendBase() {
    // 같은 origin 으로 요청. 개발 시 다른 host 면 환경변수로 override.
    return window.QUICKRUN_BACKEND || "";
  }

  function wsBase() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return window.QUICKRUN_WS || (proto + "//" + window.location.host);
  }

  // ── Map adapter ─────────────────────────────────────────────
  // backend nodes/edges → 목업 형태로 변환. label 은 id 마지막 토큰.
  function normalizeMap(rawMap) {
    const nodes = (rawMap.nodes || []).map((n) => ({
      id: n.node_id || n.id,
      x: n.x,
      y: n.y,
      type: n.kind || n.type || "wp",
      label: n.node_id || n.id,
    }));
    const edges = (rawMap.edges || []).map((e) => ({
      id: e.edge_id || e.id,
      key: e.edge_key || `${e.start_node_id || e.src}__${e.end_node_id || e.dst}`,
      a: e.start_node_id || e.src,
      b: e.end_node_id || e.dst,
      directed: e.bidirectional === false ? false : (e.directed !== false),
      corridor: e.corridor || "",
    }));
    const idx = {};
    for (const n of nodes) idx[n.id] = n;
    return {
      nodes,
      edges,
      idx,
      viewBox: rawMap.viewBox || [0, 0, 1000, 600],
    };
  }

  async function init(params) {
    const body = {
      topology: params.topology || "A",
      agvCount: params.agvCount || 12,
      speed: params.speed || 2.0,
      duration: params.duration || 600,
      blockedEdges: Array.from(params.blockedEdges || []),
    };
    // F1a: 임포트 맵 + fleet 별 AGV 수 override (있을 때만)
    if (params.importedMapId) body.importedMapId = params.importedMapId;
    if (params.agvCountByFleet) body.agvCountByFleet = params.agvCountByFleet;
    const resp = await fetch(backendBase() + "/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error("init failed: " + resp.status + " " + text);
    }
    const data = await resp.json();
    return {
      runId: data.runId,
      map: normalizeMap(data.map),
      wsUrl: data.wsUrl, // path. ws base 는 별도 prepend.
      fleets: data.fleets || [], // F1a: [{id, color, graph_idx, count, agv_ids}, ...]
    };
  }

  // F1a: 업로드된 임포트 맵 목록 + fleet 정의 조회 (UI 가 슬라이더 빌드용)
  async function listImportedMaps() {
    const resp = await fetch(backendBase() + "/imported-maps");
    if (!resp.ok) return [];
    return await resp.json();
  }

  async function control(runId, action) {
    const resp = await fetch(backendBase() + "/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId, action }),
    });
    return resp.ok;
  }

  // GAP-B: 수동 Job 발행. body: {pickup_node, dropoff_node, required_capability?, runId?}
  // 반환: {ok, demand_id, agv_id, status, reason}
  async function dispatchManualJob(req) {
    const resp = await fetch(backendBase() + "/manual-job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      return { ok: false, status: "rejected", reason: data.detail || ("HTTP " + resp.status) };
    }
    return data;
  }

  // ── Engine adapter ──────────────────────────────────────────
  // WS 구독. onTick(snapshot), onEnd(reason).
  // 반환값: {disconnect()}.
  function connectStream(wsPath, callbacks) {
    const url = wsBase() + wsPath;
    const ws = new WebSocket(url);
    let alive = true;
    ws.onopen = () => {
      if (callbacks.onOpen) callbacks.onOpen();
    };
    ws.onmessage = (ev) => {
      if (!alive) return;
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "tick" && callbacks.onTick) callbacks.onTick(msg);
        else if (msg.type === "end" && callbacks.onEnd) callbacks.onEnd(msg);
      } catch (e) {
        console.error("bad ws msg", e, ev.data);
      }
    };
    ws.onerror = (e) => {
      console.error("ws error", e);
      if (callbacks.onError) callbacks.onError(e);
    };
    ws.onclose = () => {
      alive = false;
      if (callbacks.onClose) callbacks.onClose();
    };
    return {
      disconnect() {
        alive = false;
        try { ws.close(); } catch (e) {}
      },
      // ws 가 살아있나 확인
      isOpen() { return ws.readyState === WebSocket.OPEN; },
    };
  }

  // ── State mapping ───────────────────────────────────────────
  // backend 가 이미 4종으로 매핑해서 보내므로 패스. 안전망용 fallback.
  const UI_STATES = ["NAVIGATING", "WAITING", "PROCESSING", "CHARGING"];
  function mapState(s) {
    return UI_STATES.indexOf(s) >= 0 ? s : "WAITING";
  }

  // ── F1a: fleet color helper ────────────────────────────────
  // fleets: [{id, color, agv_ids:[...]}]  → fn(agv) → "#hex" or fallback.
  function makeFleetColorLookup(fleets) {
    const byId = new Map();
    const byAgv = new Map();
    for (const fl of (fleets || [])) {
      byId.set(fl.id, fl);
      for (const aid of (fl.agv_ids || [])) byAgv.set(aid, fl);
    }
    return function colorOf(agv) {
      const fl = (agv.fleet_id && byId.get(agv.fleet_id)) || byAgv.get(agv.agv_id || agv.id);
      return (fl && fl.color) ? fl.color : "#3a4555";
    };
  }

  // ── 노출 ────────────────────────────────────────────────────
  window.QuickRunAdapter = {
    init,
    control,
    connectStream,
    listImportedMaps,
    dispatchManualJob,
    makeFleetColorLookup,
    mapState,
    UI_STATES,
  };
})();
