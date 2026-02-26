#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd)/src:$(pwd)/backend:${PYTHONPATH:-}"
python - <<'PY'
import asyncio

from autr.infra.legacy import ensure_legacy_backend_on_path
from autr.services.executor import PositionService

ensure_legacy_backend_on_path()
from api.routes import trade_tracker_db


async def main():
    service = PositionService(trade_tracker_db)
    summary = await service.get_position_summary()
    print(summary)


asyncio.run(main())
PY
