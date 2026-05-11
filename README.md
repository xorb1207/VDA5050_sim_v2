# vda5050_sim_v2

반도체 FAB AMR 플릿 시뮬레이터 + 외부 맵 임포터/에디터. 폐쇄망에서 실 평면도 JSON 을 올려 정책 변형 별 KPI 를 비교하는 것이 주 목적.

스택: Python 3.12, asyncio, FastAPI, VDA5050 / Open-RMF 개념 참조.

---

## 무엇을 할 수 있나

```
폐쇄망 외부 JSON  (node + link 구조)
   ↓ import (자동 추론: 양방향 병합 / 코리도 클러스터링 / 도달성)
[A] CLI 미리보기      —  scripts/import_map_demo.py
[B] Editor 페이지     —  Paint(방향) + Stamp(역할) + Build(노드/엣지 add/del) + Undo
   ↓ Save → *.edit.json
[C] Quickrun 라이브   —  실시간 SVG + KPI + 히트맵 + ⚠ 충돌 마커
[D] Case 비교 배치    —  여러 정책 변형 × seed → ranking.html
```

---

## 설치

### 1. 소스 가져오기

```bash
git clone <repo>            # 또는 zip 다운로드
cd vda5050_sim_v2
```

### 2. Python 3.12 + 가상환경

```bash
python3.12 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
```

### 3. 의존성

**인터넷 환경**
```bash
pip install -r requirements.txt
```

**폐쇄망 (오프라인) — 미리 wheel 받아서 옮기기**
```bash
# 인터넷 환경에서:
pip download -r requirements.txt -d ./wheels --python-version 3.12

# 폐쇄망 옮긴 후:
pip install --no-index --find-links ./wheels -r requirements.txt
```

> 의존성: `fastapi`, `pydantic`, `uvicorn`, `pyyaml`, `websockets`. 모두 순수 Python 또는 wheel 형태.

### 4. 동작 확인

```bash
python tests/integration/test_simulation.py
# → "결과: 57 passed / 0 failed"
```

---

## 빠른 시작 — 3가지 시나리오

### 시나리오 1. 외부 맵 JSON 한 번 시각화하기

폐쇄망 시스템 export 형식 (`{nodes:[{id,name,position:{x,y,z}}], links:[{id,connected:{from,to}}]}`):

```bash
PYTHONPATH=. python scripts/import_map_demo.py path/to/your_plant.json --open
```

브라우저에 정적 맵 페이지가 열림. 자동 추론 결과:
- 양방향 엣지 자동 검출
- 코리도 클러스터링 (north / center / south / bay)
- 도달성 분석 (dead-end, 고립 노드 자동 경고)

> **데이터 형식 예시** (`maps/synthetic_plant.json` 참고):
> ```json
> {
>   "nodes": [{"id":"N001","name":"...","position":{"x":12.5,"y":30.0,"z":0}}],
>   "links": [{"id":"L001","connected":{"from":"N001","to":"N002"}}]
> }
> ```
> z 좌표는 무시 (2D 평면화). 추가 필드 (`node_type_cd`, `link_type_cd`) 가 있어도 무시되니 신경 안 써도 됨.

### 시나리오 2. Editor 에서 정책 편집

```bash
PYTHONPATH=. python scripts/import_map_demo.py path/to/your_plant.json --edit --open
```

브라우저 Editor 페이지에서:
- **Paint 모드** (P): 좌클릭 드래그 = 단방향, 우클릭 드래그 = 양방향
- **Stamp 모드** (S 또는 숫자 2~5): 노드 클릭 = 역할 마킹
- **Build 모드** (B): N=노드 추가, E=엣지 추가, D=삭제
- **💾 Save** → `your_plant.edit.json` 다운로드

전체 키 매핑은 아래 "Editor 사용법" 참고.

### 시나리오 3. 라이브 시뮬 (Quickrun)

```bash
./run quickrun
# → http://127.0.0.1:8765/ 자동 오픈
```

페이지에서:
1. **📂 외부 맵 업로드** — 원본 JSON + (선택) edit.json 동시 선택
2. **토폴로지 드롭다운** → `📂 your_plant` 선택
3. AGV 수 / 시뮬 속도 / 잡 주기 조정
4. **▶ 실행** — 실시간 SVG + KPI

페이지 위 **🛠 Editor** 버튼으로 같은 맵을 Editor 페이지로 즉시 진입. Editor 의 Save 가 서버 메모리에도 자동 갱신.

### 시나리오 4. Case 비교 (배치)

여러 정책 변형의 KPI 를 정량 비교:

```yaml
# experiments/plant_what_if.yaml
source_map: maps/your_plant.json
agv_count: 12
duration_s: 1200
task_interval_s: 5.0
random_seeds: [42, 43, 44]

variants:
  - {label: "v0_baseline"}
  - {label: "v1_chargers_8", edit_file: maps/your_plant_v1.edit.json}
  - {label: "v2_bidir_main", edit_file: maps/your_plant_v2.edit.json}
```

실행:

```bash
PYTHONPATH=. python scripts/run_imported_cases.py experiments/plant_what_if.yaml --open
```

결과 `outputs/imported_cases/<timestamp>/`:
- `ranking.csv` — case × seed KPI 데이터
- `summary.json` — raw 결과
- `report.html` — 정렬된 비교 표 (자동 오픈)

---

## Editor 사용법

### 모드 (우측 상단)

| 모드 | 단축키 | 용도 |
|---|---|---|
| Paint | `P` | 엣지 방향 (단/양방향) 일괄 변경 |
| Stamp | `S` | 노드 역할 (station/charger/holding/siding) 마킹 |
| Build | `B` | 노드/엣지 추가/삭제 |

### 마우스

| 조작 | 결과 |
|---|---|
| 휠 | 줌 |
| 휠 버튼 드래그 | 어떤 모드든 pan (Photoshop 표준) |
| Space + 드래그 | pan (대체 단축) |
| 좌클릭 드래그 (Paint) | trajectory → 가까운 엣지 **단방향** |
| 우클릭 드래그 (Paint) | trajectory → 가까운 엣지 **양방향** |
| Alt + 드래그 (Paint) | 단방향 역방향 강제 |
| 좌클릭 노드 (Stamp) | 현재 도구 적용 |
| 우클릭 노드 (Stamp) | 즉시 Reset (standard 로) |
| Shift + 드래그 (Stamp/Build) | 박스 다중 선택 → 일괄 적용 |

### 키보드

| 키 | 동작 |
|---|---|
| `1` | Inspect 도구 |
| `2` | Station 도구 + Stamp 모드 자동 진입 |
| `3` | Charger 도구 + Stamp 모드 |
| `4` | Holding 도구 + Stamp 모드 |
| `5` | Siding 도구 + Stamp 모드 |
| `0` | Reset 도구 + Stamp 모드 |
| `N` | (Build 모드) Add Node |
| `E` | (Build 모드) Add Edge — 노드 A → 노드 B 순차 클릭 |
| `D` | (Build 모드) Delete |
| `Cmd/Ctrl + Z` | Undo |
| `Cmd/Ctrl + Shift + Z` (또는 `Y`) | Redo |
| `Esc` | 안전 상태 (Paint + Inspect) |

### 저장

- **💾 Save**: `<source>.edit.json` 다운로드 (Quickrun 서버 떠있으면 메모리에도 자동 갱신)
- **▶ Save & Run**: 저장 후 Quickrun 페이지로 redirect

### 검증 패널 (우측 하단)

사용자 액션마다 실시간 재계산:
- Connected components / Isolated / Dead-end 카운트
- `[warn] 1 개 dead-end` 같은 경고 (charger 마킹 후 자동 해소 등)

---

## Quickrun (라이브 시뮬) 사용법

`./run quickrun` 또는 `python -m src.interfaces.quickrun.server`

### 토폴로지 옵션

- **Type A~E**: 기본 generator (논문/실험용 정형 토폴로지)
- **📂 Imported**: 업로드된 외부 맵

### 시각화 토글

| 토글 | 효과 |
|---|---|
| 🔥 히트맵 | 엣지별 누적 사고 강도 (head-on / section conflict / follow-on) — AGV 오버레이 자동 hidden |
| ⚠ 충돌 | 실시간 동일 노드/엣지 점유 의심 (빨간 ⚠ = 노드 / 노란 ! = 엣지) — zoom 무관 |

### 키 동작

- 휠: 줌, 드래그: pan, AGV 클릭: focus (좌측 상세 패널)
- 사고 묶음 클릭: 해당 시점 시뮬 점프 (recording 후)

---

## 디렉토리 구조

```
vda5050_sim_v2/
├── README.md                       ← 이 파일
├── CLAUDE.md / ARCHITECTURE.md     ← 엔진 내부 설계
├── HISTORY.md                      ← 개발 이력
├── requirements.txt
├── run                             ← 단축 셸 스크립트 (./run quickrun 등)
│
├── src/
│   ├── domain/
│   │   ├── map/
│   │   │   ├── graph.py            ← MapGraph 핵심 자료구조
│   │   │   ├── topology_generator.py   ← Type A~E 생성기
│   │   │   └── external_importer.py    ← ★ 외부 JSON 임포트 + 자동 추론
│   │   ├── agv/                    ← AGV FSM, physics
│   │   └── reservation/            ← 4계층 예약 스케줄러
│   ├── application/
│   │   ├── engine/                 ← SimulationEngine
│   │   ├── scenario/               ← TaskGenerator, DemandSet
│   │   └── usecases/
│   │       └── experiment_runner.py   ← Type A~E 비교용 (기존)
│   ├── analytics/
│   │   ├── kpi.py                  ← KPI 계산
│   │   └── playback_trace.py       ← ★ playback HTML + Quickrun live HTML
│   └── interfaces/
│       ├── quickrun/               ← ★ FastAPI 서버 (라이브 시뮬)
│       └── map_editor/             ← ★ Editor HTML 생성기
│
├── scripts/
│   ├── import_map_demo.py          ← ★ CLI: import + preview / edit
│   ├── run_imported_cases.py       ← ★ CLI: 여러 case 배치 비교
│   ├── generate_mock_plant_json.py ← 검증용 mock 생성 (정답 라벨 포함)
│   └── generate_synthetic_plant.py ← 처음 보는 가짜 FAB 생성
│
├── maps/                           ← JSON 맵 보관
│   ├── synthetic_plant.json        ← PoC 검증용 합성 평면도
│   └── synthetic_plant.edit.json   ← 충전/스테이션 마킹된 edit
│
├── experiments/                    ← YAML 정의
│   ├── synthetic_plant_what_if.yaml  ← 임포트 맵 case 비교 예시
│   └── (기타 토폴로지 비교용)
│
├── outputs/
│   ├── experiments/                ← Type A~E 비교 결과
│   └── imported_cases/             ← ★ 임포트 맵 case 비교 결과
│
└── tests/
    └── integration/test_simulation.py   ← 57 통합 테스트
```

---

## 명령어 참조

### CLI 도구

```bash
# 1. 외부 JSON 임포트 + 정적 미리보기
PYTHONPATH=. python scripts/import_map_demo.py <map.json> --open

# 2. 외부 JSON 임포트 + Editor 페이지
PYTHONPATH=. python scripts/import_map_demo.py <map.json> --edit --open

# 3. 편집 결과 적용해서 보기 (Save 한 edit.json 사용)
PYTHONPATH=. python scripts/import_map_demo.py <map.json> --edits <edit.json> --edit --open

# 4. 라이브 시뮬 서버
./run quickrun
# 또는: python -m src.interfaces.quickrun.server

# 5. case 비교 배치
PYTHONPATH=. python scripts/run_imported_cases.py <yaml> --open

# 6. 합성 데이터 생성 (검증/연습용)
PYTHONPATH=. python scripts/generate_synthetic_plant.py --out maps/test.json

# 7. 통합 테스트
python tests/integration/test_simulation.py
```

### REST / WebSocket API (Quickrun 서버, `http://127.0.0.1:8765`)

| 메소드 | 경로 | 용도 |
|---|---|---|
| GET | `/` | 라이브 시뮬 페이지 |
| POST | `/init` | 새 시뮬 시작 (topology / agvCount / speed / duration / taskIntervalS / importedMapId) |
| POST | `/control` | stop / reset |
| WS | `/ws/stream/{runId}` | tick snapshot push |
| POST | `/upload-map` | 외부 맵 + (선택) edits 업로드 |
| GET | `/imported-maps` | 업로드된 맵 목록 |
| GET | `/edit/{mapId}` | Editor 페이지 (해당 맵으로 진입) |
| POST | `/update-map/{mapId}` | Editor Save → 서버 메모리 갱신 |
| GET | `/healthz` | 헬스체크 |

---

## 자동 추론 동작 (importer)

`external_importer.py` 가 좌표와 연결만 보고 추론:

| 추론 | 방법 | 정확도 (검증) |
|---|---|---|
| 양방향 엣지 | `(from,to)` 와 `(to,from)` 짝 자동 병합 | **100%** (Type A 108 페어 모두) |
| 코리도 (north/center/south) | y 좌표 클러스터링 (y_up 가정 — 큰 y = 위쪽) | 메인 코리도 정확 |
| 베이 (vertical) | x 좌표 클러스터링 | 베이 위치 정확 |
| Access 엣지 | 짧은 엣지 (전체 25 percentile 이하) | 자동 분리 |
| Dead-end / 고립 노드 | weakly connected component + 차수 분석 | 자동 검출 |
| Charger / Station role | hint code 또는 사용자 마킹 | 자동 추론 없음 — Editor 에서 지정 |

추론 결과는 항상 "초안". Editor 페이지에서 사용자가 검토/수정.

---

## 폐쇄망 사용 팁

1. **인터넷 의존성 없음** — HTML 페이지가 모두 self-contained (외부 CDN 불러오지 않음).
2. **데이터 형식 매핑이 다른 경우**: `node_type_cd`/`link_type_cd` 같은 코드 필드는 importer 가 무시합니다. 정책은 Editor 에서 마킹.
3. **양방향 표현 가설**: 현재 importer 는 `(from,to)` + `(to,from)` 두 링크 형식을 가정. 사용자 데이터가 한 링크 + `link_type_cd` 안에 양방향 코드라면 `external_importer.py:_detect_bidirectional()` 함수에 매핑 추가 필요.
4. **좌표 단위**: m 단위 가정. mm 등 다른 단위면 `InferenceConfig.corridor_y_tolerance` 등 임계값을 단위에 맞춰 조정.
5. **출력**: `outputs/imported_cases/<timestamp>/` 아래 ranking.csv + report.html 만 외부로 가져오면 분석 가능.

---

## 알려진 한계 / 향후

- **자동 charger/station 추론 불가**: 노드 좌표만으로는 판단 X. Editor 에서 직접 마킹.
- **3층 이상 다층 FAB**: 현재 z 좌표 무시 (2D 평면화). 다층은 별도 import 후 수동 연결.
- **link_type_cd 매핑**: 코드 별 정책 매핑 yaml 자동 적용은 미지원. Editor 후처리 필요.
- **case 비교가 ExperimentRunner 풀 통합은 아님**: `run_imported_cases.py` 가 별도 경량 CLI. ranking.html 자동 생성하되 sparkline / playback 통합은 추가 작업 필요.

자세한 엔진 설계는 [`ARCHITECTURE.md`](ARCHITECTURE.md), 개발 이력은 [`HISTORY.md`](HISTORY.md) 참고.

---

## 라이선스 / 기타

내부 연구용. 외부 배포 전 별도 협의.
