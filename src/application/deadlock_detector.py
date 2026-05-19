"""
Multi-AGV 데드락 감지 및 해소.

기존 scheduler._waiting_for (예약 기반) 와 별개로, 위치 기반 wait-for
그래프를 구성해 사이클을 탐지한다. AGV 가 N초 이상 같은 위치에 머무르고
다음 이동 경로의 엣지를 다른 AGV 가 점유 중일 때 wait-for edge 가 형성된다.

해소 우선순위:
  1. 사이클 내 fleet priority 가 가장 낮은 (priority 숫자가 가장 큰) AGV 선정
  2. 현재 위치에서 한 노드 후퇴 시도 (역방향 엣지 존재 + 예약 가능)
  3. 후퇴 실패 시 reroute 시도 (회피 경로)
  4. 모두 실패 시 alert + hold (operator 개입 필요)

scheduler._waiting_for 를 보조적으로 참고하되 신뢰하지 않는다:
  - 위치 기반은 진성 정체만 잡는다 (reservation flag 와 무관)
  - scheduler 의 register/clear_waiting 호출 누락에도 견고
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.domain.agv.agv import AGV
    from src.domain.reservation.scheduler import TimeWindowScheduler


STUCK_THRESHOLD_S = 5.0       # 위치 변화 없이 대기한 시간이 이 값 이상이면 stuck
MOVE_EPSILON_M = 0.05         # 이 거리 이내 움직임은 "정지"로 간주
BACKUP_RESERVE_DURATION_S = 2.0  # 후퇴 엣지 시범 예약 길이


class DeadlockDetector:
    """위치 기반 wait-for 그래프 + 사이클 탐지 + 해소."""

    def __init__(self, stuck_threshold_s: float = STUCK_THRESHOLD_S) -> None:
        self.stuck_threshold_s = stuck_threshold_s

        # agv_id -> (anchor_x, anchor_y, anchor_sim_time)
        # 위치가 EPSILON 이상 바뀌면 anchor 를 현재 위치+시간으로 갱신.
        self._anchor: dict[str, tuple[float, float, float]] = {}

        # 누적 통계
        self.total_count: int = 0
        # 마지막 detect 결과 (사이클 목록)
        self.last_cycles: list[list[str]] = []
        # 마지막 resolve 결과 (alert 여부)
        self.last_alert: bool = False
        # 이미 같은 사이클을 처리한 경우 동일 사이클 중복 카운트 방지용
        self._resolved_signature: set[frozenset[str]] = set()

    # ── 위치 추적 ──────────────────────────────────────────

    def update_positions(self, agvs: dict, sim_time: float) -> None:
        """매 tick 호출. anchor 갱신."""
        for aid, agv in agvs.items():
            x = agv._motion.state.x
            y = agv._motion.state.y
            cur = self._anchor.get(aid)
            if cur is None:
                self._anchor[aid] = (x, y, sim_time)
                continue
            ax, ay, _ = cur
            if abs(x - ax) + abs(y - ay) > MOVE_EPSILON_M:
                self._anchor[aid] = (x, y, sim_time)

        # 사라진 AGV anchor 제거 (메모리 누수 방지)
        dead = [aid for aid in self._anchor if aid not in agvs]
        for aid in dead:
            self._anchor.pop(aid, None)

    def stuck_duration(self, agv_id: str, sim_time: float) -> float:
        cur = self._anchor.get(agv_id)
        if cur is None:
            return 0.0
        _, _, at = cur
        return max(0.0, sim_time - at)

    # ── wait-for 그래프 구성 ──────────────────────────────

    def _next_hop(self, agv: "AGV") -> Optional[str]:
        """AGV 가 진입 시도 중인 다음 노드 id."""
        if agv._pending_edge_dst:
            return agv._pending_edge_dst
        path = agv._path
        idx = agv._path_index
        if path and 0 <= idx < len(path) - 1:
            return path[idx + 1]
        return None

    def _find_blocking_agv(
        self,
        scheduler: "TimeWindowScheduler",
        src: str,
        dst: str,
        self_id: str,
    ) -> Optional[str]:
        """해당 엣지 진입을 막고 있는 다른 AGV id. 없으면 None."""
        # 역방향 (head-on) 우선
        rev_key = f"{dst}__{src}"
        for r in scheduler._edge_reservations.get(rev_key, []):
            if not r.released and r.agv_id != self_id:
                return r.agv_id
        # 같은 방향 (follow-on)
        fwd_key = f"{src}__{dst}"
        for r in scheduler._edge_reservations.get(fwd_key, []):
            if not r.released and r.agv_id != self_id:
                return r.agv_id
        return None

    def _build_wait_for(
        self,
        agvs: dict,
        scheduler: "TimeWindowScheduler",
        sim_time: float,
    ) -> dict[str, str]:
        """wait_for[A] = B → A 가 B 때문에 못 가고 있음."""
        wait_for: dict[str, str] = {}
        for aid, agv in agvs.items():
            # 대기 상태가 아니면 스킵
            if agv._fsm.state.value != "WAITING_RESERVATION":
                continue
            # stuck 시간 부족하면 스킵
            if self.stuck_duration(aid, sim_time) < self.stuck_threshold_s:
                continue
            src = agv.current_node_id
            dst = self._next_hop(agv)
            if not src or not dst:
                continue
            blocker = self._find_blocking_agv(scheduler, src, dst, aid)
            if blocker and blocker in agvs and blocker != aid:
                wait_for[aid] = blocker
        return wait_for

    # ── 사이클 탐지 ──────────────────────────────────────

    @staticmethod
    def _find_cycles(wait_for: dict[str, str]) -> list[list[str]]:
        """단순 DFS. wait_for 는 각 노드당 outgoing 1개 → 빠르게 사이클 추적."""
        cycles: list[list[str]] = []
        visited: set[str] = set()
        for start in list(wait_for.keys()):
            if start in visited:
                continue
            path: list[str] = []
            seen: set[str] = set()
            node: Optional[str] = start
            while node and node not in seen:
                seen.add(node)
                path.append(node)
                node = wait_for.get(node)
            if node and node in seen:
                cycle_start = path.index(node)
                cycle = path[cycle_start:]
                cycles.append(cycle)
                visited.update(cycle)
            else:
                visited.update(path)
        return cycles

    def detect(
        self,
        agvs: dict,
        scheduler: "TimeWindowScheduler",
        sim_time: float,
    ) -> list[list[str]]:
        """공개 API: 데드락 사이클 목록 반환. 없으면 빈 리스트."""
        wait_for = self._build_wait_for(agvs, scheduler, sim_time)
        cycles = self._find_cycles(wait_for)
        self.last_cycles = cycles
        return cycles

    # ── 해소 ────────────────────────────────────────────

    @staticmethod
    def _priority_key(agv: Optional["AGV"]) -> tuple:
        """정렬 키. 가장 낮은 우선순위(=가장 큰 priority 숫자)가 victim.
        Tiebreak: agv_id 사전순 마지막."""
        if agv is None:
            return (10**6, "~")
        fl = getattr(agv, "fleet", None)
        priority = fl.priority if fl else 1
        # 활성 demand 있으면 +0 (긴급), 없으면 +10 (더 낮은 우선순위 → 먼저 양보)
        urgency = 0 if agv._current_demand_id else 10
        return (priority + urgency, agv.agv_id)

    def _pick_victim(self, cycle: list[str], agvs: dict) -> str:
        """사이클 내 우선순위가 가장 낮은 AGV id."""
        # priority key 최대 → 낮은 우선순위
        return max(cycle, key=lambda aid: self._priority_key(agvs.get(aid)))

    async def _try_backup(
        self,
        agv: "AGV",
        sim_time: float,
    ) -> bool:
        """현재 위치에서 한 노드 후퇴.

        조건:
          - path 상에 이전 노드 존재
          - graph 에 (current -> prev) 엣지 존재
          - 역방향 엣지 예약 가능
        성공 시 path 재구성: [current, prev, ...alt path to goal].
        """
        if not agv.current_node_id or not agv._path:
            return False
        if agv._path_index <= 0 or agv._path_index >= len(agv._path):
            return False
        cur = agv.current_node_id
        prev = agv._path[agv._path_index - 1]
        if not prev or prev == cur:
            return False

        # 역방향 엣지가 graph 에 존재해야 함
        graph = agv._graph
        has_rev = any(
            graph.edges[eid].end_node_id == prev
            for eid in graph._out_edges.get(cur, [])
        )
        if not has_rev:
            return False

        # 역방향 엣지 예약 시도
        ok = await agv._sched.reserve_edge(
            cur,
            prev,
            agv.agv_id,
            start_time=sim_time,
            end_time=sim_time + BACKUP_RESERVE_DURATION_S,
        )
        if not ok:
            return False
        # 예약은 후퇴 의도 표시용이므로 즉시 release (AGV 가 _navigate_to_next_node
        # 안에서 다시 예약하도록 둠 — 실제 이동 시간과 일치해야 정확함).
        await agv._sched.release_edge(cur, prev, agv.agv_id)

        # 회피 경로: blocked = 현재 진입 시도 중이던 엣지
        blocked: Optional[tuple[str, str]] = None
        if agv._pending_edge_src and agv._pending_edge_dst:
            blocked = (agv._pending_edge_src, agv._pending_edge_dst)
        goal = agv._path[-1]
        alt = graph.get_path(
            prev,
            goal,
            blocked_edges={blocked} if blocked else None,
        )
        if not alt or len(alt) < 2:
            # 후퇴 후 갈 곳이 없으면 후퇴 자체도 의미 없음
            return False

        # 새 path 구성: 현재 → 이전 → 회피 경로
        new_path = [cur, prev] + alt[1:]
        agv._path = new_path
        agv._path_index = 0
        agv._pending_edge_src = None
        agv._pending_edge_dst = None
        agv.collision_retry_count = 0
        agv._reroute_count += 1
        agv._sched.clear_waiting(agv.agv_id)
        # 다음 tick 에 _navigate_to_next_node 가 호출되도록 WAITING 으로 둔다
        # (AGV.tick 의 WAITING_RESERVATION 분기에서 자동 재시도).
        agv._trace(
            "deadlock_backup",
            sim_time,
            from_node=cur,
            to_node=prev,
            new_path=new_path,
        )
        return True

    async def _try_reroute(
        self,
        agv: "AGV",
        sim_time: float,
    ) -> bool:
        """blocked edge 회피 reroute. 새 경로가 잡히면 True."""
        if not agv.current_node_id or not agv._path:
            return False
        blocked: Optional[tuple[str, str]] = None
        if agv._pending_edge_src and agv._pending_edge_dst:
            blocked = (agv._pending_edge_src, agv._pending_edge_dst)
        goal = agv._path[-1]
        alt = agv._graph.get_path(
            agv.current_node_id,
            goal,
            blocked_edges={blocked} if blocked else None,
        )
        if not alt or len(alt) < 2:
            return False
        # 현재 path 와 동일하면 의미 없음
        if alt == agv._path[agv._path_index:]:
            return False
        agv._path = alt
        agv._path_index = 0
        agv._pending_edge_src = None
        agv._pending_edge_dst = None
        agv.collision_retry_count = 0
        agv._reroute_count += 1
        agv._sched.clear_waiting(agv.agv_id)
        agv._trace(
            "deadlock_reroute",
            sim_time,
            new_path=alt,
        )
        return True

    async def resolve(
        self,
        cycle: list[str],
        agvs: dict,
        scheduler: "TimeWindowScheduler",
        sim_time: float,
    ) -> dict:
        """사이클 해소 시도. {agv_id, action, success} 반환.

        action: 'backup' | 'reroute' | 'alert'
        """
        victim_id = self._pick_victim(cycle, agvs)
        victim = agvs.get(victim_id)
        if victim is None:
            self.last_alert = True
            return {"agv_id": victim_id, "action": "alert", "success": False}

        # 1) backup
        if await self._try_backup(victim, sim_time):
            self._anchor[victim_id] = (
                victim._motion.state.x,
                victim._motion.state.y,
                sim_time,
            )
            return {"agv_id": victim_id, "action": "backup", "success": True}

        # 2) reroute
        if await self._try_reroute(victim, sim_time):
            self._anchor[victim_id] = (
                victim._motion.state.x,
                victim._motion.state.y,
                sim_time,
            )
            return {"agv_id": victim_id, "action": "reroute", "success": True}

        # 3) alert
        self.last_alert = True
        victim._trace(
            "deadlock_alert",
            sim_time,
            cycle=cycle,
            reason="backup_and_reroute_failed",
        )
        return {"agv_id": victim_id, "action": "alert", "success": False}

    # ── 통합 tick API ─────────────────────────────────────

    async def step(
        self,
        agvs: dict,
        scheduler: "TimeWindowScheduler",
        sim_time: float,
    ) -> dict:
        """매 tick 호출용 단일 진입점.

        반환 payload (tick 메시지에 직접 병합 가능):
          {
            "deadlock_detected": bool,
            "deadlock_groups": list[list[str]],
            "deadlock_count_total": int,
            "deadlock_alert": bool,
            "deadlock_resolutions": list[{agv_id, action, success}],
          }
        """
        # 위치 anchor 갱신
        self.update_positions(agvs, sim_time)
        # 사이클 탐지
        cycles = self.detect(agvs, scheduler, sim_time)

        # 이전 tick 의 alert 플래그는 새 step 마다 리셋한다.
        self.last_alert = False
        resolutions: list[dict] = []

        if cycles:
            for cycle in cycles:
                sig = frozenset(cycle)
                # 이미 같은 사이클을 같은 episode 내에서 처리했는지 확인.
                # anchor 가 reset 되면 (= 누군가 움직였으면) 다음 detect 에서
                # 자연스럽게 cycle 이 사라지므로 sig 도 의미를 잃는다.
                # 따라서 매 cycle 발생을 새 deadlock 으로 카운트하되,
                # 같은 tick 안에서만 중복 알림 방지.
                if sig in self._resolved_signature:
                    continue
                self._resolved_signature.add(sig)
                self.total_count += 1
                res = await self.resolve(cycle, agvs, scheduler, sim_time)
                resolutions.append(res)
        else:
            # 사이클이 없으면 시그니처 캐시를 비워 다음 발생에 대비
            self._resolved_signature.clear()

        return {
            "deadlock_detected": bool(cycles),
            "deadlock_groups": cycles,
            "deadlock_count_total": self.total_count,
            "deadlock_alert": self.last_alert,
            "deadlock_resolutions": resolutions,
        }
