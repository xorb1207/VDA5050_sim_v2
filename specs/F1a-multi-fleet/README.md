# F1a — Multi-graph / 이기종 AGV 지원

> 다음 사이클 (ABCD 처리 후) 1순위. 견적 **~5.5~6일**.
> 2026-05-16 합의. Open-RMF traffic-editor 의 `graph_idx` + capability 매칭 차용.

---

## 한 줄 요약

같은 평면도에서 **이기종 AGV (`TYPE_1`, `TYPE_2`, ...) 가 각자의 lane graph 분리** 운영. **vertex 는 공유** (충전소/스테이션 자연스럽게). **Demand 의 `required_capability` 와 Fleet 의 `capabilities` 매칭**으로 dispatch (single-stage, no handover).

---

## 사용자 의도 매핑

> [`../operations-scenarios.md`](../operations-scenarios.md) 참조.

- **의도 #9 (★ 핵심)** — ICS 이기종 AGV: graph isolation + capability 매칭 + single-stage
- **의도 #8 (장기)** — OpenRMF / VDA5050 모사. graph_idx + capability 가 OpenRMF 핵심
- 폐쇄망 ICS 가 익숙한 모델 (capability 기반)

---

## Layer 분리 — Agent / Claude Design 의뢰 단위

| Layer | Spec 파일 | 담당 | 견적 |
|---|---|---|---|
| Engine (데이터 모델 + 라우터 + 예약 + **capability dispatch**) | [`engine.md`](engine.md) | Engine Agent | ~2~2.5일 |
| Map Editor UI (Active graph 토글 + Fleet Info 패널 + Capability stamp) | [`ui-editor.md`](ui-editor.md) | Claude Design | ~1일 |
| Quickrun UI (fleet 색, fleet 별 KPI) | [`ui-quickrun.md`](ui-quickrun.md) | Claude Design | ~0.7일 |
| Integration (YAML + demand required_capability + Case 비교 fleet 분해 + 시나리오 테스트) | [`integration.md`](integration.md) | 사용자 또는 Integration Agent | ~1.5일 |

**총 ~5.2~5.7일** (안전마진 포함 ~6일).

---

## 합산 검증 (모든 layer 완료 후)

```bash
python tests/integration/test_simulation.py
# → 기존 + F1a 신규 PASS

python scripts/import_map_demo.py maps/synthetic_3fleet.json --edit --open
# → Active graph 토글 작동

./run quickrun
# → 3 fleet 시나리오 시뮬, fleet 별 KPI 카드
```

---

## DO NOT (전체 layer 공통)

- 기존 토폴로지 generator (Type A~E) 의 동작 변경 — 모두 graph 0 으로 기본 동작
- fleet 별 motion 모델 분리 (별도 PR)
- battery 정책 fleet 분리 (별도 PR)
- 다층 / fiducial 도입 (별도 PR)
- OpenRMF rmf_fleet_adapter 통합 (별도 PR — 우리는 sandbox)

---

## 진행 룰

1. **Engine layer 먼저** (다른 layer 의 기반)
2. UI 두 layer 는 engine 완료 후 병렬 가능
3. Integration 은 마지막
4. 각 layer 별로 [`../SPEC-AUTHORING.md`](../SPEC-AUTHORING.md) 의 Pre-step → Discovery → 구현 → Final Verification

---

## 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-05-16 | ★ ICS 시나리오 #9 반영 — capability 매칭 dispatch + single-stage 명시. 견적 4.5 → 5.5~6일 |
| 2026-05-16 | 단일 파일 spec 을 layer 별 디렉토리로 분리 |
| 2026-05-16 (초안) | 단일 파일 `F1a-multi-fleet.md` 작성 |
