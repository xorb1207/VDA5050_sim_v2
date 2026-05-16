# Spec 작성 표준 (META)

> 이 문서는 **spec 을 어떻게 쓰는가** 의 표준. 새 spec 작성 시 이 가이드를 따른다.
> 변경 시: 이 문서 수정 → 기존 spec 점진적 갱신 (한꺼번에 X).

---

## 🎯 철학

```
사용자 = 아키텍트 + PM (도메인 결정자)
Agent   = 실행자        (코드 작성)
Claude Design = UI 전문가 (디자인)
Spec    = source of truth (변경의 단일 출처)
```

**핵심**: 정의서를 수정해야 코드가 바뀐다. 코드만 바꿔서 spec 과 drift 가 생기면 다음 사이클이 헷갈림.

**예외 (lightweight fix)**: 한 줄짜리 버그 fix · 오타 · README 미세 수정 등은 spec 갱신 없이 직접 코드. 임계: **30분 미만 + 한 파일 미만**.

---

## 📂 디렉토리 패턴

```
specs/
  ├── SPEC-AUTHORING.md         ← 이 문서 (메타)
  ├── operations-scenarios.md   ← 사용자 의도 박제 (모든 spec 의 상위 reference)
  ├── README.md                 ← 진입점 + 인덱스 + 상태
  ├── <feature>.md              ← 단순 한 layer feature (file 하나)
  ├── <feature>/                ← 복합 feature (layer 별 분리)
  │   ├── README.md
  │   ├── engine.md             ← 도메인/엔진 변경 (Agent 코더용)
  │   ├── ui-<surface>.md       ← UI 변경 (Claude Design용)
  │   ├── integration.md        ← 통합 + 시나리오 테스트
  │   └── changelog.md          ← (선택) 변경 이력
  └── archive/                  ← 완료/폐기된 spec 보존
```

**언제 디렉토리화?** — engine + UI + 통합이 모두 변경되는 feature. layer 별로 다른 agent / Claude Design 으로 의뢰될 때.

---

## 📐 Spec 표준 템플릿 (1 파일 spec 또는 layer subspec)

```markdown
# Task: <feature-id> — <한 줄 제목>

## Goal
<왜 이 작업을 하는가. 한두 문장.>

## 사용자 의도 매핑
> specs/operations-scenarios.md 의 어느 항목을 충족하는가?
- 의도 #N — <간단 설명>

## Scope
- IN:
  - <포함되는 변경 1>
  - <포함되는 변경 2>
- OUT:
  - <명시적으로 제외되는 것 — 별도 PR/spec>

## Pre-step (discovery — 구현 전 먼저 실행)
1. <코드 어디 만지는지 — 파일/함수 위치 확인>
2. <기존 API/필드 확인>
3. <연관 데이터 모델 확인>
→ 발견 결과 보고 후 구현 진행 ← ★ 필수 단계

## Interface
- 함수 시그니처:
  ```python
  def foo(...) -> ...
  ```
- YAML/JSON 스키마:
  ```yaml
  ...
  ```
- REST endpoint (UI spec 이라면):
  ```
  POST /endpoint  body: {...}  → {...}
  ```

## Tests
> 이름/시그니처는 illustrative. discovery 결과에 따라 실제 API 에 맞춰 조정 가능.

```python
def test_<feature>_<scenario>():
    ...
```

## DO NOT  ← ★ scope creep 방지
- <건드리지 말아야 할 영역 1>
- <건드리지 말아야 할 영역 2>

## Acceptance
- <PASS 조건 1>
- <PASS 조건 2>
- 기존 회귀 테스트 PASS 유지
- baseline 시나리오 KPI 무변화 (해당되면)

## Final Verification (마지막 단계)
```bash
python tests/integration/test_simulation.py > /tmp/test_run.log 2>&1
echo "Exit: $?" >> /tmp/test_run.log
```
보고 시 `/tmp/test_run.log` 첨부.
```

---

## ✅ Spec Quality 체크리스트

작성 후 self-check:

```
□ Goal — 한두 문장으로 끝남? 그 이상이면 spec 이 커진 것
□ 사용자 의도 매핑 — operations-scenarios 에 매칭? 없으면 의도 미정의
□ Scope IN/OUT — 명확? OUT 없으면 scope creep 위험
□ Pre-step — discovery 단계 있는가? 없으면 agent 헛 작업 위험
□ Interface — 시그니처/스키마 구체적? 모호하면 agent 가 의역
□ Tests — illustrative 이라도 시나리오 명확?
□ DO NOT — scope 침범 방지 명시? ★ 가장 중요
□ Acceptance — 무엇이 PASS 인지 측정 가능?
□ Final Verification — 명령어 박혀있고 검증 가능?
□ 전체 길이 < 1~2 페이지? — 그 이상이면 over-spec, layer 분리 고려
```

---

## ⚙ Agent 진입 룰

Agent 가 spec 받으면:

1. **Discovery 먼저** — Pre-step 항목 실행 + 결과 보고
2. **사용자 confirm 후 구현** — discovery 결과로 spec 조정 가능
3. **DO NOT 엄수** — 침범 시 즉시 멈추고 사용자에게 확인
4. **Final Verification 의무** — 로그 첨부

워크트리:
- Agent 마다 `.claude/worktrees/<branch-name>` 격리
- 통합 시 사용자 또는 별도 integration sketch

---

## 🎨 Claude Design 진입 룰

UI spec (`ui-*.md`) Claude Design 으로 의뢰 시:

1. **백엔드 contract 박혀있어야** — endpoint + 응답 형식 + 이벤트
2. **mock 데이터 제공** — prototype 가능하도록 샘플 응답
3. **인터랙션 spec** — 클릭/드래그/키 명세
4. **시각 가이드** — 기존 페이지의 색·폰트와 일치 (self-contained HTML 패턴 유지 권장 — 폐쇄망 친화)
5. **결과물 형식** — HTML/JSX/Vue 등 명시
6. **실 endpoint 연결**은 별도 단계 (사용자 또는 integration agent)

---

## 📏 Spec 작성 시간의 적정 비율

```
spec 작성 시간 < 구현 예상 시간 × 1/3
```

- spec 이 그보다 길어지면 → over-spec. 사용자가 직접 짜는 게 빠를 수 있음.
- spec 이 그보다 짧으면 → under-spec. agent 가 의역할 위험.
- **F1b-core 정도 (한 페이지) 가 sweet spot.**

---

## 🔄 변경 관리

### Spec 변경이 필요한 경우

1. spec 파일 직접 수정 (git commit)
2. 변경 후 영향받는 agent / Claude Design 다시 의뢰
3. (선택) `changelog.md` 에 변경 이유 한 줄 추가

### 새 feature 추가

1. `specs/<feature-id>.md` 또는 `specs/<feature-id>/` 신설
2. operations-scenarios 의 어느 항목 충족하는지 매핑
3. README 의 인덱스 갱신
4. Agent 의뢰

### 폐기

1. `specs/archive/` 로 이동
2. README 인덱스 갱신
