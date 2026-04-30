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
│   ├── analytics/         kpi.py, playback_trace.py
│   ├── interfaces/        bus.py
│   └── vda5050/           parser.py, translator.py
├── maps/
│   ├── sample_fab.json    10노드 테스트용 (T1~T12)
│   └── fab_nav_graph.yaml 실제 FAB 맵 (82노드, Open-RMF 포맷)
├── experiments/
│   ├── fab_topology.yaml          빠른 실험 (600s, AGV 8~20)
│   ├── fab_topology_full.yaml     전체 실험 (1800s, AGV 8~24)
│   ├── type_b_siding_sweep.yaml   Type B siding placement sweep (base/mid/dense × AGV 8~20)
│   ├── type_b_common_demand_policy_sweep.yaml
│                                Type B common-demand policy 비교
│   ├── type_b_mid_reachable_saturation.yaml
│                                Type B 대표안 포화 곡선 (20/24/28)
│   ├── type_b_mid_reachable_battery_saturation.yaml
│                                Type B battery saturation (운영 SOC 규칙 1차)
│   ├── type_b_mid_reachable_playback.yaml
│                                Type B playback 샘플 (600s, trace/report/playback 생성)
│   ├── type_b_mid_reachable_playback_short.yaml
│                                Type B 짧은 playback 샘플 (180s, UI 디버깅용)
│   ├── topology_cd_saturation_common_demand.yaml
│                                C/D 재정의 후 포화 곡선 비교
│   └── topology_saturation_common_demand.yaml
│                                A/B/C/D/E 포화 곡선 비교 (B=mid/reachable)
├── tests/integration/
│   └── test_simulation.py     T1~T59
└── outputs/experiments/       실험 결과 CSV/JSON
```

---

## FAB 맵 스펙

- **크기**: 640×120m
- **좌표**: X=0(서) ~ 640(동), Y=20(남) / 60(중앙) / 100(북)
- **통로**:
  - 북측 엔드베이 Y=100: 폭 2.0m, 1.5m/s (Type A 단방향 동→서, 그 외 양방향/lane 분리)
  - 중앙 통로 Y=60: 폭 1.5m, 1.5m/s (Type A 단방향 서→동, 그 외 양방향/lane 분리)
  - 남측 엔드베이 Y=20: 폭 2.0m, 1.5m/s (Type A 단방향 동→서, 그 외 양방향/lane 분리)
  - 베이 통로 X=0/160/320/480/640: 단방향 교대, 0.8m/s
- **노드**: topology generator 기준 WP/BAY/Station/Charger/Holding access node 포함 동적 생성
- **스테이션**: north/south는 main corridor에서 3m inset, center는 bay 축과 분리된 측면 access lane 구조
- **충전소**: north/south는 FAB 바깥 y 방향, center는 FAB 바깥 x 방향으로 분리
- **홀딩 포인트**: idle AGV 대기용 `HP_*` / `HA_*` access lane 추가

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
| D | 2차선 | 단방향 (L1:동→서, L2:서→동) | 없음 | C와 방향 구조 동일, 총 통로폭 3.0m의 wide safety 모델 |
| E | 1차선 | 양방향 | 크리프 감속 (0.3m/s) | _lane_mode 태그로 자동 적용 |

### 베이 통로 (전 타입 공통)
- **방향 교대**: B1(X=0)=북→남, B2(X=160)=남→북, B3=북→남, B4=남→북, B5=북→남
- **속도**: 0.8m/s 고정
- **단방향 강제**: bidir=False
- **내부 waypoint**: `BAY_*` 중간 노드를 생성해 playback/예약이 긴 jump edge가 아닌 실제 세로 lane처럼 보이게 구성

### Station / Charger / Holding access
- **station access**: `WP -> SA_* -> ST_*` 2-hop 구조, access midpoint 1.5m / facility 3.0m
- **charger access**: `WP -> CA_* -> CH_*` 2-hop 구조, center charger는 bay 축이 아니라 FAB 바깥 x 방향 배치
- **holding access**: `WP -> HA_* -> HP_*` 2-hop 구조, idle AGV는 main corridor 위가 아니라 holding point로 복귀

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
  - Type D wide corridor(총 3.0m)는 Type C(총 2.0m)보다 짧은 follow-on headway를 사용
Itinerary 예약:
  - reserve_itinerary([...segments]): 전체 path의 node/edge time-window를 atomic 예약
  - 실패 시 아무 segment도 추가하지 않음
Critical section 예약:
  - bay / siding / station_access / charger_access / B,E shared corridor를 section_key로 묶음
  - 같은 section time-window가 capacity를 초과해 겹치면 itinerary를 atomic reject
  - bay/access/siding은 capacity=1
  - Type C/D lane section capacity는 동일하게 1이며, 차이는 총 통로폭에 따른 follow-on headway 모델에 둔다
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
`experiment_runner.py`는 seed/AGV 대수별로 Type A-E를 정렬하고, topology별 승패를 `ranking.csv`, `ranking.json`, `ranking_aggregate.json`, `report.json`으로 저장한다.
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

## 테스트 구조 (T1~T59)

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
T30:     Type C/D width metadata 검증
T31:     DemandSet common/capability 생성 검증
T32:     Common demand lifecycle metrics 검증
T33:     Real demand completion event/KPI 검증
T34:     Topology ranking summary 검증
T35:     Same-direction follow-on headway 차단 검증
T36:     Type D wide corridor follow-on headway 축소 검증
T37:     Itinerary reservation atomic conflict 검증
T38:     Critical section conflict 검증
T39:     Critical section key generation 검증
T40:     Critical section capacity 검증
T41:     Type C/D lane section capacity 동일 검증
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
T49:     Type B reachable siding policy — 인접 siding이 없어도 도달 가능한 siding 선택
T50:     Type B policy switch — adjacent vs reachable 결과 분기 검증
T51:     Battery low SOC at charger -> CHARGING 진입
T52:     Battery charging target recovery
T53:     Battery low SOC charger reroute
T54:     TaskGenerator skips low-battery AGV
T55:     Battery payload drain rate split (8%/h vs 12%/h)
T56:     report.json schema builder — overview / per_topology / comparisons / chart_series
T57:     station/charger access geometry 검증 (3m offset / center charger 외곽 x 배치)
T58:     bay internal waypoint 검증
T59:     start pool seeded shuffle + north/center/south 분산 검증
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
- `outputs/experiments/{run_id}/report.json`: 의사결정/시각화용 structured analytics layer
- `outputs/experiments/{run_id}/{variant}/playback_trace.json`: 시뮬레이션 trace (snapshot/event/path/reservation)
- `outputs/experiments/{run_id}/{variant}/playback.html`: playback UI (planned/reserved/active/blocked edge 시각화)

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
- [x] Type B reachable siding policy 개선 — 인접 siding이 아닌 A* 경로 상 가장 가까운 siding으로 회피
  - `_find_siding_candidate()`가 인접 node 스캔 대신 reachable siding 전체 후보를 평가
  - 현재 막힌 `blocked_edge`를 제외한 경로로 siding까지 도달 가능해야 함
  - siding -> goal 재진입 경로 존재 + 최소 path distance 기준 후보 선택
  - `graph._type_b_siding_policy` 플래그로 `adjacent` / `reachable` 정책 전환 가능
- [x] Type B common-demand policy 비교 프레임 정리
  - `type_b_common_demand_policy_sweep.yaml` 추가: `base/adjacent`, `mid/adjacent`, `mid/reachable`
  - 스모크 테스트(`12 AGV / 300s / seed 42 / common_demand`)로 lifecycle이 실제로 닫히는지 먼저 확인
  - `300s`는 adjacent 정책에 짧아 `demands_completed=0`이 나올 수 있어, 본 실험은 `600s / 5 seeds`로 상향
  - **주요 결과 (600s, demand_count=30, seeds=42/100/200/300/400 평균)**:
    | variant | avg_rank | avg_completion | avg_demand_tph |
    |---------|----------|----------------|----------------|
    | B/base/adjacent | 2.10 | 0.1583 | 28.5 |
    | B/mid/adjacent | 2.00 | 0.1683 | 30.3 |
    | B/mid/reachable | 1.70 | 0.1733 | 31.2 |
  - **해석**:
    - `base -> mid`는 completion/throughput을 소폭 올리고 section conflict를 줄이지만, head-on과 wait는 증가
    - `mid/adjacent -> mid/reachable`는 head-on/section conflict를 크게 줄이고, wait 증가를 감수하는 trade-off
    - 현재 Type B 대표 개선안은 `mid/reachable`
- [x] Type B 대표안 포화 곡선 확인 (`mid/reachable`, common_demand, 600s, 5 seeds)
  - `type_b_mid_reachable_saturation.yaml` 추가: AGV `20/24/28`
  - **주요 결과**:
    | AGV | demands_completed | completion | demand_tph | total_wait_s |
    |-----|-------------------|------------|------------|--------------|
    | 20  | 9.0 | 0.3000 | 54.0 | 242.9 |
    | 24  | 11.4 | 0.3800 | 68.4 | 308.1 |
    | 28  | 12.0 | 0.4000 | 72.0 | 379.4 |
  - **해석**:
    - `20 -> 24`는 유의미한 상승으로, 아직 fleet 부족 구간
    - `24 -> 28`은 completion/throughput 증가폭이 작아져 ceiling 진입 신호
    - wait/section conflict는 계속 증가하므로 28대부터는 교통 병목 비용이 빠르게 커짐
- [x] A/B/C/D/E common-demand 포화 곡선 비교 (pre-battery baseline)
  - `topology_saturation_common_demand.yaml` 추가: AGV `20/24/28`, 600s, 5 seeds, `B=mid/reachable`
  - **2026-04-28 재실험 (siding pickup-preservation fix 후)**: AGV `20/24/28`, 600s, 5 seeds
    | Type | AGV=20 | AGV=24 | AGV=28 |
    |------|--------|--------|--------|
    | A | 0.1933 / — | 0.1933 / — | 0.1867 / — |
    | B(mid/reachable) | 0.2867 / — | 0.3600 / — | 0.4067 / — |
    | C | 0.2866 / — | 0.3867 / — | 0.3867 / — |
    | D | 0.2733 / — | 0.3733 / — | 0.3733 / — |
    | E | 0.0600 / — | 0.0933 / — | 0.1133 / — |
  - **2026-04-28 Topology ranking summary**:
    - `C`: `avg_rank=1.53`, `avg_completion=0.3533`, `avg_demand_tph=63.6`, wins=8/15
    - `B/mid/reachable`: `avg_rank=1.87`, `avg_completion=0.3511`, `avg_demand_tph=63.2`, wins=7/15
    - `D`: `avg_rank=2.80`, `avg_completion=0.3400`, `avg_demand_tph=61.2`
    - `A`: `avg_rank=4.07`, `avg_completion=0.1911`, `avg_demand_tph=34.4`
    - `E`: `avg_rank=4.73`, `avg_completion=0.0889`, `avg_demand_tph=16.0`
  - **이전(buggy siding) vs 현재(fixed) 차이**:
    - 이전 ranking은 `B/mid/reachable`이 1.47로 명확한 1위였음 (`completion=0.3600`)
    - siding 우회 시 픽업을 건너뛰던 버그가 있어 B의 실제 처리력이 부풀려져 있었음 — fix 후
      픽업까지 정상 방문하면서 B의 wait가 커지고 C/D와의 격차가 거의 사라짐
    - 현 ranking은 사실상 `C ≈ B > D ≫ A ≫ E`. C와 B가 statistical tie 수준 (Δrank=0.34)
  - **해석**:
    - C/D는 단방향 + wide 모델 덕분에 fix 영향 없음, B만 정상화로 약간 후퇴
    - C가 wait/section conflict를 가장 안정적으로 관리해 ranking 1위
    - `A`는 head-on이 없지만 20대 이후 completion이 거의 늘지 않아 조기 포화
    - `E`는 section conflict가 지배적이라 고밀도에서 가장 약함
- [x] C/D 총 통로폭 재정의 후 포화 곡선 재검증
  - `topology_cd_saturation_common_demand.yaml` 추가: AGV `20/24/28`, 600s, 5 seeds
  - 정의 수정: `C=총 통로폭 2.0m`, `D=총 통로폭 3.0m`, lane section capacity는 둘 다 `1`
  - **재실험 결과**:
    | Type | AGV=20 | AGV=24 | AGV=28 |
    |------|--------|--------|--------|
    | C | 0.2467 / 44.4 | 0.2933 / 52.8 | 0.3733 / 67.2 |
    | D | 0.2400 / 43.2 | 0.3133 / 56.4 | 0.3867 / 69.6 |
  - **해석**:
    - 수정 후 `C/D`는 거의 비슷한 scaling을 보이고, `D`는 더 넓은 통로폭 덕분에 follow-on 차단이 더 적다
    - 예전처럼 capacity 차이로 벌어지는 모델이 아니라, 의도대로 "총 통로폭에 따른 safety/headway 차이" 중심 비교로 복구됨

### 운영 현실화 2차
- [x] battery/charging 모델 1차: SOC 소모, low-battery 충전 진입, charger dwell/queue
  - 시간 기반 SOC 감소: unloaded `8%/h`, loaded `12%/h`
  - 운영 SOC band: entry `40%`, target `90%`, charge assign 기준 `30%`
  - low-battery AGV는 dispatch 대상에서 제외하고, nearest charger로 진입 후 `CHARGING` 상태에서 dwell
  - KPI 추가: `charging_sessions`, `total_charging_time_s`, `low_battery_charge_requests`, `avg_battery_pct`, `min_battery_pct`
- [ ] charging reservation/policy 1차: 충전소 점유 경쟁, 충전 우선 dispatch, starvation 방지
- [ ] **critical section 세분화**: priority/release timing 고도화
- [ ] **priority-based reservation**: 배터리/태스크 우선순위 기반 예약 순서
- [ ] **물리 모델 고도화 2차**: head-on 해소 후 재출발 시간, 회전/곡선 감속 반영
- [ ] **wait_time 현실화**: 엣지 예약 대기 + 물리 감속 시간 통합

### 다음으로 해야할 일
- [x] **result interpretation/reporting layer 1차**: `report.json`으로 overview / per_topology / comparisons / chart_series 출력
  - 목적: 제3자가 입력 조건과 결과를 함께 보고 winner, trade-off, chart data를 바로 해석할 수 있게 함
  - `overview`: 실험 조건, ranking 정책, winner, 핵심 요약
  - `per_topology`: topology_variant별 aggregate KPI, strengths/weaknesses, bottleneck, use case
  - `comparisons`: winner 대비 delta KPI 자동 생성
  - `chart_series`: completion/throughput/wait/headon/followon/section_conflict/battery chart용 집계
- [x] **battery/charging 1차 + 포화 곡선 2차**: 현재 pre-battery baseline 위에 SOC/charging을 얹어 ceiling 하락폭 확인
  - `type_b_mid_reachable_battery_saturation.yaml`로 600s common-demand sweep 재실행
  - **결론**: 현재 운영값(`8%/h`, `12%/h`, `40~90%`, `30% assign`) 기준으로는 `600s`에서도 `1800s` spot-check에서도 charging 개입이 거의 없음
  - 예: `1800s / 24 AGV / seed 42`에서 `charging_sessions=0`, `min_battery_pct=94.1`
  - 해석: 현재 목적(토폴로지/교통 비교)에서는 battery가 아직 2차 요인이고, ranking을 흔드는 주축은 여전히 topology/traffic 구조
- [ ] **charging reservation/policy 1차**: 충전소 점유 경쟁, 충전 우선 dispatch, starvation 방지
  - battery 영향이 작더라도 multi-charger contention 모델은 별도 축으로 남음
- [ ] **reachable siding policy 정밀화 (후순위)**: 탐색 반경 상한 등으로 wait 증가를 억제하는 미세 조정
  - 현재는 `B/mid/reachable`가 대표안으로 충분히 경쟁력이 있어, battery baseline 확보 후 들어가는 편이 낫다
- [ ] **세부 공간 설계 최적화는 후순위**: node spacing / edge 세부 배치 최적화는 공간 해상도 모델을 높인 뒤 진행
  - 현재 모델은 토폴로지 구조 비교와 스케일링 비교에는 강하지만, 20m vs 40m spacing 같은 세부 layout 최적화 결론엔 아직 해상도가 부족
- [x] **playback trace / UI 1차**: report 다음 단계로 trace 저장과 playback.html 제공
  - snapshot: AGV 위치/상태/배터리/current/target node
  - event: order / wait / reroute / section conflict / charging / demand completed 등
  - map: node/edge 좌표와 role/access metadata
  - playback 표현: node 정지 AGV는 원형, edge 주행 AGV는 진행방향 화살표 (단일 AGV는 edge 위에 정확히 앉음, 군집일 때만 spread)
  - edge 의미: 연파랑=계획 경로, 진파랑=예약 구간, 초록=현재 주행, 빨강=예약 실패로 대기 중인 blocked edge
  - 노드 시각 구분: ST(초록 원), CH(파랑 사각), SD(주황 원), HP(흰 도넛 링), 일반 WP/BAY/access(회색 점)
  - AGV 식별: 모든 AGV body/arrow는 단일 무채색(`#3a4555`)로 통일 — edge state 색(빨강·초록·파랑)과 충돌 회피
  - **blocked_edge_key fallback**: `_pending_edge_src/dst`가 비대칭 클리어되거나 approach 노드 슬롯 대기 중이라 비어 있어도, `blocking_agv` + `WAITING_RESERVATION`이면 `current_node → path[path_index]`로 다음 hop을 추론해 빨강 차단 edge가 항상 그려지도록 보정 (`src/analytics/playback_trace.py`)
  - 라벨 충돌: 같은 노드 버킷에서는 vertical stack으로 분산 (군집은 ID만, 단일은 ID+state)
- [x] **bay / station / charger geometry 현실화 1차**
  - bay는 `WP -> BAY_* -> WP` 내부 waypoint가 있는 세로 lane 구조로 변경
  - station/charger는 direct node jump 대신 짧은 access lane (`SA/CA`)을 거쳐 진입
  - station drop/work 지점은 main corridor에서 3m 이내로 제한
  - center charger(`CH_04/05`)는 bay 축 연장선이 아니라 FAB 바깥 x 방향 배치
- [x] **idle/holding policy 1차**
  - 시작 배치는 `WP_*` 대신 charger + holding point 풀에서 seed 고정 랜덤 분산
  - task가 없고 저전압이 아니면 nearest free holding point(`HP_*`)로 복귀
  - 운영 의미: main corridor 위에서 idle 대기하는 비현실 상태를 줄이고, 충전과 유휴 대기를 분리

### Phase 3 완료 항목
- [x] **경로 전체 사전 예약 (pre-reservation) 1차**: 출발 전 경로 전체 시간 윈도우 계산 → 일괄 예약
- [x] **critical section 예약 1차**: 교차로/좁은 bay/양방향 lane을 section 단위로 묶어 예약
- [x] **critical section capacity 1차**: lane width 기반 section capacity 반영
- [x] **물리 모델 고도화 1차**: 가감속 구간, processing 이후 재출발 시간 반영
- [x] **station processing randomness 1차**: seed 기반 pickup/dropoff 처리시간 분리

### 다음 우선순위 (2026-04-27 합의)

현 실험 호라이즌(600s~1800s) + 토폴로지 비교가 주 목적인 한, 엔진은 사실상 완료 상태이다.
미체크 엔진 항목(charging contention / priority reservation / 회전 감속 / wait_time 통합)은
ranking을 흔들 만한 효과가 거의 없거나(현재 SOC 운영값에서 charging 개입 자체가 발생 안 함),
모든 traversal에 일률적으로 더해져 상대 비교에서 상쇄되거나, 명확한 운영 시나리오 없이
넣으면 결과만 바뀌고 해석은 안 깊어진다. 새 질문이 생기기 전까지는 보류한다.

대신 다음 세 갈래로 진행한다:

- [ ] **(b) 시각화 2차** ← 1순위, 새 실험 없이 도구만 만들어 a/c 결과 해석에 그대로 재사용
  - 3-pane 레이아웃 (맵 + 이벤트/사고 묶음 우측 동시 가시)
  - 사고 묶음 클릭 시 해당 edge·AGV 맵 상 강조 (시간 점프 + 공간 강조)
  - AGV별 reserved path depth 시각화 (몇 단계 앞까지 잡혀있는지)
  - blocking AGV chain drill-down (A→B→C→A 형태의 deadlock 직전 패턴 추적)
  - 단방향 corridor 방향 마커, cross-bucket 라벨 충돌 해소 (잔여 폴리시)
- [ ] **(a) 혼합 토폴로지 실험** ← 2순위, 엔진 확장 선행 필요
  - 현재 `topology_generator.py`는 graph 전체에 단일 type만 적용. corridor별 type 분리 필요
  - 입력 스키마: `corridor_types: {north: B, center: A, south: E}`
  - invariant 검증을 corridor 단위로 분리
  - ranking variant 키에 mixed config 인코딩
  - 실험 yaml 디자인: A를 어느 row에 배치하는지가 핵심 (head-on 없는 row 위치 영향)
- [ ] **(c) 24h 호라이즌 시나리오** ← 3순위, b가 끝난 뒤
  - 엔진 변경 없음, sample interval 조정(0.5s → 5s 정도)으로 trace 크기 제어
  - 24h가 되어야 battery/charging이 비로소 ranking에 들어옴
  - b가 있어야 24h trace를 들여다볼 도구가 있음

### 보류 (현 단계에서 ROI 낮음)
- [ ] charging reservation/policy 1차 — 현 SOC 운영값에서 충전 개입 자체가 거의 없음. (c) 24h 시나리오와 함께 재검토.
- [ ] priority-based reservation — priority 정의(SLA? battery?)에 대한 운영 시나리오 없이 넣으면 해석이 안 깊어짐
- [ ] 물리 모델 2차 (회전/곡선 감속) — 모든 traversal에 일률 적용되어 상대 비교에서 상쇄됨
- [ ] critical section 세분화 (priority/release timing) — priority-based reservation과 종속
- [ ] reachable siding policy 정밀화 — 현재 `B/mid/reachable`가 충분한 경쟁력
- [ ] 세부 공간 설계 최적화 (node spacing 등) — 공간 해상도 모델 고도화 필요
- [ ] failure injection (AGV breakdown, edge closure) — 완전 새 엔진 일감, 장기

### 확장 / 장기
- [ ] **(d) failure/recovery 모델**: AGV breakdown, edge 일시 폐쇄 등 외부 변동 — 새 엔진 영역, 장기

---

## 개발 원칙

1. **공유 상태 수정 금지**: 그래프, 스케줄러는 모든 AGV가 공유. 임시 수정 대신 파라미터로 전달
2. **타입별 불변조건 보장**: topology 생성 시 `validate_invariants()` 통과 필수
3. **진성/가짜 head-on 구분**: `_edge_headon_counts` vs `_edge_retry_counts` 분리
4. **FAB 운영 특성 반영**: 장시간 정지 > secondary bottleneck → wait보다 reroute 우선
5. **테스트 출력 간결화**: lane별 PASS 출력 금지, 요약만 출력
