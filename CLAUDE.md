# CLAUDE.md — vda5050_sim_v2 FAB AMR Simulator

## 프로젝트 개요

반도체 FAB 환경의 AMR(Autonomous Mobile Robot) 플릿 운영을 시뮬레이션하고,
맵 토폴로지별 KPI를 비교 분석하는 시스템.

- **레포**: `xorb1207/amr-sim` (구버전) → `vda5050_sim_v2` (현재)
- **스택**: Python 3.12, FastAPI, asyncio, React/Vite, Recharts
- **표준**: VDA5050 (AGV 통신), Open-RMF (nav graph, 교통 스케줄링 개념 참조)

---

## 디렉토리 구조

```
vda5050_sim_v2/
├── src/
│   ├── domain/
│   │   ├── agv/           agv.py, fsm.py, motion.py
│   │   ├── map/           graph.py, topology_generator.py
│   │   ├── reservation/   scheduler.py
│   │   └── policy/
│   ├── application/
│   │   ├── engine/        simulation_engine.py
│   │   ├── scenario/      task_generator.py
│   │   └── usecases/      experiment_runner.py
│   ├── adapters/bus/      adapters.py (LocalMemoryBus)
│   ├── analytics/         kpi.py
│   ├── interfaces/        bus.py
│   └── vda5050/           parser.py, translator.py
├── maps/
│   ├── sample_fab.json    10노드 테스트용 (T1~T12)
│   └── fab_nav_graph.yaml 실제 FAB 맵 (82노드, Open-RMF 포맷)
├── experiments/
│   ├── fab_topology.yaml          빠른 실험 (600s, AGV 8~20)
│   ├── fab_topology_full.yaml     전체 실험 (1800s, AGV 8~24)
│   └── type_b_siding_sweep.yaml   Type B siding placement sweep (base/mid/dense × AGV 8~20)
├── tests/integration/
│   └── test_simulation.py     T1~T48
└── outputs/experiments/       실험 결과 CSV/JSON
```

---

## FAB 맵 스펙

- **크기**: 640×120m
- **좌표**: X=0(서) ~ 640(동), Y=20(남) / 60(중앙) / 100(북)
- **통로**:
  - 북측 엔드베이 Y=100: 폭 2.0m, 양방향, 1.5m/s
  - 중앙 통로 Y=60: 폭 1.5m, 단방향(동→서), 1.5m/s
  - 남측 엔드베이 Y=20: 폭 2.0m, 양방향, 1.5m/s
  - 베이 통로 X=0/160/320/480/640: 단방향 교대, 0.7m/s
- **노드**: 82개 (WP 51 + Station 23 + Charger 8)
- **스테이션**: 북9 / 중앙5(북쪽포켓) / 남9 = 23개, 80m 간격
- **충전소**: 8개, 서/중/동 분산

### 로봇 물리 스펙
- 크기: 800×1800mm
- 최대속도: 1.5m/s
- Protective zone: 200mm, Warning zone: 500mm

---

## 맵 토폴로지 타입

| Type | 차선 | 방향 | 교행 처리 | 특징 |
|------|------|------|------|------|
| A | 1차선 | 단방향 (순환) | 없음 | head-on 구조적 불가 |
| B | 1차선 | 양방향 | siding 대피 | head-on 발생, siding으로 해소 |
| C | 2차선 | 단방향 (L1/L2 분리) | 없음 | same-lane head-on 불가 |
| D | 2차선 | 단방향 (L1:동→서, L2:서→동) | 없음 | C와 방향 구조 동일, lane width=2.0m의 wide safety 모델 |
| E | 1차선 | 양방향 | 크리프 감속 (0.3m/s) | _lane_mode 태그로 자동 적용 |

### 베이 통로 (전 타입 공통)
- **방향 교대**: B1(X=0)=북→남, B2(X=160)=남→북, B3=북→남, B4=남→북, B5=북→남
- **속도**: 0.7m/s 고정
- **단방향 강제**: bidir=False

---

## 핵심 아키텍처 결정

### Open-RMF 기반 맵 포맷
실제 Open-RMF 라이브러리 연동이 아닌 **개념 참조 구현**:
```
Open-RMF 개념          우리 구현
rmf_traffic::Schedule  TimeWindowScheduler
Trajectory 제출        reserve_itinerary()
승인(Approval)         reserve() → True/False
nav graph YAML         fab_nav_graph.yaml
```
- 맵 로드: `MapGraph.from_rmf_yaml("maps/fab_nav_graph.yaml")`
- 기존 호환: `MapGraph.from_json("maps/sample_fab.json")`

### 엣지 예약 (Phase 3 핵심)
노드 점유만으로는 head-on 감지 불가 → edge/itinerary 예약 추가:
```
노드 점유: 정차/대기 충돌 처리
엣지 예약: 이동 중 교행(head-on) 감지
  - reserve_edge(src, dst): 역방향 활성 예약 있으면 False
  - follow-on(같은 방향)은 진입 headway로 안전거리 제어
  - Type D wide lane은 Type C보다 짧은 follow-on headway를 사용
Itinerary 예약:
  - reserve_itinerary([...segments]): 전체 path의 node/edge time-window를 atomic 예약
  - 실패 시 아무 segment도 추가하지 않음
Critical section 예약:
  - bay / siding / station_access / charger_access / B,E shared corridor를 section_key로 묶음
  - 같은 section time-window가 capacity를 초과해 겹치면 itinerary를 atomic reject
  - bay/access/siding은 capacity=1
  - Type D wide lane(width=2.0m)은 capacity=2, Type C narrow lane(width=1.5m)은 capacity=1
```

### Conflict Resolution 정책
```python
retry 1~3회:  0.1s 고정 대기
retry 4~10회: bounded exponential backoff (최대 0.3s)
retry 11+회:  force_reroute() — blocked_edge 회피 A*

타입별 reroute 임계치:
  A/C: 5회  (단방향, 우회 경로 있음)
  D:   8회
  B:   15회 (siding 탐색 우선)
  E:   8회  (크리프 우선, 그다음 reroute)
```

### 중요: 공유 그래프 race condition
`_reroute()`에서 그래프를 임시 수정하면 다른 AGV A*와 race condition 발생.
**절대 `_out_edges`를 임시 제거하지 말 것** — `get_path(blocked_edges=set())`를 사용:
```python
# 잘못된 방법 (race condition)
graph._out_edges[src].remove(eid)
path = graph.get_path(...)
graph._out_edges[src] = original  # 다른 AGV가 이 사이에 탐색하면 버그

# 올바른 방법
path = graph.get_path(src, dst, blocked_edges={(src, dst)})
```

### head-on 카운터 3종
```python
_edge_headon_counts: 역방향 차단 발생 횟수 (진성 충돌)
_edge_retry_counts:  대기 중 재시도 횟수 (병목 강도 지표)
_edge_congestion_counts: 합산 (하위호환)
```
- `retry_total`은 진성 실패가 아닌 head-on 대기 중 polling 횟수
- head-on regression 임계값과 deadlock==0 검증은 T45 (topology별 Phase 3 baseline) 기준으로 관리. T20은 FAB stress 완주/활동성 기준.

### DemandSet 비교 모드
`src/application/scenario/demand.py`는 topology 비교용 deterministic demand sequence를 생성한다.
- `common_demand`: 모든 topology에 같은 pickup/dropoff sequence를 투입한다. 불가능 task는 rejected/backlog KPI로 집계해야 한다.
- `capability`: 해당 topology에서 routeable한 pickup/dropoff pair만 생성한다. topology 내부 효율 비교용이다.
- `pickup_processing_time_s`와 `dropoff_processing_time_s`는 demand 생성 시 seed 기반 deterministic random으로 고정된다.
- `processing_time_s`는 pickup+dropoff 합산 호환 필드다.
- lifecycle KPI: `tasks_requested`, `tasks_dispatched`, `tasks_rejected_unreachable`, `tasks_backlogged`, `demands_completed`, `task_acceptance_rate`, `completion_rate`.
- `tasks_completed`는 AGV station processing 완료 횟수이며, 실제 물류 수요 완료는 dropoff processing 종료 시 발행되는 `demandCompleted` 이벤트의 `demands_completed`를 기준으로 한다.

### Ranking 기준
`experiment_runner.py`는 seed/AGV 대수별로 Type A-E를 정렬하고, topology별 승패를 `ranking.csv`, `ranking.json`, `ranking_aggregate.json`으로 저장한다.
정렬 우선순위는 `completion_rate` desc → `task_acceptance_rate` desc → `demand_throughput_per_hour` desc → `total_wait_time_s` asc → `headon_total` asc → `retry_total` asc.

---

## Topology Invariants (불변조건)

`topology_generator.py`의 `validate_invariants()` 참고:

| Type | 보장 내용 |
|------|------|
| A | 메인통로 corridor 내 역방향 엣지 쌍 없음 |
| C | 각 lane(l1/l2) 내 역방향 엣지 쌍 없음 |
| D | 각 lane 내 역방향 엣지 쌍 없음 (L1/L2 각각 단방향) |
| E | `graph._lane_mode == "bidirectional_creep"` 태그 존재 |

**Type E creep 적용 방식**: policy 주입 없이 `graph._lane_mode` 태그로 자동 적용.
`AGV._get_effective_speed()`가 `graph._lane_mode`를 직접 읽어 head-on 감지 시 0.3m/s 반환.

---

## 테스트 구조 (T1~T48)

```
T1~T5:   sample_fab.json 기반 — 그래프 로드, 노드 역할, A*, APPROACH 감지
T6~T8:   TimeWindowScheduler — 예약/충돌/release/congestion
T9~T10:  LocalMemoryBus — pub/sub, 와일드카드
T11~T12: TaskGenerator + 풀 시뮬 (300s, AGV 3대)
T13~T18: FAB 맵 (fab_nav_graph.yaml) — 노드 수, 경로, 단방향, 속도, 연결성
T19:     FAB 풀 시뮬 (300s, AGV 5대)
T20:     FAB 스트레스 완주 (1800s, AGV 20대) — sim_time ≈ 1800s + 이동 + 태스크 완료
         ※ head-on 상한은 T45로 분리. deadlock count는 fab 양방향 맵 특성상 assertion 제외.
T21:     Topology Invariant 전체 검증
T22:     Type E creep policy 주입 검증
T23:     Type A head-on 엣지 쌍 없음 (정적)
T24:     Type C same-lane head-on 없음 (정적)
T25:     Type D same-lane head-on 없음 (정적)
T26:     TaskGenerator diagnostics 카운터 검증
T27:     KPI head-on 필드 회귀 검증
T28:     Type C/D station pair reachability 검증
T29:     Type A routeable task selection 검증
T30:     Type D width metadata 검증
T31:     DemandSet common/capability 생성 검증
T32:     Common demand lifecycle metrics 검증
T33:     Real demand completion event/KPI 검증
T34:     Topology ranking summary 검증
T35:     Same-direction follow-on headway 차단 검증
T36:     Type D wide lane follow-on headway 축소 검증
T37:     Itinerary reservation atomic conflict 검증
T38:     Critical section conflict 검증
T39:     Critical section key generation 검증
T40:     Critical section capacity 검증
T41:     Type D section capacity > Type C 검증
T42:     Motion model acceleration 검증
T43:     Restart delay accounting 검증
T44:     AGV pickup/dropoff processing split 검증
T45:     Head-on semantic regression — 생성 토폴로지 5종, 600s/12AGV, Phase 3 baseline
         A/C/D == 0 (메인 통로 단방향 + station/charger access node critical section)
         B < 400 / E < 300 (seed 고정 양방향 Phase 3 upper bound)
T46:     Type B siding placement sweep — base/mid/dense coverage 비교
         coverage ratio / longest uncovered gap / siding count per placement
         dense → coverage_ratio=1.0, longest_uncovered_run_m=0.0 보장
         밀도 순서 단조 증가 검증 (mid >= base, dense >= mid)
T47:     Invalid Type B siding placement 거부
T48:     bottleneck edge 해석 — edge_type / section_key / dominant_cause 연결
```

실행:
```bash
python tests/integration/test_simulation.py
```

---

## 실험 러너

```bash
# 빠른 버전 (5타입 × 4대수 × 3 seeds = 60회)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/fab_topology.yaml

# 전체 버전 (5타입 × 5대수 × 5 seeds = 125회)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/fab_topology_full.yaml
```

결과:
- `outputs/experiments/{run_id}/summary.csv`: seed별 raw KPI
- `outputs/experiments/{run_id}/ranking.csv`: n_AGV/seed별 Type A-E rank
- `outputs/experiments/{run_id}/ranking_aggregate.json`: topology별 wins/avg_rank 자동 요약

### 결과 해석 시 주의사항
- **Type A 처리량 비선형**: AGV 과밀 시 available 스테이션 부족으로 태스크 생성 안 됨
- **Type C/D 처리량 정상화 완료**: 과거 L1-only 연결 버그 수정 후 처리량 B/E 수준으로 향상
- **가동률 유사**: 교행 대기보다 스테이션 처리 시간(30~120s)이 지배적
- **Type E 처리량 높음**: 크리프가 head-on 시에만 적용되므로 평균 속도는 1.5m/s에 가까움

---

## KPI 목록 (kpi.py)

| KPI | 설명 |
|------|------|
| tasks_completed | AGV station processing 완료 횟수 (pickup/dropoff 처리 proxy) |
| demands_completed | 실제 물류 수요 완료 횟수 (dropoff processing 종료 기준) |
| completion_rate | 실제 물류 수요 완료율 (`demands_completed / tasks_requested`) |
| demand_throughput_per_hour | 시간당 실제 물류 수요 완료량 |
| throughput_tasks_per_hour | 시간당 station processing 완료량 |
| avg_task_completion_time_s | 평균 AGV order 처리 시간 |
| avg_wait_time_s | AGV당 평균 대기 시간 |
| total_restart_delay_s | processing/wait 이후 재출발 지연 누적 시간 |
| reservation_failure_rate | 예약 실패율 |
| agv_utilization | AGV 가동률 (NAVIGATING+PROCESSING / sim_time) |
| node_occupancy_rate | 노드 점유율 |
| edge_occupancy_rate | 엣지 점유율 |
| headon_total | head-on 진성 충돌 횟수 |
| followon_total | same-direction follow-on 안전거리 차단 횟수 |
| section_conflict_total | critical section time-window 충돌 횟수 |
| retry_total | 대기 중 재시도 횟수 (병목 강도) |
| itinerary_success | 전체 path 사전 예약 성공 횟수 |
| itinerary_failure | 전체 path 사전 예약 실패 횟수 |
| avg_retry_per_headon | 충돌 1건당 평균 재시도 |
| bottleneck_nodes | congestion_score 상위 5 노드 |
| deadlock_or_stall_count | 데드락 감지/해소 횟수 |

---

## 현재 남은 리스크 및 후속 작업

### 완료된 기반
- [x] Type C/D 처리량 낮은 원인 확정 및 수정 (Bug #1: corridor 이름 불일치 → vacuous test pass, Bug #2: 스테이션/충전소 L1-only 연결 → L2 AGV 강제 U턴)
- [x] `kpi.py`에 `get_headon_summary()` 연결
- [x] station/charger access critical section을 설비 node 단위로 묶기
- [x] DemandSet common/capability 생성 기반
- [x] common demand lifecycle KPI 1차 연결
- [x] dropoff 기준 실제 demand 완료 이벤트/KPI 연결
- [x] multi-seed 반복 실험 설정
- [x] topology ranking 기준 및 승패 자동 요약
- [x] same-direction follow-on 안전거리 1차 반영
- [x] Open-RMF식 itinerary/pre-reservation 1차 API 및 AGV 연결
- [x] critical section 예약 1차 반영
- [x] edge capacity / lane width 영향 1차 반영
- [x] 가감속 / 재출발 지연 1차 반영
- [x] pickup/dropoff processing randomness 분리

### 지금 당장 (비교 설명력 강화)
- [x] Type B siding 커버리지 분석 완료 및 개선 후보 정리
  - 현재 Type B는 corridor별 `17개 main node 중 5개`만 siding 인접 (`coverage_ratio=0.2941`)
  - 모든 corridor(N/C/S)에서 `longest_uncovered_run_m=80.0`로 동일한 회피 공백 반복
  - 현재 siding은 bay x 위치 기준 15개로 배치되어 중간 구간(`WP_*_040`, `080`, `160`, `200`, `240` 등) 공백이 큼
  - 개선 후보 1: 긴 공백 구간 우선 증설하는 `placement sweep (base/mid/dense)`
  - 개선 후보 2: 인접 siding만 찾지 않고 가까운 reachable siding을 탐색하는 `reroute policy 개선`
- [x] **Type B siding placement sweep 구현 및 실험 (base/mid/dense)**
  - `topology_generator.py`에 `SIDING_POSITIONS` dict 추가 (base/mid/dense 프리셋)
  - `_add_sidings(placement)`, `generate(siding_placement)`, `_build_type_b(siding_placement)` 파라미터화
  - T46을 3-placement sweep 검증으로 확장 (dense → coverage_ratio=1.0, gap=0 보장)
  - `experiment_runner.py`: `siding_placements` 파라미터 추가, `type_b_siding_sweep.yaml` 실험 파일 생성
  - **주요 결과 (seed=42/100/200 평균, 300s)**:
    | placement | AGV=16 headon | AGV=16 wait | AGV=20 headon | AGV=20 wait |
    |-----------|--------------|-------------|--------------|-------------|
    | base  (15 sidings) | 37.3 | 111.5s | 366.7 | 121.2s |
    | mid   (27 sidings) | 86.3 | 135.8s | 383.7 | 173.9s |
    | dense (51 sidings) | 0.0  | 132.3s | 414.7 | 236.0s |
  - **인사이트**: 단순 밀도 증가가 선형적 개선을 보장하지 않음. 중부하(AGV=16)에서 dense는 head-on을 완전 제거하지만 대기시간 증가. 포화(AGV=20)에서는 dense가 오히려 최악. 개선 방향은 **reachable siding policy** (후보 2)가 더 우선.
- [x] bottleneck_edges 정확도 개선 및 head-on/follow-on/section 병목 해석 고도화
  - `bottleneck_edges`에 `edge_type`, `corridor`, `access_type`, `section_key`, `section_conflict_count`, `dominant_cause` 추가
  - shared corridor / lane / bay / siding / station_access / charger_access 분류
  - ranking/summary에서 Type B placement variant(`B/base`, `B/mid`, `B/dense`)를 분리 보존
- [ ] Type B reachable siding policy 개선 — 인접 siding이 아닌 A* 경로 상 가장 가까운 siding으로 회피

### 운영 현실화 2차
- [ ] battery/charging 모델 1차: SOC 소모, low-battery 충전 진입, charger dwell/queue
- [ ] charging reservation/policy 1차: 충전소 점유 경쟁, 충전 우선 dispatch, starvation 방지
- [ ] **critical section 세분화**: priority/release timing 고도화
- [ ] **priority-based reservation**: 배터리/태스크 우선순위 기반 예약 순서
- [ ] **물리 모델 고도화 2차**: head-on 해소 후 재출발 시간, 회전/곡선 감속 반영
- [ ] **wait_time 현실화**: 엣지 예약 대기 + 물리 감속 시간 통합

### Phase 3 완료 항목
- [x] **경로 전체 사전 예약 (pre-reservation) 1차**: 출발 전 경로 전체 시간 윈도우 계산 → 일괄 예약
- [x] **critical section 예약 1차**: 교차로/좁은 bay/양방향 lane을 section 단위로 묶어 예약
- [x] **critical section capacity 1차**: lane width 기반 section capacity 반영
- [x] **물리 모델 고도화 1차**: 가감속 구간, processing 이후 재출발 시간 반영
- [x] **station processing randomness 1차**: seed 기반 pickup/dropoff 처리시간 분리

### 확장 / 장기
- [ ] 시각화: 맵 위 AGV 실시간 이동, head-on 발생 엣지 하이라이트
- [ ] 통로별 조합 실험 (북=B, 중=A, 남=E 등 혼합 시나리오)

---

## 개발 원칙

1. **공유 상태 수정 금지**: 그래프, 스케줄러는 모든 AGV가 공유. 임시 수정 대신 파라미터로 전달
2. **타입별 불변조건 보장**: topology 생성 시 `validate_invariants()` 통과 필수
3. **진성/가짜 head-on 구분**: `_edge_headon_counts` vs `_edge_retry_counts` 분리
4. **FAB 운영 특성 반영**: 장시간 정지 > secondary bottleneck → wait보다 reroute 우선
5. **테스트 출력 간결화**: lane별 PASS 출력 금지, 요약만 출력
