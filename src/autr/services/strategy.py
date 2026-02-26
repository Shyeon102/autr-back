import os
from typing import Any, Optional

from autr.infra.bybit.client import BybitClient
from autr.strategies.breakout_volume_strategy import BreakoutVolumeStrategy
from autr.strategies.dual_timeframe_strategy import DualTimeframeStrategy
from autr.strategies.mean_reversion_strategy import MeanReversionStrategy
from autr.strategies.regime_trend_strategy import RegimeTrendStrategy
from autr.strategies.strategy_params import (
    BUILTIN_PRESETS,
    BreakoutVolumeParams,
    DualTimeframeParams,
    MeanReversionParams,
    RegimeTrendParams,
    apply_preset_overrides,
)


def build_strategy(
    strategy_name: str,
    client: BybitClient,
    trade_tracker_db,
    preset_overrides: Optional[dict[str, Any]] = None,
):
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()
    loop_seconds = int(os.getenv("STRATEGY_LOOP_SECONDS", "60"))
    overrides = dict(preset_overrides or {})
    overrides.setdefault("symbol", symbol)

    if strategy_name == "breakout_volume":
        params = BreakoutVolumeParams(symbol=symbol, loop_seconds=loop_seconds)
        apply_preset_overrides(params, overrides)
        return BreakoutVolumeStrategy(client, trade_tracker_db, params=params)

    if strategy_name == "mean_reversion":
        params = MeanReversionParams(symbol=symbol, loop_seconds=loop_seconds)
        apply_preset_overrides(params, overrides)
        return MeanReversionStrategy(client, trade_tracker_db, params=params)

    if strategy_name == "dual_timeframe":
        params = DualTimeframeParams(symbol=symbol, loop_seconds=loop_seconds)
        apply_preset_overrides(params, overrides)
        return DualTimeframeStrategy(client, trade_tracker_db, params=params)

    params = RegimeTrendParams(
        symbol=symbol,
        interval=os.getenv("STRATEGY_INTERVAL", "15"),
        lookback_bars=int(os.getenv("STRATEGY_LOOKBACK_BARS", "260")),
        ema_fast_period=int(os.getenv("STRATEGY_EMA_FAST", "50")),
        ema_slow_period=int(os.getenv("STRATEGY_EMA_SLOW", "200")),
        min_trend_gap_pct=float(os.getenv("STRATEGY_MIN_TREND_GAP_PCT", "0.001")),
        atr_period=int(os.getenv("STRATEGY_ATR_PERIOD", "14")),
        initial_stop_atr_mult=float(os.getenv("STRATEGY_INITIAL_STOP_ATR_MULT", "2.5")),
        trailing_stop_atr_mult=float(os.getenv("STRATEGY_TRAILING_STOP_ATR_MULT", "3.0")),
        loop_seconds=loop_seconds,
        cooldown_bars=int(os.getenv("STRATEGY_COOLDOWN_BARS", "2")),
    )
    apply_preset_overrides(params, overrides)
    return RegimeTrendStrategy(client, trade_tracker_db, params=params)


async def seed_builtin_presets(trade_tracker_db) -> None:
    for (strategy_name, symbol), preset in BUILTIN_PRESETS.items():
        existing = await trade_tracker_db.get_strategy_preset(strategy_name, symbol)
        if existing is None:
            await trade_tracker_db.save_strategy_preset(strategy_name, symbol, preset)


async def load_preset_overrides(trade_tracker_db, strategy_name: str, symbol: str) -> dict[str, Any]:
    db_preset = await trade_tracker_db.get_strategy_preset(strategy_name, symbol)
    if isinstance(db_preset, dict):
        return db_preset
    return dict(BUILTIN_PRESETS.get((strategy_name, symbol), {}))
