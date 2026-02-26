import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

import pandas as pd

from autr.infra.bybit.client import BybitClient
from autr.infra.db.sqlite import TradeTrackerDB
from .strategy_params import BreakoutVolumeParams, breakout_volume_param_descriptions

logger = logging.getLogger(__name__)


@dataclass
class SignalDecision:
    signal: str
    reason: str
    trailing_stop: Optional[float]
    close_price: float


class BreakoutVolumeSignalEngine:
    def __init__(self, params: BreakoutVolumeParams):
        self.params = params

    def build_indicator_frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        # ATR
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.params.atr_period).mean()

        # 이전 N봉의 최고가 (현재 봉 제외)
        df["prior_high"] = df["high"].shift(1).rolling(self.params.breakout_period).max()

        # 거래량 이동평균
        df["volume_ma"] = df["volume"].rolling(self.params.volume_ma_period).mean()

        return df.dropna().reset_index(drop=True)

    def decide(
        self,
        frame: pd.DataFrame,
        in_position: bool,
        trailing_stop: Optional[float],
        bars_since_trade: int,
    ) -> SignalDecision:
        if frame.empty:
            return SignalDecision("hold", "insufficient_data", trailing_stop, 0.0)

        last = frame.iloc[-1]
        close_price = float(last["close"])
        atr = float(last["atr"])
        prior_high = float(last["prior_high"])
        volume = float(last["volume"])
        volume_ma = float(last["volume_ma"])

        volume_surge = volume >= volume_ma * self.params.volume_multiplier

        if in_position:
            candidate_stop = close_price - atr * self.params.trailing_stop_atr_mult
            next_stop = max(trailing_stop or candidate_stop, candidate_stop)
            if close_price <= next_stop:
                return SignalDecision("sell", "trailing_stop_hit", next_stop, close_price)
            return SignalDecision("hold", "in_position_hold", next_stop, close_price)

        if bars_since_trade < self.params.cooldown_bars:
            return SignalDecision("hold", "cooldown", trailing_stop, close_price)

        # 진입: 전고점 돌파 + 거래량 급증
        if close_price > prior_high and volume_surge:
            entry_stop = close_price - atr * self.params.initial_stop_atr_mult
            return SignalDecision("buy", "breakout_volume_confirmed", entry_stop, close_price)

        if close_price > prior_high and not volume_surge:
            return SignalDecision("hold", "breakout_no_volume", trailing_stop, close_price)

        return SignalDecision("hold", "no_breakout", trailing_stop, close_price)


class BreakoutVolumeStrategy:
    def __init__(
        self,
        client: BybitClient,
        trade_tracker: TradeTrackerDB,
        params: Optional[BreakoutVolumeParams] = None,
    ):
        self.client = client
        self.trade_tracker = trade_tracker
        self.params = params or BreakoutVolumeParams()
        self.signal_engine = BreakoutVolumeSignalEngine(self.params)

        self.is_active = False
        self.position: Optional[str] = None
        self.last_signal: Optional[str] = None
        self.trade_amount: Optional[str] = None
        self.trailing_stop: Optional[float] = None
        self.bars_since_trade = self.params.cooldown_bars
        self.last_reason: Optional[str] = None
        self.last_indicators: Dict[str, float] = {}

        logger.info("BreakoutVolumeStrategy initialized with params=%s", self.params.to_dict())

    async def start_trading(self):
        await self._restore_state_from_db()
        self.is_active = True
        logger.info("BreakoutVolumeStrategy started")
        while self.is_active:
            try:
                await self.execute_strategy()
            except Exception as exc:
                logger.exception("BreakoutVolume strategy loop error: %s", exc)
            await asyncio.sleep(self.params.loop_seconds)

    def stop_trading(self):
        self.is_active = False
        logger.info("BreakoutVolumeStrategy stopped")

    async def execute_strategy(self):
        raw = await self.client.get_kline_data(
            symbol=self.params.symbol,
            interval=self.params.interval,
            limit=self.params.lookback_bars,
        )
        candles = self._candles_to_dataframe(raw)
        if candles.empty:
            logger.warning("No candle data for %s", self.params.symbol)
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
            "prior_high": float(last_row["prior_high"]),
            "volume": float(last_row["volume"]),
            "volume_ma": float(last_row["volume_ma"]),
            "atr": float(last_row["atr"]),
        }
        self.last_signal = decision.signal
        self.trailing_stop = decision.trailing_stop

        await self.trade_tracker.add_signal_log(
            symbol=self.params.symbol,
            strategy="breakout_volume",
            signal=decision.signal,
            reason=decision.reason,
            indicators=self.last_indicators,
            trailing_stop=self.trailing_stop,
            in_position=(self.position == "long"),
            params=self.params.to_dict(),
        )

        if decision.signal == "buy":
            await self._execute_buy(decision)
        elif decision.signal == "sell":
            await self._execute_sell(decision)
        else:
            self.bars_since_trade += 1

    async def _execute_buy(self, decision: SignalDecision):
        if self.position == "long":
            self.bars_since_trade += 1
            return
        safe_qty = await self.client.calculate_safe_order_size(self.params.symbol, "Buy")
        if not safe_qty:
            self.bars_since_trade += 1
            return
        result = await self.client.place_order(symbol=self.params.symbol, side="Buy", qty=safe_qty)
        if not result.get("success"):
            self.bars_since_trade += 1
            return
        self.position = "long"
        self.trade_amount = safe_qty
        self.bars_since_trade = 0
        await self.trade_tracker.add_trade(
            symbol=self.params.symbol, side="Buy", qty=float(safe_qty),
            price=decision.close_price, signal=f"breakout_buy:{decision.reason}", status="filled",
        )
        logger.info("BUY executed qty=%s price=%.4f", safe_qty, decision.close_price)

    async def _execute_sell(self, decision: SignalDecision):
        if self.position != "long":
            self.bars_since_trade += 1
            return
        result = await self.client.place_order(symbol=self.params.symbol, side="Sell", qty=None)
        if not result.get("success"):
            self.bars_since_trade += 1
            return
        sold_qty = float(self.trade_amount) if self.trade_amount else 0.0
        await self.trade_tracker.add_trade(
            symbol=self.params.symbol, side="Sell", qty=sold_qty,
            price=decision.close_price, signal=f"breakout_sell:{decision.reason}", status="filled",
        )
        self.position = None
        self.trade_amount = None
        self.trailing_stop = None
        self.bars_since_trade = 0
        logger.info("SELL executed qty=%s price=%.4f", sold_qty, decision.close_price)

    async def _restore_state_from_db(self):
        try:
            positions = await self.trade_tracker.get_current_positions()
            spot = positions.get(self.params.symbol, {}).get("spot", {})
            qty = float(spot.get("total_quantity", 0.0))
            if qty > 0:
                self.position = "long"
                self.trade_amount = f"{qty:.8f}".rstrip("0").rstrip(".")
            else:
                self.position = None
                self.trade_amount = None
        except Exception as exc:
            logger.warning("Failed to restore state: %s", exc)

    def get_strategy_status(self) -> Dict[str, Any]:
        return {
            "strategy": "breakout_volume",
            "is_active": self.is_active,
            "position": self.position,
            "last_signal": self.last_signal,
            "last_reason": self.last_reason,
            "trade_amount": self.trade_amount,
            "trailing_stop": self.trailing_stop,
            "bars_since_trade": self.bars_since_trade,
            "indicators": self.last_indicators,
            "parameters": self.params.to_dict(),
            "parameter_descriptions": breakout_volume_param_descriptions(),
        }

    @staticmethod
    def _candles_to_dataframe(raw: list) -> pd.DataFrame:
        if not raw:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        rows = []
        for k in raw:
            try:
                rows.append({
                    "timestamp": int(k[0]), "open": float(k[1]),
                    "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5]),
                })
            except (ValueError, IndexError, TypeError):
                continue
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows)
        return df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
