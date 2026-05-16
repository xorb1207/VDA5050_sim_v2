# GAP-A — Quickrun 라이브 Edge 차단 UI

> Agent 의뢰용. 견적 ~0.5일.
> 다른 GAP 과 독립 — 병렬 진행 가능.

## Goal

Quickrun 라이브 시뮬 중 사용자가 엣지를 클릭으로 차단/해제 → 즉시 reroute 발생을 육안 확인. "what-if" 가치 직결.

## 사용자 의도 매핑

- **의도 #5** — edge 하나 막고 reroute 둘러가는 것 육안 확인

## Scope

- IN:
  - Quickrun 페이지에 **"⛔ 차단 모드"** 토글 (히트맵/충돌 마커 옆)
  - 차단 모드 on + 엣지 클릭 → 그 엣지 차단 (빨간색 X 표시) + 서버에 알림
  - 차단된 엣지 다시 클릭 → 해제 (서버 알림)
  - 서버: blocked_edges set 동적 갱신 → 다음 reroute 부터 그 엣지 회피
  - 엣지 hover 시 시각 강조 (차단 가능함 표시)
  - 토글 끄면 클릭 인터랙션 비활성 (기존 AGV 클릭/포커스와 충돌 방지)
- OUT:
  - 다중 엣지 동시 토글 UI (drag select 등)
  - 차단 영구 저장 (시뮬 종료 시 초기화)
  - edit.json 에 차단 정보 저장
  - reroute 애니메이션 강조 (이미 reroute 이벤트 마커 있음)

## Pre-step (discovery — 필수)

1. `src/interfaces/quickrun/server.py` — 현재 `blockedEdges` 사용처 + `/control` endpoint 패턴
2. `src/analytics/playback_trace.py` — `build_live_html()` 의 토글 버튼 패턴 (heatmap, collision 참고)
3. `src/interfaces/quickrun/runner.py` — `RealRunner` 가 `blocked_edges` 를 어떻게 다루는지 (시뮬 시작 시 vs 동적)
4. AGV reroute 메커니즘 — `agv.py` 의 `_reroute()` 가 어떻게 트리거되는지
5. 엣지 클릭 hit-test — 현재 SVG 에 엣지 click 핸들러 있는지

→ 발견 결과 보고 후 구현. 특히 **runner 가 동적으로 blocked_edges 갱신 가능한지** (현재는 init 시점만일 가능성).

## Interface

### 신규 REST endpoint

```
POST /block-edge
body: {"runId": "...", "edge_id": "...", "blocked": true|false}
→ 200 OK + {"ok": true, "currently_blocked": [...]}
```

또는 `/control` 확장:
```
POST /control
body: {"runId": "...", "action": "block_edge", "edge_id": "...", "blocked": true}
```

### Quickrun UI 변경

```
[toolbar 의 토글 그룹]
🔥 히트맵   ⚠ 충돌   ⛔ 차단 ← 신규
```

차단 모드 on:
- 커서: pointer
- 엣지 hover: 굵게 + 노란 outline
- 클릭: 빨간 X 표시 + WS broadcast 로 다른 클라이언트 동기화
- 차단된 엣지: 빨간 점선 + ⛔ 마커

### Runner 변경 (동적 차단)

```python
class RealRunner:
    def block_edge(self, edge_id: str, blocked: bool):
        if blocked:
            self.blocked_edges.add(edge_id)
        else:
            self.blocked_edges.discard(edge_id)
        # 영향받는 AGV reroute 트리거 (현재 그 엣지를 path 에 포함한 AGV)
        for agv in self._engine.agvs.values():
            if edge_id in agv.planned_edge_keys:
                asyncio.create_task(agv._reroute(self._engine.sim_time))
```

## Tests

```python
def test_block_edge_endpoint_returns_200():
    """POST /block-edge 정상 응답"""

def test_blocked_edge_skipped_in_path():
    """차단 후 새 path 가 그 엣지 회피"""

def test_unblock_restores_path():
    """해제 후 path 가 그 엣지 다시 사용 가능"""

def test_block_triggers_reroute_for_affected_agv():
    """차단 시점에 그 엣지 위 또는 path 에 포함된 AGV 가 reroute"""

[수동] 1. Quickrun 시뮬 실행 중 ⛔ 토글 on → 엣지 hover 시 노란 outline
[수동] 2. 엣지 클릭 → 빨간 X + AGV 들이 다른 길로 reroute
[수동] 3. 같은 엣지 다시 클릭 → 해제 → 정상 통행
[수동] 4. ⛔ 토글 off → 클릭 인터랙션 비활성, AGV 클릭으로 포커스 가능
```

## DO NOT

- 다중 엣지 선택 UI (drag select)
- 차단 정보 영구 저장 (시뮬 시작 시 reset)
- edit.json 에 차단 정보 포함
- Editor 페이지에 같은 기능 추가 (Quickrun 만)
- reroute 알고리즘 변경 (기존 _reroute 활용)

## Acceptance

- 위 4개 단위 테스트 PASS
- 4개 수동 시나리오 통과
- 토글 off 시 기존 AGV 클릭/포커스 정상 작동
- 차단 해제 시 path 즉시 복구

## Final Verification

```bash
python tests/integration/test_simulation.py
# 기존 + 신규 PASS

./run quickrun
# 수동 점검: ⛔ 토글 → 엣지 클릭 → reroute 발생 → 해제
```

스크린샷 (차단 전/후) 첨부 권장.
