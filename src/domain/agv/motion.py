from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class MotionState:
    x: float       = 0.0
    y: float       = 0.0
    heading: float = 0.0   # radians
    speed: float   = 0.0   # m/s


class MotionModel:
    """
    직선 주행 물리 모델.
    AGV / FSM / 예약 로직과 완전히 분리.
    나중에 곡선 주행, 가감속 모델로 교체 가능.
    """

    def __init__(
        self,
        max_speed_mps: float = 1.5,
        acceleration_mps2: float = 0.6,
        deceleration_mps2: float = 0.8,
    ) -> None:
        self.max_speed = max_speed_mps
        self.acceleration = acceleration_mps2
        self.deceleration = deceleration_mps2
        self.state = MotionState()

    def update(
        self,
        dt: float,
        target_x: float,
        target_y: float,
    ) -> tuple[float, bool]:
        """
        dt 동안 target 방향으로 이동.
        반환: (이동 거리, 도착 여부)
        """
        dx = target_x - self.state.x
        dy = target_y - self.state.y
        dist = math.hypot(dx, dy)

        if dist < 1e-6:
            self.state.speed = 0.0
            return 0.0, True

        target_speed = self._target_speed_for_distance(dist)
        if self.state.speed < target_speed:
            next_speed = min(
                target_speed,
                self.state.speed + self.acceleration * dt,
            )
        else:
            next_speed = max(
                target_speed,
                self.state.speed - self.deceleration * dt,
            )

        avg_speed = (self.state.speed + next_speed) / 2.0
        move = min(avg_speed * dt, dist)
        self.state.x += dx / dist * move
        self.state.y += dy / dist * move
        self.state.heading = math.atan2(dy, dx)
        self.state.speed = next_speed if dt > 0 else 0.0

        arrived = dist <= move + 1e-6
        if arrived:
            self.state.x = target_x
            self.state.y = target_y
            self.state.speed = 0.0

        return move, arrived

    def _target_speed_for_distance(self, distance_m: float) -> float:
        braking_speed = math.sqrt(max(0.0, 2.0 * self.deceleration * distance_m))
        return min(self.max_speed, braking_speed)

    def snap_to(self, x: float, y: float) -> None:
        """노드 좌표에 정확히 스냅."""
        self.state.x = x
        self.state.y = y
        self.state.speed = 0.0
