# ARCHITECTURE.md — vda5050_sim_v2

CLAUDE.md에서 분리한 구조 / 맵 / 토폴로지 / KPI / 테스트 레퍼런스.

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
│   │   ├── scenario/      task_generator.py, demand.py
│   │   └── usecases/      experiment_runner.py
│   ├── adapters/bus/      adapters.py (LocalMemoryBus)
│   ├── analytics/         kpi.py, playback_trace.py
│   ├── interfaces/        bus.py
│   └── vda5050/           parser.py, translator.py
├── maps/
│   ├── sample_fab.json    10노드 테스트용 (T1~T12)
│   └── fab_nav_graph.yaml 실제 FAB 맵 (Open-RMF 포맷)
├── experiments/
│   ├── fab_topology.yaml                       빠른 실험 (600s, AGV 8~20)
│   ├── fab_topology_full.yaml                  전체 실험 (1800s, AGV 8~24)
│   ├── type_b_siding_sweep.yaml                Type B siding placement sweep
│   ├── type_b_common_demand_policy_sweep.yaml  Type B policy 비교
│   ├── type_b_mid_reachable_saturation.yaml    Type B 대표안 포화 곡선
│   ├── type_b_mid_reachable_battery_saturation.yaml  배터리 1차
│   ├── type_b_mid_reachable_playback.yaml      playback 샘플 (600s)
│   ├── type_b_mid_reachable_playback_short.yaml playback 디버깅용 (180s)
│   ├── topology_cd_saturation_common_demand.yaml C/D 재정의 후 비교
│   ├── topology_saturation_common_demand.yaml  A/B/C/D/E 포화 곡선
│   └── topology_showcase.yaml                  5타입 × 24 AGV × seed 42 (playback on)
├── scripts/
│   └── merge_showcase_into_saturation.py       saturation dir에 showcase playback 합치기
├── tests/integration/
│   └── test_simulation.py                      T1~T59
└── outputs/experiments/                        실험 결과 CSV/JSON/HTML
```

---

## FAB 맵 스펙

- **크기**: 640 × 120 m
- **좌표**: X=0(서) ~ 640(동), Y=20(남) / 60(중앙) / 100(북)
- **통로**:
  - 북측 엔드베이 Y=100: 폭 2.0m, 1.5m/s (Type A 단방향 동→서, 그 외 양방향/lane 분리)
  - 중앙 통로 Y=60: 폭 1.5m, 1.5m/s (Type A 단방향 서→동, 그 외 양방향/lane 분리)
  - 남측 엔드베이 Y=20: 폭 2.0m, 1.5m/s (Type A 단방향 동→서, 그 외 양방향/lane 분리)
  - 베이 통로 X=0/160/320/480/640: 단방향 교대, 0.8m/s
- **노드 구성** (topology generator 동적 생성):
  - WP_*: 메인 통로 40m 간격 waypoint
  - BAY_*: 베이 내부 10m 간격 waypoint
  - ST_*: 작업 스테이션 (access lane)
  - CH_*: 충전소 (access lane)
  - HP_*: 홀딩 포인트 (idle AGV 대기)
  - SD_*: Type B 사이딩
  - SA_*/CA_*/HA_*: 각 facility access midpoint
- **Holding/Station 위치**:
  - HP: `[40, 120, 200, 280, 360, 440, 520, 600]` 8 × 3 corridor = 24 HP (24 AGV 풀 동시 시작 보장)
  - 중앙 station: `[80, 240, 400, 560]` (베이 X 제외 — 베이 통로 위에는 station 없음)
  - 북/남 station: `[0, 80, ..., 640]` 80m 간격 9개씩

### 로봇 물리 스펙
- 크기: 800 × 1800 mm
- 최대속도: 1.5 m/s
- Protective zone: 200 mm, Warning zone: 500 mm
- 큐 follow-on 거리(엔진): 4.3m (= 2 × ROBOT_LENGTH + protective + warning), MIN headway 1.5s

---

## 토폴로지 타입

| Type | 차선 | 방향 | 교행 처리 | 특징 |
|------|------|------|------|------|
| A | 1차선 | 단방향 (순환) | 없음 | head-on 구조적 불가. 북·남 동→서, 중앙 서→동 |
| B | 1차선 | 양방향 | siding 대피 | head-on 발생, siding으로 해소 |
| C | 2차선 | 단방향 (L1/L2 분리) | 없음 | same-lane head-on 불가. 총 통로폭 2.0m |
| D | 2차선 | 단방향 (L1:동→서, L2:서→동) | 없음 | C와 방향 구조 동일, 총 통로폭 3.0m wide safety |
| E | 1차선 | 양방향 | 크리프 감속 (0.3m/s) | `_lane_mode` 태그로 자동 적용 |

### 베이 통로 (전 타입 공통)
- **방향 교대**: B1(X=0)=북→남, B2(X=160)=남→북, B3=북→남, B4=남→북, B5=북→남
- **속도**: 0.8m/s 고정
- **단방향 강제**: bidir=False
- **내부 waypoint**: BAY_* 중간 노드로 긴 jump edge 대신 실제 세로 lane 형태

### Station / Charger / Holding access 구조
- **station access**: `WP → SA_* → ST_*` 2-hop. access midpoint 1.5m / facility 3.0m 오프셋
- **charger access**: `WP → CA_* → CH_*` 2-hop. center charger는 bay 축이 아니라 FAB 바깥 x 방향에 배치
- **holding access**: `WP → HA_* → HP_*` 2-hop. idle AGV는 main corridor 위가 아닌 HP로 복귀

---

## Traffic Schedule Semantics Contract

시뮬레이터가 **보장하는** 교통 제어 의미:

### 예약 4계층 모델

1. **노드 점유 (Node Occupancy)**
   - capacity=1 노드는 동시에 2대 AGV가 점유할 수 없음
   - 시간 윈도우 충돌 시 예약 거부
   - 검증: T68-1, T6~T8

2. **엣지 예약 (Edge Reservation)**
   - **Head-on 방지**: 단일 양방향 엣지에서 역방향 활성 예약과 시간 겹침 시 차단
   - **Follow-on headway**: 같은 방향 진입 시 최소 headway(1.5s) 미달 시 차단
   - 검증: T68-2 (head-on), T68-3 (follow-on), T35~T36

3. **Itinerary 예약 (Atomic)**
   - 전체 path의 node/edge time-window를 atomic하게 예약
   - 하나라도 충돌 시 **전체 롤백** (부분 예약 없음)
   - 검증: T68-5, T37, T38

4. **Critical Section**
   - 교차로 / shared corridor / station access / charger access / bay를 section_key로 묶음
   - capacity 기반 동시 진입 제한 (대부분 capacity=1)
   - 명시적 hold: `_agv_held_sections`로 시간 만료와 무관하게 점유 유지
   - 검증: T68-4, T38~T41

### Station/Charger Access 제어

- **facility_node_id 메커니즘**: access lane 예약 시 facility node(ST_*/CH_*) 점유 여부 확인
- 다른 AGV가 station을 점유 중이면 access lane 진입 차단
- 검증: T68-6

### Conflict Resolution 정책

- Retry 1~3회: 0.1s 고정 대기
- Retry 4~10회: bounded exponential backoff (최대 0.3s)
- Retry 11+회: force_reroute (blocked_edge 회피 A* 재계획)
- 타입별 reroute 임계치: A/C=5, D=8, E=8, B=15 (siding 우선 탐색)

### 통계 카운터 (3종)

- `_edge_headon_counts`: 진성 head-on 충돌 (역방향 차단)
- `_edge_followon_counts`: 같은 방향 follow-on 안전거리 차단
- `_edge_retry_counts`: 대기 중 재시도 (병목 강도, 폴링 카운터)
- `retry_total`은 진성 실패가 아님 — 대기 후 성공 가능

---

시뮬레이터가 **보장하지 않는** 것:

- **Open-RMF 전체 구현**: traffic schedule database, full negotiation protocol, fleet adapter runtime 동작
- **Multi-floor semantics**: lift, door, traffic light 제어
- **연속 기하 기반 완전 충돌 감지**: 노드/엣지 기반 discrete time-window 예약으로 근사
- **Dynamic obstacle avoidance**: 실시간 센서 기반 회피는 미구현 (경로 재계획으로 대체)
- **완전 데드락 프루프**: 데드락 감지/해소 휴리스틱 존재하나, 모든 경우 방지 보장 X

**포지셔닝**: Open-RMF 포맷 호환 FAB AMR what-if 시뮬레이터. RMF 전체 구현체가 아님.

---

## Topology Invariants (`validate_invariants()`)

| Type | 보장 내용 |
|------|------|
| A | 메인 corridor 내 역방향 엣지 쌍 없음 |
| C | 각 lane(L1/L2) 내 역방향 엣지 쌍 없음 |
| D | 각 lane 내 역방향 엣지 쌍 없음 (L1/L2 각각 단방향) |
| E | `graph._lane_mode == "bidirectional_creep"` 태그 존재 |

---

## KPI 목록 (`kpi.py`)

| KPI | 설명 |
|------|------|
| tasks_completed | AGV station processing 완료 횟수 (pickup/dropoff 처리 proxy) |
| demands_completed | 실제 물류 수요 완료 (dropoff processing 종료) |
| completion_rate | `demands_completed / tasks_requested` |
| demand_throughput_per_hour | 시간당 실제 물류 수요 완료량 |
| throughput_tasks_per_hour | 시간당 station processing 완료량 |
| avg_task_completion_time_s | 평균 AGV order 처리 시간 |
| avg_wait_time_s | AGV당 평균 대기 시간 |
| total_restart_delay_s | processing/wait 이후 재출발 지연 누적 |
| reservation_failure_rate | 예약 실패율 |
| agv_utilization | NAVIGATING+PROCESSING / sim_time |
| node_occupancy_rate / edge_occupancy_rate | 노드/엣지 점유율 |
| headon_total | 진성 head-on 충돌 |
| followon_total | 같은 방향 follow-on 안전거리 차단 |
| section_conflict_total | critical section time-window 충돌 |
| retry_total | 대기 중 재시도 (병목 강도) |
| itinerary_success / itinerary_failure | path 사전 예약 성공/실패 |
| avg_retry_per_headon | 충돌 1건당 평균 재시도 |
| bottleneck_nodes | congestion_score 상위 5 노드 |
| bottleneck_edges | edge_type / corridor / access_type / section_key / dominant_cause 분류 |
| deadlock_or_stall_count | 데드락 감지/해소 횟수 |
| charging_sessions / total_charging_time_s / low_battery_charge_requests | 충전 KPI |
| avg_battery_pct / min_battery_pct | 배터리 KPI |

### 결과 해석 시 주의
- **Type A 처리량 비선형**: AGV 과밀 시 available 스테이션 부족으로 태스크 생성 안 됨
- **Type C/D 처리량 정상화 완료**: 과거 L1-only 연결 버그 수정 후 처리량 B/E 수준
- **가동률 유사**: 교행 대기보다 스테이션 처리 시간(30~120s)이 지배적
- **Type E 처리량 높음**: 크리프가 head-on 시에만 적용되므로 평균 속도는 1.5m/s에 가까움

---

## 통합 테스트 (T1~T59)

```
T1~T5    sample_fab.json — 그래프 로드, 노드 역할, A*, APPROACH 감지
T6~T8    TimeWindowScheduler — 예약/충돌/release/congestion
T9~T10   LocalMemoryBus — pub/sub, 와일드카드
T11~T12  TaskGenerator + 풀 시뮬 (300s, 3 AGV)
T13~T18  FAB 맵 — 노드 수, 경로, 단방향, 속도, 연결성
T19      FAB 풀 시뮬 (300s, 5 AGV)
T20      FAB 스트레스 완주 (1800s, 20 AGV)
T21      Topology Invariant 전체 검증
T22      Type E creep policy 주입 검증
T23      Type A head-on 엣지 쌍 없음 (정적)
T24      Type C same-lane head-on 없음
T25      Type D same-lane head-on 없음
T26      TaskGenerator diagnostics 카운터
T27      KPI head-on 필드 회귀
T28      Type C/D station pair reachability
T29      Type A routeable task selection
T30      Type C/D width metadata
T31      DemandSet common/capability 생성
T32      Common demand lifecycle metrics
T33      Real demand completion event/KPI
T34      Topology ranking summary
T35      Same-direction follow-on headway 차단
T36      Type D wide corridor follow-on headway 축소
T37      Itinerary reservation atomic conflict
T38      Critical section conflict
T39      Critical section key generation
T40      Critical section capacity
T41      Type C/D lane section capacity 동일
T42      Motion model acceleration
T43      Restart delay accounting
T44      AGV pickup/dropoff processing split
T45      Head-on semantic regression — 5 토폴로지, 600s/12AGV, Phase 3 baseline
         A/C/D == 0, B < 400 / E < 300 (seed 고정 양방향 upper bound)
T46      Type B siding placement sweep — base/mid/dense coverage 비교
T47      Invalid Type B siding placement 거부
T48      bottleneck edge 해석 — edge_type / section_key / dominant_cause
T49      Type B reachable siding policy
T50      Type B policy switch — adjacent vs reachable
T51      Battery low SOC at charger → CHARGING
T52      Battery charging target recovery
T53      Battery low SOC charger reroute
T54      TaskGenerator skips low-battery AGV
T55      Battery payload drain rate split (8%/h vs 12%/h)
T56      report.json schema builder
T57      station/charger access geometry (3m offset / center charger 외곽 x)
T58      bay internal waypoint
T59      start pool seeded shuffle + N/C/S 분산
T60      KPI distribution fields (wait/travel_times)
T61      Per-edge v_max (F1b-core) — Edge.v_max, importer, AGV effective_speed
T62~T67  (F1a Multi-graph / Fleet) — 생략 (별도 문서)
T68      Traffic Schedule Semantics Contract (GAP-0)
T68-1    Node exclusivity — capacity=1 동시 점유 차단
T68-2    Edge head-on prevention — 역방향 활성 예약 차단
T68-3    Same-direction follow-on headway — 최소 간격 차단
T68-4    Critical section capacity — section capacity 제한
T68-5    Itinerary atomic reservation — 전체 실패 시 부분 예약 없음
T68-6    Station access facility_node conflict — facility 점유 시 access 차단
T68-7    Traffic semantics integration — Type B 60s 통합 검증
```
