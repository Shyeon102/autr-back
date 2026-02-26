import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple

import pandas as pd

from autr.infra.bybit.client import BybitClient
from autr.infra.db.sqlite import TradeTrackerDB
from .strategy_params import RegimeTrendParams, regime_trend_param_descriptions

logger = logging.getLogger(__name__)


@dataclass
class SignalDecision:
    signal: str
    reason: str
    trailing_stop: Optional[float]
    close_price: float


class RegimeTrendSignalEngine:
    def __init__(self, params: RegimeTrendParams):
        self.params = params

    def build_indicator_frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["ema_fast"] = df["close"].ewm(span=self.params.ema_fast_period, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.params.ema_slow_period, adjust=False).mean()

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr"] = tr.rolling(self.params.atr_period).mean()
        df["trend_gap_pct"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"]
        return df.dropna().reset_index(drop=True)

    def decide(
        self,
        frame: pd.DataFrame,
        in_position: bool,
        trailing_stop: Optional[float],
        bars_since_trade: int,
    ) -> SignalDecision:
        if frame.empty:
            return SignalDecision("hold", "insufficient_indicators", trailing_stop, 0.0)

        last = frame.iloc[-1]
        close_price = float(last["close"])
        atr = float(last["atr"])
        bullish_regime = float(last["trend_gap_pct"]) >= self.params.min_trend_gap_pct

        if in_position:
            candidate_stop = close_price - (atr * self.params.trailing_stop_atr_mult)
            # trailing stop은 항상 위로만 올라감 (역방향 이동 방지)
            if trailing_stop is not None and trailing_stop > 0:
                next_stop = max(trailing_stop, candidate_stop)
            else:
                next_stop = candidate_stop

            if close_price <= next_stop:
                return SignalDecision("sell", "trailing_stop_hit", next_stop, close_price)
            if not bullish_regime:
                return SignalDecision("sell", "regime_exit", next_stop, close_price)
            return SignalDecision("hold", "in_position_hold", next_stop, close_price)

        if bars_since_trade < self.params.cooldown_bars:
            return SignalDecision("hold", "cooldown", trailing_stop, close_price)

        if bullish_regime and close_price > float(last["ema_fast"]):
            entry_stop = close_price - (atr * self.params.initial_stop_atr_mult)
            return SignalDecision("buy", "bullish_regime_breakout", entry_stop, close_price)
        return SignalDecision("hold", "no_entry", trailing_stop, close_price)


class RegimeTrendStrategy:
    def __init__(
        self,
        client: BybitClient,
        trade_tracker: TradeTrackerDB,
        params: Optional[RegimeTrendParams] = None,
    ):
        self.client = client
        self.trade_tracker = trade_tracker
        self.params = params or RegimeTrendParams()
        self.signal_engine = RegimeTrendSignalEngine(self.params)

        self.is_active = False
        self.position: Optional[str] = None
        self.last_signal: Optional[str] = None
        self.trade_amount: Optional[str] = None
        self.trailing_stop: Optional[float] = None
        self.bars_since_trade = self.params.cooldown_bars
        self.last_reason: Optional[str] = None
        self.last_indicators: Dict[str, float] = {}

        logger.info("RegimeTrendStrategy initialized with params=%s", self.params.to_dict())

    async def start_trading(self):
        await self._restore_state_from_db()
        self.is_active = True
        logger.info("RegimeTrendStrategy started")

        while self.is_active:
            try:
                await self.execute_strategy()
            except Exception as exc:
                logger.exception("RegimeTrend strategy loop error: %s", exc)
            await asyncio.sleep(self.params.loop_seconds)

    def stop_trading(self):
        self.is_active = False
        logger.info("RegimeTrendStrategy stopped")

    async def execute_strategy(self):
        raw = await self.client.get_kline_data(
            symbol=self.params.symbol,
            interval=self.params.interval,
            limit=self.params.lookback_bars,
        )
        candles = self._candles_to_dataframe(raw)
        if candles.empty:
            logger.warning("No candle data available for %s", self.params.symbol)
            return

        frame = self.signal_engine.build_indicator_frame(candles)
        decision = self.signal_engine.decide(
            frame=frame,
            in_position=(self.position == "long"),
            trailing_stop=self.trailing_stop,
            bars_since_trade=self.bars_since_trade,
        )
        last_row = frame.iloc[-1]
        self.last_reason = decision.reason
        self.last_indicators = {
            "close": float(last_row["close"]),
            "ema_fast": float(last_row["ema_fast"]),
            "ema_slow": float(last_row["ema_slow"]),
            "trend_gap_pct": float(last_row["trend_gap_pct"]),
            "atr": float(last_row["atr"]),
        }
        self.last_signal = decision.signal
        self.trailing_stop = decision.trailing_stop

        await self.trade_tracker.add_signal_log(
            symbol=self.params.symbol,
            strategy="regime_trend",
            signal=decision.signal,
            reason=decision.reason,
            indicators=self.last_indicators,
            trailing_stop=self.trailing_stop,
            in_position=(self.position == "long"),
            params=self.params.to_dict(),
        )

        if decision.signal == "buy":
            await self._execute_buy(decision)
            return
        if decision.signal == "sell":
            await self._execute_sell(decision)
            return

        self.bars_since_trade += 1
        logger.info(
            "Signal hold reason=%s close=%.4f trailing_stop=%s",
            decision.reason,
            decision.close_price,
            f"{self.trailing_stop:.4f}" if self.trailing_stop else None,
        )

    async def _execute_buy(self, decision: SignalDecision):
        if self.position == "long":
            self.bars_since_trade += 1
            return

        safe_qty = await self.client.calculate_safe_order_size(self.params.symbol, "Buy")
        if not safe_qty:
            logger.warning("Safe buy size unavailable")
            self.bars_since_trade += 1
            return

        result = await self.client.place_order(
            symbol=self.params.symbol,
            side="Buy",
            qty=safe_qty,
        )
        if not result.get("success"):
            logger.warning("Buy order failed: %s", result.get("error"))
            self.bars_since_trade += 1
            return

        self.position = "long"
        self.trade_amount = safe_qty
        self.bars_since_trade = 0
        await self.trade_tracker.add_trade(
            symbol=self.params.symbol,
            side="Buy",
            qty=float(safe_qty),
            price=decision.close_price,
            signal=f"regime_buy:{decision.reason}",
            status="filled",
        )
        logger.info(
            "BUY executed qty=%s price=%.4f stop=%.4f",
            safe_qty,
            decision.close_price,
            self.trailing_stop or 0.0,
        )

    async def _execute_sell(self, decision: SignalDecision):
        if self.position != "long":
            self.bars_since_trade += 1
            return

        result = await self.client.place_order(
            symbol=self.params.symbol,
            side="Sell",
            qty=None,
        )
        if not result.get("success"):
            logger.warning("Sell order failed: %s", result.get("error"))
            self.bars_since_trade += 1
            return

        sold_qty = float(self.trade_amount) if self.trade_amount else 0.0
        await self.trade_tracker.add_trade(
            symbol=self.params.symbol,
            side="Sell",
            qty=sold_qty,
            price=decision.close_price,
            signal=f"regime_sell:{decision.reason}",
            status="filled",
        )
        self.position = None
        self.trade_amount = None
        self.trailing_stop = None
        self.bars_since_trade = 0
        logger.info("SELL executed qty=%s price=%.4f", sold_qty, decision.close_price)

    async def _restore_state_from_db(self):
        """Restore in-memory position state from persisted trades."""
        try:
            positions = await self.trade_tracker.get_current_positions()
            symbol_positions = positions.get(self.params.symbol, {})
            spot_position = symbol_positions.get("spot", {})
            qty = float(spot_position.get("total_quantity", 0.0))

            if qty > 0:
                self.position = "long"
                self.trade_amount = f"{qty:.8f}".rstrip("0").rstrip(".")
                logger.info(
                    "Restored strategy state from DB: symbol=%s qty=%s position=long",
                    self.params.symbol,
                    self.trade_amount,
                )
            else:
                self.position = None
                self.trade_amount = None
                logger.info("No open DB position for %s; state restored as flat", self.params.symbol)
        except Exception as exc:
            logger.warning("Failed to restore strategy state: %s", exc)

    def get_strategy_status(self) -> Dict[str, Any]:
        return {
            "strategy": "regime_trend",
            "is_active": self.is_active,
            "position": self.position,
            "last_signal": self.last_signal,
            "last_reason": self.last_reason,
            "trade_amount": self.trade_amount,
            "trailing_stop": self.trailing_stop,
            "bars_since_trade": self.bars_since_trade,
            "indicators": self.last_indicators,
            "parameters": self.params.to_dict(),
            "parameter_descriptions": regime_trend_param_descriptions(),
        }

    @staticmethod
    def _candles_to_dataframe(raw: list) -> pd.DataFrame:
        if not raw:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        rows = []
        for k in raw:
            try:
                rows.append(
                    {
                        "timestamp": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                )
            except (ValueError, IndexError, TypeError):
                continue

        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
        return df
