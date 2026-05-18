"""
Quick Sim 백엔드 — FastAPI 단일 사용자 로컬 도구.

Phase 1: mock snapshot publisher (sin/cos AGV) — 프론트 결선 검증용.
Phase 2: 실 SimulationEngine 연결 (별도 단계).

엔드포인트:
  GET  /                    정적 index.html 서빙
  GET  /static/{...}        정적 자원 (JS 어댑터들)
  GET  /healthz             헬스체크
  POST /init                새 sim 시작. body: {topology, agvCount, speed, duration, blockedEdges}
                            응답: {runId, map, wsUrl}
  POST /control             {runId, action: "stop"|"reset"}
  WS   /ws/stream/{runId}   tick snapshot push (0.5s 간격, 또는 speed에 비례)

설계:
  - 단일 활성 sim. 새 /init 시 기존 sim 자동 stop.
  - mock 모드는 SimRunner = MockRunner 로 분기. 실 엔진 wiring은 phase 2 에서 RealRunner.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.analytics.playback_trace import build_live_html
from src.domain.map.external_importer import apply_edits, build_map_graph, import_map, import_map_json, import_map_yaml
from src.interfaces.map_editor import build_editor_html
from src.interfaces.quickrun.runner import RealRunner

# ── 정적 자원 경로 ─────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

_LIVE_HTML_CACHE: str | None = None


def _get_live_html() -> str:
    global _LIVE_HTML_CACHE
    if _LIVE_HTML_CACHE is None:
        _LIVE_HTML_CACHE = build_live_html(
            default_params={
                "topology": "A",
                "agv_count": 12,
                "speed": 2.0,
                "duration": 600,
            }
        )
    return _LIVE_HTML_CACHE


# ── 요청/응답 모델 ─────────────────────────────────────────────
class InitRequest(BaseModel):
    topology: str = "A"
    agvCount: int = 12
    speed: float = 2.0
    duration: float = 600.0   # sim 초 단위
    blockedEdges: list[str] = []
    taskIntervalS: float = 5.0   # 잡 생성 주기 (sim 초)
    importedMapId: str | None = None  # 업로드된 임포트 맵 ID (있으면 토폴로지 대신 사용)
    agvCountByFleet: dict[str, int] | None = None  # F1a: fleet 별 AGV 수 override (UI 슬라이더)


class ControlRequest(BaseModel):
    runId: str
    action: str  # "stop" | "reset"


class BlockEdgeRequest(BaseModel):
    runId: str
    edge_id: str   # edge_key "src__dst" 형식
    blocked: bool


class UploadMapRequest(BaseModel):
    name: str                      # 사용자 친화적 이름 (예: "synthetic_plant")
    map_json: dict | None = None   # 원본 외부 맵 JSON (nodes/links)
    map_yaml: str | None = None    # 또는 RMF YAML 문자열
    edits_json: dict | None = None # 선택: editor 의 *.edit.json 결과


class ManualJobRequest(BaseModel):
    """GAP-B: UI 📋 토글이 호출하는 수동 demand 발행 요청.

    pickup_node / dropoff_node 는 graph 의 node_id. runId 가 비어있으면
    현재 활성 sim 으로 라우팅. required_capability 가 있으면 매칭 fleet 만.
    """
    pickup_node: str
    dropoff_node: str
    runId: str | None = None
    required_capability: str | None = None


# 업로드된 임포트 맵을 메모리에 보관 (단일 사용자 로컬 도구라 OK)
# key: importedMapId, value: {"name": str, "graph": MapGraph}
_imported_maps: dict[str, dict] = {}


# ── Snapshot publisher 추상 ────────────────────────────────────
@dataclass
class SimRunner:
    """sim 한 번의 실행 단위. asyncio task 가 백그라운드에서 tick 마다 broadcast.

    real_runner 가 None 이면 mock 모드 (Phase 1 호환). 보통 RealRunner 가 부착됨."""
    run_id: str
    topology: str
    agv_count: int
    speed: float
    duration: float
    blocked_edges: set[str]
    map_data: dict
    ws_clients: list[WebSocket] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
    sim_time: float = 0.0
    stopped: bool = False
    started_wall: float = field(default_factory=time.time)
    real_runner: Optional["RealRunner"] = None

    async def broadcast(self, msg: dict) -> None:
        """현재 연결된 WS 모두에 메시지 push. 끊긴 client 자동 정리."""
        if not self.ws_clients:
            return
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.ws_clients.remove(ws)
            except ValueError:
                pass


# ── Mock map / snapshot 생성기 (Phase 1) ───────────────────────
def _build_mock_map(topology: str) -> dict:
    """phase 1: 토폴로지 ID 만 보고 간단한 격자 맵 생성. phase 2 에선 진짜 graph 변환."""
    nodes = []
    edges = []
    # 간단한 4×3 격자 + station/charger 마커
    for j, y in enumerate([100, 300, 500]):
        for i, x in enumerate([100, 300, 500, 700, 900]):
            nid = f"WP_{['N','C','S'][j]}_{i:02d}"
            kind = "wp"
            if j == 1 and i in (1, 3):
                kind = "station"
                nid = f"ST_C_{i:02d}"
            elif j == 0 and i == 0:
                kind = "charger"
                nid = "CH_01"
            elif j == 2 and i == 4:
                kind = "charger"
                nid = "CH_02"
            elif j == 1 and i == 2:
                kind = "holding"
                nid = "HP_C_02"
            nodes.append({"id": nid, "x": x, "y": y, "kind": kind})

    # 가로 엣지 (코리도)
    rows = [["WP_N_00","WP_N_01","WP_N_02","WP_N_03","WP_N_04"],
            None,  # 중앙은 station/holding 섞여서 별도 처리
            ["WP_S_00","WP_S_01","WP_S_02","WP_S_03","WP_S_04"]]
    # 노드 id 갱신: 위에서 j==0 i==0 은 CH_01 로 치환됐으니 그 처리
    nodes_by_pos = {(int(n["x"]), int(n["y"])): n["id"] for n in nodes}
    for j, y in enumerate([100, 300, 500]):
        ids_in_row = [nodes_by_pos[(x, y)] for x in [100, 300, 500, 700, 900]]
        for k in range(len(ids_in_row) - 1):
            src, dst = ids_in_row[k], ids_in_row[k + 1]
            edges.append({
                "id": f"{src}__{dst}",
                "src": src, "dst": dst,
                "directed": True,
                "corridor": ["north", "center", "south"][j],
            })
    # 세로 엣지 (베이)
    for i, x in enumerate([100, 300, 500, 700, 900]):
        ids = [nodes_by_pos[(x, y)] for y in [100, 300, 500]]
        for k in range(len(ids) - 1):
            src, dst = ids[k], ids[k + 1]
            edges.append({
                "id": f"{src}__{dst}",
                "src": src, "dst": dst,
                "directed": True,
                "corridor": "bay",
            })
    return {
        "viewBox": [0, 0, 1000, 600],
        "nodes": nodes,
        "edges": edges,
        "topology": topology,
    }


async def _mock_run_loop(runner: SimRunner) -> None:
    """phase 1: sin/cos 로 AGV 가 맵 위를 돈다고 가정한 mock snapshot 발행.
    speed 비례로 sim_time 진행. duration 도달 시 종료 메시지."""
    # ws 가 붙을 때까지 잠깐 대기 (race 방지)
    await asyncio.sleep(0.1)
    PUSH_INTERVAL = 0.1  # wall-clock 초당 10 push (speed 무관)
    SIM_DT = 0.5         # tick 당 sim_time 진행 (speed 곱)
    nodes_xy = [(n["x"], n["y"]) for n in runner.map_data["nodes"]]
    while not runner.stopped and runner.sim_time < runner.duration:
        runner.sim_time += SIM_DT * runner.speed
        agvs = []
        for i in range(runner.agv_count):
            # 각 AGV 가 다른 phase 의 원궤도. 시각적으로 도는 효과만.
            phase = i * 0.5 + runner.sim_time * 0.05
            cx = 500 + 300 * math.cos(phase)
            cy = 300 + 150 * math.sin(phase)
            states = ["NAVIGATING", "WAITING", "PROCESSING", "CHARGING"]
            state = states[i % len(states)] if i < 4 else "NAVIGATING"
            agvs.append({"id": f"AGV_{i+1:03d}", "x": cx, "y": cy, "state": state, "blockingAgv": ""})
        # mock KPI
        t = runner.sim_time
        kpi = {
            "tasksPerHr": max(4, 50 + math.sin(t * 0.31) * 5),
            "utilization": 0.5 + 0.2 * math.sin(t * 0.27),
            "headOn": int(2 + math.sin(t * 0.19) * 2),
            "avgWait": 6.0 + math.sin(t * 0.16) * 1.5,
            "trends": {
                "tasksPerHr": math.sin(t * 0.22) * 6,
                "utilization": math.sin(t * 0.18) * 4,
                "headOn": math.sin(t * 0.24) * 8,
                "avgWait": math.sin(t * 0.21) * 5,
            },
        }
        await runner.broadcast({
            "type": "tick",
            "simTime": runner.sim_time,
            "agvs": agvs,
            "kpi": kpi,
        })
        await asyncio.sleep(PUSH_INTERVAL)
    # 종료 알림
    await runner.broadcast({
        "type": "end",
        "reason": "stopped" if runner.stopped else "completed",
        "simTime": runner.sim_time,
    })


# ── FastAPI 앱 ────────────────────────────────────────────────
app = FastAPI(title="Quick Sim", version="0.1")
_active_runner: Optional[SimRunner] = None
_runners_by_id: dict[str, SimRunner] = {}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "active_run": _active_runner.run_id if _active_runner else None}


@app.get("/")
async def index():
    return HTMLResponse(_get_live_html())


@app.post("/upload-map")
async def upload_map(req: UploadMapRequest):
    """외부 맵 JSON/YAML 업로드 → ImportedMap → (edits 있으면) apply_edits → MapGraph 메모리 보관.

    요청:
      - map_json: dict (JSON 형식)
      - map_yaml: str (YAML 문자열 형식)
    하나만 제공하면 됨.

    응답에 importedMapId 반환. 이후 /init 호출 시 이 id 를 importedMapId 로 넘기면
    그 맵으로 시뮬 시작.
    """
    try:
        if req.map_json:
            imp = import_map_json(req.map_json)
        elif req.map_yaml:
            data = yaml.safe_load(req.map_yaml)
            imp = import_map_yaml(data)
        else:
            raise ValueError("Either map_json or map_yaml must be provided")

        if req.edits_json:
            imp = apply_edits(imp, req.edits_json)
        graph = build_map_graph(imp)
    except Exception as exc:
        raise HTTPException(400, f"map import failed: {exc}")

    map_id = "map_" + uuid.uuid4().hex[:8]
    _imported_maps[map_id] = {
        "name": req.name,
        "imported": imp,       # ImportedMap (Editor 페이지가 사용)
        "graph": graph,        # MapGraph (시뮬 엔진이 사용)
        "stats": {
            "nodes": len(imp.nodes),
            "edges": len(imp.edges),
            "chargers": imp.report.inferred_chargers,
            "stations": imp.report.inferred_stations,
        },
    }
    return {
        "importedMapId": map_id,
        "name": req.name,
        "stats": _imported_maps[map_id]["stats"],
    }


@app.get("/imported-maps")
async def list_imported_maps():
    """현재 메모리에 있는 임포트 맵 목록. Quickrun 페이지 토폴로지 드롭다운 채울 때 사용."""
    out = []
    for k, v in _imported_maps.items():
        fleets = list(getattr(v["imported"], "fleets", []) or [])
        out.append({
            "id": k,
            "name": v["name"],
            "stats": v["stats"],
            "fleets": fleets,  # F1a: UI 가 슬라이더/색 미리 빌드할 때 사용
        })
    return out


@app.get("/edit/{map_id}")
async def edit_page(map_id: str):
    """업로드된 임포트 맵을 Map Editor 페이지로 즉시 진입."""
    entry = _imported_maps.get(map_id)
    if entry is None:
        raise HTTPException(404, f"unknown map id: {map_id}")
    html = build_editor_html(
        entry["imported"],
        title=f"Edit — {entry['name']}",
        source_name=entry["name"],
        server_map_id=map_id,
    )
    return HTMLResponse(html)


class UpdateMapRequest(BaseModel):
    edits: dict


@app.post("/update-map/{map_id}")
async def update_map(map_id: str, req: UpdateMapRequest):
    """Editor 페이지의 Save → 서버 메모리 갱신.
    cumulative: 현재 ImportedMap 위에 새 edits 적용 (이전 편집 누적된 상태).
    """
    entry = _imported_maps.get(map_id)
    if entry is None:
        raise HTTPException(404, f"unknown map id: {map_id}")
    try:
        imp_after = apply_edits(entry["imported"], req.edits)
        graph_after = build_map_graph(imp_after)
    except Exception as exc:
        raise HTTPException(400, f"apply_edits failed: {exc}")
    entry["imported"] = imp_after
    entry["graph"] = graph_after
    entry["stats"] = {
        "nodes": len(imp_after.nodes),
        "edges": len(imp_after.edges),
        "chargers": imp_after.report.inferred_chargers,
        "stations": imp_after.report.inferred_stations,
    }
    return {"ok": True, "stats": entry["stats"]}


@app.post("/init")
async def init_sim(req: InitRequest):
    global _active_runner
    # 기존 sim 자동 stop
    if _active_runner is not None and not _active_runner.stopped:
        _active_runner.stopped = True
        if _active_runner.real_runner is not None:
            _active_runner.real_runner.stop()
        if _active_runner.task and not _active_runner.task.done():
            try:
                await asyncio.wait_for(_active_runner.task, timeout=2.0)
            except asyncio.TimeoutError:
                _active_runner.task.cancel()

    run_id = "rn_" + uuid.uuid4().hex[:8]
    runner = SimRunner(
        run_id=run_id,
        topology=req.topology,
        agv_count=req.agvCount,
        speed=req.speed,
        duration=req.duration,
        blocked_edges=set(req.blockedEdges),
        map_data={},
    )

    # broadcast 콜백을 RealRunner 에 넘겨준다
    async def _bcast(msg):
        await runner.broadcast(msg)

    # 임포트 맵 사용 시 메모리에서 graph + fleets 가져옴
    imported_graph = None
    imported_fleets: list[dict] = []
    if req.importedMapId:
        entry = _imported_maps.get(req.importedMapId)
        if entry is None:
            raise HTTPException(404, f"unknown importedMapId: {req.importedMapId}")
        imported_graph = entry["graph"]
        imported_fleets = list(getattr(entry["imported"], "fleets", []) or [])

    real = RealRunner(
        topology=req.topology,
        agv_count=req.agvCount,
        speed=req.speed,
        duration=req.duration,
        blocked_edges=set(req.blockedEdges),
        broadcast=_bcast,
        task_interval_s=req.taskIntervalS,
        imported_graph=imported_graph,
        imported_fleets=imported_fleets,
        agv_count_by_fleet=req.agvCountByFleet,
    )
    try:
        real.setup()
    except Exception as exc:
        raise HTTPException(400, f"setup failed: {exc}")
    runner.real_runner = real
    runner.map_data = real.map_json_for_live

    async def _runner_task():
        try:
            await real.run()
        finally:
            runner.stopped = True

    runner.task = asyncio.create_task(_runner_task())
    _active_runner = runner
    _runners_by_id[run_id] = runner
    # F1a: fleets 메타 (단일 default fleet 포함). 응답에 fleet_id/color 노출.
    fleets_payload = real.fleets_payload()
    return {
        "runId": run_id,
        "map": runner.map_data,
        "wsUrl": f"/ws/stream/{run_id}",
        "fleets": fleets_payload,
    }


@app.post("/manual-job")
async def manual_job(req: ManualJobRequest):
    """GAP-B: 수동 demand 발행. UI 의 📋 토글이 두 노드 클릭 후 호출.

    body: {pickup_node, dropoff_node, required_capability?, runId?}
    응답: {ok, demand_id, agv_id, status, reason}
      status: "dispatched" — 즉시 AGV 에 할당
              "pending"    — 매칭 idle AGV 없음 (자동 retry 안 함)
              "rejected"   — 검증 실패 (노드 없음 등)
    """
    runner = _runners_by_id.get(req.runId) if req.runId else _active_runner
    if runner is None or runner.real_runner is None:
        raise HTTPException(400, "no active simulation")
    if runner.stopped:
        raise HTTPException(400, "simulation already stopped")
    try:
        result = await runner.real_runner.dispatch_manual_demand(
            pickup_node_id=req.pickup_node,
            dropoff_node_id=req.dropoff_node,
            required_capability=req.required_capability,
        )
    except Exception as exc:
        raise HTTPException(500, f"manual dispatch failed: {exc}")
    return result


@app.post("/control")
async def control(req: ControlRequest):
    runner = _runners_by_id.get(req.runId)
    if runner is None:
        raise HTTPException(404, "unknown runId")
    if req.action == "stop":
        runner.stopped = True
        if runner.real_runner is not None:
            runner.real_runner.stop()
        return {"ok": True, "state": "stopped"}
    if req.action == "reset":
        runner.stopped = True
        if runner.real_runner is not None:
            runner.real_runner.stop()
        return {"ok": True, "state": "reset"}
    raise HTTPException(400, f"unknown action: {req.action}")


@app.post("/block-edge")
async def block_edge(req: BlockEdgeRequest):
    """GAP-A: 라이브 시뮬 중 엣지 차단/해제.

    body: {runId, edge_id (edge_key "src__dst" 형식), blocked: bool}
    응답: {ok, currently_blocked: [...], affected_agvs: [...]}
    """
    runner = _runners_by_id.get(req.runId)
    if runner is None:
        raise HTTPException(404, "unknown runId")
    if runner.real_runner is None:
        raise HTTPException(400, "no active engine for this run")
    result = runner.real_runner.block_edge(req.edge_id, req.blocked)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "block_edge failed"))
    return result


@app.websocket("/ws/stream/{run_id}")
async def ws_stream(ws: WebSocket, run_id: str):
    runner = _runners_by_id.get(run_id)
    if runner is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    runner.ws_clients.append(ws)
    try:
        # 클라이언트가 끊을 때까지 유지. 서버는 broadcast 로만 push.
        while True:
            # 빈 receive 호출로 disconnect 감지
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            runner.ws_clients.remove(ws)
        except ValueError:
            pass


# ── 정적 자원 마운트 ───────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── CLI 실행 ──────────────────────────────────────────────────
def main():
    import uvicorn
    uvicorn.run(
        "src.interfaces.quickrun.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
