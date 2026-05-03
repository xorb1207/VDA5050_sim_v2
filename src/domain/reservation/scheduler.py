from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional


@dataclass
class Reservation:
    node_id: str
    agv_id: str
    start_time: float
    end_time: float
    released: bool = False


@dataclass
class EdgeReservation:
    edge_key: str        # "src__dst" (방향 포함)
    agv_id: str
    start_time: float
    end_time: float
    released: bool = False


@dataclass(frozen=True)
class ItinerarySegment:
    segment_type: str  # "node" or "edge"
    key: str
    agv_id: str
    start_time: float
    end_time: float
    src_id: str = ""
    dst_id: str = ""
    same_direction_headway_s: float = 0.0
    section_key: str = ""
    section_capacity: int = 1


class TimeWindowScheduler:
    """
    노드 + 엣지 Time-window 예약 엔진.

    엣지 예약 추가로 head-on 및 same-direction follow-on 충돌 감지 가능:
      - reserve_edge("A__B") 시 역방향 "B__A" 예약 존재 확인
      - 같은 방향 진입 시각 간격이 headway보다 짧으면 추종 안전거리 위반
      - 충돌 시 False 반환 → AGV WAITING_RESERVATION 대기
    """

    def __init__(self) -> None:
        # 노드 예약
        self._reservations: dict[str, list[Reservation]] = defaultdict(list)
        self._congestion_counts: dict[str, int] = defaultdict(int)

        # 엣지 예약
        self._edge_reservations: dict[str, list[EdgeReservation]] = defaultdict(list)
        self._edge_headon_counts: dict[str, int] = defaultdict(int)  # 역방향 차단 (진성 충돌)
        self._edge_followon_counts: dict[str, int] = defaultdict(int)  # 같은 방향 안전거리 차단
        self._edge_retry_counts:  dict[str, int] = defaultdict(int)  # 대기 중 재시도 (병목 강도)
        self._edge_congestion_counts: dict[str, int] = defaultdict(int)  # 합산 (하위호환)
        self._section_reservations: dict[str, list[Reservation]] = defaultdict(list)
        self._section_conflict_counts: dict[str, int] = defaultdict(int)
        self._itinerary_success: int = 0
        self._itinerary_failure: int = 0

        self._lock = asyncio.Lock()

        # KPI 추적
        self._reserve_success: int = 0
        self._reserve_failure: int = 0
        self._node_occupancy_time: dict[str, float] = defaultdict(float)
        self._edge_occupancy_time: dict[str, float] = defaultdict(float)

        # 대기 그래프 (Deadlock 감지용)
        # waiting_for[agv_id] = 점유 중인 agv_id
        self._waiting_for: dict[str, str] = {}
        self._trace_recorder = None

        # AGV가 명시적으로 hold 중인 sections (베이/사이딩처럼 시간 만료가 아닌
        # 물리 위치 기반으로 점유돼야 하는 영역). _has_section_capacity_conflict가
        # 시간 만료 + 명시적 hold 둘 다 본다.
        self._agv_held_sections: dict[str, set[str]] = defaultdict(set)

    # ── 노드 예약 (기존 유지) ──────────────────────────────────

    async def reserve(
        self,
        node_id: str,
        agv_id: str,
        start_time: float,
        end_time: float,
    ) -> bool:
        async with self._lock:
            existing = self._reservations[node_id]
            active = [r for r in existing if not r.released]
            if self._has_node_conflict(active, start_time, end_time):
                self._congestion_counts[node_id] += 1
                self._reserve_failure += 1
                return False
            existing.append(Reservation(node_id, agv_id, start_time, end_time))
            self._reserve_success += 1
            self._node_occupancy_time[node_id] += (end_time - start_time)
            return True

    async def release(self, node_id: str, agv_id: str) -> None:
        async with self._lock:
            for r in self._reservations[node_id]:
                if r.agv_id == agv_id and not r.released:
                    r.released = True
                    break

    async def wait_for_slot(
        self,
        node_id: str,
        agv_id: str,
        estimated_duration: float,
        current_sim_time: float,
    ) -> float:
        while True:
            async with self._lock:
                active = sorted(
                    [r for r in self._reservations[node_id] if not r.released],
                    key=lambda r: r.end_time,
                )
                start = current_sim_time
                for r in active:
                    if r.end_time > start:
                        start = r.end_time
                end = start + estimated_duration
                if not self._has_node_conflict(active, start, end):
                    self._reservations[node_id].append(
                        Reservation(node_id, agv_id, start, end)
                    )
                    return start
            await asyncio.sleep(0.02)

    # ── 엣지 예약 (신규) ───────────────────────────────────────    

    async def reserve_itinerary(self, segments: list[ItinerarySegment]) -> bool:
        """
        전체 path의 node/edge time-window를 atomic하게 예약한다.
        Open-RMF itinerary 제출의 1차 근사: 하나라도 충돌하면 아무 segment도
        추가하지 않고 False를 반환한다.
        """
        async with self._lock:
            if self._has_itinerary_conflict(segments):
                self._itinerary_failure += 1
                self._reserve_failure += 1
                return False

            for segment in segments:
                if segment.section_key:
                    # 같은 (section_key, agv_id) 활성 항목이 있으면 새로 append하지 않고
                    # end_time을 확장한다 (베이/사이딩처럼 한 AGV가 여러 edge를 거치는
                    # 동안 점유가 지속되도록).
                    same_active = next(
                        (
                            r for r in self._section_reservations[segment.section_key]
                            if r.agv_id == segment.agv_id and not r.released
                        ),
                        None,
                    )
                    if same_active is not None:
                        if segment.end_time > same_active.end_time:
                            same_active.end_time = segment.end_time
                    else:
                        self._section_reservations[segment.section_key].append(
                            Reservation(
                                segment.section_key,
                                segment.agv_id,
                                segment.start_time,
                                segment.end_time,
                            )
                        )
                    # 명시적 hold 등록: 경로 종료 후 _maybe_release_completed_sections이
                    # release_section을 호출할 수 있도록. 이게 빠지면 pre-reserve된 path는
                    # 다음 itinerary가 같은 section을 활성 reservation으로 보고 end_time을
                    # 무한 확장하는 stale-extension 버그가 발생함.
                    self._agv_held_sections[segment.agv_id].add(segment.section_key)
                if segment.segment_type == "node":
                    self._reservations[segment.key].append(
                        Reservation(
                            segment.key,
                            segment.agv_id,
                            segment.start_time,
                            segment.end_time,
                        )
                    )
                    self._node_occupancy_time[segment.key] += (
                        segment.end_time - segment.start_time
                    )
                elif segment.segment_type == "edge":
                    self._edge_reservations[segment.key].append(
                        EdgeReservation(
                            segment.key,
                            segment.agv_id,
                            segment.start_time,
                            segment.end_time,
                        )
                    )
                    self._edge_occupancy_time[segment.key] += (
                        segment.end_time - segment.start_time
                    )

            self._itinerary_success += 1
            self._reserve_success += len(segments)
            return True

    async def release_agv_reservations(self, agv_id: str) -> None:
        async with self._lock:
            for reservations in self._reservations.values():
                for r in reservations:
                    if r.agv_id == agv_id and not r.released:
                        r.released = True
            for reservations in self._edge_reservations.values():
                for r in reservations:
                    if r.agv_id == agv_id and not r.released:
                        r.released = True
            for reservations in self._section_reservations.values():
                for r in reservations:
                    if r.agv_id == agv_id and not r.released:
                        r.released = True
            self._agv_held_sections.pop(agv_id, None)

    async def reserve_edge(
        self,
        src_id: str,
        dst_id: str,
        agv_id: str,
        start_time: float,
        end_time: float,
        same_direction_headway_s: float = 0.0,
        section_key: str = "",
        section_capacity: int = 1,
        facility_node_id: str = "",
    ) -> bool:
        """
        엣지 예약 시도.
        - section_key가 있으면 동일 critical section 용량을 먼저 확인한다.
        - 역방향(dst→src) 활성 예약과 시간 겹침 시 head-on 차단.
        - 같은 방향(src→dst)은 진입 시각 간 headway를 만족해야 추종 허용.
        """
        edge_key    = f"{src_id}__{dst_id}"
        reverse_key = f"{dst_id}__{src_id}"

        async with self._lock:
            if facility_node_id:
                facility_active = [
                    r for r in self._reservations[facility_node_id]
                    if not r.released and r.agv_id != agv_id
                ]
                if self._has_node_conflict(facility_active, start_time, end_time):
                    self._congestion_counts[facility_node_id] += 1
                    self._reserve_failure += 1
                    if self._trace_recorder is not None:
                        self._trace_recorder.record_event(
                            "facility_node_conflict",
                            start_time,
                            agv_id=agv_id,
                            edge_key=edge_key,
                            node_id=facility_node_id,
                        )
                    return False

            if section_key:
                # 명시적 hold 중인 다른 AGV는 시간 만료와 무관하게 차단해야 한다.
                holders = sum(
                    1 for other_id, sections in self._agv_held_sections.items()
                    if other_id != agv_id and section_key in sections
                )
                section_active = [
                    r for r in self._section_reservations[section_key]
                    if not r.released and r.agv_id != agv_id
                ]
                cap = max(1, section_capacity)
                if holders >= cap or self._has_section_capacity_conflict(
                    section_active,
                    start_time,
                    end_time,
                    section_capacity,
                ):
                    self._section_conflict_counts[section_key] += 1
                    self._reserve_failure += 1
                    if self._trace_recorder is not None:
                        self._trace_recorder.record_event(
                            "section_conflict",
                            start_time,
                            agv_id=agv_id,
                            edge_key=edge_key,
                            section_key=section_key,
                        )
                    return False

            # head-on 충돌 확인 — 역방향 활성 예약
            reverse_active = [
                r for r in self._edge_reservations[reverse_key]
                if not r.released
            ]
            if self._has_edge_conflict(reverse_active, start_time, end_time):
                # 진성 충돌: 역방향 차단
                self._edge_headon_counts[edge_key] += 1
                self._edge_congestion_counts[edge_key] += 1
                self._reserve_failure += 1
                if self._trace_recorder is not None:
                    self._trace_recorder.record_event(
                        "headon_block",
                        start_time,
                        agv_id=agv_id,
                        edge_key=edge_key,
                        reverse_edge_key=reverse_key,
                    )
                return False

            # same-direction follow-on 안전거리 확인.
            same_direction_active = [
                r for r in self._edge_reservations[edge_key]
                if not r.released and r.agv_id != agv_id
            ]
            if self._has_followon_conflict(
                same_direction_active,
                start_time,
                same_direction_headway_s,
            ):
                self._edge_followon_counts[edge_key] += 1
                self._edge_congestion_counts[edge_key] += 1
                self._reserve_failure += 1
                if self._trace_recorder is not None:
                    self._trace_recorder.record_event(
                        "followon_block",
                        start_time,
                        agv_id=agv_id,
                        edge_key=edge_key,
                    )
                return False

            # head-on 대기 중 재시도 여부 확인
            # (이미 같은 AGV의 이전 실패 이력이 있으면 재시도로 분류)
            is_retry = any(
                r.agv_id == agv_id
                for r in self._edge_reservations[edge_key]
                if r.released
            )
            if is_retry:
                self._edge_retry_counts[edge_key] += 1

            # 예약 확정
            if section_key:
                # 같은 (section_key, agv_id)가 이미 활성 상태이면 새로 append하지 않고
                # 기존 reservation의 end_time을 확장한다. 베이/사이딩처럼 한 AGV가
                # 여러 edge를 거치는 동안 section 점유가 지속되어야 하는 영역에서,
                # 이전 edge end_time이 만료되어 다른 AGV가 진입하는 버그를 막는다.
                same_active = next(
                    (
                        r for r in self._section_reservations[section_key]
                        if r.agv_id == agv_id and not r.released
                    ),
                    None,
                )
                if same_active is not None:
                    if end_time > same_active.end_time:
                        same_active.end_time = end_time
                else:
                    self._section_reservations[section_key].append(
                        Reservation(section_key, agv_id, start_time, end_time)
                    )
                # 명시적 hold: AGV가 이 section을 점유 중임을 시간 만료와 무관하게
                # 표시. release_section()으로 명시적 해제 시까지 다른 AGV 진입 차단.
                self._agv_held_sections[agv_id].add(section_key)
            self._edge_reservations[edge_key].append(
                EdgeReservation(edge_key, agv_id, start_time, end_time)
            )
            self._reserve_success += 1
            self._edge_occupancy_time[edge_key] += (end_time - start_time)
            if self._trace_recorder is not None:
                self._trace_recorder.record_event(
                    "edge_reserved",
                    start_time,
                    agv_id=agv_id,
                    edge_key=edge_key,
                    end_time=round(end_time, 4),
                )
            return True

    async def release_edge(self, src_id: str, dst_id: str, agv_id: str) -> None:
        edge_key = f"{src_id}__{dst_id}"
        async with self._lock:
            for r in self._edge_reservations[edge_key]:
                if r.agv_id == agv_id and not r.released:
                    r.released = True
                    break

    def release_section(self, section_key: str, agv_id: str) -> None:
        """AGV가 더 이상 이 section을 점유하지 않음을 명시적으로 표시.
        시간 만료에 의존하지 않고, AGV가 베이/사이딩 등에서 빠져나갈 때 호출."""
        held = self._agv_held_sections.get(agv_id)
        if held and section_key in held:
            held.discard(section_key)
        # 같은 (section_key, agv_id) 활성 reservation도 released=True로 표시
        for r in self._section_reservations.get(section_key, []):
            if r.agv_id == agv_id and not r.released:
                r.released = True

    def is_head_on(self, src_id: str, dst_id: str, sim_time: float) -> bool:
        """
        현재 시점에 역방향 활성 엣지 예약 존재 여부.
        AGV가 이동 시작 전 빠르게 확인용.
        """
        reverse_key = f"{dst_id}__{src_id}"
        return any(
            not r.released and r.start_time <= sim_time <= r.end_time
            for r in self._edge_reservations.get(reverse_key, [])
        )

    # ── Deadlock 감지 ──────────────────────────────────────────

    def register_waiting(self, waiting_agv: str, blocking_agv: str) -> None:
        """AGV가 다른 AGV의 점유로 대기 중임을 등록."""
        self._waiting_for[waiting_agv] = blocking_agv

    def clear_waiting(self, agv_id: str) -> None:
        """대기 해제."""
        self._waiting_for.pop(agv_id, None)

    def detect_deadlock(self) -> list[list[str]]:
        """
        대기 그래프에서 순환(사이클) 탐지.
        반환: 데드락에 걸린 AGV ID 사이클 목록.
        """
        cycles: list[list[str]] = []
        visited: set[str] = set()

        for start in list(self._waiting_for.keys()):
            if start in visited:
                continue
            path: list[str] = []
            seen: set[str] = set()
            node = start
            while node and node not in seen:
                seen.add(node)
                path.append(node)
                node = self._waiting_for.get(node)
            if node and node in seen:
                # 사이클 시작점부터 잘라내기
                cycle_start = path.index(node)
                cycle = path[cycle_start:]
                cycles.append(cycle)
                visited.update(cycle)

        return cycles

    def resolve_deadlock(self, cycle: list[str]) -> str:
        """
        사이클 중 우선순위 가장 낮은(ID 사전순 마지막) AGV를 반환.
        호출자가 해당 AGV를 강제 후진/재계획시킴.
        """
        return sorted(cycle)[-1]

    # ── 공통 통계 ──────────────────────────────────────────────

    def get_congestion_score(self, node_id: str) -> float:
        total = len(self._reservations.get(node_id, []))
        if total == 0:
            return 0.0
        return round(min(self._congestion_counts[node_id] / total, 1.0), 3)

    def get_edge_congestion_score(self, edge_key: str) -> float:
        total = len(self._edge_reservations.get(edge_key, []))
        if total == 0:
            return 0.0
        return round(min(self._edge_congestion_counts[edge_key] / total, 1.0), 3)

    def get_all_scores(self) -> dict[str, float]:
        return {nid: self.get_congestion_score(nid) for nid in self._congestion_counts}

    def get_all_edge_scores(self) -> dict[str, float]:
        return {
            ek: self.get_edge_congestion_score(ek)
            for ek in self._edge_congestion_counts
        }

    def get_headon_summary(self) -> dict:
        """
        head-on 충돌 요약.
        - headon_total: 전체 head-on 발생 횟수 (진성 충돌)
        - retry_total: 대기 중 재시도 횟수 (병목 강도)
        - avg_retry_per_headon: head-on 1건당 평균 재시도 횟수 (체류 시간 추정)
        - top_headon_edges: 충돌 상위 엣지
        """
        headon_total = sum(self._edge_headon_counts.values())
        followon_total = sum(self._edge_followon_counts.values())
        section_total = sum(self._section_conflict_counts.values())
        retry_total  = sum(self._edge_retry_counts.values())
        avg_retry = round(retry_total / headon_total, 2) if headon_total > 0 else 0.0
        top_edges = sorted(
            self._edge_headon_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]
        return {
            "headon_total":          headon_total,
            "followon_total":        followon_total,
            "section_conflict_total": section_total,
            "retry_total":           retry_total,
            "avg_retry_per_headon":  avg_retry,
            "top_headon_edges":      [{"edge": e, "count": c} for e, c in top_edges],
            "itinerary_success":     self._itinerary_success,
            "itinerary_failure":     self._itinerary_failure,
            "top_section_conflicts":  [
                {"section": section, "count": count}
                for section, count in sorted(
                    self._section_conflict_counts.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:5]
            ],
        }

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _has_node_conflict(
        self, existing: list[Reservation], start: float, end: float
    ) -> bool:
        return any(
            not r.released and not (end <= r.start_time or start >= r.end_time)
            for r in existing
        )

    def _has_edge_conflict(
        self, existing: list[EdgeReservation], start: float, end: float
    ) -> bool:
        return any(
            not r.released and not (end <= r.start_time or start >= r.end_time)
            for r in existing
        )

    def _has_followon_conflict(
        self,
        existing: list[EdgeReservation],
        start: float,
        headway_s: float,
    ) -> bool:
        if headway_s <= 0.0:
            return False
        return any(
            not r.released and abs(start - r.start_time) < headway_s
            for r in existing
        )

    def _has_section_capacity_conflict(
        self,
        existing: list[Reservation],
        start: float,
        end: float,
        capacity: int,
    ) -> bool:
        cap = max(1, capacity)
        overlap_count = sum(
            1
            for r in existing
            if not r.released and not (end <= r.start_time or start >= r.end_time)
        )
        return overlap_count >= cap

    def _has_itinerary_conflict(self, segments: list[ItinerarySegment]) -> bool:
        staged_nodes: dict[str, list[Reservation]] = defaultdict(list)
        staged_edges: dict[str, list[EdgeReservation]] = defaultdict(list)
        staged_sections: dict[str, list[Reservation]] = defaultdict(list)

        for segment in segments:
            if segment.section_key:
                existing_sections = [
                    r for r in self._section_reservations[segment.section_key]
                    if not r.released and r.agv_id != segment.agv_id
                ] + staged_sections[segment.section_key]
                if self._has_section_capacity_conflict(
                    existing_sections,
                    segment.start_time,
                    segment.end_time,
                    segment.section_capacity,
                ):
                    self._section_conflict_counts[segment.section_key] += 1
                    return True
                staged_sections[segment.section_key].append(
                    Reservation(
                        segment.section_key,
                        segment.agv_id,
                        segment.start_time,
                        segment.end_time,
                    )
                )

            if segment.segment_type == "node":
                existing = [
                    r for r in self._reservations[segment.key]
                    if not r.released and r.agv_id != segment.agv_id
                ] + staged_nodes[segment.key]
                if self._has_node_conflict(existing, segment.start_time, segment.end_time):
                    self._congestion_counts[segment.key] += 1
                    return True
                staged_nodes[segment.key].append(
                    Reservation(
                        segment.key,
                        segment.agv_id,
                        segment.start_time,
                        segment.end_time,
                    )
                )
                continue

            if segment.segment_type == "edge":
                reverse_key = f"{segment.dst_id}__{segment.src_id}"
                reverse_active = [
                    r for r in self._edge_reservations[reverse_key]
                    if not r.released and r.agv_id != segment.agv_id
                ] + staged_edges[reverse_key]
                if self._has_edge_conflict(
                    reverse_active,
                    segment.start_time,
                    segment.end_time,
                ):
                    self._edge_headon_counts[segment.key] += 1
                    self._edge_congestion_counts[segment.key] += 1
                    return True

                same_direction_active = [
                    r for r in self._edge_reservations[segment.key]
                    if not r.released and r.agv_id != segment.agv_id
                ] + staged_edges[segment.key]
                if self._has_followon_conflict(
                    same_direction_active,
                    segment.start_time,
                    segment.same_direction_headway_s,
                ):
                    self._edge_followon_counts[segment.key] += 1
                    self._edge_congestion_counts[segment.key] += 1
                    return True
                staged_edges[segment.key].append(
                    EdgeReservation(
                        segment.key,
                        segment.agv_id,
                        segment.start_time,
                        segment.end_time,
                    )
                )

        return False
