# F1a — Multi-graph / 이기종 fleet 지원

> 다음 사이클 (ABCD 처리 후) 1순위. 견적 ~4.5일.
> 2026-05-16 합의. Open-RMF traffic-editor 의 `graph_idx` 패턴 차용.

---

## 한 줄 요약

같은 평면도에서 **여러 fleet 의 lane graph 를 분리** 운영. 이기종 로봇 (예: 소형 OHT + 대형 AGV + 보조 로봇) 이 각자의 graph 를 통해 다니되, **vertex 는 공유** (charger / station / 환승점 자연스럽게 표현).

---

## 사용자 의도 매핑

> [`../operations-scenarios.md`](../operations-scenarios.md) 참조.

- **의도 #8 (장기)** — OpenRMF / VDA5050 모사. graph_idx 패턴 호환은 OpenRMF 의 핵심 데이터 구조.
- (앞서 대화) 3종 로봇 요구사항 — OHT + AGV + 보조

---

## Layer 분리 — Agent / Claude Design 의뢰 단위

| Layer | Spec 파일 | 담당 | 견적 |
|---|---|---|---|
| Engine (데이터 모델, 라우터, 예약) | [`engine.md`](engine.md) | Engine Agent | ~1.5일 |
| Map Editor UI (Active graph 토글) | [`ui-editor.md`](ui-editor.md) | Claude Design | ~1일 |
| Quickrun UI (fleet 색, KPI 카드) | [`ui-quickrun.md`](ui-quickrun.md) | Claude Design | ~0.7일 |
| Integration (YAML, Case 비교, 시나리오 테스트) | [`integration.md`](integration.md) | 사용자 또는 Integration Agent | ~1.3일 |

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
| 2026-05-16 | 단일 파일 spec 을 layer 별 디렉토리로 분리 |
| 2026-05-16 (초안) | 단일 파일 `F1a-multi-fleet.md` 작성 |
