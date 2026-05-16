# F1a Engine — 데이터 모델 + 라우터 + 예약 충돌

> Engine Agent 의뢰용. 견적 ~1.5일.
> UI 변경 없음 (별도 spec). 이 spec 완료 후 UI layer 진행.

## Goal

같은 vertex pool 위에 fleet 별 다른 lane graph 를 두고, AGV 가 자기 fleet 의 graph 내 lane 만 사용해서 라우팅하도록 엔진 확장.

## 사용자 의도 매핑

- 의도 #8 (장기) — OpenRMF graph_idx 패턴 호환
- 시뮬에서 이기종 fleet 분리 운영 가능 → ICS 대변 가치

## Scope

- IN:
  - `Edge.graph_idx: int = 0` 필드 추가
  - `Fleet` 도메인 클래스 신설 (`src/domain/fleet/fleet.py` 또는 적절한 위치)
  - `AGV.fleet_id` 또는 `AGV.fleet` 필드
  - A* `get_path()` 에 fleet 인자 → graph_idx 필터링
  - 예약 충돌 시 fleet priority tiebreaker (≤1ms 차이 시만)
- OUT:
  - YAML 스키마 파싱 (→ integration spec)
  - Editor UI / Quickrun UI (→ ui-* spec)
  - fleet 별 motion 모델 분리 (별도 PR)

## Pre-step (discovery — 필수)

1. `src/domain/map/graph.py` 의 `Edge` 데이터클래스 — 현재 필드 + dataclass 형식 확인
2. `src/domain/map/graph.py` 의 `get_path()` 함수 시그니처 + `blocked_edges` 패턴 (참조 가능)
3. `src/domain/agv/agv.py` 의 AGV 초기화 — 어떤 인자 받는지
4. `src/domain/reservation/scheduler.py` 의 예약 충돌 처리 함수 위치 + 현재 FIFO 동작
5. `src/interfaces/quickrun/runner.py` 의 AGV 생성 부분 (어디서 charger 에 배치하는지)
6. 기존 `Edge.bidirectional`, `Edge.corridor` 등 optional 필드 패턴 참고

→ **발견 결과 보고 후 구현 진행.** 특히 dataclass default 값 동작, A* 의 `_out_edges` 사용 방식.

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
    id: str                  # "A", "B", "C"
    graph_idx: int           # 사용할 lane graph
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
        # legacy: fleet=None 이면 graph 0 사용 (모든 lane)
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

## Tests (engine 단위)

```python
def test_edge_graph_idx_default_zero():
    """필드 추가 후 legacy edge 들은 모두 graph_idx=0"""
    
def test_fleet_class_default_priority():
    """Fleet() 인스턴스 기본값 — priority 1, count 1, graph_idx 미지정 시 0"""

def test_router_filters_lanes_by_fleet():
    """Fleet(graph_idx=0) 의 AGV 가 graph_idx=1 인 lane 통과 X"""

def test_router_no_fleet_uses_all_lanes():
    """fleet=None 으로 호출 시 모든 lane 사용 (legacy 동작)"""

def test_shared_vertex_two_fleets():
    """같은 vertex 에 graph 0 lane 과 graph 1 lane 이 모두 연결되어 있으면
    두 fleet 가 모두 그 vertex 로 라우팅 가능"""

def test_reservation_fifo_normal_case():
    """≥5ms 시간 차이 — priority 와 무관하게 먼저 reserve 한 쪽 우선"""

def test_reservation_priority_tiebreaker():
    """<1ms 시간 차이 — priority 낮은 fleet 우선"""

def test_reservation_same_priority_deterministic():
    """priority 같으면 agv_id 작은 쪽 (random 아님)"""
```

## DO NOT

- `Edge.bidirectional`, `Edge.corridor`, `Edge.access_type` 등 기존 필드 의미 변경
- 기존 토폴로지 generator (Type A~E) 의 출력 변경 — 모두 graph_idx=0 으로
- AGV 초기화 인자에 fleet **강제 의무화** (None 허용 — legacy 호환)
- YAML 파서 수정 (→ integration spec)
- Editor / Quickrun UI 코드 수정 (→ ui-* spec)

## Acceptance

- 위 8개 신규 테스트 PASS
- T1~T59 기존 통합 테스트 PASS 유지
- 단일 fleet baseline 시나리오 KPI 무변화 (`tests/integration/test_simulation.py` 의 토폴로지 별 시나리오)
- AGV 인스턴스 fleet=None 으로 초기화 시 legacy 동작

## Final Verification

```bash
python tests/integration/test_simulation.py > /tmp/f1a_engine_test.log 2>&1
echo "Exit: $?" >> /tmp/f1a_engine_test.log
```

보고 시 로그 첨부 + 다음 사항 명시:
- 신규 테스트 PASS 갯수 (목표 8)
- 기존 테스트 PASS 갯수 (목표 57+)
- 신규 추가된 코드 파일 목록
