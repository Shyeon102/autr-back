#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd)/src:$(pwd)/backend:${PYTHONPATH:-}"
python - <<'PY'
import asyncio
from autr.infra.legacy import ensure_legacy_backend_on_path

ensure_legacy_backend_on_path()
from models.trade_tracker_db import TradeTrackerDB


async def main():
    db = TradeTrackerDB()
    await db._init()
    print("sqlite schema initialized")


asyncio.run(main())
PY
