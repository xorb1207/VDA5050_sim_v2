# F1a Engine — 데이터 모델 + 라우터 + 예약 + Capability 매칭 dispatch

> Engine Agent 의뢰용. 견적 ~2~2.5일 (capability + dispatch 추가).
> UI 변경 없음 (별도 spec). 이 spec 완료 후 UI layer 진행.

## Goal

**이기종 AGV** 시뮬레이션. 같은 vertex pool 위에 fleet_type 별 다른 lane graph (graph_idx 분리), 각 AGV 가 자기 fleet 의 lane 만 사용. Demand 의 `required_capability` 와 fleet 의 `capabilities` 매칭으로 dispatch.

## 사용자 의도 매핑

- **의도 #9** (★ 핵심) — ICS 이기종 AGV: graph isolation + capability 매칭
- 의도 #8 (장기) — OpenRMF graph_idx + capability 패턴 호환
- 시뮬에서 이기종 AGV 분리 운영 → ICS 대변 가치

## Scope

- IN:
  - **`Edge.graph_idx: int = 0`** 필드 추가
  - **`Fleet` 도메인 클래스** 신설 (`src/domain/fleet/fleet.py` 또는 적절한 위치)
    - `id: str` (예: "TYPE_1", "TYPE_2", "TYPE_3")
    - `graph_idx: int`
    - `capabilities: list[str]` (예: `["overhead", "pickup_small"]`)
    - `color: str` (시각화용, UI spec 에서 사용)
    - `max_speed_mps: float = 1.5`
    - `priority: int = 1`
    - `count: int = 1`
  - **`AGV.fleet`** 필드 (Fleet 인스턴스 참조 또는 `fleet_id`)
  - **A* `get_path()` 에 fleet 인자** → 그 fleet 의 graph_idx 에 속한 lane 만 사용
  - **예약 충돌 시 fleet priority tiebreaker** (≤1ms 차이 시만)
  - **Demand 에 `required_capability: str | None`** 필드 (None 이면 제약 없음 — legacy)
  - **`TaskGenerator` / `JobDispatcher` 의 dispatch 매칭 로직**:
    ```python
    eligible = [agv for agv in idle_agvs
                if demand.required_capability is None
                   or demand.required_capability in agv.fleet.capabilities]
    best = nearest_to(eligible, demand.pickup)
    assign(best, demand)
    ```
  - capability 매칭 실패 시 demand 처리 — pending 으로 유지 + 통계 기록 (정해진 시간 후 expire 또는 무한 대기, 정책 spec 결정 필요 시 사용자 확인)

- OUT:
  - YAML 스키마 파싱 (→ integration spec)
  - Editor UI / Quickrun UI (→ ui-* spec)
  - fleet 별 motion 모델 분리 (별도 PR)
  - **Multi-stage / handover** (별도 사이클) — single-stage 만
  - Capability 자동 추론 (UI 의 Stamp 도구로 사용자가 직접)

## Pre-step (discovery — 필수)

1. `src/domain/map/graph.py` 의 `Edge` 데이터클래스 — 현재 필드 + dataclass 형식 확인
2. `src/domain/map/graph.py` 의 `get_path()` 함수 시그니처 + `blocked_edges` 패턴 (참조 가능)
3. `src/domain/agv/agv.py` 의 AGV 초기화 — 어떤 인자 받는지
4. `src/domain/reservation/scheduler.py` 의 예약 충돌 처리 함수 위치 + 현재 FIFO 동작
5. `src/interfaces/quickrun/runner.py` 의 AGV 생성 부분 (어디서 charger 에 배치하는지)
6. **`src/application/scenario/task_generator.py`** — demand 생성 + AGV 할당 로직 위치
7. **`src/application/dispatch.py`** + **`src/adapters/job_api.py`** (Agent B 통합) — JobDispatcher 의 매칭 로직
8. **`src/application/scenario/demand_set.py`** (또는 비슷한) — Demand 데이터 구조 위치
9. 기존 `Edge.bidirectional`, `Edge.corridor` 등 optional 필드 패턴 참고

→ **발견 결과 보고 후 구현 진행.** 특히:
- dataclass default 값 동작
- A* 의 `_out_edges` 사용 방식
- TaskGenerator vs JobDispatcher 역할 분리 (capability 매칭은 어디에서 처리할지)

## Interface

### Edge

```python
@dataclass
class Edge:
    edge_id: str
    start_node_id: str
    end_node_id: str
    ...
    graph_idx: int = 0  # ★ 신규. 기본 0 — legacy YAML 모두 graph 0 으로
```

### Fleet

```python
@dataclass
class Fleet:
    id: str                  # "TYPE_1", "TYPE_2", "TYPE_3" 등 fleet_type
    graph_idx: int           # 사용할 lane graph
    capabilities: list[str] = field(default_factory=list)
                             # 처리 가능한 task 종류 (예: ["overhead", "pickup_small"])
    color: str = "#0f9d58"   # 시각화용 (UI spec 에서 사용)
    max_speed_mps: float = 1.5
    priority: int = 1        # 낮을수록 우선 (1 = 가장 우선)
    count: int = 1           # 시뮬 시작 시 배치할 AGV 수
```

### AGV

```python
class AGV:
    def __init__(self, agv_id, ..., fleet: Fleet | None = None):
        ...
        self.fleet = fleet
        # legacy: fleet=None 이면 graph 0 + capability 매칭 무시 (모든 lane / 모든 task)
```

### Demand (확장)

```python
@dataclass
class Demand:
    pickup_node: str
    dropoff_node: str
    ...
    required_capability: str | None = None
                             # None 이면 제약 없음 (legacy)
                             # 값 있으면 그 capability 가진 fleet 만 처리 가능
```

### A* 라우터

```python
def get_path(
    self,
    src: str, dst: str,
    blocked_edges: Optional[set[tuple[str, str]]] = None,
    fleet: Optional[Fleet] = None,   # ★ 신규
) -> list[str]:
    """fleet 이 주어지면 그 fleet.graph_idx 에 속한 edge 만 사용해서 A*.
    fleet=None 이면 graph 무관 — legacy 동작."""
    ...
```

### 예약 충돌 tiebreaker

```python
SIMULTANEOUS_RESERVATION_THRESHOLD_S = 0.001  # 1 ms

def resolve_simultaneous_reservation(self, req_a, req_b):
    """동시 reserve 시 우선순위 결정.
    - 시간 차이 ≥ 임계: FIFO (먼저 도착한 쪽 우선) ← 본질 유지
    - 시간 차이 < 임계: priority 낮은 fleet 가 우선 (tiebreaker)
    - priority 같으면 agv_id 작은 쪽 (deterministic)"""
    ...
```

### Dispatch 매칭 (capability 기반)

```python
def dispatch_demand(self, demand: Demand, idle_agvs: list[AGV]) -> AGV | None:
    """Demand 를 처리할 AGV 선택.

    1. eligible 필터:
       - demand.required_capability is None → 모든 idle AGV
       - 값 있음 → 그 capability 를 가진 fleet 의 AGV 만
    2. eligible 중 demand.pickup_node 까지 거리 가장 가까운 AGV
       - 같은 fleet 의 graph 에서 path 가능해야 (라우터 호출 필요)
       - path 없으면 그 AGV 제외
    3. 누구도 eligible 아니면 None 반환 (demand pending 으로 유지)
    """
    eligible = [
        agv for agv in idle_agvs
        if demand.required_capability is None
           or (agv.fleet and demand.required_capability in agv.fleet.capabilities)
    ]
    if not eligible:
        return None

    # 거리 기반 (단순). 추후 cost 함수로 확장 가능.
    routable = []
    for agv in eligible:
        path = graph.get_path(agv.current_node_id, demand.pickup_node, fleet=agv.fleet)
        if path:
            distance = sum_lane_distances(path)
            routable.append((agv, distance))
    if not routable:
        return None
    return min(routable, key=lambda x: x[1])[0]
```

**capability 매칭 실패 처리** (기본 정책):
- demand pending queue 에 유지
- 매 tick 마다 재시도
- 통계: `unmatched_demand_count` 누적 → 시뮬 끝에 KPI 로 보고
- (옵션) timeout 후 expire — 별도 spec, 기본은 무한 대기

## Tests (engine 단위)

```python
# Graph isolation (lane filtering)
def test_edge_graph_idx_default_zero():
    """필드 추가 후 legacy edge 들은 모두 graph_idx=0"""

def test_fleet_class_default():
    """Fleet() 인스턴스 기본값 — priority 1, count 1, capabilities 빈 리스트"""

def test_router_filters_lanes_by_fleet():
    """Fleet(graph_idx=0) 의 AGV 가 graph_idx=1 인 lane 통과 X"""

def test_router_no_fleet_uses_all_lanes():
    """fleet=None 으로 호출 시 모든 lane 사용 (legacy 동작)"""

def test_shared_vertex_two_fleets():
    """같은 vertex 에 graph 0 lane 과 graph 1 lane 이 모두 연결되어 있으면
    두 fleet 가 모두 그 vertex 로 라우팅 가능"""

# Reservation tiebreaker
def test_reservation_fifo_normal_case():
    """≥5ms 시간 차이 — priority 와 무관하게 먼저 reserve 한 쪽 우선"""

def test_reservation_priority_tiebreaker():
    """<1ms 시간 차이 — priority 낮은 fleet 우선"""

def test_reservation_same_priority_deterministic():
    """priority 같으면 agv_id 작은 쪽 (random 아님)"""

# Capability matching (dispatch)
def test_demand_required_capability_none_legacy():
    """required_capability=None 인 demand → 모든 fleet 처리 가능 (legacy)"""

def test_demand_capability_match_eligible():
    """demand.required_capability='overhead' →
    capabilities 에 'overhead' 있는 fleet 의 AGV 만 eligible"""

def test_dispatch_picks_nearest_eligible():
    """eligible 중 pickup 까지 distance 가장 짧은 AGV 선택"""

def test_dispatch_no_eligible_demand_pending():
    """매칭되는 fleet 가 없으면 demand 가 pending 으로 유지"""

def test_dispatch_fleet_isolation_path_check():
    """eligible AGV 라도 자기 fleet 의 graph 로 pickup 까지 path 없으면 제외"""

def test_unmatched_demand_count_tracked():
    """capability 미매칭 demand 가 누적 카운트되어 KPI 에 보고"""
```

## DO NOT

- `Edge.bidirectional`, `Edge.corridor`, `Edge.access_type` 등 기존 필드 의미 변경
- 기존 토폴로지 generator (Type A~E) 의 출력 변경 — 모두 graph_idx=0 으로
- AGV 초기화 인자에 fleet **강제 의무화** (None 허용 — legacy 호환)
- Demand 의 required_capability **강제 의무화** (None 허용 — legacy)
- YAML 파서 수정 (→ integration spec)
- Editor / Quickrun UI 코드 수정 (→ ui-* spec)
- **Multi-stage / handover** — single-stage dispatch 만
- Capability 자동 추론 — vertex/edge 에서 자동 capability 부여 X (UI Stamp 또는 YAML 으로 명시)

## Acceptance

- 위 14개 신규 테스트 PASS (graph isolation 8 + capability 6)
- T1~T59 기존 통합 테스트 PASS 유지
- 단일 fleet baseline 시나리오 KPI 무변화 (`tests/integration/test_simulation.py` 의 토폴로지 별 시나리오)
- AGV 인스턴스 fleet=None 으로 초기화 시 legacy 동작
- Demand.required_capability=None 일 때 legacy dispatch 동작

## Final Verification

```bash
python tests/integration/test_simulation.py > /tmp/f1a_engine_test.log 2>&1
echo "Exit: $?" >> /tmp/f1a_engine_test.log
```

보고 시 로그 첨부 + 다음 사항 명시:
- 신규 테스트 PASS 갯수 (목표 14)
- 기존 테스트 PASS 갯수 (목표 57+)
- 신규 추가된 코드 파일 목록
- Fleet / Demand / dispatch 매칭 부분의 핵심 코드 hunk
