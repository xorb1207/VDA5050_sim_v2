# F1a Map Editor UI — Active Graph 토글

> Claude Design 의뢰용. 견적 ~1일.
> Engine layer ([`engine.md`](engine.md)) 완료 후 진행. 데이터 구조 변경 의존.

## Goal

Map Editor 에서 multi-fleet 환경 편집. 사용자가 "지금 어느 fleet 의 graph 를 편집 중인지" 명확히 보이고, 그 graph 의 lane 만 영향받도록.

## 사용자 의도 매핑

- 의도 #2 — Map Editor 에서 node/edge 수정. 이기종 fleet 환경에선 graph 별 분리 편집이 자연스러움.

## Scope

- IN:
  - 우측 패널에 **"Active Graph"** 섹션 추가
  - 라디오 (또는 토글): `Graph 0 (Fleet A)` / `Graph 1 (Fleet B)` / `Graph 2 (Fleet C)`
  - 토글: "Show inactive graphs (faded)" — 다른 graph 의 lane 도 회색으로 보이게 vs 숨김
  - Paint / Build 액션은 활성 graph 의 lane 만 영향
  - 엣지 색깔 fleet 색상으로 자동 (graph 0 = 파랑, graph 1 = 초록, graph 2 = 주황 등 — engine 의 Fleet.color 와 일치)
- OUT:
  - Stamp 도구 동작 변경 (vertex 는 graph 무관 — 변경 X)
  - 새 graph 추가 / 삭제 UI (3개 고정)
  - fleet 속성 (속도 등) 편집 UI

## Pre-step (discovery — 필수)

1. `src/interfaces/map_editor/editor_html.py` 의 mode toggle (Paint / Stamp / Build) 패턴
2. Paint / Build 모드 인터랙션 — 현재 어떻게 trajectory 가 엣지에 적용되는지
3. Edge 시각화 (선 색, 두께) 코드 위치
4. Inspector 우측 패널 컴포넌트 패턴

→ Engine spec 완료 후 `Edge.graph_idx`, `Fleet.color` 등 새 데이터 모델 확인.

## Backend Contract

Engine layer 완료 시점에 다음 데이터 사용 가능:

```python
# build_editor_html 입력 (이미 ImportedMap 받음)
imported.fleets  # list[Fleet] — 신규
imported.edges[i].graph_idx  # int — 신규

# Editor JS 가 받는 PAYLOAD
{
  ...
  "fleets": [
    {"id": "A", "graph_idx": 0, "color": "#0f9d58", "count": 6},
    {"id": "B", "graph_idx": 1, "color": "#2563eb", "count": 4},
    {"id": "C", "graph_idx": 2, "color": "#e0a000", "count": 2}
  ],
  "edges": [
    {"id": "...", "src": "...", "dst": "...", "bidir": true, "graph_idx": 0, ...},
    ...
  ]
}
```

`Save` 시 export 되는 `*.edit.json` 에도 `edge_overrides[id].graph_idx` 변경 가능해야.

## Interface (UI 변경 사항)

### 우측 패널 — Active Graph 섹션 (모드 토글 위 또는 아래)

```
┌─────────────────────────────────────┐
│ Active Graph                        │
│ ◉ Graph 0  ● Fleet A (6 robots)    │
│ ○ Graph 1  ● Fleet B (4 robots)    │
│ ○ Graph 2  ● Fleet C (2 robots)    │
│                                     │
│ ☑ Show inactive graphs (faded)     │
└─────────────────────────────────────┘
```

- 라디오 클릭: 활성 graph 전환
- 색 점 (●) = Fleet.color
- (N robots) = Fleet.count

### 엣지 시각화

- 활성 graph 의 lane: fleet 색깔 진하게 (현재 단방향/양방향 색은 유지 — 색은 fleet 색에서 채도 조정)
- 비활성 graph 의 lane:
  - "Show inactive" 켜짐 → 회색 + opacity 0.3
  - 꺼짐 → 보이지 않음
- 화살표 (단방향/양방향) 표시는 그대로

### Paint / Build 영향 범위

- Paint trajectory → 활성 graph 의 lane 만 방향 변경
- Build Add Edge → 새 엣지의 `graph_idx` = 활성 graph 값
- Build Delete → 활성 graph 의 엣지만 삭제 가능 (비활성 graph 엣지 클릭 무시)

### 키 단축키

- `Tab` 또는 `G` 키 → 활성 graph 순환 (Graph 0 → 1 → 2 → 0)

## Tests

> UI 테스트는 manual 또는 Playwright. illustrative.

```
[수동] 1. 임포트된 맵에 3 fleet 데이터 있음 → 우측 패널에 Active Graph 섹션 표시
[수동] 2. Graph 1 선택 → graph 1 의 lane 만 진하게, 다른 graph 회색
[수동] 3. "Show inactive" 토글 끄기 → 비활성 graph lane 사라짐
[수동] 4. Paint 모드 + 좌클릭 드래그 → 활성 graph 의 가까운 lane 만 단방향 변경
[수동] 5. Build Add Edge → 추가된 엣지의 graph_idx 가 현재 활성 graph
[수동] 6. Build Delete + 비활성 graph 엣지 클릭 → 무반응
[수동] 7. Tab 키 → graph 0 → 1 → 2 → 0 순환
```

## DO NOT

- Stamp 도구 동작 변경 (vertex 는 fleet 무관)
- 새 graph 추가/삭제 UI 만들기 (3개 고정)
- engine 코드 수정 — UI 만 변경
- 자동 추론 로직 변경 — `Edge.graph_idx` 기본값은 engine 이 결정

## Acceptance

- 위 7개 수동 시나리오 PASS
- 기존 Editor 동작 (단일 graph, fleet 정보 없음) baseline 무변화
- 비주얼 — Fleet.color 가 자동으로 엣지 색에 반영

## Final Verification

```bash
# Editor 페이지 생성
python scripts/import_map_demo.py maps/synthetic_3fleet.json --edit --open
# → 위 7개 시나리오 수동 점검
```

수동 점검 결과 + 페이지 스크린샷 첨부 권장.

## 시각 가이드

- 기존 페이지의 light 테마, font 유지
- Fleet 색: engine 의 `Fleet.color` 값 그대로 (CSS variable 또는 inline style)
- Active 표시: 명확하지만 과하지 않게 (현재 모드 토글의 active 스타일과 일관)
- 인라인 SVG 자체 렌더링 — 외부 의존성 X (폐쇄망 친화)
