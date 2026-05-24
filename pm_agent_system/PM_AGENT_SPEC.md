# PM Agent System — 설계 및 운영 명세

> **목적**: FAB AMR 시뮬레이터(vda5050_sim_v2) 프로젝트의 코딩 작업을 Telegram 채팅만으로 자동화하는 로컬 PM 봇 시스템.

---

## 1. 개요

사용자(Teo)가 Telegram으로 "이거 구현해줘" 라고 보내면:

1. **PM Agent** (Haiku API) — 요청을 분석해 태스크 스펙 `.md` 파일 생성
2. **Orchestrator** — 태스크 파일을 감지하고 Claude Code CLI 실행
3. **Claude Code CLI** (로컬 구독, API 비용 $0) — 실제 코딩
4. **Review Agent** (Haiku API) — 결과물 scope/품질 검토
5. **GitManager** — feature branch → main 머지 → `git push origin main`
6. **Telegram Bot** — 완료/실패 알림 전송

```
사용자(Telegram)
      │ 메시지
      ▼
 PM Agent (Haiku)
      │ task .md 생성
      ▼
 task_queue/*.md
      │ watchdog 감지
      ▼
 Orchestrator
      │ subprocess
      ▼
 Claude Code CLI ──── 로컬 구독 (API 비용 $0)
      │ stdout
      ▼
 Review Agent (Haiku)
      │ PASS/FAIL
      ▼
 GitManager
      │ merge + push origin/main
      ▼
 Telegram 완료 알림
```

---

## 2. 파일 구조

```
pm_agent_system/
├── main.py            # 진입점. asyncio.gather(bot, orchestrator)
├── config.py          # .env 로드 → Config dataclass
├── pm_agent.py        # Telegram ↔ Claude 대화, 태스크 .md 생성
├── orchestrator.py    # task_queue 감시, CLI 실행, 직렬 처리
├── review_agent.py    # 코드 리뷰 (scope check → LLM review)
├── git_manager.py     # branch 생성, commit, main 머지, push
├── telegram_bot.py    # Telegram 봇 인터페이스, 슬래시 명령
├── schemas.py         # TaskPacket, CompletedPacket, ReviewVerdict 등
├── .env               # 비밀값 (git 제외)
├── state/
│   └── git_state.json # 활성/완료 태스크 상태 영속화
├── task_queue/        # 대기 태스크 .md 파일 (git 제외)
├── completed/         # 완료 태스크 JSON (git 제외)
└── logs/              # 로그 (git 제외)
```

---

## 3. 컴포넌트 상세

### 3-1. PM Agent (`pm_agent.py`)

- **모델**: `PM_DIALOG_MODEL` (현재 `claude-haiku-4-5`)
- **역할**: 사용자 메시지 → 태스크 JSON 추출 → `.md` 파일 저장
- **히스토리**: 최대 15턴 유지, 초과 시 util_model로 요약 압축
- **비용 최적화**:
  - system prompt + spec summary에 `cache_control: ephemeral` 적용
  - API 전송 시 오래된 히스토리 메시지를 600자로 트리밍 (현재 메시지는 full 전달)
  - `max_tokens: 2048`

**태스크 JSON 형식** (PM이 응답에 포함하면 자동 저장):
```json
{
  "task_id": "T-70",
  "title": "한 줄 제목",
  "files": ["src/foo.py", "src/bar.py"],
  "description": "상세 구현 지침",
  "priority": 0
}
```

### 3-2. Orchestrator (`orchestrator.py`)

- **감시**: `watchdog`으로 `task_queue/` 디렉토리 모니터링
- **실행 방식**: **직렬 처리** (병렬 X) — 충돌 원천 차단
  - 태스크 A 완료 후 → main 업데이트 → 태스크 B 시작
- **CLI 호출**:
  ```
  claude --print --permission-mode acceptEdits
         --allowedTools Read,Write,Edit,Bash
         --model {CLI_MODEL}
         {task_content}
  ```
  - `cwd`: 프로젝트 루트
  - `stderr`: 상속 (터미널에 실시간 출력)
  - `timeout`: 3600초 (1시간)
- **재시도**: 최대 3회, 실패마다 Telegram 알림
- **worktree 정리**: 태스크 시작 전 `git worktree prune` 자동 실행

### 3-3. Review Agent (`review_agent.py`)

- **모델**: `ANTHROPIC_MODEL` (현재 `claude-haiku-4-5`)
- **2단계 검토**:
  1. **Pre-LLM scope check** (API 비용 $0): 변경 파일이 허용 파일 목록을 벗어나면 즉시 FAIL
  2. **LLM review**: spec context + diff + test result → PASS/FAIL/NEEDS_REVISION
- **비용 최적화**: system prompt 캐싱, `max_tokens: 512`, code_diff 4000자 제한

### 3-4. GitManager (`git_manager.py`)

**PR 없음 — main 직접 머지 구조**:
```
git checkout feat/T-70
git add -u
git commit -m "feat(T-70): ..."
git checkout main
git pull --ff-only origin main
git merge --no-ff feat/T-70
git push origin main
git branch -d feat/T-70
git worktree prune
```

- 태스크 간 직렬 실행이 보장되므로 충돌 없음
- 브랜치 이름: `feat/{task_id}-{MMDD}` (날짜 suffix로 재실행 충돌 방지)
- stale lock 자동 해제: 시작 시 2시간 초과 active_tasks 제거

### 3-5. Telegram Bot (`telegram_bot.py`)

- **라이브러리**: `python-telegram-bot` v20+ (async)
- **실행 방식**: `async with app` + `updater.start_polling()` (기존 asyncio 루프 재사용)

**슬래시 명령**:
| 명령 | 동작 |
|---|---|
| `/status` | 활성 태스크, 완료 수, 경과 시간 |
| `/approve` | 대기 중 태스크 승인 |
| `/reload` | task_queue 재스캔 |
| `/level VERBOSE\|NORMAL\|QUIET` | 알림 레벨 변경 |
| `/help` | 명령 목록 |

**보호 로직**:
- raw JSON 응답 Telegram 노출 차단 (`{"task_id":...}` 패턴)
- 단일 인스턴스 락파일 (`/tmp/pm_agent_system.lock`)

---

## 4. 설정 (`.env`)

```env
# 필수
ANTHROPIC_API_KEY=sk-ant-...
REPO_PATH=/Users/tg/vda5050_sim_v2
SPEC_PATH=/Users/tg/vda5050_sim_v2/CLAUDE.md

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 모델
ANTHROPIC_MODEL=claude-haiku-4-5      # Review/Util 모델
PM_DIALOG_MODEL=claude-haiku-4-5      # PM 대화 모델
CLI_MODEL=claude-opus-4-7              # 코딩 CLI (로컬 구독, API 비용 $0)

# 동작
DRY_RUN=False
NO_AUTO_PR=false
NOTIFICATION_LEVEL=NORMAL             # VERBOSE / NORMAL / QUIET
```

---

## 5. 실행

```bash
# 시작
python pm_agent_system/main.py

# dry-run (git 작업 없이 테스트)
python pm_agent_system/main.py --dry-run

# 상태 확인 (봇 실행 없이)
python pm_agent_system/main.py --status
```

---

## 6. 비용 구조

**API 비용이 발생하는 구간**:
| 구간 | 모델 | 시점 |
|---|---|---|
| PM 대화 | Haiku 4.5 | 사용자 메시지마다 |
| Review | Haiku 4.5 | 태스크 완료 시 (최대 3회) |
| 히스토리 압축 | Haiku 4.5 | 15턴 초과 시 |

**Claude Code CLI 코딩**: 로컬 구독 사용 → **API 비용 $0**

**50턴/일 기준 월간 예상 비용 (Haiku 4.5)**:
| 사용 패턴 | 월간 비용 |
|---|---|
| 일반 대화 (짧은 메시지) | ~$4.7 |
| 코드 붙여넣기 포함 | ~$5.0 |

*Sonnet 4.6 사용 시 동일 조건 ~$14/월*

**비용 최적화 적용 사항**:
- system prompt + spec에 `cache_control: ephemeral` (캐시 히트 시 90% 할인)
- 히스토리 오래된 메시지 600자 트리밍 (코드 붙여넣기 누적 차단)
- `max_tokens` 대화 2048, 리뷰 512로 제한

---

## 7. 태스크 처리 흐름

```
1. 사용자: "Editor에 속성 편집 기능 추가해줘"

2. PM Agent → 태스크 분석 → 응답:
   "T-70으로 등록하겠습니다.
   ```json
   {"task_id": "T-70", "files": ["src/.../editor.py"], ...}
   ```"

3. task_queue/00_T-70.md 생성 (자동)

4. Orchestrator 감지 → GitManager.create_branch("T-70", files)
   → feat/T-70-0524 브랜치 생성

5. Claude Code CLI 실행 (로컬 구독)
   → 코드 작성, 테스트 실행
   → stdout으로 결과 JSON 출력

6. Review Agent:
   → scope check: 변경 파일 ⊆ 허용 파일? ✅
   → LLM review: PASS ✅

7. GitManager:
   → feat/T-70-0524를 main에 --no-ff 머지
   → git push origin main
   → 로컬 브랜치 삭제

8. Telegram 알림: "✅ T-70 완료! main 머지+push됨
   commit: a1b2c3d
   feat(T-70): Editor 속성 풀 편집 기능"
```

---

## 8. 알려진 한계 및 개선 여지

### 현재 한계

| # | 문제 | 상태 |
|---|---|---|
| L1 | CLI stdout에서 파일 변경 목록 파싱이 취약 (CLI가 JSON 안 뱉으면 fallback) | 미해결 |
| L2 | Review Agent가 실제 `git diff` 대신 CLI stdout을 diff로 사용 | 미해결 |
| L3 | CLI 실시간 진행 상황이 Telegram에 안 보임 (터미널에만 출력) | 미해결 (B2 계획) |
| L4 | 태스크 실패 시 원인이 Telegram에 간략하게만 표시됨 | 미해결 |
| L5 | 다중 세션 간 PM Agent 히스토리가 초기화됨 (프로세스 재시작 시) | 미해결 |

### Phase 2 계획

| 우선순위 | 항목 | 내용 |
|---|---|---|
| 1 | **CLI stdout 실시간 Telegram 스트리밍** | CLI 실행 중 진행 상황 알림 |
| 2 | **실제 git diff 기반 Review** | `git diff main...HEAD` 결과를 Review Agent에 전달 |
| 3 | **히스토리 영속화** | 재시작 후에도 이전 대화 컨텍스트 유지 |
| 4 | **태스크 취소/수정 명령** | `/cancel T-70`, `/retry T-70` |
| 5 | **HISTORY.md 자동 업데이트** | 태스크 완료 시 이력 파일 자동 기록 |

---

## 9. 의존성

```
anthropic          # Claude API (PM, Review)
python-telegram-bot >= 20.0  # Telegram Bot (async)
watchdog           # 파일시스템 이벤트
python-dotenv      # .env 로드
PyGithub           # (선택) PR 생성 — 현재 미사용
```

---

## 10. 설계 결정 이유

| 결정 | 이유 |
|---|---|
| PR 없이 main 직접 머지 | PR 기반 시 여러 태스크 간 충돌 빈발, 수동 머지 부담 |
| 직렬 실행 | 파일 lock 충돌 원천 차단 — 병렬이면 같은 파일 동시 수정 위험 |
| CLI 코딩에 구독 사용 | API 과금 없이 Opus급 코딩 가능 ($20/월 구독) |
| Haiku PM 대화 | "요청 → 태스크 스펙" 변환은 Haiku로 충분. Sonnet 대비 67% 절감 |
| feature branch 유지 | main 직접 커밋 대신 branch → merge: 롤백 용이, 히스토리 명확 |
