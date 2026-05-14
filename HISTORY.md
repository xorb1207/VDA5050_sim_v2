# HISTORY.md — 개발 이력 / 실험 결과 아카이브

CLAUDE.md에서 분리한 완료 작업 기록 + 날짜별 실험 결과. 새 작업이 끝날 때마다 추가.

---

## 1. 완료된 기반 (Phase 1~2)

- [x] Type C/D 처리량 낮은 원인 확정 및 수정
  - Bug #1: corridor 이름 불일치로 vacuous test pass
  - Bug #2: 스테이션/충전소 L1-only 연결 → L2 AGV 강제 U턴
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

---

## 2. 비교 설명력 강화 (Type B 중심)

### Type B siding 커버리지 분석
- 초기 Type B는 corridor별 `17개 main node 중 5개`만 siding 인접 (`coverage_ratio=0.2941`)
- 모든 corridor(N/C/S)에서 `longest_uncovered_run_m=80.0` 동일한 회피 공백 반복
- 초기 siding은 bay x 위치 기준 15개로 배치 — 중간 구간(`WP_*_040`, `080`, `160`, `200`, `240` 등) 공백 큼
- 개선 후보 1: 긴 공백 구간 우선 증설하는 placement sweep (base/mid/dense)
- 개선 후보 2: 인접 siding만 찾지 않고 가까운 reachable siding 탐색

### Type B siding placement sweep 구현 및 실험
- `topology_generator.py`에 `SIDING_POSITIONS` dict 추가 (base/mid/dense 프리셋)
- `_add_sidings(placement)`, `generate(siding_placement)`, `_build_type_b(siding_placement)` 파라미터화
- T46을 3-placement sweep 검증으로 확장 (dense → coverage_ratio=1.0, gap=0 보장)
- `experiment_runner.py`: `siding_placements` 파라미터 추가, `type_b_siding_sweep.yaml` 실험 파일

**주요 결과 (seed=42/100/200 평균, 300s)**:
| placement | AGV=16 headon | AGV=16 wait | AGV=20 headon | AGV=20 wait |
|-----------|--------------|-------------|--------------|-------------|
| base  (15 sidings) | 37.3 | 111.5s | 366.7 | 121.2s |
| mid   (27 sidings) | 86.3 | 135.8s | 383.7 | 173.9s |
| dense (51 sidings) | 0.0  | 132.3s | 414.7 | 236.0s |

**인사이트**: 단순 밀도 증가가 선형적 개선을 보장하지 않음. 중부하(AGV=16)에서 dense는 head-on을 완전 제거하지만 대기시간 증가. 포화(AGV=20)에서는 dense가 오히려 최악. 개선 방향은 **reachable siding policy** (후보 2)가 더 우선.

### bottleneck_edges 정확도 개선
- `bottleneck_edges`에 `edge_type`, `corridor`, `access_type`, `section_key`, `section_conflict_count`, `dominant_cause` 추가
- shared corridor / lane / bay / siding / station_access / charger_access 분류
- ranking/summary에서 Type B placement variant(`B/base`, `B/mid`, `B/dense`)를 분리 보존

### Type B reachable siding policy
- `_find_siding_candidate()`가 인접 node 스캔 대신 reachable siding 전체 후보를 평가
- 막힌 `blocked_edge`를 제외한 경로로 siding까지 도달 가능해야 함
- siding → goal 재진입 경로 존재 + 최소 path distance 기준 후보 선택
- `graph._type_b_siding_policy` 플래그로 `adjacent` / `reachable` 정책 전환

### Type B common-demand policy 비교
- `type_b_common_demand_policy_sweep.yaml` 추가: `base/adjacent`, `mid/adjacent`, `mid/reachable`
- `300s`는 adjacent 정책에 짧아 `demands_completed=0`이 나올 수 있어, 본 실험은 `600s / 5 seeds`로 상향

**결과 (600s, demand_count=30, seeds=42/100/200/300/400 평균)**:
| variant | avg_rank | avg_completion | avg_demand_tph |
|---------|----------|----------------|----------------|
| B/base/adjacent | 2.10 | 0.1583 | 28.5 |
| B/mid/adjacent | 2.00 | 0.1683 | 30.3 |
| B/mid/reachable | 1.70 | 0.1733 | 31.2 |

**해석**:
- `base → mid`: completion/throughput 소폭 상승, section conflict 감소, head-on/wait 증가
- `mid/adjacent → mid/reachable`: head-on/section conflict 큰 폭 감소, wait 증가 trade-off
- 대표 개선안: `mid/reachable`

### Type B 대표안 포화 곡선 (`mid/reachable`, common_demand, 600s, 5 seeds)
`type_b_mid_reachable_saturation.yaml`, AGV `20/24/28`:
| AGV | demands_completed | completion | demand_tph | total_wait_s |
|-----|-------------------|------------|------------|--------------|
| 20  | 9.0 | 0.3000 | 54.0 | 242.9 |
| 24  | 11.4 | 0.3800 | 68.4 | 308.1 |
| 28  | 12.0 | 0.4000 | 72.0 | 379.4 |

- `20 → 24`: 유의미 상승, 아직 fleet 부족 구간
- `24 → 28`: 증가폭 작아져 ceiling 진입
- wait/section conflict 계속 증가 → 28대부터 교통 병목 비용 빠르게 커짐

---

## 3. A/B/C/D/E 포화 곡선 비교 (pre-battery baseline)

`topology_saturation_common_demand.yaml`, AGV `20/24/28`, 600s, 5 seeds, `B=mid/reachable`.

### 2026-04-28 재실험 — siding pickup-preservation fix 후
| Type | AGV=20 | AGV=24 | AGV=28 |
|------|--------|--------|--------|
| A | 0.1933 | 0.1933 | 0.1867 |
| B(mid/reachable) | 0.2867 | 0.3600 | 0.4067 |
| C | 0.2866 | 0.3867 | 0.3867 |
| D | 0.2733 | 0.3733 | 0.3733 |
| E | 0.0600 | 0.0933 | 0.1133 |

**Topology ranking summary**:
- `C`: avg_rank=1.53, avg_completion=0.3533, avg_demand_tph=63.6, wins=8/15
- `B/mid/reachable`: avg_rank=1.87, avg_completion=0.3511, avg_demand_tph=63.2, wins=7/15
- `D`: avg_rank=2.80, avg_completion=0.3400, avg_demand_tph=61.2
- `A`: avg_rank=4.07, avg_completion=0.1911, avg_demand_tph=34.4
- `E`: avg_rank=4.73, avg_completion=0.0889, avg_demand_tph=16.0

### 이전(buggy siding) vs 현재(fixed) 차이
- 이전: `B/mid/reachable`이 1.47로 명확한 1위였음 (`completion=0.3600`)
- siding 우회 시 픽업을 건너뛰던 버그가 있어 B의 처리력이 부풀려져 있었음
- fix 후 픽업까지 정상 방문하면서 B의 wait가 커지고 C/D와의 격차가 거의 사라짐
- 현 ranking은 사실상 `C ≈ B > D ≫ A ≫ E` (C와 B는 statistical tie, Δrank=0.34)

**해석**:
- C/D는 단방향 + wide 모델 덕분에 fix 영향 없음, B만 정상화로 약간 후퇴
- C가 wait/section conflict를 가장 안정적으로 관리해 ranking 1위
- A는 head-on이 없지만 20대 이후 completion이 거의 늘지 않아 조기 포화
- E는 section conflict가 지배적이라 고밀도에서 가장 약함

### C/D 총 통로폭 재정의 후 포화 곡선 재검증
`topology_cd_saturation_common_demand.yaml`, AGV `20/24/28`, 600s, 5 seeds.
정의 수정: `C=총 통로폭 2.0m`, `D=총 통로폭 3.0m`, lane section capacity 둘 다 `1`.

| Type | AGV=20 (completion / tph) | AGV=24 | AGV=28 |
|------|---------------------------|--------|--------|
| C | 0.2467 / 44.4 | 0.2933 / 52.8 | 0.3733 / 67.2 |
| D | 0.2400 / 43.2 | 0.3133 / 56.4 | 0.3867 / 69.6 |

- 수정 후 C/D는 거의 비슷한 scaling. D는 더 넓은 통로폭 덕분에 follow-on 차단이 더 적음
- 의도대로 "총 통로폭에 따른 safety/headway 차이" 중심 비교로 복구됨

---

## 4. 운영 현실화 (배터리 등)

### Battery / Charging 모델 1차
- 시간 기반 SOC 감소: unloaded `8%/h`, loaded `12%/h`
- 운영 SOC band: entry `40%`, target `90%`, charge assign 기준 `30%`
- low-battery AGV는 dispatch 대상에서 제외, nearest charger로 진입 후 `CHARGING` 상태에서 dwell
- KPI 추가: `charging_sessions`, `total_charging_time_s`, `low_battery_charge_requests`, `avg_battery_pct`, `min_battery_pct`

### Battery 포화 곡선 spot-check (`type_b_mid_reachable_battery_saturation.yaml`)
- 현재 운영값(`8%/h`, `12%/h`, `40~90%`, `30% assign`) 기준으로는 `600s`는 물론 `1800s` spot-check에서도 charging 개입 거의 없음
- 예: `1800s / 24 AGV / seed 42`에서 `charging_sessions=0`, `min_battery_pct=94.1`
- **결론**: 현재 목적(토폴로지/교통 비교)에서는 battery가 아직 2차 요인. ranking을 흔드는 주축은 여전히 topology/traffic 구조. 24h 시나리오에서나 의미를 가질 것.

---

## 5. 보조 인프라 (report / playback)

### Result interpretation/reporting layer 1차 — `report.json`
- 목적: 제3자가 입력 조건과 결과를 함께 보고 winner, trade-off, chart data를 바로 해석
- `overview`: 실험 조건, ranking 정책, winner, 핵심 요약
- `per_topology`: variant별 aggregate KPI, strengths/weaknesses, bottleneck, use case
- `comparisons`: winner 대비 delta KPI 자동 생성
- `chart_series`: completion/throughput/wait/headon/followon/section_conflict/battery chart용 집계

### Playback trace / UI 1차 — `playback.html`
- snapshot: AGV 위치/상태/배터리/current/target node
- event: order / wait / reroute / section conflict / charging / demand completed
- map: node/edge 좌표 + role/access metadata
- 표현: node 정지 AGV는 원형, edge 주행 AGV는 진행방향 화살표 (단일 AGV는 edge 위에 정확히 앉음, 군집일 때만 spread)
- edge 의미: 연파랑=계획, 진파랑=예약, 초록=주행, 빨강=차단(blocking_agv 또는 retry>0일 때만)
- 노드 시각: ST(초록 원), CH(파랑 사각), SD(주황 원), HP(흰 도넛 링), 일반 WP/BAY/access(회색 점)
- AGV 식별: 단일 무채색(`#3a4555`) — edge state 색과 충돌 회피
- **blocked_edge_key fallback**: pending이 비어 있어도 `blocking_agv` + WAITING이면 `current_node → path[path_index]`로 다음 hop 추론
- 라벨 충돌: 같은 노드 버킷 vertical stack, cross-bucket y-band stagger

### Playback 시각화 2차 (b 단계, 부분 진행 중)
- 2-pane 레이아웃 (맵 + 사고/이벤트 우측 sticky)
- 사고 묶음 클릭 → edge 펄스 + AGV chain ring 강조 (cycle 시 빨강)
- AGV 포커스 패널: 상태/현재/다음/목적/우회/배터리/예약·계획 hop bars/blocking chain badges
- 단방향 corridor 방향 chevron 마커
- AGV 라벨에 도착지 (`→ST_07` / `→휴식 HP_03` / `→충전 CH_02`) 표시
- 우회 발생 시 `(via SD_xxx)` 명시
- Topology meta 헤더 (eyebrow, headline, 차선/방향/충돌, AGV/seed/demands/duration chip)
- topology 정의 패널 (5타입 카드 with 차선/방향/충돌)

### Bay / Station / Charger geometry 1차
- bay는 `WP → BAY_* → WP` 내부 waypoint가 있는 세로 lane 구조로 변경
- station/charger는 direct node jump 대신 짧은 access lane (`SA/CA`)을 거쳐 진입
- station drop/work 지점은 main corridor에서 3m 이내로 제한
- center charger(`CH_04/05`)는 bay 축 연장선이 아닌 FAB 바깥 x 방향 배치

### Idle / Holding policy 1차
- 시작 배치: `WP_*` 대신 charger + holding point 풀에서 seed 고정 분산
- task 없고 저전압 아니면 nearest free holding point(`HP_*`)로 복귀
- 운영 의미: main corridor 위에서 idle 대기하는 비현실 상태 제거

### 추가 운영 정책 (디버깅 라운드)
- **AGV 베이/메인 통로 IDLE 금지**: `_reroute_failed` 시 active 태스크가 있으면 IDLE 강제 대신 `WAITING_RESERVATION` 유지 + retry 카운터 리셋
- **AGV PROCESSING은 ST_*에서만**: 데이터 검증으로 100% 보장 (베이/corridor에서 PROCESSING 0건)
- **두 AGV 같은 노드 동시 점유 금지**: `start_pool[i % len]` wrap 제거, `[:n_agv]` 슬라이스 + 풀 부족 시 명시 에러
- **HP 수 확장**: HOLDING_X `[80, 240, 400, 560]` (4) → `[40, 120, 200, 280, 360, 440, 520, 600]` (8). 24 HP × 3 row = 24 슬롯, 24+ AGV 풀 동시 시작 보장
- **중앙 station 베이 통로 위 분리**: BAY_X(0/160/320/480/640) 위에는 station 없음. center station은 `CENTER_STATION_X = STATION_X − BAY_X = [80, 240, 400, 560]`에 배치
- **Type A 방향 합의**: 북·남 = 동→서, 중앙 = 서→동
- **베이 속도**: 0.7 → 0.8 m/s
- **Follow-on headway 강화**: BASE_FOLLOWING_DISTANCE 2.5m → 4.3m (2 × ROBOT_LENGTH + safety), MIN headway 0.5s → 1.5s
- **AGV 마커 시각 사이즈 축소**: circle r 8→5, arrow scale 1.15→0.65 — 시각 겹침 해소
- **Siding 우회 픽업 보존 fix**: `_reroute_via_siding`이 next_hop으로 복귀 후 원 path 잔여(픽업·드롭) 이어가도록 수정. 이전엔 `path[-1]`(=dropoff)로 직행해 픽업 skip
- **False-positive 빨강 edge 제거**: `blocked_edge_key`는 `blocking_agv` 또는 `collision_retry_count>0`일 때만. 가감속/재출발 지연은 빨강 아님

---

## 6. Phase 3 완료 항목

- [x] 경로 전체 사전 예약 (pre-reservation) 1차: 출발 전 경로 시간 윈도우 일괄 예약
- [x] critical section 예약 1차: 교차로/좁은 bay/양방향 lane을 section 단위로 묶어 예약
- [x] critical section capacity 1차: lane width 기반 capacity 반영
- [x] 물리 모델 고도화 1차: 가감속 구간, processing 이후 재출발 시간 반영
- [x] station processing randomness 1차: seed 기반 pickup/dropoff 처리시간 분리

---

## 6.5 Map Editor F1 — Per-edge v_max + UX 개선 (2026-05-15)

`agent_a_map_editor_spec.md` 의 F1b-core / F1b-ux / F1c / F1d / F1e 트랙 완료. 자세한 사용성 결정은 spec 파일 상단 표 참조.

### F1b-core — Per-edge speed limit
- [x] `Edge.v_max: Optional[float] = None` 추가 (`src/domain/map/graph.py`). 기존 `max_speed`(default 1.0, topology generator corridor 속도)는 보존, `v_max`는 사용자 explicit override 분리.
- [x] YAML loader (`speed_limit` 외 새 키 `v_max`) + JSON loader (`vMax`/`v_max`) 양쪽 지원. 양방향 edge reverse 도 v_max 복사.
- [x] AGV 속도 결정 우선순위 재구성 (`_get_effective_speed`): `creep` > `edge.v_max` > `self._max_speed_mps` (intrinsic). `self._max_speed_mps` 신규 — `_motion.max_speed` 가 매 tick 덮어쓰기되는 stickiness 회피.
- [x] `_build_itinerary_segments()` 도 v_max 반영 — pre-reservation 시간 정확.
- [x] T61-1~4 추가 (YAML load / AGV 반영 / fallback / instant transition) — 모두 PASS.

### F1b-ux — Map editor v_max 편집 UI
- [x] `ImportedEdge.v_max` 추가 + `apply_edits` / `build_map_graph` passthrough (`src/domain/map/external_importer.py`).
- [x] Editor HTML payload `edges_payload` 에 v_max 포함 + `exportEdits` 의 edge_overrides / added_edges 에 v_max diff (null 도 명시 unset).
- [x] **Speed 모드 신설** (단축키 `V`). Paint / Stamp / Build 외 4번째 모드. 모드 안에서만 scroll = v_max 편집, 그 외 모드는 scroll = zoom — wheel inertia + Shift release timing 충돌 회피.
- [x] 인스펙터에 **−/+/number input/×(해제)** 버튼 — scroll 의존 불안정한 디바이스 대응.
- [x] Edge 좌클릭 → 인스펙터 pin (마우스 떠도 패널 유지). 같은 edge 재클릭 → unpin.
- [x] Speed 모드: 모든 edge 에 현재 v_max 값 라벨 항상 표시 (설정된 edge=amber, 미설정=회색).
- [x] Wheel 누적기 + deltaMode 정규화 — 마우스(deltaY ~100) / Firefox(line=1, deltaY ~3) / trackpad(작은 다발) 모두 1 tick 단위 일관.
- [x] T61-5~6 추가 (apply_edits → build_map_graph passthrough / added_edge v_max / override null=unset) — 모두 PASS.

### F1c — 저줌 미세 컨트롤
- [x] 노드 hit circle 을 outer translate + world 좌표 단위로 분리, inner labelScale 은 visual 만. Hit radius = clamp(world × zoom, 6px, 28px) / zoom — 저줌이면 화면에서 작게(정밀), 고줌이면 28px cap(이웃 충돌 방지).
- [x] Edge stroke 위에 화면 ~9px 두께의 invisible hit line 추가 — 줌인 상태에서도 edge hover/click 쉬움.

### F1d — Edge 방향 그리기 UX
- [x] Build edge 도구 드래그 중 방향 화살촉 미리보기. 좌클릭(단방향) = 끝점 chevron 1개, 우클릭(양방향) = 양 끝 chevron 2개. `labelScale` 적용으로 줌 무관 화면 픽셀 일정.
- [x] Build edge 도구 + edge 위 좌클릭(그리는 중 아닐 때) → `toggleEdgeBidir()` 단↔양방향 토글. `pushHistory` 호출하여 Undo 호환.

### F1e — Stamp 배치 UX
- [x] Build 옵션 패널에 grid snap toggle + grid size input (default 10 data units).
- [x] `addNodeAt(x, y, opts)` 확장: grid snap 적용 (round to grid) + Shift+클릭 시 직전 노드 X/Y 축에 자동 정렬 (커서가 가까운 축).
- [x] `lastPlacedDataXY` 추적 — 다중 stamp 배치 시 axis 고정 기준.
- [x] Toast 에 적용된 옵션 표시: `+ Node N0001 at (40.0, 100.0) [axis] [grid 10]`.

### 부수 효과
- `claude/trusting-jemison-264c88` 의 scheduler stale-extension 픽스가 main에 머지되며 직전 pre-existing Type D `test_headon_regression` deadlock=2 실패 자동 해소. 현재 63 passed / 0 failed.

---

## 7. 도메인 메모 (후속 작업 후보)

- **스테이션 egress가 corridor section을 짧은 look-ahead로 함께 잡기**: E 케이스에서 AGV_15/12 분석 시 발견. station에서 corridor로 진입할 때 access edge만 잡으면 corridor 이미 있던 AGV에 대한 yield-on-merge 의미가 없음. cut-in 형태로 보임. 운영 시나리오 명확해지면 들어갈 자리.
- **Reroute 루프 우려**: C/E에서 단일 AGV reroute 50+회 사례. path 안정성 부족할 수 있음. 임계치 또는 stability check 도입 후보.
- **Type A reroute_failed 다발**: 단방향 순환에서 막힘 시 우회 불가. 현 fix는 WAITING 유지로 IDLE 방지지만 wait 누적은 큼.
- **Bay block 다수**(D=401, E=297): bay capacity=1 한계. 줄서기 불가피.
