# Specs — 설계 정의서 인덱스

> **spec-driven development**: 정의서 = source of truth. 변경은 spec 갱신 → agent 가 코드 작성.
> 처음 읽으시면 [`SPEC-AUTHORING.md`](SPEC-AUTHORING.md) 부터.

---

## 📂 진입 순서

1. [`SPEC-AUTHORING.md`](SPEC-AUTHORING.md) — **spec 을 어떻게 쓰는가**. 표준 템플릿 + 작성 룰. (메타)
2. [`operations-scenarios.md`](operations-scenarios.md) — **사용자가 무엇을 원하는가**. 9개 시나리오 + GAP. 모든 spec 이 이걸 참조.
3. 개별 feature spec — 아래 인덱스.

---

## 📋 Spec 인덱스

### 🔴 활성 (다음 사이클)

| # | Spec | 견적 | 사용자 의도 | 상태 |
|---|---|---|---|---|
| 1 | [**GAP-A**](GAP-A-edge-block-ui.md) — Quickrun 라이브 Edge 차단 UI | ~0.5일 | #5 | 🟢 spec 완료 |
| 2 | [**GAP-B**](GAP-B-manual-job-ui.md) — 수동 Job 부여 UI | ~0.5일 | #4 | 🟢 spec 완료 |
| 3 | [**GAP-C**](GAP-C-traffic-heatmap.md) — Traffic 밀도 히트맵 | ~0.3일 | #3 | 🟢 spec 완료 |
| 4 | [**GAP-D**](GAP-D-rmf-yaml.md) — RMF YAML import (+ 기본 export) | ~0.5~0.7일 | #1, #8 | 🟢 spec 완료 |
| 5 | [**F1a**](F1a-multi-fleet/) — Multi-graph + capability (이기종 AGV) | ~5.5~6일 | **#9**, #8 (장기) | 🟢 spec 완료 (ICS) |

**합산 활성 spec**: GAP A~D ~1.8일 + F1a ~6일 = **~8일**.
ABCD 먼저 (병렬 의뢰 시 ~1일 + 통합), 그 후 F1a (Engine ★ → UI 병렬 → Integration) ~2주.

**합의**: GAP A~D (~1.8일) 먼저, 그 다음 F1a. ABCD 가 사용자 의도 4개 GAP 직접 해소.

### 🟢 완료 (main 머지됨)

| Spec | 위치 | 비고 |
|---|---|---|
| Per-edge v_max (Agent A) | [`archive/agent_a_map_editor_spec.md`](archive/agent_a_map_editor_spec.md) | 2026-05-15 |
| JobDispatcher / KPI (Agent B) | [`archive/agent_b_simulator_engine_spec.md`](archive/agent_b_simulator_engine_spec.md) | 2026-05 |
| Job Creation (Agent C) | [`archive/agent_c_job_creation_spec.md`](archive/agent_c_job_creation_spec.md) | 2026-05 |
| 외부 맵 임포터 + Editor + Quickrun + Case 비교 | (직접 dialog 진행) | 2026-05 trusting-jemison |

### ⏸ 백로그 (보류)

- charging contention / priority reservation / 회전 감속 / wait_time 통합 — `CLAUDE.md` 보류 섹션 참고
- 24h 호라이즌 시나리오 — battery 의미 구간
- (Advanced) 혼합 토폴로지 실험 — 운영 도구로는 후순위

---

## 🗂 디렉토리 구조

```
specs/
├── README.md                       ← 이 파일 (진입점)
├── SPEC-AUTHORING.md               ← 작성 표준 (메타)
├── operations-scenarios.md         ← 사용자 의도 8개 박제
│
├── F1a-multi-fleet/                ← 복합 feature (layer 분리)
│   ├── README.md                   ← 한 줄 요약 + layer 분배
│   ├── engine.md                   ← 도메인/엔진 (Engine Agent)
│   ├── ui-editor.md                ← Map Editor UI (Claude Design)
│   ├── ui-quickrun.md              ← Quickrun UI (Claude Design)
│   └── integration.md              ← 통합 + 시나리오 테스트
│
└── archive/                        ← 완료/폐기 spec 보존
    ├── agent_a_map_editor_spec.md
    ├── agent_b_simulator_engine_spec.md
    └── agent_c_job_creation_spec.md
```

---

## 🛤 작업 사이클 (표준)

```
1. operations-scenarios 의 의도 확인  →  GAP/feature 식별
2. spec 작성 (SPEC-AUTHORING 템플릿)   →  PR 또는 commit
3. Agent 또는 Claude Design 에 의뢰     →  worktree 격리
4. Agent: Pre-step discovery 보고      →  사용자 confirm
5. Agent: 구현 + Final Verification    →  로그 첨부
6. 사용자 통합 (merge / cherry-pick)   →  main 진입
7. spec 의 상태를 "완료" 로 갱신       →  archive 또는 status 표시
```

---

## 변경 시 절차

### 새 feature 추가
1. `specs/<feature-id>.md` 또는 `specs/<feature-id>/` 신설
2. operations-scenarios 의 의도 매핑
3. 이 파일 (`specs/README.md`) 의 인덱스 갱신
4. 의뢰

### 기존 spec 수정
1. spec 파일 직접 수정 (git commit)
2. 영향받는 agent / Claude Design 다시 의뢰
3. (선택) `changelog.md` 한 줄 추가

### Spec 폐기
1. `specs/archive/` 로 이동
2. README 인덱스에서 "활성" → "완료" 또는 "폐기" 로 이동
