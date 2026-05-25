/* Quick Sim 프론트 — React 컴포넌트.
 * 목업 디자인을 유지하면서 backend WS/REST 결선.
 * window.QuickRunAdapter 의 init/connectStream/control 사용.
 *
 * F1a: fleet 색 AGV 마커 + Fleet KPI 카드 + 임포트 맵 선택 + fleet 별 count 슬라이더
 */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// 상태 색상 (목업 디자인 그대로)
const STATE_COLORS = {
  NAVIGATING: "#cfd3df",
  WAITING:    "#ef4f5e",
  PROCESSING: "#27c281",
  CHARGING:   "#3a8dff",
};

// 노드 타입 → SVG 마커 스타일
function NodeMarker({ node, sx, sy }) {
  const cx = sx(node.x);
  const cy = sy(node.y);
  if (node.type === "station") {
    return <g><circle cx={cx} cy={cy} r="6" fill="var(--green)" stroke="#0f1117" strokeWidth="1.5" /></g>;
  }
  if (node.type === "charger") {
    return <g><rect x={cx-5} y={cy-5} width="10" height="10" fill="var(--blue)" stroke="#0f1117" strokeWidth="1" /></g>;
  }
  if (node.type === "holding") {
    return <g><circle cx={cx} cy={cy} r="6" fill="none" stroke="#fff" strokeWidth="2" /></g>;
  }
  if (node.type === "siding") {
    return <g><circle cx={cx} cy={cy} r="4" fill="var(--yellow)" /></g>;
  }
  return <circle cx={cx} cy={cy} r="2" fill="#5a6178" opacity="0.7" />;
}

// 엣지: 단방향이면 화살표, 양방향이면 단순 선
function EdgeLine({ edge, src, dst, sx, sy, blocked, onClick, interactive }) {
  if (!src || !dst) return null;
  const x1 = sx(src.x), y1 = sy(src.y);
  const x2 = sx(dst.x), y2 = sy(dst.y);
  const stroke = blocked ? "#ef4f5e" : "#363c4d";
  const opacity = blocked ? 1 : 0.85;
  return (
    <g style={{ cursor: interactive ? "pointer" : "default" }}
       onClick={interactive ? () => onClick(edge.id) : undefined}>
      {/* 클릭 영역 보강용 투명 굵은 선 */}
      {interactive && (
        <line x1={x1} y1={y1} x2={x2} y2={y2}
              stroke="transparent" strokeWidth="10" />
      )}
      <line x1={x1} y1={y1} x2={x2} y2={y2}
            stroke={stroke} strokeWidth={blocked ? 2.4 : 1.4} opacity={opacity}
            markerEnd={edge.directed ? "url(#arrow)" : undefined} />
    </g>
  );
}

// AGV 마커: 상태별 색(fill) + fleet 색(ring) + WAITING 펄스
// F1a: colorOf(agv) → fleet color hex. single fleet 시 ring 없음.
function AgvMarker({ agv, sx, sy, colorOf, multiFleet }) {
  const cx = sx(agv.x), cy = sy(agv.y);
  const stateColor = STATE_COLORS[agv.state] || "#cfd3df";
  const fleetColor = (multiFleet && colorOf) ? colorOf(agv) : null;
  const isWaiting = agv.state === "WAITING";
  return (
    <g>
      {isWaiting && (
        <circle cx={cx} cy={cy} r="9" fill="none" stroke={stateColor} strokeWidth="1.5" opacity="0.6">
          <animate attributeName="r" values="6;12;6" dur="1.1s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.7;0.1;0.7" dur="1.1s" repeatCount="indefinite" />
        </circle>
      )}
      {/* F1a: fleet color ring (multi-fleet 때만) */}
      {fleetColor && (
        <circle cx={cx} cy={cy} r="7" fill="none" stroke={fleetColor} strokeWidth="2" opacity="0.85" />
      )}
      <circle cx={cx} cy={cy} r="4.5" fill={stateColor} stroke="#0f1117" strokeWidth="1" />
    </g>
  );
}

function MapView({ map, agvs, blocked, onToggleEdge, interactive, colorOf, fleets }) {
  if (!map) {
    return (
      <div style={{ height: "100%", display:"flex", alignItems:"center", justifyContent:"center",
                    color:"#5a6178", fontSize:13 }}>
        맵 로딩 중...
      </div>
    );
  }
  const [vx, vy, vw, vh] = map.viewBox;
  const sx = (x) => x;
  const sy = (y) => y;
  const multiFleet = (fleets || []).length > 1;

  return (
    <svg viewBox={`${vx} ${vy} ${vw} ${vh}`}
         style={{ width:"100%", height:"100%", display:"block", background:"#0f1117" }}
         preserveAspectRatio="xMidYMid meet">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
                markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill="#363c4d" />
        </marker>
      </defs>
      {/* 엣지 먼저 (배경) */}
      {map.edges.map(edge => (
        <EdgeLine key={edge.id} edge={edge}
                  src={map.idx[edge.a]} dst={map.idx[edge.b]}
                  sx={sx} sy={sy}
                  blocked={blocked.has(edge.id)}
                  onClick={onToggleEdge}
                  interactive={interactive} />
      ))}
      {/* 노드 */}
      {map.nodes.map(n => (
        <NodeMarker key={n.id} node={n} sx={sx} sy={sy} />
      ))}
      {/* AGV (최상단) */}
      {(agvs || []).map(a => (
        <AgvMarker key={a.id || a.agv_id} agv={a} sx={sx} sy={sy}
                   colorOf={colorOf} multiFleet={multiFleet} />
      ))}
      {/* F1a: fleet legend (multi-fleet, 좌하단 SVG 내) */}
      {multiFleet && fleets && (
        <g>
          {fleets.map((fl, i) => (
            <g key={fl.id} transform={`translate(${vx + 6}, ${vy + vh - 14 - i * 14})`}>
              <circle cx="5" cy="0" r="4" fill="none" stroke={fl.color} strokeWidth="2" />
              <text x="14" y="4" fill={fl.color}
                    fontSize="9" fontFamily="JetBrains Mono, monospace">{fl.id}</text>
            </g>
          ))}
        </g>
      )}
    </svg>
  );
}

function TopBar({ status, agvs, blocked, onReset }) {
  const counts = useMemo(() => {
    const c = { NAVIGATING:0, WAITING:0, PROCESSING:0, CHARGING:0 };
    for (const a of (agvs || [])) {
      if (c[a.state] !== undefined) c[a.state] += 1;
    }
    return c;
  }, [agvs]);

  const pad2 = (n) => String(n).padStart(2, "0");

  return (
    <div style={{ flex:"0 0 auto", height:54, background:"#161924",
                  borderBottom:"1px solid #222633", display:"flex",
                  alignItems:"center", padding:"0 18px", gap:18 }}>
      <div style={{ display:"flex", alignItems:"center", gap:10 }}>
        <div style={{ width:22, height:22, background:"var(--blue)",
                      display:"flex", alignItems:"center", justifyContent:"center" }}>
          <span style={{ fontFamily:"JetBrains Mono, monospace", fontSize:11,
                         fontWeight:700, color:"#0f1117" }}>F</span>
        </div>
        <div>
          <div style={{ fontSize:13, fontWeight:600, letterSpacing:0.3 }}>
            FAB AMR — Quick Sim
          </div>
          <div className="mono" style={{ fontSize:10, color:"var(--text-mute)",
                                          letterSpacing:1.2 }}>
            v0.1 / live
          </div>
        </div>
      </div>

      {/* 상태 인디케이터 */}
      <div style={{ display:"flex", alignItems:"center", gap:8 }}>
        <span style={{
          display:"inline-block", width:8, height:8, borderRadius:"50%",
          background: status === "running" ? "var(--green)"
                    : status === "stopped" ? "var(--red)"
                    : "var(--text-mute)",
        }} />
        <span className="mono" style={{ fontSize:11, color:"var(--text-dim)",
                                         letterSpacing:1.2 }}>
          {status === "running" ? "RUNNING"
           : status === "stopped" ? "STOPPED"
           : "IDLE"}
        </span>
      </div>

      {/* AGV 상태 카운터 */}
      <div style={{ display:"flex", alignItems:"center", gap:14 }}>
        {[
          ["NAV", counts.NAVIGATING, STATE_COLORS.NAVIGATING],
          ["WAIT", counts.WAITING, STATE_COLORS.WAITING],
          ["PROC", counts.PROCESSING, STATE_COLORS.PROCESSING],
          ["CHG", counts.CHARGING, STATE_COLORS.CHARGING],
        ].map(([label, val, color]) => (
          <div key={label} className="mono" style={{ fontSize:11, color:"var(--text-mute)",
                                                       display:"flex", gap:4, letterSpacing:0.8 }}>
            <span style={{ color }}>●</span>
            <span>{label}</span>
            <span style={{ color:"var(--text)" }}>{pad2(val)}</span>
          </div>
        ))}
      </div>

      <div style={{ flex:1 }} />

      <div style={{ display:"flex", gap:8 }}>
        <button onClick={onReset}
                style={{ padding:"6px 14px", background:"var(--panel-2)",
                         border:"1px solid var(--line-2)", color:"var(--text)",
                         fontSize:11, fontFamily:"JetBrains Mono, monospace",
                         letterSpacing:1, borderRadius:0 }}>
          ↺ RESET
        </button>
      </div>
    </div>
  );
}

// KPI 카드 (값 + trend 화살표)
function KpiCard({ label, value, unit, trend }) {
  const trendColor = trend > 0.5 ? "var(--green)"
                   : trend < -0.5 ? "var(--red)"
                   : "var(--text-mute)";
  const trendArrow = trend > 0.5 ? "▲" : trend < -0.5 ? "▼" : "—";
  return (
    <div style={{ background:"var(--panel-2)", border:"1px solid var(--line)",
                  padding:"10px 12px", minHeight:78,
                  display:"flex", flexDirection:"column", justifyContent:"space-between" }}>
      <div className="mono" style={{ fontSize:9, color:"var(--text-mute)",
                                       letterSpacing:1.2 }}>
        {label}
      </div>
      <div style={{ display:"flex", alignItems:"baseline", gap:4 }}>
        <span style={{ fontSize:24, fontWeight:600 }}>{value}</span>
        {unit && <span style={{ fontSize:11, color:"var(--text-dim)" }}>{unit}</span>}
      </div>
      <div className="mono" style={{ fontSize:10, color:trendColor }}>
        {trendArrow} {Math.abs(trend).toFixed(1)}%
      </div>
    </div>
  );
}

// F1a: fleet 별 KPI 행 (compact)
function FleetKpiRow({ fleetId, fkpi }) {
  return (
    <div style={{ display:"flex", alignItems:"center", gap:8, padding:"5px 0",
                  borderBottom:"1px solid var(--line)" }}>
      <span style={{ color: fkpi.color || "#888", fontSize:13 }}>●</span>
      <span className="mono" style={{ fontSize:10, color:"var(--text)", flex:"0 0 80px",
                                       overflow:"hidden", textOverflow:"ellipsis",
                                       whiteSpace:"nowrap" }}>
        {fleetId}
      </span>
      <span className="mono" style={{ fontSize:10, color:"var(--text-mute)", flex:1 }}>
        <span style={{ color:"var(--text)" }}>{Math.round(fkpi.tasksPerHr)}</span>/h
      </span>
      <span className="mono" style={{ fontSize:10, color:"var(--text-mute)", flex:1 }}>
        <span style={{ color:"var(--text)" }}>{Math.round(fkpi.utilization * 100)}</span>%
      </span>
      <span className="mono" style={{ fontSize:10, color:"var(--text-mute)", flex:"0 0 36px",
                                       textAlign:"right" }}>
        <span style={{ color: fkpi.headOn > 0 ? "var(--red)" : "var(--text-mute)" }}>
          {fkpi.headOn}↯
        </span>
      </span>
    </div>
  );
}

function ControlPanel({
  topology, setTopology,
  agvCount, setAgvCount,
  speed, setSpeed,
  duration, setDuration,
  kpi,
  blocked,
  edgeMap,
  onUnblock,
  onRun, onStop,
  status, simTime,
  // F1a
  fleets,
  agvCountByFleet,
  setAgvCountByFleet,
  importedMaps,
}) {
  const fmtTime = (s) => {
    const hh = Math.floor(s / 3600);
    const mm = Math.floor((s % 3600) / 60);
    const ss = Math.floor(s % 60);
    if (hh > 0) return `${hh}:${String(mm).padStart(2,"0")}:${String(ss).padStart(2,"0")}`;
    return `${String(mm).padStart(2,"0")}:${String(ss).padStart(2,"0")}`;
  };
  const running = status === "running";
  const k = kpi || { tasksPerHr:0, utilization:0, headOn:0, avgWait:0, trends:{} };
  const multiFleet = (fleets || []).length > 1;

  // by_fleet entries (정렬: fleet id 순)
  const byFleetEntries = useMemo(() => {
    const bf = k.by_fleet || {};
    return Object.entries(bf).sort(([a], [b]) => a < b ? -1 : 1);
  }, [k.by_fleet]);

  return (
    <div style={{ flex:"0 0 280px", background:"var(--panel)",
                  borderLeft:"1px solid var(--line)", display:"flex",
                  flexDirection:"column", overflowY:"auto" }}>

      {/* SCENARIO */}
      <Section title="SCENARIO">
        <Field label="Topology">
          <select value={topology} onChange={(e)=>setTopology(e.target.value)}
                  disabled={running}
                  style={selectStyle}>
            <option value="A">A — 단방향 순환</option>
            <option value="B">B — 양방향 + siding</option>
            <option value="B/mid/reachable">B/mid/reachable</option>
            <option value="C">C — 2차선 분리</option>
            <option value="D">D — 2차선 wide</option>
            <option value="E">E — 양방향 크리프</option>
            {/* F1a: 임포트 맵 목록 */}
            {(importedMaps || []).length > 0 && (
              <optgroup label="── 임포트 맵 ──">
                {importedMaps.map(m => (
                  <option key={m.id} value={`imported:${m.id}`}>
                    📂 {m.name || m.id}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </Field>

        {/* F1a: fleet 별 count 슬라이더 (multi-fleet) vs 단일 슬라이더 */}
        {multiFleet ? (
          fleets.map(fl => {
            const cnt = agvCountByFleet[fl.id] ?? fl.count;
            return (
              <Field key={fl.id} label={
                <span>
                  <span style={{ color: fl.color }}>●</span>
                  {` ${fl.id}: ${cnt}`}
                </span>
              }>
                <input type="range" min="0" max="16" step="1"
                       value={cnt} disabled={running}
                       onChange={e => setAgvCountByFleet(prev => ({
                         ...prev, [fl.id]: Number(e.target.value)
                       }))} />
              </Field>
            );
          })
        ) : (
          <Field label={`AGV count: ${agvCount}`}>
            <input type="range" min="1" max="32" step="1"
                   value={agvCount} disabled={running}
                   onChange={(e)=>setAgvCount(Number(e.target.value))} />
          </Field>
        )}

        <Field label={`Speed: ${speed.toFixed(1)}x`}>
          <input type="range" min="0.5" max="10" step="0.5"
                 value={speed} disabled={running}
                 onChange={(e)=>setSpeed(Number(e.target.value))} />
        </Field>
        <Field label={`Duration: ${duration}s (${(duration/60).toFixed(1)}min)`}>
          <input type="range" min="60" max="7200" step="60"
                 value={duration} disabled={running}
                 onChange={(e)=>setDuration(Number(e.target.value))} />
        </Field>
        <div style={{ display:"flex", gap:8, marginTop:8 }}>
          {!running && (
            <button onClick={onRun} style={runButtonStyle}>▶ RUN</button>
          )}
          {running && (
            <button onClick={onStop} style={stopButtonStyle}>■ STOP</button>
          )}
        </div>
      </Section>

      {/* LIVE KPI — overall */}
      <Section title="LIVE KPI">
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:8 }}>
          <KpiCard label="TASKS / HR" value={Math.round(k.tasksPerHr)}
                   trend={k.trends?.tasksPerHr ?? 0} />
          <KpiCard label="UTILIZATION" value={Math.round(k.utilization*100)} unit="%"
                   trend={k.trends?.utilization ?? 0} />
          <KpiCard label="HEAD-ON" value={k.headOn} unit="ev"
                   trend={k.trends?.headOn ?? 0} />
          <KpiCard label="AVG WAIT" value={k.avgWait.toFixed(1)} unit="s"
                   trend={k.trends?.avgWait ?? 0} />
        </div>
      </Section>

      {/* F1a: FLEET KPI (multi-fleet 에서만) */}
      {byFleetEntries.length > 1 && (
        <Section title="FLEET KPI">
          <div className="mono" style={{ fontSize:9, color:"var(--text-mute)",
                                          letterSpacing:1.2, marginBottom:4,
                                          display:"flex", gap:8 }}>
            <span style={{ flex:"0 0 80px" }}>FLEET</span>
            <span style={{ flex:1 }}>TASK/H</span>
            <span style={{ flex:1 }}>UTIL</span>
            <span style={{ flex:"0 0 36px", textAlign:"right" }}>H-ON</span>
          </div>
          {byFleetEntries.map(([fid, fkpi]) => (
            <FleetKpiRow key={fid} fleetId={fid} fkpi={fkpi} />
          ))}
        </Section>
      )}

      {/* BLOCKED EDGES */}
      <Section title={`BLOCKED EDGES${blocked.size ? ` (${blocked.size})` : ""}`}>
        {blocked.size === 0 ? (
          <div className="mono" style={{ fontSize:11, color:"var(--text-mute)",
                                          padding:"8px 0", letterSpacing:1 }}>
            클릭하여 엣지 차단{running ? " (실행 중엔 비활성)" : ""}
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
            {Array.from(blocked).map(eid => {
              const e = edgeMap[eid];
              const lbl = e ? `${e.a} → ${e.b}` : eid;
              return (
                <div key={eid} className="mono"
                     style={{ fontSize:10, color:"var(--red)", display:"flex",
                              justifyContent:"space-between", alignItems:"center" }}>
                  <span style={{ overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                    {lbl}
                  </span>
                  {!running && (
                    <button onClick={()=>onUnblock(eid)}
                            style={{ background:"none", border:"none",
                                     color:"var(--text-mute)", padding:0, fontSize:11 }}>
                      ✕
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Section>

      {/* SIM TIME */}
      <Section title="SIM TIME">
        <div className="mono" style={{ fontSize:24, fontWeight:600 }}>
          {fmtTime(simTime)}
        </div>
        <div className="mono" style={{ fontSize:10, color:"var(--text-mute)",
                                        letterSpacing:1.2, marginTop:4 }}>
          tick {Math.floor(simTime * 10)}
        </div>
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ borderBottom:"1px solid var(--line)", padding:"12px 14px" }}>
      <div className="mono" style={{ fontSize:9, color:"var(--text-mute)",
                                       letterSpacing:1.5, marginBottom:8 }}>
        {title}
      </div>
      {children}
    </div>
  );
}
function Field({ label, children }) {
  return (
    <div style={{ marginBottom:10 }}>
      <div className="mono" style={{ fontSize:10, color:"var(--text-dim)",
                                       letterSpacing:0.8, marginBottom:4 }}>
        {label}
      </div>
      {children}
    </div>
  );
}
const selectStyle = {
  width:"100%", padding:"6px 8px", background:"var(--panel-2)",
  border:"1px solid var(--line-2)", color:"var(--text)",
  fontSize:11, borderRadius:0, fontFamily:"inherit",
};
const runButtonStyle = {
  flex:1, padding:"8px 14px", background:"var(--accent)",
  border:"none", color:"#0f1117", fontSize:12, fontWeight:600,
  fontFamily:"JetBrains Mono, monospace", letterSpacing:1.2, borderRadius:0,
};
const stopButtonStyle = {
  flex:1, padding:"8px 14px", background:"var(--red)",
  border:"none", color:"#fff", fontSize:12, fontWeight:600,
  fontFamily:"JetBrains Mono, monospace", letterSpacing:1.2, borderRadius:0,
};

// 메인 App
function App() {
  // localStorage 복원
  const restored = useMemo(() => {
    try {
      const s = JSON.parse(localStorage.getItem("quickrun.params") || "{}");
      return {
        topology: s.topology || "A",
        agvCount: s.agvCount || 12,
        speed: s.speed || 2.0,
        duration: s.duration || 600,
      };
    } catch (e) { return { topology:"A", agvCount:12, speed:2.0, duration:600 }; }
  }, []);

  const [topology, setTopology] = useState(restored.topology);
  const [agvCount, setAgvCount] = useState(restored.agvCount);
  const [speed, setSpeed] = useState(restored.speed);
  const [duration, setDuration] = useState(restored.duration);
  const [blocked, setBlocked] = useState(new Set());
  const [status, setStatus] = useState("idle"); // idle | running | stopped
  const [map, setMap] = useState(null);
  const [agvs, setAgvs] = useState([]);
  const [kpi, setKpi] = useState({ tasksPerHr:0, utilization:0, headOn:0, avgWait:0, trends:{} });
  const [simTime, setSimTime] = useState(0);
  const [runId, setRunId] = useState(null);
  const streamRef = useRef(null);
  const autoStartedRef = useRef(false);

  // F1a: fleet 상태
  const [fleets, setFleets] = useState([]);
  const [agvCountByFleet, setAgvCountByFleet] = useState({});
  const [importedMaps, setImportedMaps] = useState([]);
  // colorOf: agv → hex color. 함수를 state에 저장 시 () => fn 형태 필요
  const [colorOf, setColorOf] = useState(() => () => "#2563eb");

  // 임포트 맵 목록 로드 (마운트 시 + 폴링 없이 1회)
  useEffect(() => {
    window.QuickRunAdapter.listImportedMaps()
      .then(maps => setImportedMaps(maps || []))
      .catch(() => {});
  }, []);

  const edgeMap = useMemo(() => {
    if (!map) return {};
    const m = {}; map.edges.forEach(e => m[e.id] = e); return m;
  }, [map]);

  // localStorage 저장 (params 변경 시)
  useEffect(() => {
    try {
      localStorage.setItem("quickrun.params", JSON.stringify({ topology, agvCount, speed, duration }));
    } catch (e) {}
  }, [topology, agvCount, speed, duration]);

  // topology 변경 시 fleet 슬라이더 초기화 (임포트 맵 선택 시 새 fleet list 로드)
  // fleet list 는 init 응답에서 오므로 여기선 reset 만
  useEffect(() => {
    if (!topology.startsWith("imported:")) {
      setFleets([]);
      setAgvCountByFleet({});
    }
  }, [topology]);

  // Run 함수
  const runSim = useCallback(async () => {
    if (streamRef.current) {
      streamRef.current.disconnect();
      streamRef.current = null;
    }
    try {
      // F1a: topology 가 "imported:{id}" 형태인지 파악
      const isImported = topology.startsWith("imported:");
      const importedMapId = isImported ? topology.slice("imported:".length) : undefined;
      const topoParam = isImported ? "A" : topology; // imported 때 topology 파라미터 무시됨

      // F1a: multi-fleet 이면 agvCountByFleet 전달
      const multiFleet = Object.keys(agvCountByFleet).length > 1;

      const res = await window.QuickRunAdapter.init({
        topology: topoParam,
        agvCount,
        speed,
        duration,
        blockedEdges: Array.from(blocked),
        importedMapId,
        agvCountByFleet: multiFleet ? agvCountByFleet : undefined,
      });
      setMap(res.map);
      setRunId(res.runId);
      setStatus("running");
      setSimTime(0);
      setAgvs([]);

      // F1a: fleet 정보 처리
      const resFleets = res.fleets || [];
      setFleets(resFleets);
      // fleet count 초기화 (아직 override 없으면 서버 기본값으로)
      if (resFleets.length > 0) {
        const initCounts = {};
        for (const fl of resFleets) initCounts[fl.id] = fl.count;
        setAgvCountByFleet(prev => {
          // 이미 사용자가 조정한 값 유지, 새 fleet 만 기본값
          const merged = { ...initCounts };
          for (const [k, v] of Object.entries(prev)) {
            if (k in merged) merged[k] = v;
          }
          return merged;
        });
      }
      // fleet color lookup 함수 빌드
      const lookup = window.QuickRunAdapter.makeFleetColorLookup(resFleets);
      setColorOf(() => lookup);

      const stream = window.QuickRunAdapter.connectStream(res.wsUrl, {
        onTick: (msg) => {
          setSimTime(msg.simTime || 0);
          setAgvs(msg.agvs || []);
          if (msg.kpi) setKpi(msg.kpi);
        },
        onEnd: () => setStatus("stopped"),
        onError: () => setStatus("stopped"),
        onClose: () => {
          setStatus(prev => prev === "running" ? "stopped" : prev);
        },
      });
      streamRef.current = stream;
    } catch (e) {
      console.error("run failed", e);
      alert("run 실패: " + e.message);
      setStatus("idle");
    }
  }, [topology, agvCount, agvCountByFleet, speed, duration, blocked]);

  // Stop
  const stopSim = useCallback(async () => {
    if (!runId) return;
    await window.QuickRunAdapter.control(runId, "stop");
    setStatus("stopped");
  }, [runId]);

  // Reset: stop + UI 초기화
  const resetSim = useCallback(async () => {
    if (runId) {
      await window.QuickRunAdapter.control(runId, "reset");
    }
    if (streamRef.current) {
      streamRef.current.disconnect();
      streamRef.current = null;
    }
    setBlocked(new Set());
    setAgvs([]);
    setKpi({ tasksPerHr:0, utilization:0, headOn:0, avgWait:0, trends:{} });
    setSimTime(0);
    setStatus("idle");
    autoStartedRef.current = false;
  }, [runId]);

  // 마운트 시 자동 Run (default params)
  useEffect(() => {
    if (autoStartedRef.current) return;
    autoStartedRef.current = true;
    runSim();
    return () => {
      if (streamRef.current) streamRef.current.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 자동 재시작 (status idle 됐는데 mounted 상태면)
  useEffect(() => {
    if (status === "idle" && autoStartedRef.current === false) {
      autoStartedRef.current = true;
      runSim();
    }
  }, [status, runSim]);

  // 엣지 클릭 (running 중엔 막음)
  const toggleEdge = useCallback((eid) => {
    if (status === "running") return;
    setBlocked(prev => {
      const n = new Set(prev);
      if (n.has(eid)) n.delete(eid); else n.add(eid);
      return n;
    });
  }, [status]);

  const unblockEdge = useCallback((eid) => {
    setBlocked(prev => { const n = new Set(prev); n.delete(eid); return n; });
  }, []);

  // topology label 표시 (imported 맵은 이름으로)
  const topoLabel = useMemo(() => {
    if (topology.startsWith("imported:")) {
      const id = topology.slice("imported:".length);
      const m = importedMaps.find(m => m.id === id);
      return m ? m.name || id : id;
    }
    return `TOPOLOGY-${topology}`;
  }, [topology, importedMaps]);

  return (
    <div style={{ width:"100vw", height:"100vh", display:"flex",
                  flexDirection:"column", background:"#0f1117" }}>
      <TopBar status={status} agvs={agvs} blocked={blocked} onReset={resetSim} />
      <div style={{ flex:1, display:"flex", minHeight:0 }}>
        <div style={{ flex:1, position:"relative", minWidth:0,
                      borderRight:"1px solid var(--line)" }}>
          <MapView map={map} agvs={agvs} blocked={blocked}
                   onToggleEdge={toggleEdge}
                   interactive={status !== "running"}
                   colorOf={colorOf}
                   fleets={fleets} />
          {/* 좌상단 라벨 */}
          <div style={{ position:"absolute", left:14, top:12, fontSize:10,
                        color:"var(--text-mute)", fontFamily:"JetBrains Mono, monospace",
                        letterSpacing:1.3 }}>
            FAB-MAP / {topoLabel}
          </div>
          {map && (
            <div style={{ position:"absolute", left:14, bottom:14, fontSize:10,
                          color:"var(--text-mute)", fontFamily:"JetBrains Mono, monospace",
                          display:"flex", gap:14 }}>
              <span>NODES {map.nodes.length}</span>
              <span>EDGES {map.edges.length}</span>
              <span>AGV {agvs.length}</span>
              <span style={{ color: blocked.size?"var(--red)":"var(--text-mute)" }}>
                BLOCKED {blocked.size}
              </span>
            </div>
          )}
        </div>
        <ControlPanel
          topology={topology} setTopology={setTopology}
          agvCount={agvCount} setAgvCount={setAgvCount}
          speed={speed} setSpeed={setSpeed}
          duration={duration} setDuration={setDuration}
          kpi={kpi}
          blocked={blocked}
          edgeMap={edgeMap}
          onUnblock={unblockEdge}
          onRun={runSim} onStop={stopSim}
          status={status} simTime={simTime}
          fleets={fleets}
          agvCountByFleet={agvCountByFleet}
          setAgvCountByFleet={setAgvCountByFleet}
          importedMaps={importedMaps}
        />
      </div>
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<App />);
