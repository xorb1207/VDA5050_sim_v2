from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskState(Enum):
    PENDING    = "PENDING"
    ASSIGNED   = "ASSIGNED"
    TRAVELING  = "TRAVELING"
    LOADING    = "LOADING"
    DELIVERING = "DELIVERING"
    UNLOADING  = "UNLOADING"
    DONE       = "DONE"
    CANCELLED  = "CANCELLED"
    FAILED     = "FAILED"


@dataclass
class Task:
    """
    내부 실행 단위. VDA5050 Order와 독립.
    task_generator(내부 생성)와 translator(외부 변환) 모두
    이 모델로 수렴한다.
    """
    task_id:      str
    agv_id:       str
    node_path:    list[str]          # 경유할 node_id 순서
    state:        TaskState          = TaskState.PENDING

    created_at:   float              = 0.0
    assigned_at:  Optional[float]    = None
    done_at:      Optional[float]    = None

    # 원본 VDA5050 order 참조 (없으면 내부 생성)
    source_order_id: Optional[str]   = None

    def is_complete(self) -> bool:
        return self.state in (TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED)
