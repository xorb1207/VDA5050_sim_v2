# CLAUDE.md — vda5050_sim_v2 FAB AMR Simulator

반도체 FAB AMR 플릿 시뮬레이터. 토폴로지별 KPI 비교가 주 목적.
스택: Python 3.12, asyncio, VDA5050, Open-RMF 개념 참조.

> **Companions**: 디렉토리/맵/토폴로지/KPI 표는 [`ARCHITECTURE.md`](ARCHITECTURE.md). 날짜별 실험 결과·완료 작업 이력은 [`HISTORY.md`](HISTORY.md).

---

## 핵심 아키텍처 결정 (수정 시 주의)

### 공유 그래프 race condition
`_reroute()`에서 그래프를 임시 수정하면 다른 AGV의 A*와 race condition 발생.
**절대 `_out_edges`를 임시 제거하지 말 것** — `get_path(blocked_edges=set())` 사용.

```python
# ❌ race condition
graph._out_edges[src].remove(eid); path = graph.get_path(...); graph._out_edges[src] = orig
# ✅ 안전
path = graph.get_path(src, dst, blocked_edges={(src, dst)})
```

### 예약 4계층
- **노드 점유**: 정차/대기 충돌
- **엣지 예약**: 이동 중 head-on 차단 (역방향 활성 시 reject) + same-direction follow-on headway
- **Itinerary 예약**: 전체 path 시간윈도우 atomic (실패 시 전부 롤백)
- **Critical section**: bay / siding / station_access / charger_access / shared corridor를 section_key로 묶고 capacity 검사 (대부분 1)

### Conflict Resolution
```
retry 1~3회: 0.1s 고정 / 4~10회: backoff (≤0.3s) / 11+회: force_reroute()
타입별 reroute 임계치: A/C 5, D 8, E 8, B 15 (siding 우선)
```

### DemandSet
- `common_demand`: 모든 토폴로지에 동일 pickup/dropoff seq → 처리력 비교용
- `capability`: 해당 토폴로지에서 routeable한 pair만 → 토폴로지 내부 효율
- `pickup/dropoff_processing_time_s`는 seed 기반 deterministic
- `tasks_completed` = AGV station processing 완료 / `demands_completed` = dropoff processing 종료 (실수요)

### Ranking 정렬
`completion_rate` desc → `task_acceptance_rate` desc → `demand_throughput_per_hour` desc → `total_wait_time_s` asc → `headon_total` asc → `retry_total` asc.

### Type E creep
`graph._lane_mode == "bidirectional_creep"` 태그로 자동 적용.
`AGV._get_effective_speed()`가 head-on 감지 시 0.3m/s 반환.

### head-on 카운터 3종
`_edge_headon_counts`(진성) / `_edge_retry_counts`(폴링) / `_edge_congestion_counts`(합산).
`retry_total`은 진성 실패 아님.

---

## 실행

```bash
# 통합 테스트 (T1~T59)
python tests/integration/test_simulation.py

# 실험 (예: 5타입 포화 곡선)
python -m src.application.usecases.experiment_runner \
  --experiment experiments/topology_saturation_common_demand.yaml

# 시뮬레이션 페이지(playback) 함께 보고 싶으면 showcase + merge:
python -m src.application.usecases.experiment_runner \
  --experiment experiments/topology_showcase.yaml
python scripts/merge_showcase_into_saturation.py <SAT_DIR> <SHOWCASE_DIR>
```

산출물: `outputs/experiments/{run_id}/{summary,ranking,report}.{csv,json,html}` + `{variant}/playback.{html,trace.json}`.

---

## 현재 우선순위 (2026-04-27 합의)

엔진은 현 호라이즌(600s~1800s) + 토폴로지 비교 목적으론 **사실상 완료**. 미체크 항목은 ranking을 흔들 만한 효과가 거의 없거나 운영 시나리오 부재로 보류.

다음 세 갈래로 진행:

- [ ] **(b) 시각화 2차** ← 1순위, 새 실험 없이 도구만 — 3-pane / 사고 클릭→맵 강조 / reserved path depth / blocking chain / 방향 마커. 부분 진행 중.
- [ ] **(a) 혼합 토폴로지 실험** ← 2순위, 엔진 확장 선행 (corridor별 type 분리)
- [ ] **(c) 24h 호라이즌 시나리오** ← 3순위, b 끝난 뒤. battery/charging이 의미를 갖는 구간

### 보류 (현 단계 ROI 낮음)
charging contention / priority reservation / 회전 감속 / wait_time 통합 / reachable siding 정밀화 / 세부 공간 설계 / failure injection — 새 질문이 생기기 전까지 보류.

자세한 완료/추진 이력은 [`HISTORY.md`](HISTORY.md).

---

## 개발 원칙

1. **공유 상태 수정 금지** — 그래프/스케줄러는 모든 AGV가 공유. 임시 수정 대신 파라미터 전달.
2. **타입별 invariant 보장** — `validate_invariants()` 통과 필수.
3. **진성/가짜 head-on 구분** — `_edge_headon_counts` vs `_edge_retry_counts`.
4. **FAB 운영 특성 반영** — 장시간 정지 > secondary bottleneck. wait보다 reroute 우선.
5. **AGV는 베이/메인 통로에서 IDLE/PROCESSING 금지** — IDLE은 HP에서, PROCESSING은 ST에서만.
6. **테스트 출력 간결화** — 요약만, lane별 PASS 출력 금지.
