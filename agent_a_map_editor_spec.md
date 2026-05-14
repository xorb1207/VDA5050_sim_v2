# Agent A — Map Editor Track (F1b → F1e)

> Claude Code Agent View용 태스크 큐.  
> **작업 순서 엄수.** 각 태스크 완료 후 테스트 통과 확인 → 다음으로 이동.  
> 완료 보고 시 3줄 요약 + test log 첨부.

---

## 🎯 상태 (2026-05-15 완료)

| TASK | 상태 | 핵심 변경 |
|---|---|---|
| F1b-core | ✅ | `Edge.v_max` 옵션 필드 + AGV 속도 결정 우선순위(creep > v_max > intrinsic) + itinerary 반영 |
| F1b-ux   | ✅ | Map editor **Speed 모드** (별도 모드 토글), 인스펙터 +/-/input + 클릭 pin + 모든 edge v_max 라벨 |
| F1c      | ✅ | 노드 hit-test world-coord 기반 + 화면 px 6~28 clamp + edge invisible 9px hit line |
| F1d      | ✅ | 드래그 중 방향 화살촉 미리보기 + 완성 edge 좌클릭 단↔양방향 토글 |
| F1e      | ✅ | Grid snap (옵션) + Shift+클릭 축 고정 (직전 노드 X/Y) + 다중 stamp 위치 추적 |

**변경 파일:**
- `src/domain/map/graph.py` — `Edge.v_max`, YAML/JSON loader
- `src/domain/agv/agv.py` — `_max_speed_mps`, `_get_effective_speed()` 확장, itinerary v_max 반영
- `src/domain/map/external_importer.py` — `ImportedEdge.v_max` 추가, apply_edits/build_map_graph passthrough
- `src/interfaces/map_editor/editor_html.py` — Speed 모드, edge UI, hit-test, 드래그 화살촉, grid snap
- `tests/integration/test_simulation.py` — T61-1~6 추가 (모두 PASS)

**테스트:** 63 passed / 0 failed (T1~T60 baseline + T61 신규 6건).

**비고 (사용성 변경):**
- 초기 spec: "Normal scroll / Shift+scroll 로 v_max 조절" → 실 사용성에서 wheel inertia/Shift release 충돌로 폐기.
- 변경: **Speed 모드 별도 추가** (단축키 `V`). 모드 안에서만 scroll = v_max 편집, 그 외는 scroll = zoom.
- 인스펙터에 +/− 버튼 + number input 추가 — scroll 동작 불안정한 디바이스(일부 마우스)에서도 정확 조작 가능.
- Edge 클릭으로 인스펙터 pin → 마우스 떠도 +/-/input 사용 가능.

---

## 작업 범위 (Boundary)

- **건드려도 되는 구역:** `frontend/map-editor/`, `fab_nav_graph.yaml`, edge 관련 도메인 모델
- **절대 건드리지 말 것:** `domain/`, `application/`, `analytics/`, `tests/integration/T*.py` (기존 T1~T59)
- **공유 스키마 변경 시:** 반드시 보고 후 대기 (Agent C가 참조함)

---

## TASK 1 — F1b-core: Per-edge speed limit (data + engine)

### Goal
Edge마다 개별 속도 제한 부여. 충돌 위험 구역(블라인드 스팟 등) 속도 제어 가능.

### Scope IN
- `fab_nav_graph.yaml` edge 정의에 `v_max` 필드 추가 (optional, float, m/s)
- `AGV.step()`에서 현재 주행 중인 edge의 `v_max` 참조해 속도 결정
- 결합 규칙: **edge 우선** — `speed = edge.v_max if edge.v_max is not None else agv.max_speed`
- 전환 방식: **즉시(instant)** — edge 진입 시점에 바로 새 속도 적용

### Scope OUT
- Map editor UI (→ TASK 2)
- 가속/감속(smooth deceleration) 모델
- 런타임 동적 변경
- edge.v_max > agv.max_speed 안전 클램프 (config-time discipline으로 운영)

### Pre-step (구현 전 반드시 먼저 실행)
```
1. src/ 에서 edge 데이터 정의 및 로더 위치 찾기
2. AGV.step() 위치 + 현재 속도 결정 로직 파악
3. 기존 agv.max_speed 변수 위치 확인
→ 발견 결과 3줄로 보고 후 구현 진행
```

### YAML Schema
```yaml
edges:
  - from: node_id
    to: node_id
    v_max: 1.0   # optional, m/s. 없으면 agv.max_speed fallback
```

### Tests (tests/integration/ 에 추가)
```python
test_edge_v_max_loaded_from_yaml()
test_agv_respects_edge_v_max()            # edge v_max < agv.max → edge 값 사용
test_edge_without_v_max_uses_agv_max()    # v_max 없음 → agv.max_speed fallback
test_speed_change_at_edge_transition_is_instant()
# T1~T59 PASS 유지 필수
```

### Acceptance
- 위 테스트 전부 PASS
- T1~T59 PASS
- v_max 미설정 baseline 시뮬 결과 변화 없음

### Completion Report Format
```
[F1b-core 완료]
- 변경 파일: (목록)
- 추가 테스트: (목록)
- T1~T59: PASS / FAIL
- 특이사항: (있으면)
```

---

## TASK 2 — F1b-ux: Map editor에서 edge v_max 편집 UI

> TASK 1 완료 확인 후 시작.

### Goal
맵 에디터에서 edge를 선택해 v_max 값을 설정/수정할 수 있는 UX.

### UX 상세 스펙

**Edge 활성화 방식**
- 거리 기준 nearest edge 감지 (클릭/호버 시)
- 활성화된 edge: 색 변경 + 두께 증가로 피드백

**속도 값 입력**
- Normal scroll: 0.7 ~ 1.5 m/s 범위, 0.1 단위 조절
- `Shift+scroll`: 0.7 미만 unlock — 0.1 ~ 0.6 m/s 범위
- 속도 값 표시: **hover 시에만** (항시 표시 X — edge 과밀 방지)

**시각 피드백**
- v_max 설정된 edge: 색 구분 (미설정 edge와 다른 색)
- 활성화(선택) 상태: 두께 증가 + 색 강조
- hover 시: 속도 값 툴팁 표시

**저장**
- 기존 맵 저장 플로우와 동일하게 `fab_nav_graph.yaml`에 반영

### Scope OUT
- 노드 속도 설정 (edge만)
- 런타임 실시간 반영 UI

### Tests
```
- edge 클릭 → v_max 패널 열림
- scroll로 값 변경 → 저장 후 YAML 반영 확인
- Shift+scroll → 0.7 미만 값 설정 가능
- hover 시만 값 표시 확인
```

### Completion Report Format
```
[F1b-ux 완료]
- 변경 파일: (목록)
- UX 동작 확인 항목: (체크리스트)
- 특이사항: (있으면)
```

---

## TASK 3 — F1c: 저줌 미세 컨트롤 개선

> TASK 2 완료 후 시작.

### Goal
맵을 많이 축소했을 때 edge 방향 그리기 / 노드 배치가 부정확해지는 문제 해결.

### 문제 현상
- 줌 아웃 시 마우스 커서가 실제 클릭 위치보다 덜 정밀하게 스냅됨
- Edge 그리기 시 시작/끝 노드가 의도치 않은 위치에 연결됨

### 구현 방향
- 줌 레벨에 따른 스냅 threshold 동적 조정
  - 고줌: 넓은 스냅 반경 (편하게 선택)
  - 저줌: 좁은 스냅 반경 (정밀 제어 보장)
- 줌 레벨별 hit-test 반경을 픽셀 고정값이 아닌 **world 좌표 고정값**으로 변환

### Discovery 먼저
```
1. 현재 스냅/hit-test 로직 위치 파악
2. 줌 변환 행렬 또는 scale factor 관리 위치 파악
→ 발견 결과 보고 후 구현
```

### Acceptance
- 줌 0.3x 이하에서 edge 그리기 시 올바른 노드에 연결됨
- 기존 고줌 UX 변화 없음

---

## TASK 4 — F1d: Edge 방향 그리기 UX 개선

> TASK 3 완료 후 시작.

### Goal
Edge 방향(화살표) 설정이 직관적이지 않은 문제 개선.

### 현재 문제
- 방향 전환 시 여러 단계 필요
- 그리기 중 방향 미리보기 없음

### 구현 방향
- Edge 그리기 드래그 중 **방향 화살표 미리보기** 표시
- 완성된 edge 클릭 시 방향 토글 가능 (단방향 ↔ 양방향)
- 방향 화살표를 줌 레벨에 맞게 크기 조정

### Discovery 먼저
```
1. 현재 edge 그리기 플로우 (mousedown → drag → mouseup) 위치 파악
2. 기존 화살표 렌더링 로직 위치 파악
```

---

## TASK 5 — F1e: Stamp 배치 UX 개선

> TASK 4 완료 후 시작.

### Goal
노드 stamp 배치 시 정렬/간격 조정이 불편한 문제 개선.

### 구현 방향
- `Shift+drag` 로 수평/수직 축 고정 배치 (snap to axis)
- 그리드 스냅 옵션 (설정에서 on/off, grid size 조정)
- 다중 stamp 배치 시 이전 stamp 위치를 참조해 간격 유지 옵션

### Discovery 먼저
```
1. 현재 stamp 배치 이벤트 핸들러 위치 파악
2. 기존 그리드/스냅 관련 코드 있는지 확인
```

---

## 최종 검증 (전체 TASK 완료 후)

```bash
pytest tests/ -v > /tmp/agent_a_final.log 2>&1
echo "Exit: $?" >> /tmp/agent_a_final.log
```

보고 형식:
```
[Agent A 전체 완료]
- 완료 태스크: F1b-core / F1b-ux / F1c / F1d / F1e
- 최종 테스트: PASS / FAIL
- T1~T59: PASS / FAIL
- 미완/defer 항목: (있으면)
```

---

## 실행 명령어

```bash
claude --bg "$(cat agent_a_map_editor_spec.md)"
```

또는 Agent View에서:
```
/bg agent_a_map_editor_spec.md 내용 기반으로 Map Editor 트랙 순서대로 작업해줘
```
