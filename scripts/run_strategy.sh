#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd)/src:$(pwd)/backend:${PYTHONPATH:-}"
python - <<'PY'
import asyncio
import os

from autr.infra.bybit.client import BybitClient
from autr.infra.legacy import ensure_legacy_backend_on_path
from autr.services.strategy import build_strategy

ensure_legacy_backend_on_path()
from api.routes import trade_tracker_db


async def main():
    strategy_name = os.getenv("TRADING_STRATEGY", "regime_trend")
    strategy = build_strategy(strategy_name, BybitClient(), trade_tracker_db)
    await strategy.start_trading()


asyncio.run(main())
PY
