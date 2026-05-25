# PM Agent System — 운영 가이드

Claude Code CLI를 자동화하는 Telegram 기반 PM Bot.
태스크 파일(`.md`)을 `task_queue/`에 넣으면 자동으로 실행 → 리뷰 → Ship 승인까지 처리.

---

## 빠른 시작

### tmux에서 실행 (권장)

```bash
# 새 세션 생성
tmux new-session -s pmbot

# PM Bot 시작
cd /Users/tg/vda5050_sim_v2/pm_agent_system
python main.py

# 세션 분리 (백그라운드 유지)
Ctrl+B, D

# 세션 재접속
tmux attach -t pmbot
```

### 일반 실행

```bash
cd pm_agent_system
python main.py           # 기본 실행
python main.py --status  # 현재 상태만 출력 후 종료
```

---

## Telegram 명령 레퍼런스

### 점검/조회

| 명령 | 설명 |
|------|------|
| `/doctor` | PM Bot 전체 상태 점검 (repo/git/태스크 현황) |
| `/queue` | 전체 작업 대기열 요약 |
| `/running` | 현재 실행 중인 태스크 확인 |
| `/log T-ID` | 태스크 최근 로그 (50줄) |
| `/diff T-ID` | 태스크 git diff 요약 + 스니펫 |
| `/status` | 시스템 상태 |

### 배포 제어

| 명령 | 설명 |
|------|------|
| `/ship T-ID` | READY_TO_SHIP 태스크 main 배포 승인 |
| `/hold T-ID` | READY_TO_SHIP 태스크 보류 (branch 유지, merge 없음) |

### Adopt / Resume

| 명령 | 설명 |
|------|------|
| `/adopt T-ID` | 직접 작업한 내용을 PM Bot에 편입 → ADOPTED 상태 |
| `/review T-ID` | ADOPTED 태스크를 Review Agent로 검토 → READY_TO_SHIP |
| `/resume T-ID` | Handoff 기반 중단 작업 재개 |

### Handoff

| 명령 | 설명 |
|------|------|
| `/handoff T-ID` | 태스크 Handoff 파일 생성/갱신 |

---

## 표준 작업 흐름

### 자동 처리 흐름 (PM Bot이 실행)

```
task_queue/01_T-91.md 생성
        ↓
[QUEUED] 📋 T-91 대기열 등록
        ↓
[RUNNING] Claude Code CLI 실행
        ↓
[REVIEWING] Review Agent 검토
        ↓
[READY_TO_SHIP] ✅ 카드 + 버튼 전송
        ↓ (Telegram에서 "Ship 승인" 버튼)
[SHIPPED] 🚀 main merge/push 완료
```

### 외부 이동 전 추천 루틴

1. `/doctor` — 현재 상태 확인
2. `/queue` — 대기 작업 없는지 확인
3. `/running` — 실행 중인 태스크 없으면 안전
4. 실행 중이라면: 태스크 종료 기다리거나 handoff 작성 후 이동

### 직접 작업 후 편입 흐름 (Adopt)

```
Claude Code CLI로 직접 작업 완료 (branch: feature/T-91)
        ↓
/adopt T-91
        → ADOPTED 상태 (git diff 수집)
        ↓
/review T-91
        → Review Agent가 실제 diff 검토
        → PASS → READY_TO_SHIP
        → FAIL → ADOPTED 유지 (재시도 가능)
        ↓
/ship T-91
        → main merge/push
```

**주의**: `/adopt` 후 `/ship` 직접 실행 불가. 반드시 `/review` 통과 필요.

### 중단 작업 재개 흐름 (Resume)

```
외부 이동 전: /handoff T-91 생성
        ↓ (이동 후 돌아와서)
/resume T-91
        → handoff 기반 resume_{T-91}.md 생성
        → watchdog이 감지 → Claude Code CLI 재실행
```

---

## 실패 시 대응 루틴

### 상황 1: CLI_ERROR / TEST_FAILED

```
실패 카드 수신
→ "🔁 재시도" 버튼: 실패 context 포함해 재실행 (최대 3회)
→ "▶ 이어서" 버튼: handoff 기반 재개
→ /log T-ID: 상세 오류 확인
```

### 상황 2: SCOPE_VIOLATION (파일 범위 초과/민감 파일)

```
실패 카드 수신 (재시도 버튼 없음)
→ 태스크 파일의 allowed_files 목록 수정
→ 새 태스크로 재등록
```

### 상황 3: 이미 배포된 카드 버튼 클릭

```
"이미 배포 완료되었습니다" 메시지
→ /status 로 확인
```

### 상황 4: handoff로 이어받기

```
/handoff T-ID  → 현재 상태 파일 생성
/adopt T-ID    → git diff 기반 현재 변경 편입
/review T-ID   → Review Agent 검토
/ship T-ID     → 배포 승인
```

---

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | (필수) | Anthropic API 키 (ReviewAgent/PMAgent용) |
| `REPO_PATH` | (필수) | 대상 repo 절대경로 |
| `SPEC_PATH` | (필수) | Claude.md / 스펙 파일 경로 |
| `TELEGRAM_BOT_TOKEN` | (필수) | Telegram Bot 토큰 |
| `TELEGRAM_CHAT_ID` | (필수) | 알림 수신 채팅 ID |
| `AUTO_SHIP_AFTER_REVIEW` | `false` | true: 리뷰 PASS 즉시 자동 머지 |
| `CLI_MODEL` | `claude-sonnet-4-5` | Claude Code CLI 모델 |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | ReviewAgent 모델 |
| `NOTIFICATION_LEVEL` | `NORMAL` | VERBOSE / NORMAL / QUIET |
| `DRY_RUN` | `false` | true: git 작업 없이 시뮬레이션 |

---

## 상태 흐름도

```
task_queue/*.md 생성
      │
      ▼
   QUEUED
      │
      ▼
   RUNNING ──(실패)──► FAILED ──► /resume (RUNNING 재진입)
      │                             │
      │                             ▼
      │                          /hold_branch (보존)
      ▼
  REVIEWING
      │
   PASS/FAIL
    │    │
    │    └──► FAILED (구조적) → 새 태스크로
    │
    ▼
READY_TO_SHIP ──► /hold ──► HELD ──► /resume
      │
      └──► /ship
              │
              ▼
          SHIPPED

외부 작업 경로:
직접 작업 → /adopt → ADOPTED → /review → READY_TO_SHIP
                                    └──► FAILED (retry 가능)
```

---

## 프로젝트 구성 (`projects.yaml`)

```yaml
default_project: vda5050
projects:
  vda5050:
    repo_path: /Users/tg/vda5050_sim_v2
    spec_path: /Users/tg/vda5050_sim_v2/CLAUDE.md
    pmbot_dir: /Users/tg/vda5050_sim_v2/pm_agent_system
```

전환: `/project ios_capture`

---

## 디렉토리 구조

```
pm_agent_system/
  main.py           진입점
  orchestrator.py   태스크 실행/상태관리
  telegram_bot.py   Telegram 인터페이스
  review_agent.py   코드 리뷰 (실제 git diff 기반)
  schemas.py        데이터 클래스
  config.py         설정 로더
  git_manager.py    git/GitHub 작업
  pm_agent.py       LLM 대화 에이전트
  project_manager.py 멀티 프로젝트 관리
  .env              환경 변수
  projects.yaml     프로젝트 목록
  task_queue/       태스크 입력 디렉토리
  completed/        완료된 태스크 JSON
  logs/tasks/       태스크별 로그
  handoffs/         Handoff 파일
  tests/            자동화 테스트
```
