from dataclasses import dataclass
from datetime import datetime
from typing import Literal


PositionType = Literal["long", "short"]


@dataclass
class Position:
    symbol: str
    position_type: PositionType
    quantity: float
    entry_price: float
    current_price: float
    opened_at: datetime

    @property
    def unrealized_pnl(self) -> float:
        if self.position_type == "short":
            return (self.entry_price - self.current_price) * self.quantity
        return (self.current_price - self.entry_price) * self.quantity


@dataclass
class OrderIntent:
    symbol: str
    side: Literal["Buy", "Sell"]
    qty: float
    reason: str
