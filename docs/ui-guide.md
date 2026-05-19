# Quickrun Live UI 가이드

운영자가 자주 헷갈리는 토글·색 표시·모드 동작을 한곳에 정리. 실행은
`./run quickrun` 후 `http://127.0.0.1:8765/`.

## 토글 4종

| 버튼 | 의미 | 카운트 대상 |
|---|---|---|
| 🔥 **히트맵** | 엣지별 **사고** 누적 (충돌·차단) | `headon_block`, `section_conflict`, `followon_block` |
| 🚦 **트래픽** | 엣지별 **AGV 통과 횟수** | `edge_enter` |
| ⚠ **충돌** | 실시간 충돌 의심 마커 | 노드 동시 점유 추정 / 엣지 head-on 추정 |
| ⛔ **차단** | 엣지 클릭으로 차단 / 해제 | — |

- 🔥 와 🚦 는 **동시 활성 불가** (서로 토글 시 자동 해제).
- 🔥 는 reroute / deadlock_resolved 같은 *결과적* 사건은 가산 안 함. 진성 차단 이벤트만.

## 엣지 색의 의미

| 색 | 의미 |
|---|---|
| 회색 | 평상 통로 (방향 화살표 표시 있을 수 있음) |
| 옅은 보라 | AGV 가 통과 중 / 예약 중인 엣지 (단순 "사용 중", 차단 아님) |
| 🔴 빨강 | **다른 AGV 때문에 진입 못 하는 AGV 의 차단 엣지** (= `WAITING_RESERVATION` 상태에서 그 AGV 가 들어가려던 엣지) |
| 🔴 두꺼운 빨강 | 사용자가 ⛔ 로 차단한 엣지 |
| 빨강 그라데이션 (히트맵 모드) | 사고 누적량 |
| 파/초 그라데이션 (트래픽 모드) | 통과 횟수 |

**점유 ≠ 차단**:
- 엣지 위에 있는 AGV → 단순 점유. 색 표시 없음 (또는 보라 path overlay).
- AGV 가 옆에서 "들어가고 싶은데 못 들어가는" 상태 → 그 진입 시도 엣지가 빨강.

## ⛔ 차단 동작

1. ⛔ 토글 → 엣지 클릭 → 즉시 차단.
2. 차단된 엣지를 path 에 포함한 AGV 들은 **즉시 `_reroute()`** 트리거.
3. **이미 그 엣지 위를 이동 중인 AGV** 는 끝까지 진행 (motion 모델 한계) → 다음 hop 부터 우회 적용.
4. 우회 경로가 없으면 AGV 는 IDLE 또는 WAITING_RESERVATION 으로 잔존.
5. 다시 클릭하면 해제. 해제 시점에는 자동 reroute 없음 (다음 dispatch 부터 적용).

## 📋 수동 Job 모드

1. 📋 토글 → 노드 두 개 클릭 (pickup → dropoff) → POST `/manual-job`.
2. 모드 진입 시:
   - IDLE AGV 둘레 **초록 dashed pulse ring** (할당 후보)
   - 상단 패널에 IDLE AGV 수 표시. 0대면 warn toast.
3. dispatch 성공 시:
   - 할당된 AGV 가 **우측 상세 패널의 focus** 로 자동 설정
   - 그 AGV 둘레 **노란 펄스 ring 8초**
   - "📍 방금 할당: AGV_xxx" 패널 상단에 8초 유지 (클릭하면 포커스 토글)
   - 성공 toast 4.5초

## 🚨 데드락 표시

위치 기반 wait-for 사이클이 감지되면:
- 상단 KPI strip 의 `데드락 N` chip 빨강 강조
- 사이클 멤버 AGV 둘레 빨강 펄스 ring 4.5초
- 우측 이벤트 패널에 "데드락 해소" 항목 추가
- backup / reroute 모두 실패 시 **빨강 alert 배너** ("operator 개입 필요") 표시

자세한 detector 알고리즘은 `src/application/deadlock_detector.py` 주석 참조.

## AGV 상태 색 (마커)

| 상태 | 라벨 | 색 |
|---|---|---|
| NAVIGATING | 주행 | fleet 색 (단일 fleet 시 디폴트) |
| WAITING_RESERVATION | 대기 | 주황 (`#f39c12`) |
| PROCESSING | 작업 | 초록 (`#0f9d58`) |
| CHARGING | 충전 | 파랑 (`#1f6feb`) |
| IDLE | IDLE | 회색 |

다중 fleet 케이스는 fleet 마다 다른 색을 부여하고 우측 legend 에 색 매핑이 표시됨.
