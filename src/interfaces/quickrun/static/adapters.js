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
      id: n.id,
      x: n.x,
      y: n.y,
      type: n.kind || "wp",
      label: n.id, // backend id 가 의미 있는 라벨 (WP_C_000, ST_N_01 등)
    }));
    const edges = (rawMap.edges || []).map((e) => ({
      id: e.id,
      a: e.src,
      b: e.dst,
      directed: e.directed !== false,
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
    };
  }

  async function control(runId, action) {
    const resp = await fetch(backendBase() + "/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId, action }),
    });
    return resp.ok;
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

  // ── 노출 ────────────────────────────────────────────────────
  window.QuickRunAdapter = {
    init,
    control,
    connectStream,
    mapState,
    UI_STATES,
  };
})();
