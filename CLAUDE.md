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
│   ├── fab_topology.yaml      빠른 실험 (600s, AGV 8~20)
│   └── fab_topology_full.yaml 전체 실험 (1800s, AGV 8~24)
├── tests/integration/
│   └── test_simulation.py     T1~T33
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
Trajectory 제출        reserve_edge()
승인(Approval)         reserve() → True/False
nav graph YAML         fab_nav_graph.yaml
```
- 맵 로드: `MapGraph.from_rmf_yaml("maps/fab_nav_graph.yaml")`
- 기존 호환: `MapGraph.from_json("maps/sample_fab.json")`

### 엣지 예약 (Phase 3 핵심)
노드 점유만으로는 head-on 감지 불가 → 엣지 예약 추가:
```
노드 점유: 정차/대기 충돌 처리
엣지 예약: 이동 중 교행(head-on) 감지
  - reserve_edge(src, dst): 역방향 활성 예약 있으면 False
  - follow-on(같은 방향)은 허용 — 노드 점유로 제어
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
- `headon_total < 1000` (AGV 20대, 1800초 기준) — regression 임계값

### DemandSet 비교 모드
`src/application/scenario/demand.py`는 topology 비교용 deterministic demand sequence를 생성한다.
- `common_demand`: 모든 topology에 같은 pickup/dropoff sequence를 투입한다. 불가능 task는 rejected/backlog KPI로 집계해야 한다.
- `capability`: 해당 topology에서 routeable한 pickup/dropoff pair만 생성한다. topology 내부 효율 비교용이다.
- `processing_time_s`는 demand에 고정되어 topology 간 processing randomness를 분리하는 기반이다.
- lifecycle KPI: `tasks_requested`, `tasks_dispatched`, `tasks_rejected_unreachable`, `tasks_backlogged`, `demands_completed`, `task_acceptance_rate`, `completion_rate`.
- `tasks_completed`는 AGV station processing 완료 횟수이며, 실제 물류 수요 완료는 dropoff processing 종료 시 발행되는 `demandCompleted` 이벤트의 `demands_completed`를 기준으로 한다.

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

## 테스트 구조 (T1~T33)

```
T1~T5:   sample_fab.json 기반 — 그래프 로드, 노드 역할, A*, APPROACH 감지
T6~T8:   TimeWindowScheduler — 예약/충돌/release/congestion
T9~T10:  LocalMemoryBus — pub/sub, 와일드카드
T11~T12: TaskGenerator + 풀 시뮬 (300s, AGV 3대)
T13~T18: FAB 맵 (fab_nav_graph.yaml) — 노드 수, 경로, 단방향, 속도, 연결성
T19:     FAB 풀 시뮬 (300s, AGV 5대)
T20:     FAB 스트레스 (1800s, AGV 20대) — head-on regression
T21:     Topology Invariant 전체 검증
T22:     Type E creep policy 주입 검증
T23:     Type A head-on 엣지 쌍 없음
T24:     Type C same-lane head-on 없음
T25:     Type D same-lane head-on 없음
T26:     TaskGenerator diagnostics 카운터 검증
T27:     KPI head-on 필드 회귀 검증
T28:     Type C/D station pair reachability 검증
T29:     Type A routeable task selection 검증
T30:     Type D width metadata 검증
T31:     DemandSet common/capability 생성 검증
T32:     Common demand lifecycle metrics 검증
T33:     Real demand completion event/KPI 검증
```

실행:
```bash
python tests/integration/test_simulation.py
```

---

## 실험 러너

```bash
# 빠른 버전 (5타입 × 4대수 = 20회, ~20분)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/fab_topology.yaml

# 전체 버전 (5타입 × 5대수 = 25회, ~45분)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/fab_topology_full.yaml
```

결과: `outputs/experiments/{run_id}/summary.csv`

### 결과 해석 시 주의사항
- **Type A 처리량 비선형**: AGV 과밀 시 available 스테이션 부족으로 태스크 생성 안 됨
- **Type C/D 처리량 낮음**: 원인 분석 중 (스테이션 접근 구조 의심)
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
| reservation_failure_rate | 예약 실패율 |
| agv_utilization | AGV 가동률 (NAVIGATING+PROCESSING / sim_time) |
| node_occupancy_rate | 노드 점유율 |
| edge_occupancy_rate | 엣지 점유율 |
| headon_total | head-on 진성 충돌 횟수 |
| retry_total | 대기 중 재시도 횟수 (병목 강도) |
| avg_retry_per_headon | 충돌 1건당 평균 재시도 |
| bottleneck_nodes | congestion_score 상위 5 노드 |
| deadlock_or_stall_count | 데드락 감지/해소 횟수 |

---

## 현재 남은 리스크 및 후속 작업

### 단기
- [ ] Type C/D 처리량 낮은 원인 확정 (스테이션 L1 연결 구조)
- [x] `kpi.py`에 `get_headon_summary()` 연결
- [ ] bottleneck_edges 정확도 개선 및 head-on 병목 해석 고도화
- [ ] Type B siding 커버리지 분석 (베이 사이 중간 구간 siding 없음)
- [x] DemandSet common/capability 생성 기반
- [x] common demand lifecycle KPI 1차 연결
- [x] dropoff 기준 실제 demand 완료 이벤트/KPI 연결

### 중기 (Phase 3 완성)
- [ ] **경로 전체 사전 예약 (pre-reservation)**: 출발 전 경로 전체 시간 윈도우 계산 → 일괄 예약. 실제 RMF Trajectory 방식에 가장 가까운 구현
- [ ] **priority-based reservation**: 배터리/태스크 우선순위 기반 예약 순서
- [ ] **물리 모델 고도화**: 가감속 구간, head-on 해소 후 재출발 시간 반영
- [ ] **wait_time 현실화**: 엣지 예약 대기 + 물리 감속 시간 통합

### 장기
- [ ] 시각화: 맵 위 AGV 실시간 이동, head-on 발생 엣지 하이라이트
- [ ] 통로별 조합 실험 (북=B, 중=A, 남=E 등 혼합 시나리오)

---

## 개발 원칙

1. **공유 상태 수정 금지**: 그래프, 스케줄러는 모든 AGV가 공유. 임시 수정 대신 파라미터로 전달
2. **타입별 불변조건 보장**: topology 생성 시 `validate_invariants()` 통과 필수
3. **진성/가짜 head-on 구분**: `_edge_headon_counts` vs `_edge_retry_counts` 분리
4. **FAB 운영 특성 반영**: 장시간 정지 > secondary bottleneck → wait보다 reroute 우선
5. **테스트 출력 간결화**: lane별 PASS 출력 금지, 요약만 출력
