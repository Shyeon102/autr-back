"""
App lifecycle hooks.

on_startup:
  - Redis, BybitClient, DB 초기화
  - 내장 전략 프리셋 DB 시딩
  - app.state.trading_strategy 는 /trading/params 파라미터 관리 전용
    (실제 거래는 run_strategy_consumer 프로세스가 담당)

on_shutdown:
  - 리소스 정리
"""
import os
from fastapi import FastAPI

from autr.infra.bybit.client import BybitClient
from autr.infra.db.factory import create_db
from autr.infra.redis import create_redis_adapter
from autr.api.routes.trading import trade_tracker_db
from autr.services.strategy import build_strategy, load_preset_overrides, seed_builtin_presets


async def on_startup(app: FastAPI) -> None:
    # Redis
    app.state.redis = create_redis_adapter(os.getenv("REDIS_URL", ""))

    # Bybit Client (가격 조회, 잔고 등 API 엔드포인트용)
    app.state.trading_client = BybitClient()

    # DB (factory를 통해 SQLite/Postgres 선택)
    app.state.trade_tracker = trade_tracker_db

    strategy_name = os.getenv("TRADING_STRATEGY", "regime_trend").lower()
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()

    # 내장 프리셋 시딩
    await seed_builtin_presets(trade_tracker_db)

    # 전략 파라미터 로드 (/trading/params 엔드포인트용 in-memory 객체)
    # 실제 거래 루프는 run_strategy_consumer 프로세스가 처리
    preset_overrides = await load_preset_overrides(trade_tracker_db, strategy_name, symbol)
    preset_overrides.setdefault("symbol", symbol)

    app.state.trading_strategy = build_strategy(
        strategy_name,
        app.state.trading_client,
        trade_tracker_db,
        preset_overrides=preset_overrides,
    )
    app.state.build_strategy = build_strategy


async def on_shutdown(app: FastAPI) -> None:
    pass
