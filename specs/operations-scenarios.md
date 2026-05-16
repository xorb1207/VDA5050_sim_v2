# Operations Scenarios — 사용자 의도 박제

> 2026-05-16 사용자 확정. 모든 spec 은 이 8개 항목 중 어느 것을 충족하는지 매핑한다.
> 새 spec 작성 시 의도가 여기 매핑 안 되면 → 의도 자체를 먼저 정의.

---

## 정체성 (한 줄)

**실 평면도 기반 FAB AMR what-if 시뮬레이터 + RMF 호환 sandbox**

대상: 운영/배치 의사결정자 (Primary), 알고리즘 연구자 (Secondary, Advanced 영역)

---

## 📋 8가지 운영 시나리오

### #1. 현장 맵 → Map Editor 업로드
> 현장의 map 정보를 JSON 파일 형식으로 뽑아서 Map Editor 에 업로드한다.

**구현 매핑**:
- ✅ JSON import (`scripts/import_map_demo.py`, Quickrun `/upload-map`)
- ✅ 자동 추론 (양방향 병합, 코리도 클러스터링, 도달성)
- ⚠ **YAML import 도 필요** (실 시스템 export 가 YAML 인 경우) — `GAP-D`

---

### #2. node/edge 수정 + station/holding/charger 배치
> Map editor 로 node 와 edge 수정이 가능하고, station/holding/charger 등의 배치가 가능하다.

**구현 매핑**:
- ✅ Map Editor 완전 (Paint 방향 + Stamp 역할 + Build 노드/엣지 add·del + 다중선택 + Undo)
- ✅ Stamp 도구: Station / Charger / Holding / Siding / Reset

---

### #3. 시뮬 + 집중 영역 히트맵
> 엔지니어가 그린 map 을 토대로 로봇을 배치하여 시뮬레이션 기본 엔진으로 로봇을 돌리면서 어느 곳에 집중이 많이 되는지 히트맵으로 확인 가능.

**구현 매핑**:
- ✅ Quickrun 라이브 시뮬
- ✅ **사고 히트맵** (head-on / section conflict / follow-on 누적)
- ⚠ **Traffic 밀도 히트맵 필요** (AGV 통과 횟수 누적) — `GAP-C`

---

### #4. 수동 Job 부여
> 중간중간에 실제 엔지니어가 job 을 주는 것처럼 job 을 수동으로도 내릴 수 있다.

**구현 매핑**:
- ✅ JobDispatcher + JobApi + task_generator 의 `manual` 모드 (Agent B 통합)
- ⚠ **Quickrun UI 노출 없음** — REST API 로만 호출 가능 → `GAP-B`

---

### #5. Edge 막기 + reroute 시각 확인
> 지금의 what-if 기능처럼 edge 하나를 막았을 때 reroute 를 어떻게 처리하여 둘러가는지 육안 확인 가능.

**구현 매핑**:
- ✅ Engine: `blocked_edges` 지원 (graph.py:240, agv.py reroute 로직)
- ✅ API: `POST /init` 의 `blockedEdges` 파라미터
- ✅ Reroute 이벤트 마커 (playback)
- ⚠ **Quickrun 라이브 시뮬 중 엣지 클릭으로 차단 UI 없음** → `GAP-A`

---

### #6. KPI 획득
> 시뮬레이션에 대해 KPI 를 얻을 수 있다.

**구현 매핑**:
- ✅ Quickrun 라이브 KPI 카드 (처리량/가동률/충돌/대기) — 60s rolling
- ✅ Case 비교 ranking (`run_imported_cases.py` → `report.html`)
- ✅ Playback 결과 KPI (`outputs/experiments/<id>/`)

---

### #7. Case 비교 + 우선순위 판단
> 위 case 대로 몇 개를 돌려보고 비교해보며 우선순위를 엔지니어가 판단하고 실제 운영 방안 선택 가능.

**구현 매핑**:
- ✅ Case 비교 CLI (`run_imported_cases.py`)
- ✅ 정렬된 비교 표 (`report.html`) — 완료율 / 처리량 / 평균대기 / head-on / retry / deadlock

---

### #8. OpenRMF / VDA5050 모사 — ICS 대변
> 시뮬레이션이 헛된 게 아닌 실제 OpenRMF, VDA5050 을 모사하여 설계해서 우리 파트의 ICS 를 대변할 수 있음을 주변 엔지니어들이 이해 가능해야 함.

**구현 매핑**:
- ✅ VDA5050 / Open-RMF 개념 차용 (CLAUDE.md, README 명시)
- ✅ 예약 4계층 (노드/엣지/Itinerary/Critical section)
- ⚠ **RMF building_map YAML import/export 필요** → `GAP-D`
- ⚠ **VDA5050 메시지 포맷 일부 호환** (선택)
- (장기) **F1a Multi-fleet** — Open-RMF graph_idx 패턴 차용

---

## 🚨 GAP 종합 (다음 사이클 작업 후보)

| GAP | 시나리오 | 견적 | 우선순위 |
|---|---|---|---|
| **GAP-A** Quickrun 라이브 Edge 차단 UI | #5 | ~0.5일 | **1순위** |
| **GAP-B** 수동 Job 부여 UI | #4 | ~0.5일 | **2순위** |
| **GAP-C** Traffic 밀도 히트맵 | #3 | ~0.3일 | **3순위** |
| **GAP-D** RMF YAML import/export | #1, #8 | ~0.5일 | **4순위** |
| **F1a** Multi-fleet | #8 (장기) | ~4.5일 | 5순위 (ABCD 후) |

**합산**: GAP A~D 합쳐도 **~1.8일** — F1a 의 절반 미만이면서 사용자 의도 4개 GAP 해소.
→ **다음 사이클 ABCD 먼저, 그 후 F1a 권장**.

---

## 🔗 관련 spec

각 GAP / Feature 의 자세한 정의:

- (TBD) `specs/GAP-A-edge-block-ui.md`
- (TBD) `specs/GAP-B-manual-job-ui.md`
- (TBD) `specs/GAP-C-traffic-heatmap.md`
- (TBD) `specs/GAP-D-rmf-yaml.md`
- [`specs/F1a-multi-fleet/`](F1a-multi-fleet/) — Multi-fleet (분리됨)

---

## 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-05-16 | 8개 시나리오 박제 + GAP A-D 도출 + 우선순위 재정의 (ABCD > F1a) |
