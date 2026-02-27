"""
Collector Runner — Bybit 시계열 데이터 수집 + tick 큐 발행.

수집 완료 후 각 심볼의 최신 1분봉을 Redis queue:ticks:{symbol}에 발행.
Strategy Consumer가 이 큐를 구독해 시그널을 생성한다.
"""
import asyncio
import json
import os
import argparse
import logging
from typing import List

from dotenv import load_dotenv

from autr.infra.bybit.client import BybitClient
from autr.infra.db.quant_store import QuantSQLiteStore
from autr.infra.queue_keys import tick_queue
from autr.infra.redis import create_redis_adapter
from autr.ops.heartbeat import record_heartbeat
from autr.services.collector import QuantDataCollector

_HB_TTL = int(os.getenv("WATCHDOG_COLLECTOR_THRESHOLD_SEC", "120"))


def _parse_list(value: str, default: List[str]) -> List[str]:
    if not value:
        return default
    items = [x.strip() for x in value.split(",") if x.strip()]
    return items or default


def _latest_tick(store: QuantSQLiteStore, symbol: str) -> dict | None:
    """수집된 1분봉 최신 캔들을 반환."""
    rows = store.fetch_ohlcv(symbol, "spot", "1", limit=1)
    if not rows:
        return None
    r = dict(rows[0])
    return {
        "symbol": symbol,
        "ts": r["ts"],
        "open": r["open"],
        "high": r["high"],
        "low": r["low"],
        "close": r["close"],
        "volume": r["volume"],
    }


async def _async_main(args, symbols, timeframes, db_path, redis_url, max_workers):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger("quant-pipeline")

    client = BybitClient()
    store = QuantSQLiteStore(db_path)
    collector = QuantDataCollector(client, store)
    redis = create_redis_adapter(redis_url)

    logger.info("Pipeline started db=%s symbols=%s intervals=%s", db_path, symbols, timeframes)

    loop = asyncio.get_running_loop()

    while True:
        run_id = store.start_run()
        try:
            # 동기 수집 작업을 스레드풀에서 실행 (이벤트 루프 블로킹 방지)
            futures = [
                loop.run_in_executor(None, collector.collect_symbol, symbol, timeframes)
                for symbol in symbols
            ]
            await asyncio.gather(*futures)
            store.end_run(run_id, "success", "cycle completed")
            logger.info("Cycle completed")

            # 각 심볼의 최신 1분봉을 tick 큐에 발행
            for symbol in symbols:
                tick = _latest_tick(store, symbol)
                if tick:
                    await redis.queue_push(tick_queue(symbol), json.dumps(tick))
                    logger.debug("[Collector] tick 발행: %s close=%.4f", symbol, tick["close"])

            # Watchdog heartbeat 기록
            await record_heartbeat("collector", redis, ttl=_HB_TTL)

        except Exception as exc:
            store.end_run(run_id, "error", str(exc))
            logger.exception("Cycle failed: %s", exc)

        if args.once:
            break
        await asyncio.sleep(max(5, args.sleep))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bybit quant time-series ingestion pipeline")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--sleep", type=int, default=int(os.getenv("DATA_PIPELINE_SLEEP_SEC", "60")))
    args = parser.parse_args()

    symbols = _parse_list(
        os.getenv("DATA_PIPELINE_SYMBOLS", "BTCUSDT,XRPUSDT,SOLUSDT"),
        ["BTCUSDT", "XRPUSDT", "SOLUSDT"],
    )
    timeframes = _parse_list(
        os.getenv("DATA_PIPELINE_INTERVALS", "1,5,15,60"),
        ["1", "5", "15", "60"],
    )
    db_path = os.getenv("QUANT_DB_PATH", "./data/quant_timeseries.db")
    redis_url = os.getenv("REDIS_URL", "")
    max_workers = int(os.getenv("DATA_PIPELINE_WORKERS", "2"))

    asyncio.run(_async_main(args, symbols, timeframes, db_path, redis_url, max_workers))


if __name__ == "__main__":
    main()
