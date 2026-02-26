from datetime import datetime, timezone

from fastapi import APIRouter, Request


router = APIRouter(tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    trade_tracker = getattr(request.app.state, "trade_tracker", None)
    trading_strategy = getattr(request.app.state, "trading_strategy", None)

    positions = {}
    recent_orders = []
    if trade_tracker:
        positions = await trade_tracker.get_current_positions()
        recent_orders = await trade_tracker.recent_trades(limit=5)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy_active": bool(trading_strategy and trading_strategy.is_active),
        "positions": positions,
        "recent_orders": recent_orders,
    }
