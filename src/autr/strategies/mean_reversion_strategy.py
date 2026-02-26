import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

import pandas as pd
import numpy as np

from autr.infra.bybit.client import BybitClient
from autr.infra.db.sqlite import TradeTrackerDB
from .strategy_params import MeanReversionParams, mean_reversion_param_descriptions

logger = logging.getLogger(__name__)


@dataclass
class SignalDecision:
    signal: str
    reason: str
    stop_price: Optional[float]
    close_price: float


class MeanReversionSignalEngine:
    def __init__(self, params: MeanReversionParams):
        self.params = params

    def build_indicator_frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()

        # Bollinger Bands
        df["bb_mid"] = df["close"].rolling(self.params.bb_period).mean()
        rolling_std = df["close"].rolling(self.params.bb_period).std()
        df["bb_upper"] = df["bb_mid"] + self.params.bb_std * rolling_std
        df["bb_lower"] = df["bb_mid"] - self.params.bb_std * rolling_std

        # RSI (Wilder 표준: EMA smoothing, alpha=1/period)
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = (-delta.clip(upper=0))
        avg_gain = gain.ewm(alpha=1 / self.params.rsi_period, min_periods=self.params.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.params.rsi_period, min_periods=self.params.rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        # ATR
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.params.atr_period).mean()

        return df.dropna().reset_index(drop=True)

    def decide(
        self,
        frame: pd.DataFrame,
        in_position: bool,
        stop_price: Optional[float],
        bars_since_trade: int,
    ) -> SignalDecision:
        if frame.empty:
            return SignalDecision("hold", "insufficient_data", stop_price, 0.0)

        last = frame.iloc[-1]
        close_price = float(last["close"])
        rsi = float(last["rsi"])
        bb_lower = float(last["bb_lower"])
        bb_mid = float(last["bb_mid"])
        atr = float(last["atr"])

        if in_position:
            # 청산: RSI 과매수 OR 가격이 볼린저 중간선 도달 OR 손절
            if stop_price and close_price <= stop_price:
                return SignalDecision("sell", "stop_hit", stop_price, close_price)
            if rsi >= self.params.rsi_overbought:
                return SignalDecision("sell", "rsi_overbought", stop_price, close_price)
            if close_price >= bb_mid:
                return SignalDecision("sell", "bb_mid_reached", stop_price, close_price)
            return SignalDecision("hold", "in_position_hold", stop_price, close_price)

        if bars_since_trade < self.params.cooldown_bars:
            return SignalDecision("hold", "cooldown", stop_price, close_price)

        # 진입: RSI 과매도 AND 가격이 볼린저 하단 이하
        if rsi <= self.params.rsi_oversold and close_price <= bb_lower:
            entry_stop = close_price - atr * self.params.stop_atr_mult
            return SignalDecision("buy", "rsi_oversold_bb_lower", entry_stop, close_price)

        return SignalDecision("hold", "no_entry", stop_price, close_price)


class MeanReversionStrategy:
    def __init__(
        self,
        client: BybitClient,
        trade_tracker: TradeTrackerDB,
        params: Optional[MeanReversionParams] = None,
    ):
        self.client = client
        self.trade_tracker = trade_tracker
        self.params = params or MeanReversionParams()
        self.signal_engine = MeanReversionSignalEngine(self.params)

        self.is_active = False
        self.position: Optional[str] = None
        self.last_signal: Optional[str] = None
        self.trade_amount: Optional[str] = None
        self.stop_price: Optional[float] = None
        self.bars_since_trade = self.params.cooldown_bars
        self.last_reason: Optional[str] = None
        self.last_indicators: Dict[str, float] = {}

        logger.info("MeanReversionStrategy initialized with params=%s", self.params.to_dict())

    async def start_trading(self):
        await self._restore_state_from_db()
        self.is_active = True
        logger.info("MeanReversionStrategy started")
        while self.is_active:
            try:
                await self.execute_strategy()
            except Exception as exc:
                logger.exception("MeanReversion strategy loop error: %s", exc)
            await asyncio.sleep(self.params.loop_seconds)

    def stop_trading(self):
        self.is_active = False
        logger.info("MeanReversionStrategy stopped")

    async def execute_strategy(self):
        raw = await self.client.get_kline_data(
            symbol=self.params.symbol,
            interval=self.params.interval,
            limit=self.params.lookback_bars,
        )
        candles = self._candles_to_dataframe(raw)
        if candles.empty:
            return

        frame = self.signal_engine.build_indicator_frame(candles)
        decision = self.signal_engine.decide(
            frame=frame,
            in_position=(self.position == "long"),
            stop_price=self.stop_price,
            bars_since_trade=self.bars_since_trade,
        )
        last_row = frame.iloc[-1]
        self.last_reason = decision.reason
        self.last_indicators = {
            "close": float(last_row["close"]),
            "rsi": float(last_row["rsi"]),
            "bb_upper": float(last_row["bb_upper"]),
            "bb_mid": float(last_row["bb_mid"]),
            "bb_lower": float(last_row["bb_lower"]),
            "atr": float(last_row["atr"]),
        }
        self.last_signal = decision.signal
        self.stop_price = decision.stop_price

        await self.trade_tracker.add_signal_log(
            symbol=self.params.symbol,
            strategy="mean_reversion",
            signal=decision.signal,
            reason=decision.reason,
            indicators=self.last_indicators,
            trailing_stop=self.stop_price,
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
            price=decision.close_price, signal=f"mr_buy:{decision.reason}", status="filled",
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
            price=decision.close_price, signal=f"mr_sell:{decision.reason}", status="filled",
        )
        self.position = None
        self.trade_amount = None
        self.stop_price = None
        self.bars_since_trade = 0
        logger.info("SELL executed qty=%s price=%.4f", sold_qty, decision.close_price)

    async def _restore_state_from_db(self):
        try:
            positions = await self.trade_tracker.get_current_positions()
            spot = positions.get(self.params.symbol, {}).get("spot", {})
            qty = float(spot.get("total_quantity", 0.0))
            self.position = "long" if qty > 0 else None
            self.trade_amount = f"{qty:.8f}".rstrip("0").rstrip(".") if qty > 0 else None
        except Exception as exc:
            logger.warning("Failed to restore state: %s", exc)

    def get_strategy_status(self) -> Dict[str, Any]:
        return {
            "strategy": "mean_reversion",
            "is_active": self.is_active,
            "position": self.position,
            "last_signal": self.last_signal,
            "last_reason": self.last_reason,
            "trade_amount": self.trade_amount,
            "trailing_stop": self.stop_price,
            "bars_since_trade": self.bars_since_trade,
            "indicators": self.last_indicators,
            "parameters": self.params.to_dict(),
            "parameter_descriptions": mean_reversion_param_descriptions(),
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
