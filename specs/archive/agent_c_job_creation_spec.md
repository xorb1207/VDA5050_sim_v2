# Agent C — Job Creation Page Track (F2)

> Claude Code Agent View용 태스크 큐.
> **출발 조건: Agent B TASK 4 완료 후 시작** (`POST /job/dispatch` 스키마 확정 후).
> Agent A와 파일 구역 분리 — B T4 완료 후 동시 진행 가능.

---

## 아키텍처 개요

```
[Job Creation Page]          [Main Dashboard]
  노드 선택                    AMR 상태 확인
  + 태스크 입력          →     + Job Template 선택
  + 시퀀스 조합                + dispatch 실행
  → Job Template 저장              ↓
                           POST /job/dispatch
                           { amr_id, job_id }
```

**Job Template** = "무엇을 할지" 정의 (AMR 지정 없음)
**Dispatch** = "누가 언제 할지" 결정 (메인 대시보드에서)

---

## 작업 범위 (Boundary)

- **건드려도 되는 구역:** `frontend/job-creation/`, `frontend/dashboard/` (dispatch UI 추가), `adapters/job_api.py`
- **절대 건드리지 말 것:** `domain/`, `application/` (Agent B 구역), `frontend/map-editor/` (Agent A 구역)
- **맵 뷰 재사용:** 기존 맵 에디터 컴포넌트를 read-only 모드로 import — 새로 만들지 말 것

---

## TASK 1 — Pre-step: 현재 프론트엔드 구조 Discovery

> 구현 전 반드시 먼저 실행.

```
1. 기존 맵 에디터 컴포넌트 위치 파악 (read-only 모드 prop 있는지 확인)
2. 현재 라우팅 구조 파악 (/map-editor, /dashboard 등)
3. 기존 API 클라이언트 레이어 위치 파악
4. Agent B에서 확정된 POST /job/dispatch 스키마 확인
5. 저장소(state management) 라이브러리 파악 (Redux? Zustand? Context?)
```

보고 형식:
```
[Discovery 결과]
- 맵 에디터 컴포넌트: (파일명, read-only prop 있음/없음)
- 라우팅: (현재 라우트 목록)
- API 클라이언트: (파일명)
- /job/dispatch 스키마: (Agent B 확정 내용)
- 상태관리: (라이브러리명)
```

---

## TASK 2 — Job Template 데이터 모델 + API

> TASK 1 보고 확인 후 시작.

### Job Template 스키마

```typescript
// Job Template — AMR 지정 없는 재사용 가능한 작업 정의
interface JobTemplate {
  job_id: string           // 자동 생성 UUID
  job_name: string         // 오퍼레이터가 붙인 이름 (e.g. "웨이퍼 반출 A")
  created_at: string       // ISO timestamp
  steps: JobStep[]         // 순서 있는 태스크 시퀀스
}

interface JobStep {
  step_id: string
  node_id: string          // 대상 노드
  node_label: string       // 표시용 (e.g. "Station-A")
  task_type: TaskType      // 수행할 태스크
  task_params?: object     // 태스크별 추가 파라미터 (optional)
}

type TaskType =
  | "pick"       // 집기
  | "place"      // 놓기
  | "wait"       // 대기
  | "charge"     // 충전
  | "inspect"    // 점검
  | "custom"     // 커스텀 (label 직접 입력)
```

> **TaskType 목록은 확장 가능하도록 설계.** 나중에 항목 추가 쉽게.

### Backend API (adapters/job_api.py)

```
POST   /job/template          — Job Template 저장
GET    /job/template          — 전체 목록 조회
GET    /job/template/{job_id} — 단일 조회
DELETE /job/template/{job_id} — 삭제

POST   /job/dispatch          — AMR에게 Job Template 실행 지시
  body: { amr_id: str, job_id: str }
  response: { dispatch_id, status, estimated_completion_s }
```

### 저장소
- 초기: JSON 파일 (`data/job_templates.json`) — DB 없이 시작
- 구조: `{ templates: JobTemplate[] }`

### Tests
```python
test_create_job_template()
test_get_all_templates()
test_delete_template()
test_dispatch_with_valid_job_id()
test_dispatch_fails_with_invalid_job_id()
```

---

## TASK 3 — Job Creation 페이지 UI

> TASK 2 완료 후 시작. 라우트: `/job/create`

### 페이지 레이아웃

```
┌─────────────────────────────────────────────────┐
│  Job 이름 입력 [ 웨이퍼 반출 A ______________ ]   │
├──────────────────┬──────────────────────────────┤
│                  │  Step 시퀀스                  │
│   맵 뷰          │  ┌─────────────────────────┐ │
│   (read-only)    │  │ 1. Station-A  [pick  ▼] │ │
│                  │  │ 2. Station-B  [place ▼] │ │
│   노드 클릭       │  │ 3. Charger-1  [charge▼] │ │
│   → Step 추가    │  │ [+ 스텝 추가]            │ │
│                  │  └─────────────────────────┘ │
│                  │                               │
│                  │  [취소]        [Job 저장]     │
└──────────────────┴──────────────────────────────┘
```

### 맵 뷰 동작

- 기존 맵 에디터 컴포넌트 **read-only 모드**로 재사용
  - 노드 선택 가능 (클릭 시 하이라이트)
  - stamp, edge 그리기 등 편집 기능 전부 비활성화
- 노드 클릭 시: 우측 시퀀스 패널에 새 Step 추가
- 이미 시퀀스에 있는 노드: 색으로 구분 표시

### Step 시퀀스 패널

- 드래그&드롭으로 순서 변경
- 각 Step: 노드 이름 + TaskType 드롭다운 + 삭제 버튼
- Step 최소 1개 이상이어야 저장 가능

### 저장 플로우

```
[Job 저장] 클릭
  → 이름 validation (비어있으면 경고)
  → Step 최소 1개 validation
  → POST /job/template 호출
  → 성공 시: /job/list 로 이동
  → 실패 시: 에러 토스트 표시
```

---

## TASK 4 — Job 목록 페이지

> TASK 3 완료 후 시작. 라우트: `/job/list`

### 페이지 내용

```
┌──────────────────────────────────────────────────┐
│  저장된 Job Templates          [+ 새 Job 만들기]  │
├──────────────────────────────────────────────────┤
│  웨이퍼 반출 A   3 steps  2026-05-14  [삭제]     │
│  웨이퍼 반출 B   5 steps  2026-05-13  [삭제]     │
│  충전 루틴       2 steps  2026-05-10  [삭제]     │
└──────────────────────────────────────────────────┘
```

- GET /job/template 로 목록 로드
- 삭제 시 확인 다이얼로그 → DELETE /job/template/{id}
- Job 이름 클릭 시 상세 보기 (Step 시퀀스 표시)

---

## TASK 5 — 메인 대시보드 Dispatch UI 추가

> TASK 4 완료 후 시작.
> **이 태스크가 Agent B T4의 POST /job/dispatch API와 연결되는 지점.**

### 추가 내용 (기존 대시보드에 패널 추가)

```
┌──────────────────────────────────────┐
│  Job Dispatch                        │
│                                      │
│  AMR 선택  [ AMR-001 (idle)    ▼ ]  │
│  Job 선택  [ 웨이퍼 반출 A     ▼ ]  │
│                                      │
│              [Dispatch 실행]         │
└──────────────────────────────────────┘
```

### AMR 상태 표시

- GET /fleet_states 로 AMR 목록 + 상태 로드
- 드롭다운에 상태 함께 표시:
  - `AMR-001 (idle)` — 선택 가능
  - `AMR-002 (busy)` — 선택 가능하나 경고
  - `AMR-003 (charging)` — 선택 시 경고

### Dispatch 플로우

```
[Dispatch 실행] 클릭
  → AMR + Job 둘 다 선택됐는지 validation
  → AMR이 busy/charging이면 확인 다이얼로그
  → POST /job/dispatch { amr_id, job_id }
  → 성공: 대시보드에 실행 중 잡 표시
  → 실패: 에러 토스트 (amr_busy / no_path 등 메시지 표시)
```

### 실행 중 잡 표시

- 대시보드 플릿 상태 패널에 현재 실행 중인 Job 이름 추가
  - `AMR-001 | idle → 웨이퍼 반출 A | Step 2/3`

---

## 최종 검증 (전체 TASK 완료 후)

```bash
# 백엔드 API 테스트
pytest tests/ -v > /tmp/agent_c_final.log 2>&1
echo "Exit: $?" >> /tmp/agent_c_final.log

# 프론트엔드 빌드 확인
npm run build >> /tmp/agent_c_final.log 2>&1
```

보고 형식:
```
[Agent C 전체 완료]
- 완료 태스크: Discovery / 데이터모델 / Job Creation UI / 목록 / Dispatch UI
- API 테스트: PASS / FAIL
- 빌드: PASS / FAIL
- T1~T59: PASS / FAIL
- 미완/defer 항목: (있으면)
```

---

## 출발 조건 체크리스트

Agent C 출발 전 반드시 확인:

```
□ Agent B TASK 4 완료 보고 수신
□ POST /job/dispatch 최종 스키마 확인
□ Agent A F1b-core 완료 (맵 노드 스키마 확정)
□ 기존 맵 에디터 컴포넌트 read-only prop 지원 여부 확인
```
