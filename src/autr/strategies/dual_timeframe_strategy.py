import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

import pandas as pd
import numpy as np

from autr.infra.bybit.client import BybitClient
from autr.infra.db.sqlite import TradeTrackerDB
from .strategy_params import DualTimeframeParams, dual_timeframe_param_descriptions

logger = logging.getLogger(__name__)


@dataclass
class SignalDecision:
    signal: str
    reason: str
    trailing_stop: Optional[float]
    close_price: float


class DualTimeframeSignalEngine:
    def __init__(self, params: DualTimeframeParams):
        self.params = params

    def _compute_ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _compute_rsi(self, series: pd.Series, period: int) -> pd.Series:
        """Wilder 표준 RSI (EMA smoothing, alpha=1/period)"""
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta.clip(upper=0))
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _compute_atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def htf_is_bullish(self, candles: pd.DataFrame) -> tuple:
        """상위 타임프레임에서 상승 추세 여부 판단 + 진단 정보 반환"""
        info = {"htf_len": len(candles), "htf_close": 0.0, "htf_ema_fast": 0.0, "htf_ema_slow": 0.0}
        if len(candles) < self.params.htf_ema_slow + 5:
            return False, info  # 데이터 부족
        ema_fast = self._compute_ema(candles["close"], self.params.htf_ema_fast)
        ema_slow = self._compute_ema(candles["close"], self.params.htf_ema_slow)
        info["htf_close"] = float(candles["close"].iloc[-1])
        info["htf_ema_fast"] = float(ema_fast.iloc[-1])
        info["htf_ema_slow"] = float(ema_slow.iloc[-1])
        bullish = float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1])
        return bullish, info

    def build_ltf_frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["ema"] = self._compute_ema(df["close"], self.params.ltf_ema_period)
        df["rsi"] = self._compute_rsi(df["close"], self.params.ltf_rsi_period)
        df["atr"] = self._compute_atr(df, self.params.atr_period)
        return df.dropna().reset_index(drop=True)

    def decide(
        self,
        ltf_frame: pd.DataFrame,
        htf_bullish: bool,
        htf_data_sufficient: bool,
        in_position: bool,
        trailing_stop: Optional[float],
        bars_since_trade: int,
    ) -> SignalDecision:
        if ltf_frame.empty or len(ltf_frame) < 2:
            return SignalDecision("hold", "insufficient_data", trailing_stop, 0.0)

        last = ltf_frame.iloc[-1]
        prev = ltf_frame.iloc[-2]
        close_price = float(last["close"])
        rsi = float(last["rsi"])
        rsi_prev = float(prev["rsi"])
        ema = float(last["ema"])
        atr = float(last["atr"])

        if in_position:
            candidate_stop = close_price - atr * self.params.trailing_stop_atr_mult
            next_stop = max(trailing_stop or candidate_stop, candidate_stop)
            if close_price <= next_stop:
                return SignalDecision("sell", "trailing_stop_hit", next_stop, close_price)
            if not htf_bullish:
                return SignalDecision("sell", "htf_trend_reversed", next_stop, close_price)
            return SignalDecision("hold", "in_position_hold", next_stop, close_price)

        if bars_since_trade < self.params.cooldown_bars:
            return SignalDecision("hold", "cooldown", trailing_stop, close_price)

        # HTF 데이터 부족 vs 실제 bearish 구분
        if not htf_bullish:
            reason = "htf_insufficient_data" if not htf_data_sufficient else "no_htf_bull"
            return SignalDecision("hold", reason, trailing_stop, close_price)

        # 진입: 상위 추세 상승 + RSI 과매도권 회복(cross) + 가격이 LTF EMA 위
        oversold = self.params.ltf_rsi_oversold
        if rsi_prev <= oversold and rsi > oversold and close_price > ema:
            entry_stop = close_price - atr * self.params.initial_stop_atr_mult
            return SignalDecision("buy", "htf_bull_rsi_recovery", entry_stop, close_price)

        return SignalDecision("hold", "ltf_no_entry", trailing_stop, close_price)


class DualTimeframeStrategy:
    def __init__(
        self,
        client: BybitClient,
        trade_tracker: TradeTrackerDB,
        params: Optional[DualTimeframeParams] = None,
    ):
        self.client = client
        self.trade_tracker = trade_tracker
        self.params = params or DualTimeframeParams()
        self.signal_engine = DualTimeframeSignalEngine(self.params)

        self.is_active = False
        self.position: Optional[str] = None
        self.last_signal: Optional[str] = None
        self.trade_amount: Optional[str] = None
        self.trailing_stop: Optional[float] = None
        self.bars_since_trade = self.params.cooldown_bars
        self.last_reason: Optional[str] = None
        self.last_indicators: Dict[str, float] = {}
        self._htf_bullish: bool = False

        logger.info("DualTimeframeStrategy initialized with params=%s", self.params.to_dict())

    async def start_trading(self):
        await self._restore_state_from_db()
        self.is_active = True
        logger.info("DualTimeframeStrategy started")
        while self.is_active:
            try:
                await self.execute_strategy()
            except Exception as exc:
                logger.exception("DualTimeframe strategy loop error: %s", exc)
            await asyncio.sleep(self.params.loop_seconds)

    def stop_trading(self):
        self.is_active = False
        logger.info("DualTimeframeStrategy stopped")

    async def execute_strategy(self):
        # 상위/하위 타임프레임 데이터 동시 수신
        # HTF: EMA 안정성을 위해 최소 htf_ema_slow * 3 캔들 요청
        htf_limit = max(self.params.htf_ema_slow * 3, self.params.htf_ema_slow + 20)
        htf_raw, ltf_raw = await asyncio.gather(
            self.client.get_kline_data(
                symbol=self.params.symbol,
                interval=self.params.htf_interval,
                limit=htf_limit,
            ),
            self.client.get_kline_data(
                symbol=self.params.symbol,
                interval=self.params.ltf_interval,
                limit=self.params.ltf_lookback_bars,
            ),
        )

        htf_candles = self._candles_to_dataframe(htf_raw)
        ltf_candles = self._candles_to_dataframe(ltf_raw)
        if ltf_candles.empty:
            return

        htf_bullish, htf_info = self.signal_engine.htf_is_bullish(htf_candles)
        htf_data_sufficient = len(htf_candles) >= self.params.htf_ema_slow + 5
        self._htf_bullish = htf_bullish
        ltf_frame = self.signal_engine.build_ltf_frame(ltf_candles)

        decision = self.signal_engine.decide(
            ltf_frame=ltf_frame,
            htf_bullish=htf_bullish,
            htf_data_sufficient=htf_data_sufficient,
            in_position=(self.position == "long"),
            trailing_stop=self.trailing_stop,
            bars_since_trade=self.bars_since_trade,
        )
        last_row = ltf_frame.iloc[-1]
        prev_row = ltf_frame.iloc[-2] if len(ltf_frame) >= 2 else last_row
        self.last_reason = decision.reason
        self.last_indicators = {
            # LTF 지표
            "close": float(last_row["close"]),
            "rsi": float(last_row["rsi"]),
            "rsi_prev": float(prev_row["rsi"]),
            "ema": float(last_row["ema"]),
            "atr": float(last_row["atr"]),
            # HTF 진단 지표
            "htf_bullish": float(htf_bullish),
            "htf_len": float(htf_info["htf_len"]),
            "htf_close": htf_info["htf_close"],
            "htf_ema_fast": htf_info["htf_ema_fast"],
            "htf_ema_slow": htf_info["htf_ema_slow"],
        }
        self.last_signal = decision.signal
        self.trailing_stop = decision.trailing_stop

        await self.trade_tracker.add_signal_log(
            symbol=self.params.symbol,
            strategy="dual_timeframe",
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
            price=decision.close_price, signal=f"dtf_buy:{decision.reason}", status="filled",
        )
        logger.info("BUY executed qty=%s price=%.4f", safe_qty, decision.close_price)

    async def _execute_sell(self, decision: SignalDecision):
        if self.position != "long":
            self.bars_since_trade += 1
            return
        # qty=None → BybitClient.calculate_safe_order_size()가 보유량 전량 조회 후 매도
        result = await self.client.place_order(symbol=self.params.symbol, side="Sell", qty=None)
        if not result.get("success"):
            self.bars_since_trade += 1
            return
        sold_qty = float(self.trade_amount) if self.trade_amount else 0.0
        await self.trade_tracker.add_trade(
            symbol=self.params.symbol, side="Sell", qty=sold_qty,
            price=decision.close_price, signal=f"dtf_sell:{decision.reason}", status="filled",
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
            self.position = "long" if qty > 0 else None
            self.trade_amount = f"{qty:.8f}".rstrip("0").rstrip(".") if qty > 0 else None
        except Exception as exc:
            logger.warning("Failed to restore state: %s", exc)

    def get_strategy_status(self) -> Dict[str, Any]:
        return {
            "strategy": "dual_timeframe",
            "is_active": self.is_active,
            "position": self.position,
            "last_signal": self.last_signal,
            "last_reason": self.last_reason,
            "trade_amount": self.trade_amount,
            "trailing_stop": self.trailing_stop,
            "bars_since_trade": self.bars_since_trade,
            "indicators": self.last_indicators,
            "parameters": self.params.to_dict(),
            "parameter_descriptions": dual_timeframe_param_descriptions(),
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
