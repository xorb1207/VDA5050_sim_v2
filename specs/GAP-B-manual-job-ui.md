# GAP-B — 수동 Job 부여 UI

> Agent 의뢰용. 견적 ~0.5일.
> Agent B 의 JobDispatcher / JobApi / manual mode 가 이미 main 통합됨 — UI 만 추가.

## Goal

Quickrun 라이브 시뮬에서 사용자가 엔지니어처럼 수동으로 demand 부여. 자동 발행 (task_interval_s) 과 병행 가능.

## 사용자 의도 매핑

- **의도 #4** — 엔지니어가 실제로 job 주는 것처럼 수동으로 내릴 수 있다

## Scope

- IN:
  - Quickrun 페이지에 **"📋 수동 Job"** 토글 (toolbar 그룹)
  - 토글 on + 노드 클릭 1회 → pickup 후보 (시각 강조: 초록 ring)
  - 같은 노드 또 클릭 → 취소
  - 다른 노드 클릭 → dropoff 확정 → 즉시 demand 생성
  - 서버: `JobApi.dispatch()` 호출 (Agent B 결과 활용)
  - 폼 입력 옵션 (선택): "수동 Job 패널" 펼치면 pickup/dropoff/capability 직접 입력
  - 토글 끄면 클릭 인터랙션 비활성
- OUT:
  - 복잡한 job 정의 (스케줄, 의존성 chain)
  - 미래 시점 예약
  - 자동 demand 정지 (자동 + 수동 둘 다 동작)
  - capability 자동 추론 (수동 입력 또는 노드 capability 태그)

## Pre-step (discovery — 필수)

1. `src/application/dispatch.py` — `JobDispatcher` 클래스 + `dispatch()` 메서드 시그니처
2. `src/adapters/job_api.py` — `JobApi` 클래스 + dict-in/dict-out 형식
3. `src/application/scenario/task_generator.py` — `manual` 모드 동작 (자동 발행 정지 등 확인)
4. `src/interfaces/quickrun/server.py` — REST endpoint 추가 패턴
5. `src/analytics/playback_trace.py` — toolbar 토글 추가 패턴 (히트맵 참고)
6. 노드 클릭 hit-test (Editor 의 Stamp 모드 노드 클릭과 동일 패턴)

→ 발견 결과 보고 후 구현. 특히 **JobApi 가 task_generator 의 manual mode 활성화 자동인지**, 수동 demand 가 자동 demand 와 같은 큐로 가는지.

## Interface

### 신규 REST endpoint

```
POST /dispatch-demand
body: {
  "runId": "...",
  "pickup_node": "ST_001",
  "dropoff_node": "ST_002",
  "required_capability": "overhead"   // 선택, null 이면 제약 없음
}
→ 200 OK + {"ok": true, "demand_id": "..."}
```

### Quickrun UI 변경

```
[toolbar]
🔥 히트맵   ⚠ 충돌   ⛔ 차단   📋 수동 Job ← 신규
```

토글 on 시:
- 우측에 작은 패널 표시 "수동 Job: pickup 노드 선택"
- 노드 hover: 초록 outline + "pickup 으로 선택"
- 노드 클릭 → pickup 확정 + 패널 갱신 "dropoff 노드 선택"
- 다른 노드 hover: 파란 outline + "dropoff 으로 선택"
- 클릭 → demand 생성 + 토스트 알림 "📋 Demand 발행 — pickup → dropoff"
- ESC: pickup 선택 취소

### (선택) 폼 입력 모드

토글 옆에 "📝 폼" 보조 버튼:
- 펼치면 작은 폼: pickup [드롭다운] / dropoff [드롭다운] / capability [입력] / [발행]
- 마우스 클릭 어려운 케이스 또는 정밀 입력용

## Tests

```python
def test_dispatch_demand_endpoint_returns_200():
    """POST /dispatch-demand 정상 응답"""

def test_manual_demand_assigned_to_agv():
    """수동 demand 가 eligible idle AGV 에 할당됨"""

def test_manual_and_auto_demands_coexist():
    """자동 발행과 수동 발행 동시 동작 — 둘 다 처리됨"""

def test_manual_demand_with_capability():
    """required_capability 지정 시 매칭 fleet 만 처리"""

def test_manual_demand_unmatched_pending():
    """매칭 capability 없으면 pending 으로 유지"""

[수동] 1. 📋 토글 on → 우측 패널 "pickup 선택"
[수동] 2. 노드 hover/클릭 → pickup 확정
[수동] 3. 다른 노드 클릭 → demand 발행 토스트 + AGV 가 그 pickup 으로 이동
[수동] 4. 폼 모드 → 드롭다운 + 발행 동작
[수동] 5. ESC → 진행 중 pickup 선택 취소
[수동] 6. 토글 off → 노드 클릭으로 AGV 포커스 가능 (기존 동작)
```

## DO NOT

- 자동 demand 발행 정지 (둘 다 동작)
- multi-stage / handover 정의
- demand 우선순위 명시 입력 (FIFO 기본)
- 복잡한 스케줄링 (미래 시점, 의존성)
- ⛔ 차단 모드 / Stamp 등 다른 모드와 충돌 — 토글 분리 명확

## Acceptance

- 위 5개 단위 테스트 PASS
- 6개 수동 시나리오 통과
- 자동 + 수동 demand 동시 처리 확인
- 토글 off 시 기존 인터랙션 복귀

## Final Verification

```bash
python tests/integration/test_simulation.py

./run quickrun
# 수동 점검: 📋 토글 → 두 노드 클릭 → demand 발행 → AGV 이동 확인
```

스크린샷 (수동 발행 토스트 + AGV 이동) 첨부 권장.
