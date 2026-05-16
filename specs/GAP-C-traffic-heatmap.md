# GAP-C — Traffic 밀도 히트맵

> Agent 의뢰용. 견적 ~0.3일.
> 다른 GAP 과 독립.

## Goal

기존 사고 히트맵 (head-on / section conflict / follow-on 누적) 과 별도로, **AGV 가 자주 지나가는 엣지** 시각화. 사용자가 "어디에 집중이 많이 되는지" 의미와 정확히 일치.

## 사용자 의도 매핑

- **의도 #3** — "어느 곳에 집중이 많이 되는지 히트맵" — traffic 밀도

## Scope

- IN:
  - Quickrun + playback 페이지에 새 토글 **"🚦 트래픽"** (히트맵 옆)
  - edge_traverse 카운트: AGV 가 엣지 진입 (또는 통과 완료) 시 +1
  - 색조 그라데이션: 옅음 (1회) → 진함 (max). 사고 히트맵과 다른 색조 (예: 사고=빨강, 트래픽=파랑/보라)
  - 사고 히트맵과 **동시 활성 불가** (둘 중 하나만) — 색 혼동 방지
  - legend: "통과 횟수 N회 — 색조 그라데이션"
- OUT:
  - 시간대별 차이 (현 시점 누적만)
  - AGV 별 분해
  - 사고 히트맵과의 동시 비교 (별도 spec)
  - 노드 점유 횟수 (엣지만)

## Pre-step (discovery — 필수)

1. `src/analytics/playback_trace.py` — 기존 사고 히트맵 코드 (`heatmapCounts`, `HEATMAP_KINDS`)
2. `src/domain/agv/agv.py` — 엣지 진입 이벤트 발생 위치 (예: `edge_enter` 이벤트가 record_event 로 기록되는지)
3. `src/analytics/playback_trace.py` 의 `PlaybackTraceRecorder` — 어떤 이벤트들이 trace 에 들어가는지
4. snapshot 안의 `current_edge_key` 활용 가능성 (매 tick 같은 엣지면 +1 일종의 통과 시간)
5. toolbar 토글 추가 패턴 (히트맵, 충돌 마커 참고)

→ 발견 결과 보고 후 구현. 특히 **edge_enter 이벤트가 이미 기록되는지** vs 새로 추가 필요한지.

## Interface

### 데이터 source 옵션

**(A)** trace.events 에 `edge_enter` 이벤트 (이미 있을 가능성 높음):
```python
events: [{"t": 1.5, "kind": "edge_enter", "agv_id": "AGV_001", "edge_key": "N1__N2"}, ...]
```
→ 클라이언트에서 edge_key 별 count 집계

**(B)** snapshot 의 `current_edge_key` 활용:
```python
# 매 snapshot 마다 각 AGV 의 current_edge_key 카운트
# 같은 엣지에 여러 tick 머무르면 그만큼 +1 (= 통과 시간 비례)
```

→ Pre-step 결과로 결정. (A) 권장 (정확한 통과 카운트), 없으면 (B) 로 대체.

### UI 변경

```
[toolbar]
🔥 히트맵   🚦 트래픽 ← 신규   ⚠ 충돌   ⛔ 차단
```

- 🔥 히트맵 / 🚦 트래픽 = **mutually exclusive** (하나 켜면 다른 거 자동 끔)
- legend (활성 시):
  - 🔥: "누적 사고 강도" (현재)
  - 🚦: "통과 횟수 — 옅음 1회 → 진함 N회"
- 색조:
  - 🔥: 빨강~주황 (현재)
  - 🚦: 파랑~보라 (다른 색조)

### JS 변경 (`build_live_html`, `build_playback_html`)

```js
let trafficMode = false;
let trafficCounts = new Map();  // edge_key → count

function rebuildTraffic() {
    trafficCounts.clear();
    for (const ev of events) {
        if (ev.kind === 'edge_enter' && ev.edge_key) {
            trafficCounts.set(ev.edge_key, (trafficCounts.get(ev.edge_key) || 0) + 1);
        }
    }
}

// renderMap 에서:
if (trafficMode) {
    // 사고 히트맵과 같은 패턴, 색조만 다르게
    ...
}
```

## Tests

```python
def test_edge_enter_events_collected():
    """시뮬 후 trace.events 에 edge_enter 이벤트 들어있음"""

def test_traffic_count_matches_traversal():
    """간단한 시나리오 — AGV 가 엣지 N 번 통과 → count == N"""

[수동] 1. 시뮬 1~2분 돌린 후 🚦 토글 on → 색조 깔림
[수동] 2. 🔥 토글 → 🚦 자동 off (mutually exclusive)
[수동] 3. legend 가 활성 모드에 맞게 변경
[수동] 4. 둘 다 off → 평소 화면
[수동] 5. AGV 가 많이 지나간 메인 코리도가 가장 진하게 표시
```

## DO NOT

- 시간대별 분해 / 윈도우 카운트
- AGV 별 분해
- 사고 히트맵과 동시 활성 (혼동 방지)
- 노드 점유 카운트 (별도)
- traffic 카운트 영구 저장

## Acceptance

- 위 2개 단위 테스트 PASS
- 5개 수동 시나리오 통과
- 사고 히트맵과 색조 명확히 구분
- legend 자동 갱신

## Final Verification

```bash
python tests/integration/test_simulation.py

./run quickrun
# 1~2분 시뮬 → 🚦 토글 → 메인 코리도가 진하게 표시 확인
```

스크린샷 (사고 히트맵 vs 트래픽 히트맵 비교) 첨부 권장.
