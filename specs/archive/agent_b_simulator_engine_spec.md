# Agent B — Simulator Engine Track (F3: Traffic + Dispatch 현실화)

> Claude Code Agent View용 태스크 큐.
> **작업 순서 엄수.** 각 태스크 완료 후 테스트 통과 확인 → 다음으로 이동.
> Agent A와 파일 구역 완전 분리 — 동시 출발 가능.

---

## 작업 범위 (Boundary)

- **건드려도 되는 구역:** `domain/`, `application/`, `analytics/`
- **절대 건드리지 말 것:** `frontend/`, `fab_nav_graph.yaml` (Agent A 구역)
- **공유 접촉:** `adapters/job_api.py` — Agent C와 겹칠 수 있음. 변경 전 보고 필수.

---

## 현재 상태 (As-Is)

```
문제 1: 충돌 없음 — 두 AMR이 같은 노드/엣지에 겹쳐서 통과
문제 2: 노드에 여러 AMR 스택 가능 (물리 불가)
문제 3: wait 로직은 있으나 해소(re-route/unblock) 없음
문제 4: 잡 발생이 랜덤 — 현실은 "특정 AMR + 목적지 노드 지정" dispatch
문제 5: 경로 탐색 로직 불명확
```

---

## TASK 1 — Pre-step: 현재 엔진 구조 Discovery

> 구현 전 반드시 먼저 실행. 발견 결과 보고 후 TASK 2 진행.

```
1. AMR 이동/step 루프 위치 파악 (어디서 위치 업데이트?)
2. 현재 pathfinding 로직 위치 + 알고리즘 파악
3. 노드 점유 상태 추적하는 자료구조 있는지 확인
4. 현재 잡 생성/dispatch 로직 위치 파악 (랜덤 로직 어디?)
5. T1~T59 테스트 중 교통/충돌 관련 테스트 목록 파악
```

보고 형식:
```
[Discovery 결과]
- Step 루프: (파일명:라인)
- Pathfinding: (파일명:라인, 알고리즘)
- 노드 점유 추적: 있음/없음 (위치)
- 잡 dispatch: (파일명:라인)
- 교통 관련 기존 테스트: (목록)
```

---

## TASK 2 — 노드/엣지 예약 시스템 (VDA5050 스타일)

> TASK 1 보고 확인 후 시작.

### Goal
AMR이 노드/엣지를 점유하기 전 예약(reserve) → 점유 중 lock → 이탈 시 해제.
물리적으로 두 AMR이 같은 공간을 동시에 점유하는 상황 방지.

### 구현 내용

**ReservationManager (신규 — domain 레이어)**
```python
class ReservationManager:
    # node_id → AMR ID (점유 중)
    # edge_id → AMR ID (점유 중)

    def try_reserve_node(self, node_id, amr_id) -> bool
    def try_reserve_edge(self, edge_id, amr_id) -> bool
    def release_node(self, node_id, amr_id)
    def release_edge(self, edge_id, amr_id)
    def is_node_free(self, node_id) -> bool
    def is_edge_free(self, edge_id) -> bool
```

**AMR 이동 로직 수정**
- 다음 노드 이동 전: `try_reserve_node()` 호출
- 실패 시: wait 상태로 전환 (TASK 3에서 해소 로직 추가)
- 이탈 시: 이전 노드 `release_node()` 호출

**Edge 예약 (head-on 방지)**
- A→B 이동 중 B→A 이동 요청 시 충돌 → 한쪽 wait

### Tests
```python
test_two_amrs_cannot_occupy_same_node()
test_node_released_after_amr_departs()
test_head_on_edge_conflict_blocked()
test_reservation_manager_free_after_release()
# T1~T59 PASS 유지
```

---

## TASK 3 — 대기 큐 + 해소 로직 (Wait Queue + Re-route)

> TASK 2 완료 후 시작.

### Goal
노드 점유 실패 시 무한 대기 방지. Wait → 일정 시간 후 대안 경로 탐색.

### 구현 내용

**WaitQueue (domain 레이어)**
```python
class WaitQueue:
    # node_id → [(amr_id, wait_start_time), ...]
    
    def enqueue(self, amr_id, waiting_for_node_id, timestamp)
    def dequeue_if_free(self, node_id, reservation_manager) -> Optional[str]
    def get_wait_duration(self, amr_id) -> float  # seconds
```

**해소 로직 (application 레이어)**
```
노드 예약 실패 시:
  1. WaitQueue에 등록
  2. WAIT_TIMEOUT (기본 10초) 동안 대기
  3. 대기 중 해당 노드 free 되면 → 즉시 진입
  4. WAIT_TIMEOUT 초과 시:
     → 대안 경로 탐색 (우회 경로)
     → 우회 경로 없으면: 계속 대기 (15초 후 재시도)
```

**데드락 감지 (기본)**
```
순환 대기 감지:
  A가 B의 노드 대기 중 + B가 A의 노드 대기 중
  → 우선순위 낮은 AMR에게 강제 후퇴 명령 (이전 노드로 retreat)
우선순위 결정: 잡 dispatch 시간이 빠른 쪽이 우선
```

### 설정값 (config로 조정 가능)
```yaml
traffic:
  wait_timeout_s: 10       # 대기 후 우회 탐색 트리거
  deadlock_check_interval_s: 5
  retreat_on_deadlock: true
```

### Tests
```python
test_amr_waits_when_node_occupied()
test_amr_proceeds_when_node_freed()
test_reroute_triggered_after_timeout()
test_deadlock_detected_and_resolved()
test_no_deadlock_false_positive()
```

---

## TASK 4 — 잡 Dispatch 모델 교체 (랜덤 → AMR+노드 지정)

> TASK 3 완료 후 시작.

### Goal
랜덤 스테이션 간 잡 생성 제거. "특정 AMR + 목적지 노드" 단위 dispatch로 교체.

### 현재 vs 목표
```
현재: 엔진이 랜덤으로 AMR에 스테이션 배정
목표: dispatch_job(amr_id, destination_node_id) API로 외부에서 주입
```

### 구현 내용

**JobDispatcher (application 레이어)**
```python
class JobDispatcher:
    def dispatch(self, amr_id: str, destination_node_id: str) -> JobResult
    # AMR 상태 검증 (idle인지 확인)
    # 노드 존재 여부 검증
    # 경로 계산 후 AMR에 할당
    # 결과: success / amr_busy / node_not_found / no_path
```

**시뮬레이션 모드 분리**
- `mode: manual` — dispatch API로만 잡 주입 (기본값으로 변경)
- `mode: random` — 기존 랜덤 동작 유지 (테스트/데모용)

```yaml
simulation:
  dispatch_mode: manual   # manual | random
```

**REST API 엔드포인트 (adapters)**
```
POST /job/dispatch
  body: { amr_id: "AMR-001", destination_node_id: "Station-B" }
  response: { job_id, status, estimated_arrival_s }
```

> **Agent C 연계:** 이 엔드포인트를 Job Creation UI가 호출함.
> 스키마 변경 시 반드시 보고.

### Tests
```python
test_dispatch_assigns_job_to_correct_amr()
test_dispatch_fails_when_amr_busy()
test_dispatch_fails_when_node_not_found()
test_dispatch_fails_when_no_path()
test_random_mode_still_works()
test_manual_mode_no_auto_job_generation()
```

---

## TASK 5 — 교통 KPI 추가 (analytics 레이어)

> TASK 4 완료 후 시작. 선택적이지만 권장.

### Goal
교통/대기 현실화 후 이를 측정할 지표 추가.

### 신규 KPI
```python
# 기존 KPI에 추가
"avg_wait_time_s"        # 노드 대기 평균 시간
"max_wait_time_s"        # 최대 대기 시간
"deadlock_count"         # 데드락 발생 횟수
"reroute_count"          # 우회 경로 탐색 횟수
"node_contention_rate"   # 노드 경합률 (요청 대비 즉시 점유 실패 비율)
```

### Tests
```python
test_wait_time_kpi_tracked()
test_deadlock_count_increments()
test_node_contention_rate_calculated()
```

---

## 최종 검증 (전체 TASK 완료 후)

```bash
pytest tests/ -v > /tmp/agent_b_final.log 2>&1
echo "Exit: $?" >> /tmp/agent_b_final.log
```

보고 형식:
```
[Agent B 전체 완료]
- 완료 태스크: Discovery / 예약시스템 / Wait큐 / Dispatch / KPI
- 최종 테스트: PASS / FAIL
- T1~T59: PASS / FAIL
- POST /job/dispatch 스키마: (최종 확정 내용)  ← Agent C 전달용
- 미완/defer 항목: (있으면)
```

---

## Agent A와 동시 출발 가능 이유

| 항목 | Agent A | Agent B |
|---|---|---|
| 주 파일 구역 | `frontend/map-editor/` | `domain/`, `application/` |
| YAML 접근 | `fab_nav_graph.yaml` 읽기/쓰기 | 읽기만 (스키마 변경 X) |
| 공유 접촉 | 없음 | `adapters/job_api.py` (C와 조율) |

> Agent B가 `fab_nav_graph.yaml` 스키마를 변경해야 할 경우:
> **반드시 멈추고 보고.** Agent A의 v_max 스키마 작업과 충돌 가능.
