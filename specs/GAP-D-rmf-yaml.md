# GAP-D — RMF building_map YAML import (+ 기본 export)

> Agent 의뢰용. 견적 ~0.5~0.7일.
> 다른 GAP 과 독립. F1a 와 자연 결합.

## Goal

폐쇄망 ICS 시스템이 export 하는 **YAML 형식 (Open-RMF building_map)** 을 우리 임포터가 직접 받을 수 있도록. JSON 도 그대로 지원 유지.

## 사용자 의도 매핑

- **의도 #1** — 현장 map 정보를 JSON/YAML 형식으로 뽑아서 Map Editor 에 업로드
- **의도 #8** — OpenRMF 모사 → 데이터 호환

## Scope

- IN:
  - `scripts/import_map_demo.py` 가 `.yaml` / `.yml` 확장자 자동 감지
  - `external_importer.import_map_json()` 또는 분기 함수 — YAML 도 파싱
  - YAML 형식: Open-RMF `building_map.yaml` 표준
    - `levels[].vertices`, `levels[].lanes`
    - lanes 의 `graph_idx` 인식 (F1a 와 호환)
  - Quickrun `/upload-map` 도 YAML 받을 수 있도록 (파일명 확장자 또는 content-type 분기)
  - 기본 export: `ImportedMap` → YAML 으로 저장 (CLI `--export-yaml` 옵션, 선택)
- OUT:
  - YAML 전체 export (level/walls/floors/fiducials 등) — vertices + lanes + fleets 만
  - `.dxf` / `.svg` / 다른 포맷
  - VDA5050 메시지 호환 (별도)
  - 다층 (multi-level) — 첫 레벨만 사용

## Pre-step (discovery — 필수)

1. `src/domain/map/graph.py` 의 `from_rmf_yaml()` 함수 — 기존 YAML 로더 분석. 사용 가능?
2. `src/domain/map/external_importer.py` 의 `import_map_json()` — 분기 패턴
3. `src/interfaces/quickrun/server.py` 의 `/upload-map` — 파일 받는 방식 (현재 JSON 만?)
4. `scripts/import_map_demo.py` — 확장자 분기 위치
5. (의존성) `pyyaml` 이미 requirements.txt 에 있음 — 추가 X

→ 발견 결과 보고 후 구현. `from_rmf_yaml()` 이 이미 graph.py 에 있으니 **재활용 vs 어댑터 신설** 결정.

## Interface

### CLI

```bash
# 자동 감지 (확장자로)
python scripts/import_map_demo.py path/to/plant.yaml --edit --open
python scripts/import_map_demo.py path/to/plant.json --edit --open  # 기존 동작

# (선택) 강제 형식
python scripts/import_map_demo.py path/to/plant.txt --format yaml --edit --open

# (선택) export
python scripts/import_map_demo.py plant.json --export-yaml plant.yaml
```

### Quickrun /upload-map

기존:
```json
POST /upload-map
{ "name": "plant", "map_json": {...} }
```

확장:
```json
POST /upload-map
{ "name": "plant", "map_json": {...} }    // JSON (기존)
또는
{ "name": "plant", "map_yaml": "..." }    // YAML 문자열 (신규)
```

또는 form-data 로 파일 업로드 (확장자 자동 감지):
```
POST /upload-map  (multipart/form-data)
file: plant.yaml
name: plant
```

### YAML 스키마 (Open-RMF building_map 표준 부분 집합)

```yaml
levels:
  L1:
    elevation: 0.0   # 첫 레벨만 사용 (다층 무시)
    vertices:
      - [x, y, "name", {is_charger: true, is_holding_point: false}]
      - [x, y, "name", {}]
      ...
    lanes:
      - [v0_idx, v1_idx, {graph_idx: 0, bidirectional: true, speed_limit: 1.5}]
      ...

# 우리 확장 (선택)
fleets:
  - {id: TYPE_1, graph_idx: 0, color: "#0f9d58",
     capabilities: [overhead], count: 6}
```

## Tests

```python
def test_import_yaml_basic_vertices_lanes():
    """간단 YAML — vertices/lanes 정상 파싱"""

def test_import_yaml_graph_idx_recognized():
    """lanes 의 graph_idx 가 Edge.graph_idx 로 들어옴 (F1a 호환)"""

def test_import_yaml_fleets_section():
    """fleets 섹션 있으면 ImportedMap.fleets 채워짐"""

def test_import_yaml_legacy_no_fleets():
    """fleets 없는 legacy YAML 도 정상 (단일 fleet)"""

def test_import_extension_auto_detect():
    """import_map_demo.py 가 .yaml/.yml/.json 확장자 보고 자동 분기"""

def test_upload_map_yaml_via_rest():
    """POST /upload-map 이 map_yaml 또는 multipart file 처리"""

def test_unknown_yaml_format_clear_error():
    """RMF 표준 아닌 YAML — 명확한 에러 메시지"""

def test_export_yaml_roundtrip():
    """(선택) JSON import → YAML export → YAML import → 같은 결과"""
```

## DO NOT

- VDA5050 메시지 포맷 호환
- 다층 (multi-level) — 첫 레벨만 사용
- walls / floors / models 시뮬 통합 (정적 환경 무시)
- fiducials (좌표 정렬용 — UI 외)
- 새 파일 형식 추가 (.dxf, .svg 등)
- JSON 파서 변경 (그대로 유지)

## Acceptance

- 위 7~8개 테스트 PASS (export 는 선택)
- 기존 JSON 시나리오 무영향
- Open-RMF traffic-editor 가 export 한 YAML 직접 import 가능
- 확장자 자동 감지 작동

## Final Verification

```bash
python tests/integration/test_simulation.py

# 합성 YAML 생성
cat > /tmp/test.yaml <<EOF
levels:
  L1:
    vertices:
      - [0, 0, "N1", {is_charger: true}]
      - [10, 0, "N2", {}]
      - [20, 0, "N3", {}]
    lanes:
      - [0, 1, {bidirectional: true}]
      - [1, 2, {bidirectional: false}]
EOF
python scripts/import_map_demo.py /tmp/test.yaml --open
# → preview HTML 정상 생성
```

스크린샷 + import log 첨부 권장.
