"""
Strategy Consumer — tick 큐 구독 → signal 큐 발행.

흐름:
  queue:ticks:{symbol}  (Collector 발행)
        ↓  BRPOP
  trading:enabled:{symbol} 확인 → 비활성이면 상태만 기록
        ↓  활성
  선택된 SignalEngine.decide()
        ↓  signal 있으면
  queue:signals  (Executor Consumer 구독)
  state:strategy:{symbol}  (API 서버 상태 조회용)

재시작 복구:
  DB의 positions 테이블에서 in_position 상태 복구
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

from autr.domain import ids as id_gen
from autr.infra.db.factory import create_db
from autr.infra.db.quant_store import QuantSQLiteStore
from autr.infra.queue_keys import (
    SIGNAL_QUEUE,
    active_strategy_key,
    strategy_state_key,
    tick_queue,
    trading_enabled_key,
)
from autr.infra.redis import create_redis_adapter
from autr.ops.heartbeat import record_heartbeat
from autr.strategies.strategy_params import (
    BUILTIN_PRESETS,
    STRATEGY_PARAMS_MAP,
    apply_preset_overrides,
    DualTimeframeParams,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("strategy-consumer")

_HB_TTL = int(os.getenv("WATCHDOG_STRATEGY_THRESHOLD_SEC", "180"))
_HB_INTERVAL = 60
_POP_TIMEOUT = 30
_STATE_TTL = 300   # 5분 — consumer 중단 시 stale state 방지


def _build_engine(strategy_name: str, params):
    """전략 이름에 맞는 SignalEngine 반환."""
    if strategy_name == "regime_trend":
        from autr.strategies.regime_trend_strategy import RegimeTrendSignalEngine
        return RegimeTrendSignalEngine(params)
    elif strategy_name == "dual_timeframe":
        from autr.strategies.dual_timeframe_strategy import DualTimeframeSignalEngine
        return DualTimeframeSignalEngine(params)
    elif strategy_name == "breakout_volume":
        from autr.strategies.breakout_volume_strategy import BreakoutVolumeSignalEngine
        return BreakoutVolumeSignalEngine(params)
    elif strategy_name == "mean_reversion":
        from autr.strategies.mean_reversion_strategy import MeanReversionSignalEngine
        return MeanReversionSignalEngine(params)
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")


class StrategyConsumer:
    """
    단일 심볼/전략 소비자.
    - 재시작 시 DB에서 in_position 복구
    - trading:enabled 플래그 확인 후 signal 발행
    - 매 tick마다 state:strategy 갱신 (API 서버 상태 조회용)
    """

    def __init__(self, strategy_name: str, symbol: str, params, store: QuantSQLiteStore, redis, db):
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.params = params
        self.store = store
        self.redis = redis
        self.db = db
        self.engine = _build_engine(strategy_name, params)

        self.in_position: bool = False
        self.trailing_stop: Optional[float] = None
        self.bars_since_trade: int = params.cooldown_bars

        self._last_hb: float = 0.0
        self._last_signal: str = "hold"
        self._last_reason: str = ""
        self._last_close: float = 0.0

    # ------------------------------------------------------------------ #
    # 메인 루프
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        await self._restore_state()
        logger.info(
            "[StrategyConsumer] 시작: strategy=%s symbol=%s in_position=%s",
            self.strategy_name, self.symbol, self.in_position,
        )

        while True:
            try:
                raw = await self.redis.queue_pop(tick_queue(self.symbol), timeout=_POP_TIMEOUT)

                await self._maybe_heartbeat()
                await self._check_strategy_change()

                if raw is None:
                    continue

                tick = json.loads(raw)
                await self._process_tick(tick)

            except Exception as exc:
                logger.exception("[StrategyConsumer] 오류: %s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------ #
    # 상태 복구 (재시작 시)
    # ------------------------------------------------------------------ #

    async def _check_strategy_change(self) -> None:
        """Redis에서 전략 변경 요청 확인 — 변경 시 엔진 동적 교체."""
        requested = await self.redis.get(active_strategy_key(self.symbol))
        if not requested or requested == self.strategy_name:
            return

        new_strategy = requested.strip()
        params_cls = STRATEGY_PARAMS_MAP.get(new_strategy)
        if params_cls is None:
            logger.warning("[StrategyConsumer] 알 수 없는 전략 요청 무시: %s", new_strategy)
            return

        logger.info(
            "[StrategyConsumer] 전략 교체: %s → %s",
            self.strategy_name, new_strategy,
        )
        self.strategy_name = new_strategy
        self.params = params_cls(symbol=self.symbol)

        preset = BUILTIN_PRESETS.get((new_strategy, self.symbol), {})
        if preset:
            apply_preset_overrides(self.params, preset)

        self.engine = _build_engine(new_strategy, self.params)
        # 포지션 상태는 유지, 시그널/트레일링 스탑 초기화
        self.trailing_stop = None
        self._last_signal = "hold"
        self._last_reason = "strategy_changed"

    async def _restore_state(self) -> None:
        """DB의 open positions에서 in_position 복구."""
        try:
            positions = await self.db.get_current_positions()
            symbol_data = positions.get(self.symbol, {})
            spot_data = symbol_data.get("spot", {})
            qty = float(spot_data.get("total_quantity", 0))
            self.in_position = qty > 0
            logger.info(
                "[StrategyConsumer] 상태 복구 완료: symbol=%s in_position=%s (qty=%.6f)",
                self.symbol, self.in_position, qty,
            )
        except Exception as exc:
            logger.warning("[StrategyConsumer] 상태 복구 실패, 초기값 사용: %s", exc)

    # ------------------------------------------------------------------ #
    # Tick 처리
    # ------------------------------------------------------------------ #

    async def _process_tick(self, tick: dict) -> None:
        symbol = tick.get("symbol", self.symbol)

        decision = self._decide(symbol)
        if decision is None:
            return

        self.bars_since_trade += 1
        self.trailing_stop = decision.trailing_stop
        self._last_signal = decision.signal
        self._last_reason = decision.reason
        self._last_close = decision.close_price

        enabled = bool(await self.redis.get(trading_enabled_key(self.symbol)))

        if enabled:
            if decision.signal == "buy" and not self.in_position:
                self.in_position = True
                self.bars_since_trade = 0
                await self._publish_signal("buy", decision.close_price)
                logger.info(
                    "[StrategyConsumer] BUY | %s close=%.4f reason=%s",
                    symbol, decision.close_price, decision.reason,
                )
            elif decision.signal == "sell" and self.in_position:
                self.in_position = False
                self.bars_since_trade = 0
                await self._publish_signal("sell", decision.close_price)
                logger.info(
                    "[StrategyConsumer] SELL | %s close=%.4f reason=%s",
                    symbol, decision.close_price, decision.reason,
                )

        await self._write_state(enabled)

    def _decide(self, symbol: str):
        """전략별 decide() 호출 분기."""
        if self.strategy_name == "dual_timeframe":
            return self._decide_dual(symbol)
        else:
            return self._decide_single(symbol)

    def _decide_single(self, symbol: str):
        """regime_trend / breakout_volume / mean_reversion 공통 경로."""
        interval = getattr(self.params, "interval", "15")
        lookback = getattr(self.params, "lookback_bars", 260)

        rows = self.store.fetch_ohlcv(symbol, "spot", interval, limit=lookback)
        if len(rows) < 50:
            logger.debug("[StrategyConsumer] 캔들 부족 (%d개)", len(rows))
            return None

        df = pd.DataFrame([dict(r) for r in rows]).sort_values("ts").reset_index(drop=True)
        frame = self.engine.build_indicator_frame(df)
        if frame.empty:
            return None

        return self.engine.decide(
            frame=frame,
            in_position=self.in_position,
            trailing_stop=self.trailing_stop,
            bars_since_trade=self.bars_since_trade,
        )

    def _decide_dual(self, symbol: str):
        """dual_timeframe 전용 경로 — HTF + LTF 각각 fetch."""
        params: DualTimeframeParams = self.params

        # LTF 캔들
        ltf_rows = self.store.fetch_ohlcv(
            symbol, "spot", params.ltf_interval, limit=params.ltf_lookback_bars
        )
        if len(ltf_rows) < 50:
            logger.debug("[StrategyConsumer] LTF 캔들 부족 (%d개)", len(ltf_rows))
            return None

        ltf_df = pd.DataFrame([dict(r) for r in ltf_rows]).sort_values("ts").reset_index(drop=True)
        ltf_frame = self.engine.build_ltf_frame(ltf_df)
        if ltf_frame.empty:
            return None

        # HTF 캔들
        htf_rows = self.store.fetch_ohlcv(
            symbol, "spot", params.htf_interval, limit=params.htf_ema_slow + 10
        )
        htf_df = (
            pd.DataFrame([dict(r) for r in htf_rows]).sort_values("ts").reset_index(drop=True)
            if htf_rows else pd.DataFrame()
        )
        htf_bullish, _ = self.engine.htf_is_bullish(htf_df) if not htf_df.empty else (False, {})
        htf_sufficient = len(htf_rows) >= params.htf_ema_slow + 5

        return self.engine.decide(
            ltf_frame=ltf_frame,
            htf_bullish=htf_bullish,
            htf_data_sufficient=htf_sufficient,
            in_position=self.in_position,
            trailing_stop=self.trailing_stop,
            bars_since_trade=self.bars_since_trade,
        )

    async def _publish_signal(self, side: str, close_price: float) -> None:
        sig_id = id_gen.signal_id(self.strategy_name, self.symbol)
        payload = json.dumps({
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "signal": side,
            "sig_id": sig_id,
            "close_price": close_price,
        })
        await self.redis.queue_push(SIGNAL_QUEUE, payload)

    async def _write_state(self, trading_enabled: bool) -> None:
        """API 서버 /trading/status 조회용 상태를 Redis에 기록."""
        state = {
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "is_active": True,
            "trading_enabled": trading_enabled,
            "in_position": self.in_position,
            "trailing_stop": self.trailing_stop,
            "last_signal": self._last_signal,
            "last_reason": self._last_reason,
            "close_price": self._last_close,
            "bars_since_trade": self.bars_since_trade,
            "parameters": self.params.to_dict(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.set(strategy_state_key(self.symbol), json.dumps(state), ex=_STATE_TTL)

    # ------------------------------------------------------------------ #
    # Heartbeat
    # ------------------------------------------------------------------ #

    async def _maybe_heartbeat(self) -> None:
        import time
        now = time.monotonic()
        if now - self._last_hb >= _HB_INTERVAL:
            await record_heartbeat("strategy", self.redis, ttl=_HB_TTL)
            self._last_hb = now


# ------------------------------------------------------------------ #
# Entrypoint
# ------------------------------------------------------------------ #

_DEFAULT_STRATEGY = "regime_trend"


async def main() -> None:
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()
    redis_url = os.getenv("REDIS_URL", "")
    db_path = os.getenv("QUANT_DB_PATH", "./data/quant_timeseries.db")
    database_url = os.getenv("DATABASE_URL", "")

    redis = create_redis_adapter(redis_url)

    # 시작 전략: Redis에 저장된 값 우선, 없으면 기본값
    strategy_name = await redis.get(active_strategy_key(symbol)) or _DEFAULT_STRATEGY
    strategy_name = strategy_name.strip().lower()

    params_cls = STRATEGY_PARAMS_MAP.get(strategy_name)
    if params_cls is None:
        logger.warning("[main] 알 수 없는 전략 '%s', 기본값 '%s' 사용", strategy_name, _DEFAULT_STRATEGY)
        strategy_name = _DEFAULT_STRATEGY
        params_cls = STRATEGY_PARAMS_MAP[strategy_name]

    params = params_cls(symbol=symbol)

    preset = BUILTIN_PRESETS.get((strategy_name, symbol), {})
    if preset:
        apply_preset_overrides(params, preset)

    logger.info("[main] strategy=%s symbol=%s params=%s", strategy_name, symbol, params.to_dict())

    store = QuantSQLiteStore(db_path)
    db = create_db(database_url)

    consumer = StrategyConsumer(
        strategy_name=strategy_name,
        symbol=symbol,
        params=params,
        store=store,
        redis=redis,
        db=db,
    )
    await consumer.run()


if __name__ == "__main__":
    asyncio.run(main())
