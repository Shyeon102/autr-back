"""
Executor Consumer — signal 큐 구독 → 주문 실행.

흐름:
  queue:signals  (Strategy Consumer 발행)
        ↓  BRPOP
  Executor.execute_buy() / execute_sell()
        ↓
  Bybit 주문 + PostgreSQL 기록 + Discord 알림

설정 환경변수:
  REDIS_URL
  DATABASE_URL
  BYBIT_API_KEY / BYBIT_API_SECRET / BYBIT_TESTNET
"""
import asyncio
import json
import logging
import os

from dotenv import load_dotenv

from autr.infra.bybit.client import BybitClient
from autr.infra.db.factory import create_db
from autr.infra.queue_keys import SIGNAL_QUEUE
from autr.infra.redis import create_redis_adapter
from autr.ops.heartbeat import record_heartbeat
from autr.services.executor import Executor

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("executor-consumer")

_HB_TTL = int(os.getenv("WATCHDOG_EXECUTOR_THRESHOLD_SEC", "300"))
_HB_INTERVAL = 60   # heartbeat 갱신 주기 (초)
_POP_TIMEOUT = 30   # blocking pop 대기 시간 (초)


async def run(executor: Executor, redis) -> None:
    """Signal 큐에서 무한 소비."""
    import time
    last_hb = 0.0

    logger.info("[ExecutorConsumer] 시작, 큐=%s", SIGNAL_QUEUE)

    while True:
        try:
            raw = await redis.queue_pop(SIGNAL_QUEUE, timeout=_POP_TIMEOUT)

            # heartbeat 갱신 (주기적)
            now = time.monotonic()
            if now - last_hb >= _HB_INTERVAL:
                await record_heartbeat("executor", redis, ttl=_HB_TTL)
                last_hb = now

            if raw is None:
                continue

            signal_data = json.loads(raw)
            symbol = signal_data.get("symbol", "")
            strategy = signal_data.get("strategy", "unknown")
            side = signal_data.get("signal", "")  # "buy" | "sell"
            sig_id = signal_data.get("sig_id", "")
            close_price = float(signal_data.get("close_price", 0))

            if not symbol or not side or not sig_id:
                logger.warning("[ExecutorConsumer] 잘못된 signal payload: %s", signal_data)
                continue

            logger.info(
                "[ExecutorConsumer] signal 수신: symbol=%s side=%s sig_id=%s price=%.4f",
                symbol, side, sig_id, close_price,
            )

            if side == "buy":
                result = await executor.execute_buy(
                    symbol=symbol,
                    strategy=strategy,
                    sig_id=sig_id,
                    close_price=close_price,
                )
            elif side == "sell":
                result = await executor.execute_sell(
                    symbol=symbol,
                    strategy=strategy,
                    sig_id=sig_id,
                    close_price=close_price,
                )
            else:
                logger.warning("[ExecutorConsumer] 알 수 없는 side: %s", side)
                continue

            if result.get("success"):
                logger.info("[ExecutorConsumer] 실행 완료: %s", result)
            else:
                logger.warning("[ExecutorConsumer] 실행 실패: %s", result)

        except Exception as exc:
            logger.exception("[ExecutorConsumer] 오류: %s", exc)
            await asyncio.sleep(5)


async def main() -> None:
    redis_url = os.getenv("REDIS_URL", "")

    client = BybitClient()
    db = create_db(os.getenv("DATABASE_URL", ""))
    redis = create_redis_adapter(redis_url)

    executor = Executor(client=client, db=db, redis=redis)

    # 시작 시 reconcile 수행 (재시작 복구)
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()
    try:
        reconcile_result = await executor.reconcile(symbol)
        logger.info("[ExecutorConsumer] Reconcile 완료: %s", reconcile_result)
    except Exception as exc:
        logger.warning("[ExecutorConsumer] Reconcile 실패 (계속 진행): %s", exc)

    await run(executor, redis)


if __name__ == "__main__":
    asyncio.run(main())
