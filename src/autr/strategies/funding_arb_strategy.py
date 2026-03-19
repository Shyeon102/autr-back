"""
Funding Rate Arbitrage Signal Engine.

양의 펀딩비 → 현물 롱 + 선물 숏 → 8시간마다 펀딩비 수취.
음의 펀딩비 또는 위험 조건 시 포지션 청산.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

from .strategy_params import FundingArbParams

logger = logging.getLogger(__name__)


@dataclass
class FundingArbDecision:
    signal: str             # "open" | "close" | "emergency_close" | "hold"
    reason: str
    funding_rate: float     # 평균 펀딩비
    spot_price: float
    perp_price: float
    basis_pct: float        # (perp - spot) / spot * 100

    def to_dict(self) -> dict:
        return asdict(self)


class FundingArbSignalEngine:
    def __init__(self, params: Optional[FundingArbParams] = None):
        self.params = params or FundingArbParams()

    def decide(
        self,
        funding_rates: list[float],
        spot_price: float,
        perp_price: float,
        in_position: bool,
        margin_ratio: Optional[float] = None,
    ) -> FundingArbDecision:
        """
        펀딩비 아비트라지 시그널 판정.

        Args:
            funding_rates: 최근 N회 펀딩비 리스트 (newest first)
            spot_price: 현물 가격
            perp_price: 선물 마크 가격
            in_position: 현재 아비트라지 포지션 보유 여부
            margin_ratio: 현재 마진 비율 (available / total). None이면 체크 안함
        """
        p = self.params

        # 펀딩비 평균 계산
        lookback = min(p.funding_rate_lookback, len(funding_rates))
        if lookback == 0:
            return FundingArbDecision(
                signal="hold", reason="no_funding_data",
                funding_rate=0, spot_price=spot_price,
                perp_price=perp_price, basis_pct=0,
            )
        avg_funding = sum(funding_rates[:lookback]) / lookback

        # Basis (현물-선물 가격차)
        basis_pct = (perp_price - spot_price) / spot_price * 100 if spot_price > 0 else 0

        base = dict(
            funding_rate=avg_funding,
            spot_price=spot_price,
            perp_price=perp_price,
            basis_pct=round(basis_pct, 4),
        )

        # ── 긴급 청산 조건 ──
        if in_position:
            # 1. Basis 과대 이탈
            if abs(basis_pct) > p.max_basis_divergence_pct:
                return FundingArbDecision(
                    signal="emergency_close",
                    reason=f"basis_divergence_{basis_pct:.4f}%",
                    **base,
                )
            # 2. 마진 부족
            if margin_ratio is not None and margin_ratio < p.margin_safety_ratio:
                return FundingArbDecision(
                    signal="emergency_close",
                    reason=f"margin_low_{margin_ratio:.4f}",
                    **base,
                )

        # ── 포지션 보유 중 ──
        if in_position:
            # 펀딩비가 exit threshold 미만 → 청산
            if avg_funding < p.funding_rate_exit_threshold:
                return FundingArbDecision(
                    signal="close",
                    reason=f"funding_below_exit_{avg_funding:.6f}",
                    **base,
                )
            # 펀딩비 음수 전환 → 즉시 청산
            if avg_funding < 0:
                return FundingArbDecision(
                    signal="close",
                    reason=f"funding_negative_{avg_funding:.6f}",
                    **base,
                )
            return FundingArbDecision(signal="hold", reason="collecting_funding", **base)

        # ── 포지션 없음: 진입 판단 ──
        if avg_funding >= p.funding_rate_entry_threshold:
            return FundingArbDecision(
                signal="open",
                reason=f"funding_above_entry_{avg_funding:.6f}",
                **base,
            )

        return FundingArbDecision(signal="hold", reason="waiting", **base)
