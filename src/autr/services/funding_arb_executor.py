"""
Funding Rate Arbitrage Executor.

아비트라지 포지션 실행:
- OPEN:  현물 매수(Spot Buy) + 선물 매도(Linear Sell) 동시 실행
- CLOSE: 현물 매도(Spot Sell) + 선물 매수(Linear Buy, reduceOnly) 동시 실행
- 한쪽만 체결되고 반대쪽 실패 시 롤백
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Union

from autr.infra.bybit.client import BybitClient
from autr.infra.notifier_discord import DiscordNotifier
from autr.infra.redis import InMemoryRedis, RealRedis
from autr.strategies.strategy_params import FundingArbParams

logger = logging.getLogger(__name__)

_LOCK_TTL = 15  # arb는 두 레그 → 락 TTL 넉넉히
_notifier = DiscordNotifier()


async def _notify(msg: str) -> None:
    _notifier.notify(msg)


class FundingArbExecutor:
    """현물 + 선물 페어 주문 실행기."""

    def __init__(
        self,
        client: BybitClient,
        redis: Union[InMemoryRedis, RealRedis],
        params: FundingArbParams | None = None,
    ):
        self.client = client
        self.redis = redis
        self.params = params or FundingArbParams()

    # ── 수량 계산 ──────────────────────────────────────────────────────

    def _calc_qty(self, price: float) -> str | None:
        """max_notional_usd 기반 수량 계산."""
        if price <= 0:
            return None
        raw_qty = self.params.max_notional_usd / price
        precision = self.client._get_symbol_precision(self.params.symbol)
        qty = math.floor(raw_qty * 10**precision) / 10**precision
        if qty <= 0:
            return None
        return f"{qty:.{precision}f}"

    # ── OPEN: Spot Buy + Perp Short ───────────────────────────────────

    async def open_position(self, spot_price: float, perp_price: float) -> dict:
        """아비트라지 포지션 진입 (현물 롱 + 선물 숏)."""
        symbol = self.params.symbol
        lock_key = f"lock:funding_arb:{symbol}"

        if not await self.redis.acquire_lock(lock_key, ttl=_LOCK_TTL):
            return {"success": False, "reason": "lock_failed"}

        try:
            # 레버리지 설정
            lev = str(self.params.leverage)
            await self.client.set_leverage(symbol, lev, lev)

            qty = self._calc_qty(spot_price)
            if not qty:
                return {"success": False, "reason": "qty_too_small"}

            logger.info("[FundingArb] OPEN 시작: %s qty=%s", symbol, qty)

            # 1) 선물 숏 먼저 (유동성 더 좋고, 실패 시 현물 안 건드림)
            perp_result = await self.client.place_linear_order(
                symbol=symbol, side="Sell", qty=qty,
            )
            if not perp_result.get("success"):
                err = perp_result.get("error", "unknown")
                logger.error("[FundingArb] 선물 숏 실패: %s", err)
                await _notify(f"❌ [{symbol}] FundingArb 선물 숏 실패: {err}")
                return {"success": False, "reason": "perp_short_failed", "error": err}

            # 2) 현물 매수
            spot_result = await self.client.place_order(
                symbol=symbol, side="Buy", qty=qty,
            )
            if not spot_result.get("success"):
                # 현물 실패 → 선물 롤백 (reduceOnly Buy)
                err = spot_result.get("error", "unknown")
                logger.error("[FundingArb] 현물 매수 실패, 선물 롤백: %s", err)
                rollback = await self.client.place_linear_order(
                    symbol=symbol, side="Buy", qty=qty, reduce_only=True,
                )
                await _notify(
                    f"❌ [{symbol}] FundingArb 현물 매수 실패: {err}\n"
                    f"🔄 선물 롤백: {'성공' if rollback.get('success') else '실패⚠️'}"
                )
                return {"success": False, "reason": "spot_buy_failed", "error": err}

            spot_filled = spot_result.get("filled_qty", float(qty))
            perp_filled = perp_result.get("filled_qty", float(qty))

            await _notify(
                f"✅ [{symbol}] FundingArb OPEN\n"
                f"현물 매수: {spot_filled:.6f} @ {spot_price:.2f}\n"
                f"선물 숏:   {perp_filled:.6f} @ {perp_price:.2f}"
            )

            logger.info(
                "[FundingArb] OPEN 완료: spot=%.6f perp=%.6f",
                spot_filled, perp_filled,
            )
            return {
                "success": True,
                "spot_qty": spot_filled,
                "perp_qty": perp_filled,
                "spot_order_id": spot_result.get("order_id"),
                "perp_order_id": perp_result.get("order_id"),
            }

        finally:
            await self.redis.release_lock(lock_key)

    # ── CLOSE: Spot Sell + Perp Buy (reduceOnly) ──────────────────────

    async def close_position(self, reason: str = "normal") -> dict:
        """아비트라지 포지션 청산 (현물 매도 + 선물 reduceOnly 매수)."""
        symbol = self.params.symbol
        lock_key = f"lock:funding_arb:{symbol}"

        if not await self.redis.acquire_lock(lock_key, ttl=_LOCK_TTL):
            return {"success": False, "reason": "lock_failed"}

        try:
            # 현재 선물 포지션 조회 → 수량 결정
            perp_pos = await self.client.get_linear_positions(symbol)
            perp_size = perp_pos.get("size", 0)

            if perp_size <= 0:
                logger.warning("[FundingArb] 선물 포지션 없음, 청산 스킵")
                return {"success": False, "reason": "no_perp_position"}

            precision = self.client._get_symbol_precision(symbol)
            qty = f"{perp_size:.{precision}f}"

            logger.info("[FundingArb] CLOSE 시작: %s qty=%s reason=%s", symbol, qty, reason)

            # 1) 선물 청산 (reduceOnly Buy)
            perp_result = await self.client.place_linear_order(
                symbol=symbol, side="Buy", qty=qty, reduce_only=True,
            )
            if not perp_result.get("success"):
                err = perp_result.get("error", "unknown")
                logger.error("[FundingArb] 선물 청산 실패: %s", err)
                await _notify(f"❌ [{symbol}] FundingArb 선물 청산 실패: {err}")
                return {"success": False, "reason": "perp_close_failed", "error": err}

            # 2) 현물 매도
            spot_result = await self.client.place_order(
                symbol=symbol, side="Sell", qty=qty,
            )
            if not spot_result.get("success"):
                err = spot_result.get("error", "unknown")
                logger.error("[FundingArb] 현물 매도 실패: %s", err)
                await _notify(
                    f"⚠️ [{symbol}] FundingArb 현물 매도 실패: {err}\n"
                    f"선물은 이미 청산됨 — 수동 확인 필요"
                )
                return {"success": False, "reason": "spot_sell_failed", "error": err}

            spot_filled = spot_result.get("filled_qty", float(qty))
            perp_filled = perp_result.get("filled_qty", float(qty))

            emoji = "🚨" if reason.startswith("emergency") else "🔒"
            await _notify(
                f"{emoji} [{symbol}] FundingArb CLOSE ({reason})\n"
                f"현물 매도: {spot_filled:.6f}\n"
                f"선물 청산: {perp_filled:.6f}"
            )

            logger.info(
                "[FundingArb] CLOSE 완료: spot=%.6f perp=%.6f reason=%s",
                spot_filled, perp_filled, reason,
            )
            return {
                "success": True,
                "spot_qty": spot_filled,
                "perp_qty": perp_filled,
                "reason": reason,
            }

        finally:
            await self.redis.release_lock(lock_key)

    # ── 상태 조회 (Consumer에서 사용) ─────────────────────────────────

    async def get_arb_status(self) -> dict:
        """현물 잔고 + 선물 포지션 + 지갑 정보 종합 조회."""
        symbol = self.params.symbol
        try:
            perp_pos, wallet, ticker = await asyncio.gather(
                self.client.get_linear_positions(symbol),
                self.client.get_wallet_balance(),
                self.client.get_linear_ticker(symbol),
            )
            perp_size = perp_pos.get("size", 0)
            in_position = perp_size > 0

            margin_ratio = None
            if wallet:
                total = wallet.get("total_equity", 0)
                avail = wallet.get("total_available_balance", 0)
                margin_ratio = avail / total if total > 0 else 1.0

            return {
                "in_position": in_position,
                "perp_size": perp_size,
                "perp_entry_price": perp_pos.get("entry_price", 0),
                "perp_unrealized_pnl": perp_pos.get("unrealized_pnl", 0),
                "funding_rate": ticker.get("funding_rate", 0),
                "next_funding_time": ticker.get("next_funding_time", 0),
                "mark_price": ticker.get("mark_price", 0),
                "margin_ratio": margin_ratio,
                "total_equity": wallet.get("total_equity", 0),
                "available_balance": wallet.get("total_available_balance", 0),
            }
        except Exception as e:
            logger.error("[FundingArb] 상태 조회 오류: %s", e)
            return {"in_position": False, "error": str(e)}
