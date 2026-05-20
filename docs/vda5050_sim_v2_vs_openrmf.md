# vda5050_sim_v2 vs open-rmf 비교 분석

> 자체 개발 시뮬레이터의 기술적 완성도와 오픈소스 표준과의 정합성을 객관적으로 평가하고, 발표·설명 시 근거 자료로 활용하기 위해 작성됨. (2025년 5월 기준)

---

## 1. open-rmf 구성 개요

| 컴포넌트 | 역할 |
|---|---|
| **rmf_traffic** | C++ 기반 교통 협상 엔진. 경로 예약 + 충돌 회피 |
| **rmf_traffic_editor** | building.yaml 맵 에디터. nav graph (vertices/lanes/graph_idx) |
| **rmf_task** | Task 할당 — bid 기반 경쟁 입찰 방식 |
| **free_fleet** | ROS2 없이 경량 fleet 통신 |
| **rmf_ros2** | ROS2 통합 레이어 |
| **VDA5050 어댑터** | ROSCon 2024에서 "First Target" 발표. **아직 미완성 상태** |

---

## 2. 기능 비교 매트릭스 (최신 구현 반영)

| 기능 영역 | open-rmf | vda5050_sim_v2 | 유사도 | 비고 |
|---|---|---|---|---|
| **맵 포맷** | building.yaml (vertices/lanes/graph_idx) | building.yaml import/export 완전 지원 ✅ | ★★★★★ | 포맷 완전 호환 |
| **Multi-fleet lane 분리** | graph_idx per fleet | graph_idx 기반 lane 분리 동일 구조 | ★★★★★ | 구조적 정합성 최고 |
| **Task/Job dispatch** | rmf_task (bid 기반 경쟁) | capability 매칭 + TaskGenerator (F1a) ✅ | ★★★★☆ | capability 필터 방식으로 구현 |
| **VDA5050 프로토콜** | 어댑터 미완성 | 네이티브 구현 완성 ✅ | ★★★★★ | **최강 차별점** |
| **Traffic 분석** | 기본 충돌 회피 | 히트맵 시각화 + 포화도 분석 ✅ | ★★★★☆ | FAB 특화 분석 추가 |
| **시뮬레이션 엔진** | Gazebo 연동 (무거움, ROS2 필수) | 경량 Python + FastAPI, 브라우저 시각화 | 차별화 | 배포 속도 압도적 우위 |
| **AGV 상태 시각화** | 없음 | 상태/Job/Fleet별 동적 색상 ✅ | 차별화 | 신규 구현 완료 |
| **맵 에디터** | rmf_traffic_editor (Qt 앱) | Standalone HTML 에디터 | ★★★☆☆ | 브라우저 기반, 설치 불필요 |
| **Speed 일괄 편집** | 없음 | 대량 엣지 선택 → v_max 일괄 적용 ✅ | 차별화 | 신규 구현 완료 |
| **YAML Export** | 에디터 내장 | API Export (`GET /export-map`) ✅ | ★★★★★ | 동일 포맷 출력 |
| **Deadlock 감지** | rmf_traffic 예약으로 예방 | Wait-for Graph 기반 감지 + 해소 🔄 | ★★★★☆ | 구현 진행 중 |
| **Lift/Door 연동** | rmf_door, rmf_lift 서버 별도 | 미구현 (단층 환경 대상) | ★☆☆☆☆ | 향후 계획 |
| **Traffic negotiation** | 예약 기반 경로 조율 (강력) | 미구현 | ★★☆☆☆ | 향후 과제 |
| **FAB 특화 분석** | 없음 | 포화곡선, 히트맵, capability dispatch | 차별화 | 핵심 강점 |

---

## 3. vda5050_sim_v2 강점 (차별화 포인트)

### ① VDA5050 네이티브 구현 — 가장 강한 무기
open-rmf가 ROSCon 2024에서 VDA5050을 "First Target"으로 발표했음에도 어댑터가 미완성인 상황. vda5050_sim_v2는 이미 VDA5050 기반으로 완전히 동작한다. 표준 프로토콜 기반 시뮬레이터로는 현재 가장 완성도 높은 구현 수준.

### ② FAB/반도체 환경 특화
- 트래픽 히트맵: 구간별 AGV 통과 빈도 시각화
- 포화 곡선: AGV 대수 증가에 따른 처리량 한계 분석
- open-rmf에는 이런 분석 도구 없음

### ③ 경량 스택 — 빠른 도입
Python + FastAPI, ROS2 불필요. 브라우저만 있으면 시뮬레이션 확인 가능. 폐쇄망 환경 배포 용이.

### ④ 맵 포맷 완전 호환
open-rmf building.yaml import/export 지원. 동일한 graph_idx 구조로 향후 open-rmf 연동 기반 마련.

### ⑤ 실시간 시각화 + Playback
Live 모드(WebSocket 실시간 렌더링) + Playback 모드(과거 시뮬레이션 재생 및 분석). AGV 상태별 동적 색상(Error/Charging/Job/Moving/Idle) 지원.

---

## 4. 부족한 부분 및 개발 방향

| 영역 | 현황 | 우선순위 | 설명 |
|---|---|---|---|
| **Deadlock 감지/해소** | 구현 진행 중 🔄 | 높음 | Wait-for Graph 사이클 탐지 → 최저우선순위 AGV 후퇴 |
| **Traffic negotiation** | 미구현 | 중간 | 경로 예약 기반 충돌 회피 (rmf_traffic 수준) |
| **Fleet Adapter (open-rmf 호환)** | 미구현 | 중간 | open-rmf Fleet Adapter 인터페이스 옵션 노출 |
| **테스트 커버리지** | 일부 백필 필요 | 중간 | GAP A/B/C/D + F1a 통합 테스트 |
| **Lift/Door 연동** | 단층 환경 대상 | 낮음 | 다층 FAB 도입 시 rmf_lift/rmf_door 참고 |
| **다층 맵 지원** | 미구현 | 낮음 | 향후 계획 |

---

## 5. 포지셔닝 전략

**"open-rmf를 대체"가 아닌 "보완재 + FAB 특화 검증 플랫폼"**

- **프리프로덕션 검증**: open-rmf 도입 전 맵/fleet/트래픽 사전 검증 도구
- **FAB 특화 분석**: 포화도, 히트맵, capability dispatch 시뮬레이션
- **VDA5050 브릿지**: open-rmf VDA5050 어댑터 미완성 구간을 커버

open-rmf 사용자 입장에서도 "building.yaml 호환 경량 시뮬레이터"로 즉시 활용 가능하며, 향후 Fleet Adapter 인터페이스 추가 시 open-rmf 생태계와의 연동도 가능해진다.

---

## 6. 발표 핵심 메시지 (3줄 요약)

1. **표준 준수**: open-rmf와 동일한 building.yaml 맵 포맷, graph_idx 기반 multi-fleet 구조 채택. 포맷 수준의 완전 호환성 확보.

2. **VDA5050 선도**: open-rmf가 아직 완성하지 못한 VDA5050 네이티브 통신을 이미 구현. 표준 프로토콜 기반 fleet 시뮬레이터로서의 선행 구현 완료.

3. **FAB 특화**: 반도체 공장 환경에 맞는 트래픽 분석(히트맵, 포화곡선, capability dispatch)을 추가하여 실무 적용 가능한 수준으로 발전. 이기종 fleet 관리 및 데드락 감지까지 포함.

---

*References: [open-rmf GitHub](https://github.com/open-rmf) · [ROSCon 2024 - The State of Open-RMF](https://roscon.ros.org/2024/) · [VDA5050 Standard](https://github.com/VDA5050/VDA5050)*
