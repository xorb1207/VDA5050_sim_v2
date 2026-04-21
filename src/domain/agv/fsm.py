from __future__ import annotations
from enum import Enum


class AGVState(Enum):
    IDLE                = "IDLE"
    NAVIGATING          = "NAVIGATING"
    WAITING_RESERVATION = "WAITING_RESERVATION"
    PROCESSING          = "PROCESSING"
    CHARGING            = "CHARGING"
    ERROR               = "ERROR"


class AGVStateMachine:
    """
    AGV 상태 전환 규칙 관리.
    전환은 반드시 이 클래스를 통해서만 — agv.py가 직접 state를 쓰지 않음.
    """

    ALLOWED_TRANSITIONS: dict[AGVState, set[AGVState]] = {
        AGVState.IDLE: {
            AGVState.NAVIGATING,
            AGVState.WAITING_RESERVATION,
            AGVState.CHARGING,
            AGVState.ERROR,
        },
        AGVState.NAVIGATING: {
            AGVState.IDLE,
            AGVState.PROCESSING,
            AGVState.WAITING_RESERVATION,
            AGVState.ERROR,
        },
        AGVState.WAITING_RESERVATION: {
            AGVState.NAVIGATING,
            AGVState.IDLE,
            AGVState.ERROR,
        },
        AGVState.PROCESSING: {
            AGVState.IDLE,
            AGVState.ERROR,
        },
        AGVState.CHARGING: {
            AGVState.IDLE,
            AGVState.ERROR,
        },
        AGVState.ERROR: {
            AGVState.IDLE,
        },
    }

    def __init__(self) -> None:
        self._state = AGVState.IDLE

    @property
    def state(self) -> AGVState:
        return self._state

    def transition(self, new_state: AGVState) -> bool:
        """전환 시도. 허용된 전환이면 True, 아니면 False (상태 유지)."""
        if new_state in self.ALLOWED_TRANSITIONS.get(self._state, set()):
            self._state = new_state
            return True
        return False

    def force(self, new_state: AGVState) -> None:
        """검증 없이 강제 전환. 복구·초기화 용도로만 사용."""
        self._state = new_state
