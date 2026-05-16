# ICS 시연 outline (1페이지)

> 내일 ICS 팀에게 시연 시 따라갈 흐름 + 강조 포인트 + Q&A 대응.
> 실 시연 5~10분, Q&A 별도. 무거운 deck 아니라 head-up reference.

---

## 한 줄 setup

> "지금까지 토폴로지 비교 시뮬레이터 만들었는데, 폐쇄망 실 평면도 그대로 가져와서 운영 의사결정 도구로 진화시켰습니다. **OpenRMF / VDA5050 호환 sandbox** — ICS 가 익숙한 데이터 형식 그대로."

---

## 🎬 시연 흐름 (5~10분)

### 1. 정체성 (30초)
- 한 줄: "실 평면도 기반 FAB AMR what-if + RMF 호환 sandbox"
- **Primary**: 운영/배치 의사결정자
- **Secondary**: 알고리즘 연구자
- Open-RMF 클론 X — 호환 + 가벼운 도구로 차별화

### 2. 외부 맵 import (1~2분)
- `./run quickrun` → http://127.0.0.1:8765/
- 📂 외부 맵 업로드 → 미리 준비한 합성 plant (또는 실 데이터 small subset)
- **포인트**: 자동 추론 — 양방향 페어 100% 검출, 코리도 자동 클러스터링, dead-end 자동 발견
- **강조**: "코드 한 줄 안 짜고 import 즉시 시각"

### 3. Editor 정책 부여 (2분)
- 🛠 Editor 진입
- **Paint 모드**: 좌클릭 = 단방향, 우클릭 = 양방향
- **Stamp 도구**: 숫자키 2 → Station, 3 → Charger 빠른 마킹
- **Build 모드**: 노드/엣지 추가 (실 데이터 결함 보정용)
- Undo / Redo (Cmd+Z)
- 💾 Save → `*.edit.json` (원본 안 건드림, override 만)
- **강조**: "ICS 가 직관적으로 정책 부여, 5분이면 충분"

### 4. Quickrun 라이브 시뮬 (2분)
- 토폴로지 = 업로드한 맵 선택
- 잡 주기 슬라이더 + ▶ 실행
- **실시간 KPI 카드**: 처리량 / 가동률 / 정면충돌 / 평균대기
- **시각 토글**:
  - 🔥 히트맵: 사고 누적 패턴 (어디서 자주 충돌?)
  - ⚠ 충돌 마커: 실시간 동시 점유 감지
- 이벤트 마커 클릭 → 사고 시점 점프
- **강조**: "실 운영 의사결정 — 어디가 병목인지 한 눈에"

### 5. Case 비교 배치 (2분)
- `python scripts/run_imported_cases.py experiments/plant_what_if.yaml`
- YAML 한 파일에 case 4~5개 정의 (원본 + edit_v1 + edit_v2 + edit_v3)
- `outputs/imported_cases/<ts>/report.html` 열기
- **순위 표**: 완료율 / 처리량 / 평균대기 / head-on / retry / deadlock
- **강조**: "여러 정책 변형 자동 비교 → 데이터 기반 의사결정"

### 6. (선택, 시간 남으면) 다음 단계 — F1a 이기종 AGV (1분)
- spec 파일 보여주기 (`specs/F1a-multi-fleet/`)
- "이기종 AGV (TYPE_1, TYPE_2, ...) 별 lane 분리 + capability 매칭"
- "OpenRMF traffic-editor 의 graph_idx 패턴 그대로"
- 다음 사이클 ~6일 견적, ICS 요구 시나리오 #9 박혀있음
- **강조**: "spec-driven 개발 — 변경사항은 spec 만 갱신, agent 가 코드"

---

## 🎯 핵심 강조 포인트 (반복 메시지)

1. **OpenRMF 호환** — graph_idx, building_map YAML, capability 매칭. ICS 가 익숙한 모델.
2. **실 데이터 즉시 사용** — 코드 변경 없이 JSON/YAML import. 폐쇄망 친화 (모든 의존성 wheel).
3. **운영 의사결정** — what-if 변형 case 비교로 정량적 비교. 추측 X.
4. **spec-driven** — 변경 추적/관리 체계. ICS 요구가 spec 으로 박혀서 다음 사이클 즉시 진입.

---

## ❓ Q&A 예상 + 대응

| 질문 | 답변 한 줄 |
|---|---|
| "실 데이터 형식이 우리 거랑 다른데?" | "어댑터 패턴. node/link 두 필드만 매핑하면 import 가능. YAML 도 곧 지원" |
| "충돌 처리는 정확한가요?" | "4계층 예약 (노드/엣지/Itinerary/Critical section). VDA5050 컨벤션 + 진성/가짜 head-on 구분" |
| "OpenRMF 랑 뭐가 달라요?" | "OpenRMF = production fleet adapter. 우리 = 가벼운 what-if sandbox. 호환만 — 클론 아님" |
| "VDA5050 메시지 포맷 호환?" | "현재 개념 차용 수준. 메시지 포맷 호환은 별도 사이클 (백로그)" |
| "이기종 로봇 지원?" | "spec 박혀있음 (F1a). graph_idx + capability 매칭. 다음 사이클 ~6일" |
| "확장 가능?" | "spec-driven. 새 요구 → spec 갱신 → agent 코드. operations-scenarios.md 가 사용자 의도 박제" |
| "다층 (multi-level)?" | "현재 첫 레벨만. 다층은 별도 spec (백로그)" |
| "battery / 충전 정책은?" | "현재 단순화. 24h 시나리오 spec 백로그 — battery 모델 그때 정밀화" |
| "실시간 모니터링 도구?" | "Quickrun 이 그 역할 (라이브 KPI + 히트맵). 단 production 아닌 시뮬용" |
| "수동 job 부여 (엔지니어 개입)?" | "GAP-B spec 박혀있음 — 곧 추가. JobDispatcher 이미 통합됨" |
| "edge 막아서 reroute 확인하고 싶다" | "GAP-A spec — Quickrun 라이브 차단 UI 곧 추가" |

---

## 🎁 시연 후 행동 (ICS 피드백 수집)

체크리스트로 즉시 물어볼 것:

```
□ Q1: 폐쇄망 JSON 의 양방향 표현 — (a) 두 링크 vs (b) link_type_cd 인코딩?
□ Q3: capability 미매칭 demand 정책 — 무한 대기 / timeout / fail?
□ ICS 의 capability 종류는 몇 개? 명명 규칙은?
□ Multi-stage handover 가 실제로 필요한 시나리오 있나?
□ VDA5050 메시지 호환 어느 수준이 필요한가? (orderId / nodeState / actionState)
```

---

## 📋 시연 직전 체크리스트

```
□ Quickrun 서버 미리 띄워두기 (run.bat 또는 ./run quickrun)
□ 합성 plant 또는 실 데이터 준비
□ 미리 만들어둔 edit.json (charger/station 마킹된)
□ experiments/plant_what_if.yaml 준비
□ Pre-run 으로 outputs/imported_cases/<ts>/report.html 한 번 생성해둠
□ 브라우저 탭 미리 열기 (Quickrun + report.html + spec README)
□ 화면 share 도구 (시연 환경) 확인
□ 인터넷 없는 환경이면 self-contained HTML 확인 (Quickrun 페이지 폐쇄망 친화)
```

---

## ⏱ 시간 분배 (10분 시연 기준)

```
0:00 ─ 정체성 설명          (30s)
0:30 ─ 외부 맵 import       (1:30)
2:00 ─ Editor 정책 부여     (2:00)
4:00 ─ Quickrun 라이브      (2:00)
6:00 ─ Case 비교 ranking   (2:00)
8:00 ─ (선택) F1a 미래     (1:00)
9:00 ─ Q&A 시작
```

타이트하면 6번 생략, Q&A 만.

---

## 🔚 닫는 한 줄

> "운영 의사결정 도구로 시작했지만, 이미 OpenRMF 호환 데이터 모델 + spec-driven 체계가 잡혀 있어서 **ICS 가 다음에 원하는 게 뭐든 spec 한 페이지 갱신 + agent 의뢰로 들어갈 수 있습니다.** 오늘 피드백 듣고 다음 사이클 들어갑니다."
