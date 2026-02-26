from dataclasses import dataclass
from typing import Literal


Action = Literal["noop", "open_missing_local", "close_orphan_local", "adjust_qty"]


@dataclass
class ReconcileDecision:
    action: Action
    reason: str


def reconcile(local_qty: float, exchange_qty: float, tolerance: float = 1e-9) -> ReconcileDecision:
    if abs(local_qty - exchange_qty) <= tolerance:
        return ReconcileDecision(action="noop", reason="local and exchange quantities match")
    if local_qty == 0 and exchange_qty > 0:
        return ReconcileDecision(action="open_missing_local", reason="exchange has open qty but local is empty")
    if local_qty > 0 and exchange_qty == 0:
        return ReconcileDecision(action="close_orphan_local", reason="local has open qty but exchange is empty")
    return ReconcileDecision(action="adjust_qty", reason="both sides have qty but values differ")
