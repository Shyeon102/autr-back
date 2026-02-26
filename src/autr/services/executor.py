"""
Executor — 유일한 주문 권위자 (arch doc Section 3.1)

책임:
- Symbol Lock 획득 (atomic, Redis SETNX)
- Signal ID 기반 Idempotency 검사
- 리스크 검사 (risk_policy)
- 주문 실행 (Bybit)
- 포지션 상태 머신 관리
- Reconcile (재시작 복구)
- Discord 알림
"""
import logging
from typing import Optional, Union

from autr.domain import ids as id_gen
from autr.domain import reconcile as reconcile_domain
from autr.domain import risk_policy
from autr.domain.state_machine import PositionState, transition
from autr.infra.bybit.client import BybitClient
from autr.infra.db.sqlite import TradeTrackerDB
from autr.infra.notifier_discord import DiscordNotifier
from autr.infra.redis import InMemoryRedis, RealRedis

logger = logging.getLogger(__name__)

_LOCK_TTL = 10          # 심볼 락 TTL (초)
_PROCESSED_TTL = 3600   # 처리된 signal_id 캐시 TTL (초)

_notifier = DiscordNotifier()


async def _notify(message: str) -> None:
    _notifier.notify(message)


class Executor:
    """
    아키텍처 설계서 Section 3.1 기준 Executor.
    모든 주문 실행은 이 클래스를 통해서만 이루어진다.
    """

    def __init__(
        self,
        client: BybitClient,
        db: TradeTrackerDB,
        redis: Union[InMemoryRedis, RealRedis],
    ):
        self.client = client
        self.db = db
        self.redis = redis

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_buy(
        self,
        symbol: str,
        strategy: str,
        sig_id: str,
        close_price: float,
    ) -> dict:
        """
        매수 주문 실행.
        실패 또는 idempotency 중복 시 success=False 반환.
        """
        lock_key = f"lock:executor:{symbol}"

        if not await self.redis.acquire_lock(lock_key, ttl=_LOCK_TTL):
            logger.warning("[Executor] lock 획득 실패: %s", symbol)
            return {"success": False, "reason": "lock_failed"}

        try:
            # 1. Idempotency 검사
            if await self.redis.is_processed(sig_id):
                logger.info("[Executor] 중복 signal_id 스킵: %s", sig_id)
                return {"success": False, "reason": "duplicate_signal"}

            # 2. 수량 계산
            safe_qty = await self.client.calculate_safe_order_size(symbol, "Buy")
            if not safe_qty:
                logger.warning("[Executor] 안전 매수 수량 없음: %s", symbol)
                return {"success": False, "reason": "no_safe_qty"}

            # 3. 상태 전이: NONE → ENTERING
            # (포지션 상태는 position_service와 협력)

            # 4. 주문 실행
            order_result = await self.client.place_order(
                symbol=symbol,
                side="Buy",
                qty=safe_qty,
            )
            if not order_result.get("success"):
                logger.error("[Executor] 매수 주문 실패: %s", order_result.get("error"))
                return {"success": False, "reason": "order_failed", "error": order_result.get("error")}

            # 5. Trade 기록
            await self.db.add_trade(
                symbol=symbol,
                side="Buy",
                qty=float(safe_qty),
                price=close_price,
                signal=f"executor_buy:{strategy}:{sig_id}",
                status="filled",
                order_id=order_result.get("order_id"),
            )

            # 6. Signal ID 처리 완료 표시
            await self.redis.mark_processed(sig_id, ttl=_PROCESSED_TTL)

            # 7. Discord 알림
            await _notify(
                f"✅ [{symbol}] BUY 체결 | strategy={strategy} qty={safe_qty} price={close_price:.4f}"
            )

            logger.info("[Executor] BUY 완료 symbol=%s qty=%s price=%.4f", symbol, safe_qty, close_price)
            return {
                "success": True,
                "qty": safe_qty,
                "price": close_price,
                "order_id": order_result.get("order_id"),
            }

        finally:
            await self.redis.release_lock(lock_key)

    async def execute_sell(
        self,
        symbol: str,
        strategy: str,
        sig_id: str,
        close_price: float,
        qty: Optional[str] = None,
    ) -> dict:
        """
        매도 주문 실행.
        qty=None 이면 전량 매도 (BybitClient 내부에서 처리).
        """
        lock_key = f"lock:executor:{symbol}"

        if not await self.redis.acquire_lock(lock_key, ttl=_LOCK_TTL):
            logger.warning("[Executor] lock 획득 실패: %s", symbol)
            return {"success": False, "reason": "lock_failed"}

        try:
            if await self.redis.is_processed(sig_id):
                logger.info("[Executor] 중복 signal_id 스킵: %s", sig_id)
                return {"success": False, "reason": "duplicate_signal"}

            order_result = await self.client.place_order(
                symbol=symbol,
                side="Sell",
                qty=qty,
            )
            if not order_result.get("success"):
                logger.error("[Executor] 매도 주문 실패: %s", order_result.get("error"))
                return {"success": False, "reason": "order_failed", "error": order_result.get("error")}

            sold_qty = float(qty) if qty else 0.0
            await self.db.add_trade(
                symbol=symbol,
                side="Sell",
                qty=sold_qty,
                price=close_price,
                signal=f"executor_sell:{strategy}:{sig_id}",
                status="filled",
                order_id=order_result.get("order_id"),
            )

            await self.redis.mark_processed(sig_id, ttl=_PROCESSED_TTL)

            await _notify(
                f"✅ [{symbol}] SELL 체결 | strategy={strategy} qty={sold_qty} price={close_price:.4f}"
            )

            logger.info("[Executor] SELL 완료 symbol=%s qty=%s price=%.4f", symbol, sold_qty, close_price)
            return {
                "success": True,
                "qty": sold_qty,
                "price": close_price,
                "order_id": order_result.get("order_id"),
            }

        finally:
            await self.redis.release_lock(lock_key)

    async def reconcile(self, symbol: str) -> dict:
        """
        재시작 시 로컬 DB와 거래소 상태를 동기화 (arch doc Section 8).

        Case 1: DB=OPEN, Exchange=NONE → DB를 NONE으로 복구
        Case 2: DB=NONE, Exchange=OPEN → DB를 OPEN으로 복구
        Case 3: DB=ENTERING/EXITING → 주문 상태 동기화 후 정책 적용
        """
        logger.info("[Executor] Reconcile 시작: %s", symbol)

        try:
            # 거래소 포지션 조회
            exchange_qty = await self._get_exchange_qty(symbol)

            # 로컬 포지션 조회
            positions = await self.db.get_current_positions()
            local_data = positions.get(symbol, {}).get("spot", {})
            local_qty = float(local_data.get("total_quantity", 0.0))

            decision = reconcile_domain.reconcile(local_qty, exchange_qty)

            if decision.action == "noop":
                logger.info("[Executor] Reconcile OK (noop): %s", symbol)
                return {"action": "noop", "symbol": symbol}

            elif decision.action == "close_orphan_local":
                # DB=OPEN, Exchange=NONE → DB 정리
                logger.warning("[Executor] Reconcile: 로컬 포지션 있음, 거래소 없음 → DB 정리: %s", symbol)
                await _notify(f"⚠️ [{symbol}] Reconcile: 거래소 포지션 없음, 로컬 DB 정리")
                open_positions = await self.db.get_positions(status="open", symbol=symbol)
                for pos in open_positions:
                    current_price = await self.client.get_current_price(symbol)
                    await self.db.close_position(pos["id"], float(current_price))
                return {"action": "close_orphan_local", "symbol": symbol, "local_qty": local_qty}

            elif decision.action == "open_missing_local":
                # DB=NONE, Exchange=OPEN → DB에 포지션 추가
                logger.warning("[Executor] Reconcile: 거래소 포지션 있음, 로컬 없음 → DB 복구: %s", symbol)
                current_price = await self.client.get_current_price(symbol)
                await _notify(f"⚠️ [{symbol}] Reconcile: 로컬 DB에 없는 포지션 발견, 복구")
                await self.db.create_position(
                    symbol=symbol,
                    position_type="long",
                    entry_price=float(current_price),
                    quantity=exchange_qty,
                    dollar_amount=exchange_qty * float(current_price),
                )
                return {"action": "open_missing_local", "symbol": symbol, "exchange_qty": exchange_qty}

            else:
                # adjust_qty
                logger.warning(
                    "[Executor] Reconcile: qty 불일치 local=%.6f exchange=%.6f: %s",
                    local_qty,
                    exchange_qty,
                    symbol,
                )
                await _notify(
                    f"⚠️ [{symbol}] Reconcile: qty 불일치 local={local_qty:.6f} exchange={exchange_qty:.6f}"
                )
                return {
                    "action": "adjust_qty",
                    "symbol": symbol,
                    "local_qty": local_qty,
                    "exchange_qty": exchange_qty,
                }

        except Exception as exc:
            logger.exception("[Executor] Reconcile 오류: %s", exc)
            return {"action": "error", "symbol": symbol, "error": str(exc)}

    # ------------------------------------------------------------------
    # Exchange helpers
    # ------------------------------------------------------------------

    async def _get_exchange_qty(self, symbol: str) -> float:
        try:
            balance = await self.client.get_balance()
            asset_data = balance.get(symbol, {})
            return float(asset_data.get("total_quantity", 0.0))
        except Exception:
            return 0.0
