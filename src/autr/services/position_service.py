import os
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

from autr.infra.db.sqlite import TradeTrackerDB

logger = logging.getLogger(__name__)


class PositionService:
    """SQLite-backed position management with optional live execution."""

    def __init__(self, trade_db: TradeTrackerDB):
        self.trade_db = trade_db
        self.paper_trading = os.getenv("PAPER_TRADING", "false").lower() == "true"
        logger.info("PositionService initialized (paper_trading=%s)", self.paper_trading)

    async def open_position(
        self,
        symbol: str,
        position_type: str,
        entry_price: float,
        quantity: float,
        dollar_amount: float,
        trading_client=None,
    ) -> Dict[str, Any]:
        try:
            exchange_order_id: Optional[str] = None

            if not self.paper_trading and trading_client:
                if position_type == "short":
                    return {
                        "success": False,
                        "error": "short_not_supported",
                        "message": "Spot short is not supported for live execution.",
                    }

                side = "Buy" if position_type == "long" else "Sell"
                result = await trading_client.place_order(
                    symbol=symbol,
                    side=side,
                    qty=f"{quantity}",
                )
                if not result.get("success"):
                    return {
                        "success": False,
                        "error": result.get("error", "order_failed"),
                        "message": "Exchange order failed; no local position stored.",
                    }
                exchange_order_id = result.get("order_id")
                market_price = await trading_client.get_current_price(symbol)
                if market_price > 0:
                    entry_price = float(market_price)

            entry_trade = await self.trade_db.add_trade(
                symbol=symbol,
                side="Buy" if position_type == "long" else "Sell",
                qty=quantity,
                price=entry_price,
                signal="position_open",
                position_type=position_type,
                dollar_amount=dollar_amount,
                status="filled",
                order_id=exchange_order_id,
            )

            position = await self.trade_db.create_position(
                symbol=symbol,
                position_type=position_type,
                entry_price=entry_price,
                quantity=quantity,
                dollar_amount=dollar_amount,
                entry_trade_id=entry_trade["id"],
            )

            return {
                "success": True,
                "position": position,
                "entry_trade": entry_trade,
                "exchange_order_id": exchange_order_id,
                "message": f"Position opened: {symbol} {position_type}",
            }
        except Exception as e:
            logger.error("Failed to open position: %s", e)
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to open position",
            }

    async def close_position(
        self,
        position_id: str,
        close_price: Optional[float] = None,
        trading_client=None,
    ) -> Dict[str, Any]:
        try:
            position = await self.trade_db.get_position_by_id(position_id)
            if not position:
                return {
                    "success": False,
                    "error": "Position not found",
                    "message": f"Position {position_id} not found",
                }
            if position.get("status") != "open":
                return {
                    "success": False,
                    "error": "Position already closed",
                    "message": f"Position {position_id} is already closed",
                }

            if close_price is None:
                if trading_client:
                    close_price = await trading_client.get_current_price(position["symbol"])
                else:
                    close_price = float(position["current_price"])

            exchange_order_id: Optional[str] = None
            if not self.paper_trading and trading_client:
                side = "Sell" if position["position_type"] == "long" else "Buy"
                result = await trading_client.place_order(
                    symbol=position["symbol"],
                    side=side,
                    qty=f"{position['quantity']}",
                )
                if not result.get("success"):
                    return {
                        "success": False,
                        "error": result.get("error", "order_failed"),
                        "message": "Exchange close order failed; local position unchanged.",
                    }
                exchange_order_id = result.get("order_id")

            exit_trade = await self.trade_db.add_trade(
                symbol=position["symbol"],
                side="Sell" if position["position_type"] == "long" else "Buy",
                qty=float(position["quantity"]),
                price=float(close_price),
                signal="position_close",
                position_type=position["position_type"],
                dollar_amount=float(position["quantity"]) * float(close_price),
                status="filled",
                order_id=exchange_order_id,
            )

            closed_position = await self.trade_db.close_position(
                position_id=position_id,
                close_price=float(close_price),
                exit_trade_id=exit_trade["id"],
            )

            return {
                "success": True,
                "position": closed_position,
                "exit_trade": exit_trade,
                "exchange_order_id": exchange_order_id,
                "final_pnl": closed_position.get("unrealized_pnl") if closed_position else 0,
                "final_pnl_percent": closed_position.get("unrealized_pnl_percent") if closed_position else 0,
                "message": "Position closed",
            }
        except Exception as e:
            logger.error("Failed to close position: %s", e)
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to close position",
            }

    async def update_position_prices(self, price_updates: Dict[str, float]) -> Dict[str, Any]:
        try:
            updated_count = await self.trade_db.update_open_position_prices(price_updates)
            return {"success": True, "updated_count": updated_count}
        except Exception as e:
            logger.error("Failed to update position prices: %s", e)
            return {"success": False, "error": str(e), "updated_count": 0}

    async def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        positions = await self.trade_db.get_positions(status="open", symbol=symbol, limit=1000)
        return [self._with_derived_fields(p) for p in positions]

    async def get_closed_positions(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        positions = await self.trade_db.get_positions(status="closed", symbol=symbol, limit=limit)
        return [self._with_derived_fields(p) for p in positions]

    async def get_position_summary(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        summary = await self.trade_db.get_position_summary(symbol)
        summary["open_positions"] = [self._with_derived_fields(p) for p in summary.get("open_positions", [])]
        summary["recent_closed_positions"] = [
            self._with_derived_fields(p) for p in summary.get("recent_closed_positions", [])
        ]
        return summary

    async def get_position_by_id(self, position_id: str) -> Optional[Dict[str, Any]]:
        position = await self.trade_db.get_position_by_id(position_id)
        if not position:
            return None

        entry_trade = None
        exit_trade = None
        if position.get("entry_trade_id"):
            entry_trade = await self.trade_db.get_trade_by_id(int(position["entry_trade_id"]))
        if position.get("exit_trade_id"):
            exit_trade = await self.trade_db.get_trade_by_id(int(position["exit_trade_id"]))

        pos = self._with_derived_fields(position)
        pos["entry_trade"] = entry_trade
        pos["exit_trade"] = exit_trade
        return pos

    async def auto_close_positions_by_criteria(
        self,
        symbol: Optional[str] = None,
        max_loss_percent: Optional[float] = None,
        min_profit_percent: Optional[float] = None,
        max_days_open: Optional[int] = None,
        trading_client=None,
    ) -> List[Dict[str, Any]]:
        open_positions = await self.get_open_positions(symbol)
        closed_results = []

        for position in open_positions:
            should_close = False
            close_reason = ""
            pnl_pct = float(position.get("unrealized_pnl_percent") or 0.0)

            if max_loss_percent is not None and pnl_pct <= -abs(max_loss_percent):
                should_close = True
                close_reason = f"Stop loss: {pnl_pct:.2f}%"
            elif min_profit_percent is not None and pnl_pct >= min_profit_percent:
                should_close = True
                close_reason = f"Take profit: {pnl_pct:.2f}%"
            elif max_days_open is not None and int(position.get("days_open_or_held", 0)) >= max_days_open:
                should_close = True
                close_reason = f"Max days reached: {position.get('days_open_or_held', 0)} days"

            if should_close:
                result = await self.close_position(position["id"], trading_client=trading_client)
                result["close_reason"] = close_reason
                closed_results.append(result)

        return closed_results

    def _with_derived_fields(self, position: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(position)
        quantity = float(p.get("quantity") or 0.0)
        current_price = float(p.get("current_price") or 0.0)
        dollar_amount = float(p.get("dollar_amount") or 0.0)
        pnl = float(p.get("unrealized_pnl") or 0.0)
        p["current_value"] = quantity * current_price
        p["invested_value"] = dollar_amount
        p["pnl_color"] = "green" if pnl >= 0 else "red"

        if p.get("status") == "closed":
            p["days_open_or_held"] = self._calculate_days_held(p.get("open_time"), p.get("close_time"))
            p["entry_date"] = (p.get("open_time") or "")[:10]
            p["exit_date"] = (p.get("close_time") or "")[:10] if p.get("close_time") else None
            p["realized_pnl"] = pnl
            p["realized_pnl_percent"] = float(p.get("unrealized_pnl_percent") or 0.0)
        else:
            p["days_open_or_held"] = self._calculate_days_open(p.get("open_time"))
            p["entry_date"] = (p.get("open_time") or "")[:10]

        return p

    def _calculate_days_open(self, open_time: Optional[str]) -> int:
        if not open_time:
            return 0
        try:
            open_dt = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            now = datetime.now(open_dt.tzinfo) if open_dt.tzinfo else datetime.now()
            return (now - open_dt).days
        except Exception:
            return 0

    def _calculate_days_held(self, open_time: Optional[str], close_time: Optional[str]) -> int:
        if not open_time or not close_time:
            return 0
        try:
            open_dt = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            return (close_dt - open_dt).days
        except Exception:
            return 0
