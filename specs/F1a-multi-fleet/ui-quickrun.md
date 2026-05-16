# F1a Quickrun UI — Fleet 색 + Fleet 별 KPI

> Claude Design 의뢰용. 견적 ~0.7일.
> Engine layer 완료 후 진행. Editor UI 와 병렬 가능.

## Goal

Quickrun 라이브 시뮬 페이지에서 3 fleet 시나리오 가시화. AGV 가 fleet 색으로 구분되고, KPI 카드가 fleet 별로 분리되어 어느 fleet 가 어떻게 동작하는지 한 눈에 보임.

## 사용자 의도 매핑

- 의도 #3 — 시뮬 + 집중 영역 확인 (multi-fleet 환경에서도 동일하게)
- 의도 #6 — KPI (fleet 별 분리해야 의미 있음)
- 의도 #8 — OpenRMF / VDA5050 모사 (3종 로봇 시각)

## Scope

- IN:
  - 시뮬 시작 시 fleet 별로 AGV 배치 (engine 의 Fleet.count, Fleet.graph_idx 사용)
  - AGV 마커 색깔이 Fleet.color
  - 상단 KPI strip 에 fleet 별 카드 추가:
    - "Fleet A · 처리량 N/h · 가동률 M%"
    - "Fleet B · ..."
    - "Fleet C · ..."
  - 토폴로지 드롭다운에서 임포트 맵 선택 시 fleet 정보 자동 인식 (Editor 와 동일 contract)
  - 시뮬 파라미터 폼: fleet 별 count 슬라이더 (선택 — 기본은 YAML/imported map 값)
- OUT:
  - Editor 페이지 (별도 spec)
  - 충돌 마커 / 히트맵 동작 변경 (그대로)
  - fleet 별 다른 속도 모델 (engine 에서 처리)

## Pre-step (discovery — 필수)

1. `src/analytics/playback_trace.py` 의 `build_live_html()` — KPI strip / 파라미터 폼 위치
2. AGV 시각화 함수 — 현재 `agvShapeMarkup`, `agvArrowMarkup` 의 색 결정 로직
3. `/init` POST 응답 — fleet 정보 어떻게 받아오는지
4. WS tick 의 `snapshot.agvs` — 각 AGV 에 fleet_id 가 들어오는지 (engine spec 후 확정)
5. KPI 데이터 source — `RollingKpi` 클래스가 fleet 별 분리 가능한지

→ Engine 완료 후 데이터 흐름 확인.

## Backend Contract

Engine layer 완료 시점:

```python
# /init POST 응답 — 신규 필드
{
  ...
  "fleets": [
    {"id": "A", "color": "#0f9d58", "graph_idx": 0, "count": 6, "agv_ids": ["AGV_001", ..., "AGV_006"]},
    ...
  ]
}

# WS tick — snapshot.agvs 각 AGV 에 fleet_id 추가
{
  "type": "tick",
  "snapshot": {
    "agvs": [
      {"agv_id": "AGV_001", "fleet_id": "A", "state": "NAVIGATING", ...},
      ...
    ]
  },
  "kpi": {
    "by_fleet": {
      "A": {"tasksPerHr": 12.3, "utilization": 0.75, "headOn": 2, "avgWait": 4.5},
      "B": {...},
      "C": {...}
    },
    "overall": {...}  # 기존 동작 유지
  }
}
```

## Interface (UI 변경)

### KPI strip — fleet 별 카드

```
┌────────────────────────────────────────────────────────────────────┐
│ 처리량 50/h   가동률 72%   정면충돌 12회   평균대기 6.3s            │  ← overall (기존)
├────────────────────────────────────────────────────────────────────┤
│ ● A  처리량 22/h  가동률 80%  | ● B  처리량 18/h  가동률 70%  | ● C │  ← fleet 별 (신규)
└────────────────────────────────────────────────────────────────────┘
```

- 색 점 (●) = Fleet.color
- 4개 KPI 가 좁아질 수 있음 → 2개 (처리량, 가동률) 만 fleet 별로
- overall 행은 그대로

### AGV 시각화

- 마커 색깔 = Fleet.color (현재 `#3a4555` 단일 색 → fleet 별 분리)
- legend 에 fleet 표시 추가:
  ```
  ● Fleet A   ● Fleet B   ● Fleet C   (현재 legend 옆에)
  ```

### 시뮬 파라미터 폼

기존:
```
토폴로지: [드롭다운]   AGV: [슬라이더 12]   시뮬속도: ...
```

확장 (imported map 에 fleet 정보 있을 때):
```
토폴로지: [📂 plant_v1]
Fleet A: ● [count 슬라이더 6]
Fleet B: ● [count 슬라이더 4]
Fleet C: ● [count 슬라이더 2]
시뮬속도: ...
```

토폴로지가 단일 fleet (Type A~E 같은 기본) 이면 → 기존 AGV 단일 슬라이더 유지.

## Tests

```
[수동] 1. 3 fleet 임포트 맵 업로드 → 토폴로지 선택 시 Fleet A/B/C 카운트 슬라이더 표시
[수동] 2. ▶ 실행 → AGV 가 fleet 색깔로 구분되어 그려짐
[수동] 3. KPI strip 에 fleet 별 카드 (Fleet A 처리량, Fleet B 처리량) 갱신
[수동] 4. 단일 fleet 토폴로지 (Type A) 선택 → 기존 UI (AGV 단일 슬라이더, fleet 카드 없음)
[수동] 5. Fleet 색이 Editor 페이지의 색과 일치
[수동] 6. legend 에 fleet 표시
```

## DO NOT

- 토폴로지 드롭다운 다중 선택 변경 (계속 단일 선택)
- 히트맵 / 충돌 마커 동작 변경
- 기존 KPI overall 계산 변경
- WebSocket 프로토콜 큰 변경 — kpi.by_fleet 만 추가

## Acceptance

- 위 6개 수동 시나리오 PASS
- 단일 fleet baseline (Type A~E) UI 변화 없음
- Fleet 색이 Editor 와 일치

## Final Verification

```bash
./run quickrun
# → 다음 수동 점검:
# 1. 3 fleet 임포트 맵 업로드
# 2. 토폴로지 선택 → fleet 별 슬라이더
# 3. 실행 → 색 분리 AGV + fleet 별 KPI 카드
# 4. 단일 fleet 토폴로지 → 기존 UI
```

스크린샷 첨부.

## 시각 가이드

- 기존 Quickrun 페이지 light 테마, font 유지
- Fleet 색은 채도 적당히 (너무 진하면 노드/엣지 색과 충돌)
- KPI 카드 크기는 한 줄에 3 fleet 다 들어오게
- 인라인 SVG / CSS 유지 — 외부 의존성 X
