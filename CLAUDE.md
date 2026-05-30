# CLAUDE.md — vda5050_sim_v2

## 🎯 정체성

**실 평면도 기반 FAB AMR what-if 시뮬레이터 + RMF 호환 sandbox.**

- **Primary 사용자**: 운영/배치 의사결정자 — "이 실제 맵을 이렇게 운영하면 어떻게 될까" 답하는 것이 주 목적
- **Secondary 사용자**: 알고리즘 연구자 — 토폴로지 비교 등 정형 실험은 Advanced 영역으로 유지
- **포지셔닝**: Open-RMF 가 되려 하지 않음. RMF 데이터 포맷 호환 + 가벼운 what-if 도구로 차별화.

### 핵심 가치 (우선순위 순)

1. **실 평면도 즉시 import** → 5분 안에 시각/시뮬 (외부 JSON/YAML)
2. **GUI 정책 부여** (방향, 노드 역할, fleet 분리) — Map Editor
3. **정책 변형 case 비교** — `run_imported_cases.py`
4. **RMF 호환 데이터** 로 장기 통합 경로 보존
5. **토폴로지 비교 (Advanced)** — 알고리즘 검증·연구용. 더이상 주력 X.

### 진입점

- **운영 도구 (주력)**: `./run quickrun` → 외부 맵 업로드 → Editor → 시뮬
- **Case 비교**: `python scripts/run_imported_cases.py <yaml>`
- **연구/토폴로지 비교 (Advanced)**: `python -m src.application.usecases.experiment_runner --experiment ...`

스택: Python 3.12, asyncio, FastAPI, VDA5050 / Open-RMF 개념 참조.

> **Companions**:
> - 사용자 가이드: [`README.md`](README.md) — 사용자가 무엇을 어떻게 쓰나
> - 시스템 설계: [`ARCHITECTURE.md`](ARCHITECTURE.md) — 디렉토리/도메인/토폴로지/KPI 표
> - 이력: [`HISTORY.md`](HISTORY.md) — 날짜별 실험 결과 + 완료 작업
> - **다음 작업 정의서: [`specs/`](specs/)** — spec-driven, 진입은 [`specs/README.md`](specs/README.md)
>   - 작성 표준: [`specs/SPEC-AUTHORING.md`](specs/SPEC-AUTHORING.md)
>   - 사용자 의도 박제: [`specs/operations-scenarios.md`](specs/operations-scenarios.md) ← 모든 spec 의 상위 reference

---

## 핵심 아키텍처 결정 (수정 시 주의)

### 공유 그래프 race condition
`_reroute()`에서 그래프를 임시 수정하면 다른 AGV의 A*와 race condition 발생.
**절대 `_out_edges`를 임시 제거하지 말 것** — `get_path(blocked_edges=set())` 사용.

```python
# ❌ race condition
graph._out_edges[src].remove(eid); path = graph.get_path(...); graph._out_edges[src] = orig
# ✅ 안전
path = graph.get_path(src, dst, blocked_edges={(src, dst)})
```

### 예약 4계층
- **노드 점유**: 정차/대기 충돌
- **엣지 예약**: 이동 중 head-on 차단 (역방향 활성 시 reject) + same-direction follow-on headway
- **Itinerary 예약**: 전체 path 시간윈도우 atomic (실패 시 전부 롤백)
- **Critical section**: bay / siding / station_access / charger_access / shared corridor를 section_key로 묶고 capacity 검사 (대부분 1)

### Conflict Resolution
```
retry 1~3회: 0.1s 고정 / 4~10회: backoff (≤0.3s) / 11+회: force_reroute()
타입별 reroute 임계치: A/C 5, D 8, E 8, B 15 (siding 우선)
```

### DemandSet
- `common_demand`: 모든 토폴로지에 동일 pickup/dropoff seq → 처리력 비교용
- `capability`: 해당 토폴로지에서 routeable한 pair만 → 토폴로지 내부 효율
- `pickup/dropoff_processing_time_s`는 seed 기반 deterministic
- `tasks_completed` = AGV station processing 완료 / `demands_completed` = dropoff processing 종료 (실수요)

### Ranking 정렬
`completion_rate` desc → `task_acceptance_rate` desc → `demand_throughput_per_hour` desc → `total_wait_time_s` asc → `headon_total` asc → `retry_total` asc.

### Type E creep
`graph._lane_mode == "bidirectional_creep"` 태그로 자동 적용.
`AGV._get_effective_speed()`가 head-on 감지 시 0.3m/s 반환.

### head-on 카운터 3종
`_edge_headon_counts`(진성) / `_edge_retry_counts`(폴링) / `_edge_congestion_counts`(합산).
`retry_total`은 진성 실패 아님.

---

## 실행

### 운영 도구 (Primary)

```bash
# 1. 라이브 시뮬 서버 (Quickrun)
./run quickrun                                     # Linux/Mac
run.bat quickrun                                   # Windows
# → http://127.0.0.1:8765/ — 외부 맵 업로드 + Editor + 시뮬

# 2. 외부 맵 임포트 + Editor (CLI)
python scripts/import_map_demo.py <map.json> --edit --open

# 3. Case 비교 (여러 정책 변형 × seed 배치)
python scripts/run_imported_cases.py experiments/<yaml> --open
```

### 연구·검증 (Advanced)

```bash
# 토폴로지 비교 (A~E 정형 토폴로지)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/topology_saturation_common_demand.yaml

# playback 함께 보고 싶으면 showcase + merge
python -m src.application.usecases.experiment_runner \
  --experiment experiments/topology_showcase.yaml
python scripts/merge_showcase_into_saturation.py <SAT_DIR> <SHOWCASE_DIR>
```

### 회귀 안전망

```bash
python tests/integration/test_simulation.py        # T1~T68 (70개 테스트)
```

산출물:
- 운영: `outputs/imported_cases/<ts>/{ranking.csv,summary.json,report.html}`
- 연구: `outputs/experiments/<run_id>/{summary,ranking,report}.{csv,json,html}` + `{variant}/playback.{html,trace.json}`

---

## 현재 우선순위 (2026-05-25 재정의)

### 완료 (이번 사이클)
- [x] **외부 맵 임포터** — JSON/YAML import + 자동 추론 (양방향 100%, 코리도 클러스터링, 도달성)
- [x] **Map Editor** — Paint(방향) / Stamp(역할) / Build(노드·엣지 add·del) / 다중선택 / Undo
- [x] **Quickrun 라이브 시뮬** — 실시간 SVG + KPI + 히트맵 + 충돌 마커
- [x] **Quickrun ↔ Editor 통합** — Save & Run, /upload-map, /edit/{id}
- [x] **Case 비교 CLI** — `run_imported_cases.py`
- [x] **(F1) Per-edge v_max** (Agent A) — `Edge.v_max` + editor Speed 모드
- [x] **(b) 시각화 2차** — 3-pane / 사고 클릭→맵 강조 / blocking chain / 히트맵
- [x] **Windows 호환** — `run.bat` + sys.path 자동 추가
- [x] **Agent B Dispatcher/KPI** — JobDispatcher, JobApi, manual mode
- [x] **F1a (Multi-graph / 이기종 fleet)** — 2026-05-25 완료.
  - 엔진: `Edge.graph_idx`, `Fleet` 클래스, `MapGraph.get_path(fleet=)` 필터링
  - 디스패처: `required_capability` → fleet capability 매칭
  - AGV: 7개 `get_path` 호출에 `fleet=self.fleet` 연결
  - Quickrun UI: AGV fleet 색 ring, Fleet KPI 카드, fleet별 count 슬라이더, 임포트 맵 드롭다운
  - 임포터: `type/is_charger/is_station/is_holding_point/capability` 명시 필드 인식
  - 검증 맵: `scripts/generate_synthetic_3fleet.py` → TYPE_A/B/C 3-fleet 합성 맵
  - 테스트: T66 20/20, 통합 63/63 PASS
- [x] **GAP-0 Traffic Semantics Audit** — 2026-05-30 완료.
  - 예약 4계층 (node/edge/itinerary/section) contract 검증 및 문서화
  - T68-1~T68-7 추가 (node exclusivity, head-on, follow-on, critical section, itinerary atomic, facility node)
  - Quickrun AGV 겹침 조사: 시각화 현상 (실제 버그 아님, edge interpolation/node stacking)
  - ARCHITECTURE.md: "Traffic Schedule Semantics Contract" 섹션 — 보장/미보장 명확화
  - 테스트: 70/70 PASS (기존 63 + 새 T68 7)
  - RMF YAML import/export 진행 가능 확인 완료

### 다음 사이클 — 주력 (운영 도구)

- [ ] **RMF building_map YAML import/export** ← **1순위**. 폐쇄망 데이터 호환. 견적 ~1일.
- [ ] **Background image overlay** ← 2순위. 실 도면 PNG 위에 그리기. 견적 ~1일.
- [ ] **Editor 속성 풀 편집** — vertex lock_radius, edge capacity/width 등.

### Advanced (연구·검증 영역, 후순위)

- [ ] **(a) 혼합 토폴로지 실험** — corridor 별 type 분리. 운영 도구로는 우선순위 X. 알고리즘 연구 시 가치 있음.
- [ ] **(c) 24h 호라이즌 시나리오** — battery/charging 의미 구간. 폐쇄망 dry run 후 결정.

### 보류 (현 단계 ROI 낮음)
charging contention / priority reservation / 회전 감속 / wait_time 통합 / reachable siding 정밀화 / failure injection — 새 질문 생기기 전까지 보류.

자세한 완료/추진 이력은 [`HISTORY.md`](HISTORY.md).

---

## 개발 원칙

1. **공유 상태 수정 금지** — 그래프/스케줄러는 모든 AGV가 공유. 임시 수정 대신 파라미터 전달.
2. **타입별 invariant 보장** — `validate_invariants()` 통과 필수.
3. **진성/가짜 head-on 구분** — `_edge_headon_counts` vs `_edge_retry_counts`.
4. **FAB 운영 특성 반영** — 장시간 정지 > secondary bottleneck. wait보다 reroute 우선.
5. **AGV는 베이/메인 통로에서 IDLE/PROCESSING 금지** — IDLE은 HP에서, PROCESSING은 ST에서만.
6. **테스트 출력 간결화** — 요약만, lane별 PASS 출력 금지.
