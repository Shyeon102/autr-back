"""
Regime Classifier — 시장 상태 분류 도메인 모듈.

OHLCV 데이터로 현재 시장을 4가지 레짐으로 분류:
  TRENDING_UP   — 강한 상승 추세
  TRENDING_DOWN — 강한 하락 추세
  RANGE         — 횡보/박스권
  VOLATILE      — 고변동성

분류 인디케이터:
  1. ADX (Average Directional Index) — 추세 강도
  2. EMA trend gap — 추세 방향
  3. 변동성 백분위수 (수익률 표준편차)
  4. Bollinger Band Width 백분위수 — 변동성 확장/수축
  5. 거래량 추세 — 추세/횡보 확인
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class RegimeType(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE = "range"
    VOLATILE = "volatile"


# 각 레짐에 적합한 전략 매핑
REGIME_STRATEGY_MAP: dict[RegimeType, Optional[str]] = {
    RegimeType.TRENDING_UP: "regime_trend",
    RegimeType.TRENDING_DOWN: None,           # 관망 (매수 안함)
    RegimeType.RANGE: "mean_reversion",
    RegimeType.VOLATILE: None,                # 관망 또는 보수적
}


@dataclass
class RegimeState:
    regime: str               # RegimeType.value
    confidence: float         # 0.0 ~ 1.0
    adx: float
    di_plus: float
    di_minus: float
    trend_gap_pct: float
    vol_percentile: float
    bb_width_percentile: float
    vol_ratio: float          # 최근 거래량 / 평균 거래량
    ts: int                   # Unix timestamp (ms)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def suggested_strategy(self) -> Optional[str]:
        return REGIME_STRATEGY_MAP.get(RegimeType(self.regime))


@dataclass
class RegimeParams:
    """Regime 분류기 파라미터."""
    # ADX
    adx_period: int = 14
    adx_trend_threshold: float = 25.0
    adx_range_threshold: float = 20.0

    # EMA trend gap
    ema_fast_period: int = 50
    ema_slow_period: int = 200
    trend_gap_threshold: float = 0.005

    # 변동성
    vol_lookback: int = 100
    vol_high_percentile: float = 0.80

    # Bollinger Band Width
    bb_period: int = 20
    bb_std: float = 2.0
    bb_width_high_percentile: float = 0.80

    # 거래량
    volume_ma_period: int = 20

    def to_dict(self) -> dict:
        return asdict(self)


class RegimeClassifier:
    """OHLCV DataFrame에서 시장 레짐을 판정."""

    def __init__(self, params: Optional[RegimeParams] = None):
        self.params = params or RegimeParams()

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    def compute(self, df: pd.DataFrame) -> Optional[RegimeState]:
        """
        OHLCV DataFrame → RegimeState.
        df에 최소 params.ema_slow_period + 50 행이 필요.
        """
        p = self.params
        min_rows = max(p.ema_slow_period, p.vol_lookback, p.adx_period * 3) + 10
        if len(df) < min_rows:
            logger.debug("[RegimeClassifier] 데이터 부족 (%d < %d)", len(df), min_rows)
            return None

        try:
            adx, di_plus, di_minus = self._compute_adx(df, p.adx_period)
            trend_gap = self._compute_trend_gap(df, p.ema_fast_period, p.ema_slow_period)
            vol_pct = self._compute_vol_percentile(df, p.vol_lookback)
            bb_width_pct = self._compute_bb_width_percentile(df, p.bb_period, p.bb_std, p.vol_lookback)
            vol_ratio = self._compute_volume_ratio(df, p.volume_ma_period)

            regime, confidence = self._classify(
                adx, di_plus, di_minus, trend_gap,
                vol_pct, bb_width_pct, vol_ratio,
            )

            last_ts = int(df.iloc[-1].get("ts", time.time() * 1000))

            return RegimeState(
                regime=regime.value,
                confidence=confidence,
                adx=round(adx, 2),
                di_plus=round(di_plus, 2),
                di_minus=round(di_minus, 2),
                trend_gap_pct=round(trend_gap, 6),
                vol_percentile=round(vol_pct, 4),
                bb_width_percentile=round(bb_width_pct, 4),
                vol_ratio=round(vol_ratio, 4),
                ts=last_ts,
            )
        except Exception:
            logger.exception("[RegimeClassifier] compute 오류")
            return None

    # ------------------------------------------------------------------ #
    # Classification logic
    # ------------------------------------------------------------------ #

    def _classify(
        self,
        adx: float,
        di_plus: float,
        di_minus: float,
        trend_gap: float,
        vol_pct: float,
        bb_width_pct: float,
        vol_ratio: float,
    ) -> tuple[RegimeType, float]:
        """레짐 판정 + 신뢰도 반환."""
        p = self.params

        # ── 1. TRENDING (최우선): ADX 강함 + EMA gap 확인 ──
        # 강한 추세는 변동성이 높아도 추세로 분류 (하락장에서 변동성은 자연스럽게 커짐)
        if adx >= p.adx_trend_threshold and abs(trend_gap) >= p.trend_gap_threshold:
            # 추세 방향 결정: DI+ vs DI- 와 trend_gap 부호 일치 시 신뢰도 높음
            if trend_gap > 0 and di_plus > di_minus:
                di_spread = (di_plus - di_minus) / max(di_plus + di_minus, 1)
                conf = min(1.0, 0.5 + di_spread + (adx - p.adx_trend_threshold) / 50)
                return RegimeType.TRENDING_UP, round(conf, 3)

            if trend_gap < 0 and di_minus > di_plus:
                di_spread = (di_minus - di_plus) / max(di_plus + di_minus, 1)
                conf = min(1.0, 0.5 + di_spread + (adx - p.adx_trend_threshold) / 50)
                return RegimeType.TRENDING_DOWN, round(conf, 3)

            # DI 방향과 trend_gap 불일치 → 약한 추세
            if trend_gap > 0:
                return RegimeType.TRENDING_UP, 0.4
            return RegimeType.TRENDING_DOWN, 0.4

        # ── 2. VOLATILE: 변동성 극단 (추세가 아닐 때만) ──
        if vol_pct >= p.vol_high_percentile and bb_width_pct >= p.bb_width_high_percentile:
            conf = min(1.0, (vol_pct + bb_width_pct) / 2.0)
            return RegimeType.VOLATILE, round(conf, 3)

        # ── 3. RANGE: 기본값 ──
        # ADX가 낮을수록 신뢰도 높음
        range_conf = min(1.0, 0.5 + (p.adx_trend_threshold - adx) / 50)
        return RegimeType.RANGE, round(max(0.3, range_conf), 3)

    # ------------------------------------------------------------------ #
    # Indicator calculations
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int) -> tuple[float, float, float]:
        """ADX, DI+, DI- 계산 (Wilder smoothing)."""
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        n = len(high)
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)

        for i in range(1, n):
            h_diff = high[i] - high[i - 1]
            l_diff = low[i - 1] - low[i]

            plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
            minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )

        # Wilder smoothing
        atr = np.zeros(n)
        smooth_plus = np.zeros(n)
        smooth_minus = np.zeros(n)

        atr[period] = np.mean(tr[1 : period + 1])
        smooth_plus[period] = np.mean(plus_dm[1 : period + 1])
        smooth_minus[period] = np.mean(minus_dm[1 : period + 1])

        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            smooth_plus[i] = (smooth_plus[i - 1] * (period - 1) + plus_dm[i]) / period
            smooth_minus[i] = (smooth_minus[i - 1] * (period - 1) + minus_dm[i]) / period

        last_atr = atr[-1]
        if last_atr == 0:
            return 0.0, 0.0, 0.0

        di_plus = 100 * smooth_plus[-1] / last_atr
        di_minus = 100 * smooth_minus[-1] / last_atr

        # DX → ADX (Wilder smoothing)
        dx = np.zeros(n)
        for i in range(period, n):
            s_p = smooth_plus[i]
            s_m = smooth_minus[i]
            denom = s_p + s_m
            dx[i] = 100 * abs(s_p - s_m) / denom if denom > 0 else 0.0

        adx_arr = np.zeros(n)
        start = period * 2
        if start < n:
            adx_arr[start] = np.mean(dx[period : start + 1])
            for i in range(start + 1, n):
                adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period

        return float(adx_arr[-1]), float(di_plus), float(di_minus)

    @staticmethod
    def _compute_trend_gap(df: pd.DataFrame, fast: int, slow: int) -> float:
        """EMA fast-slow gap 비율 (마지막 행)."""
        close = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        last_slow = ema_slow.iloc[-1]
        if last_slow == 0:
            return 0.0
        return float((ema_fast.iloc[-1] - last_slow) / last_slow)

    @staticmethod
    def _compute_vol_percentile(df: pd.DataFrame, lookback: int) -> float:
        """최근 수익률 변동성의 lookback 윈도우 내 백분위수."""
        returns = df["close"].pct_change().dropna()
        if len(returns) < lookback:
            return 0.5
        vol_series = returns.rolling(20).std().dropna()
        if vol_series.empty:
            return 0.5
        recent = vol_series.iloc[-lookback:]
        current_vol = recent.iloc[-1]
        return float((recent < current_vol).sum() / len(recent))

    @staticmethod
    def _compute_bb_width_percentile(
        df: pd.DataFrame, period: int, std_mult: float, lookback: int,
    ) -> float:
        """Bollinger Band Width의 lookback 윈도우 내 백분위수."""
        close = df["close"]
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        bb_width = (2 * std_mult * std) / sma
        bb_width = bb_width.dropna()
        if len(bb_width) < lookback:
            return 0.5
        recent = bb_width.iloc[-lookback:]
        current = recent.iloc[-1]
        return float((recent < current).sum() / len(recent))

    @staticmethod
    def _compute_volume_ratio(df: pd.DataFrame, ma_period: int) -> float:
        """최근 거래량 / 거래량 이동평균."""
        if "volume" not in df.columns:
            return 1.0
        vol = df["volume"]
        vol_ma = vol.rolling(ma_period).mean()
        last_ma = vol_ma.iloc[-1]
        if last_ma == 0 or pd.isna(last_ma):
            return 1.0
        return float(vol.iloc[-1] / last_ma)
