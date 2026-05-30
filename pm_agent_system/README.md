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

## 공식 작업 등록 방식 (3가지)

> **운영 원칙** (고정): `task_inbox` = 작성/수정/검토 가능 · `task_queue` = 실행 전용

| 방식 | 흐름 | 용도 |
|------|------|------|
| **① 외부 즉석 등록** | `/enqueue 제목\n본문` → inbox 대기 → `[▶ 진행해]` 승인 | 간단한 작업, Telegram에서 바로 등록 |
| **② 로컬 스펙 등록** | `.pmbot/task_inbox/*.md` 작성 → `/inbox` 확인 → `[▶ 진행해]` | 긴 스펙 파일, 로컬에서 설계 후 원격 승인 |
| **③ 직접 queue 투입** | `.pmbot/task_queue/*.md` 직접 생성 | ⚠️ 비추천 — 테스트/긴급용만 |

### 로컬 스펙 등록 워크플로우 (②)

```
1. 로컬에서 작성:
   .pmbot/task_inbox/my-feature.md  (frontmatter 없어도 OK)

2. Telegram에서 확인:
   /inbox            → 목록 확인
   /inbox T-ID       → 상세 확인

3. 승인:
   [▶ 진행해] 버튼   → task_queue/ 이동 → 자동 실행
   또는: /run T-ID

4. 진행 추적:
   /queue → /log T-ID → /diff T-ID → /ship T-ID
```

> **핵심**: Telegram에 긴 스펙을 붙여넣지 않고, 로컬 파일을 Telegram에서 승인만 합니다.

---

## Telegram 명령 레퍼런스

> **평소에는 5개만 쓴다.** 나머지는 필요할 때만.

### 📋 평소 명령 (5개)

| 명령 | 설명 |
|------|------|
| `/menu` | 버튼 메뉴 (시작점) |
| `/enqueue 제목\n본문` | 즉석 작업 등록 → inbox 대기 → 승인 후 실행 |
| `/inbox` | 로컬 inbox 목록 확인 / 승인 |
| `/queue` | 전체 현황 (pending + 실행 대기, next action 포함) |
| `/log T-ID` | 실행 중 로그 확인 |
| `/ship T-ID` | 배포 승인 |

실사용 흐름: **`/enqueue` (또는 `/inbox`) → `/queue` → `/log` → `/diff` → `/ship`**

---

### 🔧 고급 명령 (`/help advanced`)

| 명령 | 설명 |
|------|------|
| `/inbox T-ID` | 특정 inbox 작업 상세 확인 |
| `/run T-ID` | inbox 작업 승인 → 실행 대기열 이동 |
| `/run next` | inbox 최우선 작업 승인 |
| `/diff T-ID` | git diff 요약 + 스니펫 |
| `/handoff T-ID` | Handoff 파일 생성/갱신 |
| `/adopt T-ID` | 직접 작업한 내용 편입 → ADOPTED |
| `/review T-ID` | ADOPTED → Review Agent → READY_TO_SHIP |
| `/resume T-ID` | Handoff 기반 중단 작업 재개 |
| `/hold T-ID` | 배포 보류 (branch 유지) |

---

### ⚙️ 운영자 명령 (`/help admin`)

| 명령 | 설명 |
|------|------|
| `/doctor` | PM Bot 전체 상태 점검 |
| `/running` | 현재 실행 중인 태스크 |
| `/status` | 시스템 상태 요약 |
| `/projects` | 등록된 프로젝트 목록 |
| `/project ID` | 프로젝트 전환 |
| `/current` | 현재 활성 프로젝트 |
| `/history` | 최근 완료/실패 이력 |
| `/stats` | 누적 통계 |
| `/stale` | 방치 작업 감지 (HELD 7일+, RTS 3일+, ADOPTED 5일+) |
| `/archive T-ID` | 태스크 archive에 수동 보관 |
| `/level VERBOSE\|NORMAL\|QUIET` | 알림 레벨 변경 |
| `/reload` | 큐 재스캔 |
| `/approve` | 콜백 승인 |

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
| `LOG_RETENTION_DAYS` | `30` | 로그 파일 보존 일수 (0=삭제 안 함) |
| `ARCHIVE_RETENTION_DAYS` | `90` | archive 보존 일수 (0=삭제 안 함) |
| `DAILY_REPORT` | `false` | true: 매일 `DAILY_REPORT_HOUR`시에 요약 Telegram 전송 |
| `DAILY_REPORT_HOUR` | `9` | 일일 요약 전송 시각 (0-23, 로컬 시간) |

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

## 운영 전 체크리스트

Bot 재시작 또는 새로운 태스크 투입 전 확인 항목:

| 항목 | 확인 방법 | 정상 상태 |
|------|-----------|-----------|
| Bot 프로세스 | `tmux attach -t pmbot` | Python 프로세스 실행 중 |
| `/doctor` 정상 | Telegram `/doctor` | ✅ 모두 초록 |
| `/current` 프로젝트 | Telegram `/current` | 대상 repo 정확히 표시 |
| `AUTO_SHIP_AFTER_REVIEW` | `.env` 확인 | `false` (수동 승인 필수) |
| task_queue 감시 | 테스트 파일 투입 후 Telegram 알림 | 📋 대기열 등록 알림 |
| git branch clean | `git status` | 진행 중 branch 없음 |
| Telegram callback | 실패 카드 버튼 클릭 | 응답 수신 |
| `/stale` 방치 없음 | Telegram `/stale` | 방치 태스크 0개 |

### 고위험 설정 주의

```
⚠️  AUTO_SHIP_AFTER_REVIEW=true  → 리뷰 PASS 즉시 main 자동 머지
    이 설정 시 모든 PASS 결과가 승인 없이 배포됨

✅  AUTO_SHIP_AFTER_REVIEW=false  → 반드시 /ship 명령으로만 배포
    RC 운영 표준 설정
```

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
  task_inbox/       로컬 스펙 작성 디렉토리 (작성·수정 가능, 승인 전 단계)
  task_queue/       실행 전용 디렉토리 (승인 후 이동, 직접 수정 금지)
  completed/        완료된 태스크 JSON
  logs/tasks/       태스크별 로그
  handoffs/         Handoff 파일
  archive/          완료/실패 태스크 압축 archive
    {task_id}/
      meta.json     태스크 메타 + 상태
      handoff.md    Handoff 사본
      *.log.gz      로그 gzip 압축본
      *.diff        git diff 파일
  stats.json        누적 작업 통계 (최근 500건)
  tests/            자동화 테스트
```

---

## task_inbox / task_queue 운영 규칙

```
task_inbox/ = 작성·수정·검토 가능    task_queue/ = 실행 전용
```

### task_inbox 규칙

| 규칙 | 설명 |
|------|------|
| **자유롭게 작성·편집 가능** | 승인 전까지 언제든 수정 OK |
| **frontmatter 선택 사항** | 없어도 자동 파싱 (heading → 제목, 파일명 → fallback) |
| **승인 시 자동 이동** | `[▶ 진행해]` 또는 `/run T-ID` → task_queue/ 로 이동 |
| **빈 파일 / 100자 미만** | 승인 거부됨 (너무 짧은 스펙 방지) |
| **`.tmp` / `.bak` / 숨김 파일** | 자동 skip (편집기 임시 파일 오염 방지) |

### task_queue 규칙

| 규칙 | 설명 |
|------|------|
| **사람이 직접 수정/rename 금지** | 파일 상태는 Bot이 관리. 수동 편집 시 Bot 상태와 불일치 발생 |
| **이미 queue에 들어간 작업 수정** | `/hold T-ID` 후 `/archive` → 새 등록 권장 |
| **`.done.md` / `.failed.md` / `.cancelled.md`** | 기록 파일. 실행 대상 아님. 봇이 자동 skip |
| **빈 파일 / 100자 미만 파일** | 자동 skip (preflight 실패) |

> **재실행 방지 동작:**
> 봇이 재시작되거나 `/reload`를 실행하면, `.done/.failed/.cancelled` 파일은
> `processed_ids`에 자동 등록되어 절대 재실행되지 않습니다.

---

## RC1 이후 운영 루틴 (첫 1주일)

새 기능 추가 없이 실제 외부 환경에서 3~5개 태스크를 돌리면서 UX 흐름을 검증합니다.

```
1. tmux에서 PM Bot 실행
2. /doctor 확인
3. /queue 확인
4. 작은 작업 1개 실제 투입
5. RUNNING → REVIEWING → READY_TO_SHIP 흐름 확인
6. /diff 확인
7. /ship 수동 승인
8. /history, /stats 확인
```

---

## Post-RC1 Backlog (우선순위 순)

### 인프라 / 경로 정리

| 순위 | 항목 | 비고 |
|------|------|------|
| 1 | `git_state.json` 경로 통일 | `./state/` vs `pm_agent_system/state/` 혼용 → 운영 혼란 제거 |
| 2 | archive 경로 멀티프로젝트 재설계 | 프로젝트별 격리 |
| 3 | untracked 파일 처리 정책 | `git add .` 대신 **감지 → Telegram 승인** 방식 권장 (의도치 않은 파일 포함 방지) |
| 4 | pytest/hydra conda 환경 충돌 해소 | 현재 `python` 직접 실행으로 우회 중 |

### 미구현 커맨드

| 커맨드 | 우선순위 | 설명 | 비고 |
|--------|---------|------|------|
| `/retry T-ID` | 높음 | 실패 태스크 재시도 (원인 반영) | `/resume`과 의미 구분: resume=handoff 기반 이어받기, retry=동일 조건 재실행 |
| `/cancel T-ID` | 보통 | RUNNING 태스크 graceful stop → FAILED/CANCELLED | 2단계: cancel_requested 표시 → subprocess 종료. 급할 땐 `/hold` 우회 가능 |
| `/debug` | 보류 | 내부 상태 덤프 | `DEBUG_MODE=true` 환경일 때만 활성화 고려. 일반 운영에서는 `/doctor`+`/log`로 충분 |
