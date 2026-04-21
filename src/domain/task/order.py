from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class OrderState(Enum):
    PENDING     = "PENDING"
    ASSIGNED    = "ASSIGNED"
    TRAVELING   = "TRAVELING"
    LOADING     = "LOADING"
    DELIVERING  = "DELIVERING"
    UNLOADING   = "UNLOADING"
    DONE        = "DONE"
    CANCELLED   = "CANCELLED"
    FAILED      = "FAILED"

@dataclass
class VDA5050NodeRef:
    nodeId: str
    sequenceId: int
    released: bool
    actions: list[dict] = field(default_factory=list)

@dataclass
class VDA5050EdgeRef:
    edgeId: str
    sequenceId: int
    released: bool
    maxSpeed: Optional[float] = None

@dataclass
class Order:
    order_id: str
    order_update_id: int
    agv_id: str
    base_nodes:    list[VDA5050NodeRef] = field(default_factory=list)
    base_edges:    list[VDA5050EdgeRef] = field(default_factory=list)
    horizon_nodes: list[VDA5050NodeRef] = field(default_factory=list)
    horizon_edges: list[VDA5050EdgeRef] = field(default_factory=list)
    state: OrderState = OrderState.PENDING
    created_at:  float          = 0.0
    assigned_at: Optional[float] = None
    done_at:     Optional[float] = None

    def is_complete(self) -> bool:
        return self.state in (OrderState.DONE, OrderState.CANCELLED, OrderState.FAILED)