"""
Funding Rate Arbitrage Consumer — 독립 Docker 컨테이너용.

Strategy Consumer + Executor를 하나로 합친 단일 루프:
  1. Bybit에서 실시간 펀딩비/가격 조회
  2. FundingArbSignalEngine.decide() → 시그널 판단
  3. FundingArbExecutor.open/close_position() → 즉시 실행
  4. Redis에 상태 기록 (API 대시보드용)

별도 큐 불필요 (단일 심볼, 단일 전략).
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from autr.infra.bybit.client import BybitClient
from autr.infra.queue_keys import funding_arb_enabled_key, funding_arb_state_key
from autr.infra.redis import create_redis_adapter
from autr.ops.heartbeat import record_heartbeat
from autr.services.funding_arb_executor import FundingArbExecutor
from autr.strategies.funding_arb_strategy import FundingArbSignalEngine
from autr.strategies.strategy_params import FundingArbParams

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("funding-arb")

_HB_TTL = int(os.getenv("WATCHDOG_FUNDING_ARB_THRESHOLD_SEC", "300"))
_HB_INTERVAL = 60
_STATE_TTL = 300


class FundingArbConsumer:
    """펀딩비 아비트라지 전체 루프."""

    def __init__(self):
        self.symbol = os.getenv("FUNDING_ARB_SYMBOL", "BTCUSDT").upper()
        self.params = FundingArbParams(symbol=self.symbol)

        # env 오버라이드
        if os.getenv("FUNDING_ARB_MAX_NOTIONAL"):
            self.params.max_notional_usd = float(os.getenv("FUNDING_ARB_MAX_NOTIONAL"))
        if os.getenv("FUNDING_ARB_ENTRY_THRESHOLD"):
            self.params.funding_rate_entry_threshold = float(os.getenv("FUNDING_ARB_ENTRY_THRESHOLD"))
        if os.getenv("FUNDING_ARB_LOOP_SECONDS"):
            self.params.loop_seconds = int(os.getenv("FUNDING_ARB_LOOP_SECONDS"))

        self.client = BybitClient()
        self.redis = create_redis_adapter(os.getenv("REDIS_URL", ""))
        self.engine = FundingArbSignalEngine(self.params)
        self.executor = FundingArbExecutor(
            client=self.client, redis=self.redis, params=self.params,
        )

        self._last_hb = 0.0
        self._cycle_count = 0

    # ── 메인 루프 ──────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            "[FundingArb] 시작 symbol=%s notional=$%.2f entry_thr=%.6f loop=%ds",
            self.symbol,
            self.params.max_notional_usd,
            self.params.funding_rate_entry_threshold,
            self.params.loop_seconds,
        )

        while True:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception("[FundingArb] 사이클 오류: %s", exc)
                await self._write_state(error=str(exc))
            await asyncio.sleep(self.params.loop_seconds)

    async def _cycle(self) -> None:
        self._cycle_count += 1
        now = time.monotonic()

        # heartbeat
        if now - self._last_hb >= _HB_INTERVAL:
            await record_heartbeat("funding_arb", self.redis, ttl=_HB_TTL)
            self._last_hb = now

        # 활성 여부 확인
        enabled = await self.redis.get(funding_arb_enabled_key(self.symbol))
        if enabled != "1":
            if self._cycle_count % 30 == 1:  # 30사이클마다 로그
                logger.info("[FundingArb] 비활성 상태 (trading:funding_arb:%s)", self.symbol)
            await self._write_state(enabled=False)
            return

        # 1) 실시간 데이터 수집
        ticker = await self.client.get_linear_ticker(self.symbol)
        if not ticker:
            logger.warning("[FundingArb] 티커 조회 실패")
            return

        spot_price = await self.client.get_current_price(self.symbol)
        perp_price = ticker.get("mark_price", 0)
        current_funding = ticker.get("funding_rate", 0)

        # 최근 펀딩비 이력 (현재 + 과거)
        funding_rates = [current_funding]
        # TODO: DB에서 과거 펀딩비 이력 추가 가능
        # 현재는 실시간 값만 사용 (lookback=1 효과)

        # 2) 포지션 상태 확인
        arb_status = await self.executor.get_arb_status()
        in_position = arb_status.get("in_position", False)
        margin_ratio = arb_status.get("margin_ratio")

        # 3) 시그널 판단
        decision = self.engine.decide(
            funding_rates=funding_rates,
            spot_price=spot_price,
            perp_price=perp_price,
            in_position=in_position,
            margin_ratio=margin_ratio,
        )

        logger.info(
            "[FundingArb] [%s] signal=%s reason=%s funding=%.6f basis=%.4f%% pos=%s",
            self.symbol, decision.signal, decision.reason,
            decision.funding_rate, decision.basis_pct, in_position,
        )

        # 4) 실행
        exec_result = None
        if decision.signal == "open":
            exec_result = await self.executor.open_position(spot_price, perp_price)
        elif decision.signal in ("close", "emergency_close"):
            exec_result = await self.executor.close_position(reason=decision.reason)

        if exec_result:
            logger.info("[FundingArb] 실행 결과: %s", exec_result)

        # 5) 상태 기록
        await self._write_state(
            enabled=True,
            decision=decision,
            arb_status=arb_status,
            exec_result=exec_result,
        )

    # ── Redis 상태 기록 ────────────────────────────────────────────────

    async def _write_state(
        self,
        enabled: bool = True,
        decision=None,
        arb_status: dict | None = None,
        exec_result: dict | None = None,
        error: str | None = None,
    ) -> None:
        state = {
            "symbol": self.symbol,
            "enabled": enabled,
            "cycle": self._cycle_count,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if decision:
            state["signal"] = decision.signal
            state["reason"] = decision.reason
            state["funding_rate"] = decision.funding_rate
            state["basis_pct"] = decision.basis_pct
            state["spot_price"] = decision.spot_price
            state["perp_price"] = decision.perp_price
        if arb_status:
            state["in_position"] = arb_status.get("in_position", False)
            state["perp_size"] = arb_status.get("perp_size", 0)
            state["margin_ratio"] = arb_status.get("margin_ratio")
            state["total_equity"] = arb_status.get("total_equity", 0)
        if exec_result:
            state["last_exec"] = {
                "success": exec_result.get("success"),
                "reason": exec_result.get("reason"),
            }
        if error:
            state["error"] = error

        key = funding_arb_state_key(self.symbol)
        await self.redis.set(key, json.dumps(state), ex=_STATE_TTL)


async def main() -> None:
    consumer = FundingArbConsumer()
    await consumer.run()


if __name__ == "__main__":
    asyncio.run(main())
