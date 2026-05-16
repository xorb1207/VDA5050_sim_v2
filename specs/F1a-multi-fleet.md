# Task: F1a — Multi-graph / 이기종 fleet 지원

> 2026-05-16 합의. 다음 사이클 1순위. 견적 ~4.5일.

## Goal

같은 평면도에서 **여러 fleet 의 lane graph 를 분리** 운영. 이기종 로봇 (예: 소형 OHT + 대형 AGV + 보조 로봇) 이 각자의 graph 를 통해 다니되, **vertex 는 공유** (charger / station / 환승점 자연스럽게 표현).

Open-RMF traffic-editor 의 `graph_idx` 패턴 차용. 단 OpenRMF 클론 X — 호환 데이터 + 가벼운 sandbox 차별화.

## Scope

- IN:
  - **데이터 모델**: `Edge.graph_idx: int = 0`, `Fleet` 도메인 클래스, `AGV.fleet_id`
  - **라우터 필터링**: A* 가 fleet 의 graph_idx 에 속한 lane 만 사용
  - **충돌 정책**: FIFO + fleet priority tiebreaker (동시 예약 시 priority 높은 fleet 가 우선)
  - **공유 vertex**: vertex 자체엔 graph 정보 없음. 노드 점유 충돌은 기존 4계층 예약 그대로 작동
  - **Map Editor**: Active graph 토글 (graph 0/1/2 색 분리, 활성 graph 만 편집), Paint/Build 가 활성 graph 영향
  - **Quickrun**: fleet 별 AGV 색깔 / 시뮬 시작 시 fleet 별 AGV 배치 / fleet 별 KPI 카드
  - **Case 비교 YAML**: `fleets:` 섹션
  - **상한**: 최대 3 fleet
- OUT:
  - Open-RMF rmf_fleet_adapter 통합 (별도 PR)
  - fleet 별 다른 dynamics (가속/회전 모델) — 기존 단일 motion 모델 사용
  - fleet 별 battery 정책 분리
  - 다층 (level + fiducial)

## Pre-step (discovery — 구현 전 먼저 실행)

1. `Edge` 데이터 클래스 위치 + 기존 필드 확인 (`src/domain/map/graph.py`)
2. A* / `get_path()` 구현 위치 + `blocked_edges` 같은 필터 인자 패턴 확인
3. AGV 가 graph 와 결합되는 곳 (`src/domain/agv/agv.py`) — 초기화 시 어떤 정보 받는지
4. 예약 스케줄러 (`src/domain/reservation/scheduler.py`) — 충돌 시 priority 비교 지점 있는지
5. Map Editor 의 mode 시스템 (`src/interfaces/map_editor/editor_html.py`) — 새 mode 추가 패턴
6. Quickrun `runner.py` 의 AGV 배치 로직

→ 발견 결과 보고 후 구현 진행

## Interface

### YAML schema (Open-RMF building_map 호환 지향)

```yaml
levels:
  L1:
    vertices:
      - [x, y, "name", {is_charger: true}]
      ...
    lanes:
      - [v0_idx, v1_idx, {graph_idx: 0, bidirectional: true, speed_limit: 1.5}]
      - [v2_idx, v3_idx, {graph_idx: 1, bidirectional: false}]
      ...

# 우리 확장 — 시뮬 시나리오 정의
fleets:
  - {id: A, color: "#0f9d58", graph_idx: 0, count: 6, max_speed_mps: 1.5, priority: 1}
  - {id: B, color: "#2563eb", graph_idx: 1, count: 4, max_speed_mps: 1.0, priority: 2}
  - {id: C, color: "#e0a000", graph_idx: 2, count: 2, max_speed_mps: 0.8, priority: 3}

conflict_policy: fifo_with_priority_tiebreaker
```

### Engine 결정 로직

```python
# A* 라우팅 (라우터)
def get_path(src, dst, fleet: Fleet, blocked_edges=None) -> list[str]:
    """fleet.graph_idx 에 속한 edge 만 사용해서 A* 실행."""
    candidate_edges = [e for e in graph.edges if e.graph_idx == fleet.graph_idx]
    ...

# 예약 충돌 시 tiebreaker
def resolve_simultaneous_reservation(agv_a, agv_b) -> AGV:
    """동시 reserve 요청 시 (≤1ms 차이) priority 높은 fleet 가 우선.
    FIFO 본질은 유지 — 5ms 이상 차이는 그대로 먼저 도착한 AGV 우선."""
    if abs(t_a - t_b) < SIMULTANEOUS_THRESHOLD:
        return agv_a if agv_a.fleet.priority < agv_b.fleet.priority else agv_b
    return agv_a if t_a < t_b else agv_b
```

### Map Editor

- **우측 패널 신규 섹션** "Active Graph":
  - 라디오: `Graph 0 (Fleet A)` / `Graph 1 (Fleet B)` / `Graph 2 (Fleet C)`
  - 토글: "Show inactive graphs (faded)" 켜고 끄기
- **Paint / Build 영향 범위**: 활성 graph 의 lane 만 색 진하게, 다른 graph 는 회색 fade. 액션도 활성 graph 만.
- **Stamp**: vertex 작동이라 graph 무관 (기존 그대로)
- **Edge 색 분리**: graph 0 = 파랑, graph 1 = 초록, graph 2 = 주황 (fleet color 와 일치)

### Quickrun

- **시뮬 시작 시 AGV 배치**: 각 fleet 의 count 만큼 AGV 를 그 fleet 의 graph 상 charger 에 배치
- **AGV 색**: fleet color
- **KPI 카드**: fleet 별 분리 — Fleet A 처리량 / Fleet B 처리량 / Fleet C 처리량
- **충돌 마커**: 기존 그대로 (fleet 무관)

## Tests (must add)

```python
def test_edge_graph_idx_default_zero():
    """legacy YAML (graph_idx 없음) → 모두 graph 0 으로"""
    
def test_fleet_router_filters_lanes():
    """Fleet A (graph 0) AGV 가 graph 1 의 lane 통과 안 함"""

def test_shared_vertex_charger_accessible_by_multiple_fleets():
    """charger vertex 에 두 fleet 의 lane 이 모두 연결되어 있으면
    두 fleet 가 모두 그 charger 사용 가능"""

def test_simultaneous_reservation_priority_tiebreaker():
    """동시 reserve 시 priority 높은 fleet 우선 — 5ms 이상 차이는 FIFO"""

def test_no_priority_inversion_under_normal_fifo():
    """priority 차이가 있어도 5ms+ 먼저 reserve 한 AGV 가 우선 (FIFO 본질)"""

def test_editor_active_graph_filters_paint():
    """Map Editor 의 Paint 가 활성 graph 의 lane 만 영향"""

def test_yaml_with_multi_fleet_loads():
    """fleets: 섹션 + lanes graph_idx 가 정상 파싱"""

def test_baseline_single_fleet_kpi_unchanged():
    """단일 fleet (기존 시나리오) KPI 변화 없음"""
```

## DO NOT

- 기존 토폴로지 generator (Type A~E) 의 동작 변경 — 모두 graph 0 으로 기본 동작
- fleet 별 motion 모델 분리 (별도 PR)
- battery 정책 fleet 분리 (별도 PR)
- 다층 / fiducial 도입 (별도 PR)
- OpenRMF rmf_fleet_adapter 통합 (별도 PR)

## Acceptance

- 위 8개 신규 테스트 PASS
- T1~T59 기존 테스트 PASS (단일 fleet baseline 무변화)
- 합성 plant + 3 fleet 시나리오 dry run 정상 (각 fleet AGV 가 자기 graph 의 lane 만 사용)
- Map Editor 에서 Active graph 토글 + Paint/Build 활성 graph 영향 시각 확인
- Quickrun 에서 fleet 색 + fleet 별 KPI 카드 표시 확인

## 구현 순서 (~4.5일)

| 단계 | 작업 | 견적 |
|---|---|---|
| 1 | 데이터 모델 — `Edge.graph_idx`, `Fleet`, `AGV.fleet_id` + 기본값 호환 | 0.5일 |
| 2 | YAML import 확장 — `fleets:` 섹션 + `lanes graph_idx` 파싱 | 0.5일 |
| 3 | A* 라우터 fleet 필터링 | 0.5일 |
| 4 | 예약 충돌 priority tiebreaker | 0.2일 |
| 5 | Map Editor — Active graph 토글 + Paint/Build 활성 graph 영향 + 색 분리 | 1일 |
| 6 | Quickrun — fleet 색 + fleet 별 KPI + 시뮬 시작 시 fleet 배치 | 0.7일 |
| 7 | Case 비교 (`run_imported_cases.py`) — fleets 정의 + ranking fleet 분해 | 0.3일 |
| 8 | 테스트 + 합성 plant dry run | 0.8일 |

## Final Verification

```bash
python tests/integration/test_simulation.py
# → 기존 + 신규 다 PASS

python scripts/import_map_demo.py maps/synthetic_3fleet.json --edit --open
# → Active graph 토글 + Paint 동작 확인

./run quickrun
# → 3 fleet 시나리오 시뮬, fleet 별 KPI 카드 확인
```
