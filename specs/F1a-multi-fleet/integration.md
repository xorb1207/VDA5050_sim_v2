# F1a Integration — YAML 스키마 + Case 비교 + 시나리오 테스트

> 사용자 또는 Integration Agent. 견적 ~1.3일.
> Engine + UI 두 layer 완료 후 마지막. 합산 검증 책임.

## Goal

3개 layer (engine / ui-editor / ui-quickrun) 통합. YAML 데이터 형식 호환, Case 비교 시 fleet 별 분해, end-to-end 시나리오 dry run 검증.

## 사용자 의도 매핑

- 의도 #1 — YAML 도 import 가능 (RMF building_map 호환)
- 의도 #7 — Case 비교 (fleet 별 분해된 ranking)
- 의도 #8 — OpenRMF 데이터 포맷 호환

## Scope

- IN:
  - `external_importer.py` 에 fleet 정보 파싱 추가 (JSON 의 `fleets:` 또는 YAML 의 `levels[].lanes[].graph_idx`)
  - `run_imported_cases.py` 에 fleet 별 KPI 컬럼 추가
  - 합성 3-fleet 테스트 맵 (`maps/synthetic_3fleet.json`) 생성
  - End-to-end 시나리오 테스트 (engine + UI + 시뮬 통합)
  - Editor 의 Save 시 fleets 정보 포함된 edit.json export
- OUT:
  - 새 import 형식 추가 (JSON / YAML 만)
  - VDA5050 메시지 포맷 호환 (별도)

## Pre-step (discovery — 필수)

1. Engine spec 완료 후 — `Fleet` 클래스 위치, `ImportedMap.fleets` 속성 형태
2. `src/domain/map/external_importer.py` — JSON / YAML 분기 로직
3. `scripts/run_imported_cases.py` — variant 별 결과 집계 로직
4. `scripts/generate_synthetic_plant.py` — 합성 맵 generator 패턴

## Interface

### JSON 스키마 (사용자 폐쇄망 형식 확장)

```json
{
  "nodes": [...],
  "links": [
    {"id": "L001", "connected": {"from": "N001", "to": "N002"},
     "graph_idx": 0}    // ★ 신규 (없으면 0)
  ],
  "fleets": [             // ★ 신규 (optional — 없으면 단일 fleet)
    {"id": "A", "graph_idx": 0, "color": "#0f9d58", "count": 6,
     "max_speed_mps": 1.5, "priority": 1}
  ]
}
```

### YAML 스키마 (Open-RMF building_map 호환)

```yaml
levels:
  L1:
    vertices:
      - [x, y, "name", {is_charger: true}]
    lanes:
      - [v0, v1, {graph_idx: 0, bidirectional: true}]

fleets:
  - {id: A, graph_idx: 0, color: "#0f9d58", count: 6}
```

### Case 비교 YAML 확장

```yaml
source_map: maps/plant.yaml
duration_s: 1200
random_seeds: [42, 43, 44]

# ★ 신규 — fleet 별 시나리오 정의
fleets:
  - {id: A, count: 6}
  - {id: B, count: 4}
  - {id: C, count: 2}

variants:
  - {label: "v0_baseline"}
  - {label: "v1_more_A", fleets: [{id: A, count: 10}]}  # variant 가 fleet 덮어쓰기
  - {label: "v2_swap_graph", edit_file: maps/v2.edit.json}
```

### Ranking CSV 컬럼 추가

기존:
```
label, seed, completion_rate, throughput, avg_wait, headon, retry, deadlock
```

확장:
```
label, seed, completion_rate, throughput, avg_wait, headon, retry, deadlock,
fleet_A_throughput, fleet_A_utilization,
fleet_B_throughput, fleet_B_utilization,
fleet_C_throughput, fleet_C_utilization
```

## Tests

```python
def test_json_with_fleets_parsed():
    """JSON 에 fleets 필드 있으면 ImportedMap.fleets 에 들어옴"""

def test_yaml_rmf_building_map_loaded():
    """Open-RMF YAML 형식 import — lanes graph_idx 인식"""

def test_legacy_json_no_fleets_default_single_fleet():
    """기존 JSON (fleets 없음) → 단일 fleet 자동 생성 (호환성)"""

def test_run_imported_cases_fleet_breakdown_in_csv():
    """ranking.csv 에 fleet_A_throughput 등 컬럼 존재"""

def test_run_imported_cases_per_fleet_columns_populated():
    """fleet 별 KPI 가 0 이 아닌 값으로 채워짐 (3 fleet 시나리오)"""

def test_editor_save_includes_fleets():
    """*.edit.json 에 fleet override 정보 포함 (count 변경 등)"""

def test_e2e_3fleet_synthetic_runs():
    """maps/synthetic_3fleet.json 로 시뮬 정상 종료"""
```

## DO NOT

- 단일 fleet (기존) 시나리오 변경
- Quickrun WS 프로토콜 큰 변경 — engine spec 의 contract 따름
- 새 데이터 형식 도입 (JSON/YAML 만)

## Acceptance

- 위 7개 신규 테스트 PASS
- 합성 3-fleet 맵 (`maps/synthetic_3fleet.json`) 생성됨
- `run_imported_cases.py` 3-fleet 시나리오 정상 실행 → ranking.html 에 fleet 별 컬럼
- 단일 fleet baseline 동작 무변화

## Final Verification

```bash
# 1. 합성 데이터로 일관성 검증
python scripts/generate_synthetic_3fleet.py --out maps/synthetic_3fleet.json

# 2. Engine + UI + Case 비교 end-to-end
python scripts/import_map_demo.py maps/synthetic_3fleet.json --edit --open
# → Editor 의 Active Graph 토글 확인 → Save & Run

./run quickrun
# → 토폴로지 드롭다운에서 임포트 → fleet 별 슬라이더 → 실행
# → fleet 색 AGV + fleet 별 KPI 카드

python scripts/run_imported_cases.py experiments/3fleet_compare.yaml --open
# → ranking.html 에 fleet_A_throughput 등 컬럼

# 3. 회귀
python tests/integration/test_simulation.py
```

로그 + 페이지 스크린샷 + ranking.html 첨부.

## 시나리오 점검 체크리스트

```
□ 의도 #1: YAML 도 import 됨
□ 의도 #2: Editor 에서 fleet 별 graph 편집됨
□ 의도 #3: 시뮬 + 히트맵 작동 (fleet 무관)
□ 의도 #4: 수동 job 동작 (GAP-B 와 통합 시 검증)
□ 의도 #5: Edge 차단 + reroute (GAP-A 와 통합 시 검증)
□ 의도 #6: KPI fleet 별 분리됨
□ 의도 #7: ranking.html 에서 case 비교 + fleet 분해
□ 의도 #8: RMF building_map YAML import 됨
```
