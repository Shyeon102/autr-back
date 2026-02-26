import os
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any

from autr.api.limiter import limiter
from autr.infra.db.factory import create_db
from autr.services.position_service import PositionService
from autr.strategies.strategy_params import BUILTIN_PRESETS, apply_preset_overrides

router = APIRouter()
logger = logging.getLogger(__name__)

# 단순 헤더 기반 API 키 보호
def require_api_key(request: Request):
    api_key = os.getenv("ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ADMIN_KEY or ADMIN_API_KEY is not configured")
    provided = request.headers.get("X-API-KEY")
    if provided != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True

# 글로벌 인스턴스 — DATABASE_URL 기반으로 SQLite 또는 PostgreSQL 선택
trade_tracker_db = create_db(os.getenv("DATABASE_URL", ""))
position_service = PositionService(trade_tracker_db)
SUPPORTED_ASSETS = ["BTCUSDT", "XRPUSDT", "SOLUSDT"]

# Pydantic models for request bodies
class OpenPositionRequest(BaseModel):
    symbol: str
    position_type: Literal['long', 'short']
    entry_price: float = Field(gt=0, description="Entry price must be positive")
    quantity: float = Field(gt=0, description="Quantity must be positive")
    dollar_amount: float = Field(gt=0, description="Dollar amount must be positive")

class ClosePositionRequest(BaseModel):
    position_id: str
    close_price: Optional[float] = None


class ManualOrderRequest(BaseModel):
    symbol: str = "BTCUSDT"
    side: Literal["Buy", "Sell"] = "Buy"
    qty: float = Field(default=0.001, gt=0, description="Order quantity must be positive")


class StrategyParamsUpdateRequest(BaseModel):
    params: dict


AVAILABLE_STRATEGIES = ["regime_trend", "breakout_volume", "mean_reversion", "dual_timeframe"]

class StrategyChangeRequest(BaseModel):
    strategy: str


class StrategyPresetSaveRequest(BaseModel):
    params: dict


class SymbolChangeRequest(BaseModel):
    symbol: str


@router.get("/auth/validate")
@limiter.limit("10/minute")
async def validate_auth(request: Request, _auth=Depends(require_api_key)):
    """API 키 유효성 검증 전용 엔드포인트 (부작용 없음)"""
    return {"valid": True}


def get_trading_client(request: Request):
    trading_client = getattr(request.app.state, "trading_client", None)
    if not trading_client:
        raise HTTPException(status_code=500, detail="Trading client not initialized")
    return trading_client


def get_trading_strategy(request: Request):
    trading_strategy = getattr(request.app.state, "trading_strategy", None)
    if not trading_strategy:
        raise HTTPException(status_code=500, detail="Trading strategy not initialized")
    return trading_strategy


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return bool(value)


def _normalize_param_values(current_params: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in updates.items():
        current_value = current_params.get(key)
        target_type = type(current_value)
        try:
            if target_type is bool:
                normalized[key] = _parse_bool(value)
            elif target_type is int:
                normalized[key] = int(value)
            elif target_type is float:
                normalized[key] = float(value)
            elif target_type is str:
                normalized[key] = str(value)
            else:
                normalized[key] = value
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"Invalid value for '{key}': {value}")
    return normalized


def _validate_strategy_name(strategy: Any) -> str:
    strategy_name = str(strategy or "").strip().lower()
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown strategy '{strategy_name}'. Available: {AVAILABLE_STRATEGIES}",
        )
    return strategy_name


def _validate_symbol(symbol: Any) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in SUPPORTED_ASSETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported symbol '{normalized_symbol}'. Supported: {SUPPORTED_ASSETS}",
        )
    return normalized_symbol


async def _load_preset_overrides(strategy_name: str, symbol: str) -> Dict[str, Any]:
    db_preset = await trade_tracker_db.get_strategy_preset(strategy_name, symbol)
    if isinstance(db_preset, dict):
        return db_preset
    return dict(BUILTIN_PRESETS.get((strategy_name, symbol), {}))


@router.get("/portfolio")
async def get_portfolio(request: Request):
    """포트폴리오 현황 조회 (실제 거래 모드)"""
    trading_client = get_trading_client(request)

    try:
        balance = await trading_client.get_balance()
        current_price = await trading_client.get_current_price()
        
        # API 키가 없는 경우 경고 메시지와 함께 빈 데이터 반환
        if not balance:
            return {
                "balances": {},
                "current_btc_price": current_price,
                "total_value_usd": 0,
                "timestamp": datetime.utcnow().isoformat(),
                "error": "API 키가 설정되지 않았습니다. 실제 잔고를 조회할 수 없습니다.",
                "live_trading": True,
                "authenticated": trading_client.authenticated
            }

        total_usd = 0
        for coin, data in balance.items():
            if coin == "BTC":
                total_usd += data["balance"] * current_price
            elif coin == "USDT":
                total_usd += data["balance"]

        portfolio_data = {
            "balances": balance,
            "current_btc_price": current_price,
            "total_value_usd": total_usd,
            "timestamp": datetime.utcnow().isoformat(),
            "live_trading": True,
            "authenticated": trading_client.authenticated,
            "max_trade_amount": trading_client.max_trade_amount
        }
        
        # 실제 잔고가 있는 경우에만 히스토리에 추가
        if balance:
            await trade_tracker_db.add_portfolio_snapshot(portfolio_data)
        
        # 수익률 통계 추가 (잔고가 없어도 기본값 반환)
        performance_stats = await trade_tracker_db.get_portfolio_performance()
        portfolio_data.update(performance_stats)
        
        # BTC 포지션 정보 추가
        btc_pnl = await trade_tracker_db.get_pnl("BTCUSDT", current_price)
        portfolio_data["btc_position"] = btc_pnl
        
        return portfolio_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades")
async def get_trade_history(limit: int = 50):
    """거래 내역 조회 (로컬 DB)"""
    try:
        trades = await trade_tracker_db.recent_trades(limit)
        return {"trades": trades}
    except Exception as e:
        # 거래내역 조회 실패 시 대시보드가 죽지 않도록 빈 목록을 반환
        return {"trades": [], "error": str(e)}


@router.get("/price/{symbol}")
async def get_price(request: Request, symbol: str = "BTCUSDT"):
    """특정 심볼 가격 조회"""
    trading_client = get_trading_client(request)

    try:
        price = await trading_client.get_current_price(symbol)
        return {"symbol": symbol, "price": price}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chart/{symbol}")
async def get_chart_data(
    request: Request, symbol: str = "BTCUSDT", interval: str = "1", limit: int = 100
):
    """차트 데이터 조회"""
    trading_client = get_trading_client(request)

    try:
        kline_data = await trading_client.get_kline_data(symbol, interval, limit)

        chart_data = []
        for kline in kline_data:
            chart_data.append(
                {
                    "timestamp": int(kline[0]),
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                    "volume": float(kline[5]),
                }
            )

        return {"symbol": symbol, "data": chart_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trading/start")
@limiter.limit("5/minute")
async def start_trading(request: Request, _auth=Depends(require_api_key)):
    """자동매매 활성화 (Queue Consumer 방식)"""
    import json as _json
    from autr.infra.queue_keys import trading_enabled_key
    redis = request.app.state.redis
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()

    already = await redis.get(trading_enabled_key(symbol))
    if already:
        return {"message": "Trading is already active", "symbol": symbol}

    await redis.set(trading_enabled_key(symbol), "1")
    logger.info("[API] 자동매매 활성화: %s", symbol)
    return {"message": "Auto-trading started", "symbol": symbol, "mode": "queue"}


@router.post("/trading/stop")
@limiter.limit("5/minute")
async def stop_trading(request: Request, _auth=Depends(require_api_key)):
    """자동매매 비활성화 (Queue Consumer 방식)"""
    from autr.infra.queue_keys import trading_enabled_key
    redis = request.app.state.redis
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()

    await redis.delete(trading_enabled_key(symbol))
    logger.info("[API] 자동매매 비활성화: %s", symbol)
    return {"message": "Auto-trading stopped", "symbol": symbol}


@router.get("/trading/status")
async def get_trading_status(request: Request):
    """자동매매 상태 조회 — Consumer가 기록한 Redis 상태 반환"""
    import json as _json
    from autr.infra.queue_keys import trading_enabled_key, strategy_state_key
    redis = request.app.state.redis
    symbol = os.getenv("STRATEGY_SYMBOL", "BTCUSDT").upper()

    enabled = bool(await redis.get(trading_enabled_key(symbol)))
    state_raw = await redis.get(strategy_state_key(symbol))

    if state_raw:
        state = _json.loads(state_raw)
        state["trading_enabled"] = enabled
        return state

    # Consumer 미실행 시 fallback: in-memory 전략 객체의 파라미터 정보 반환
    trading_strategy = getattr(request.app.state, "trading_strategy", None)
    if trading_strategy:
        status = trading_strategy.get_strategy_status()
        status["trading_enabled"] = enabled
        status["is_active"] = False
        status["note"] = "strategy_consumer not running"
        return status

    return {
        "strategy": os.getenv("TRADING_STRATEGY", "regime_trend"),
        "symbol": symbol,
        "is_active": False,
        "trading_enabled": enabled,
        "note": "strategy_consumer not running",
    }


@router.post("/trading/params")
async def update_trading_params(
    request: Request,
    payload: StrategyParamsUpdateRequest,
    _auth=Depends(require_api_key),
):
    """전략 파라미터 업데이트"""
    strategy = get_trading_strategy(request)
    updates = payload.params or {}
    if not updates:
        raise HTTPException(status_code=422, detail="params is required")

    status = strategy.get_strategy_status()
    current_params = status.get("parameters", {})
    if not isinstance(current_params, dict):
        raise HTTPException(status_code=400, detail="Current strategy has no editable parameters")

    unknown_keys = [k for k in updates.keys() if k not in current_params]
    if unknown_keys:
        raise HTTPException(status_code=422, detail=f"Unknown params: {', '.join(unknown_keys)}")

    normalized = _normalize_param_values(current_params, updates)

    if not hasattr(strategy, "params"):
        raise HTTPException(status_code=400, detail="Current strategy has no editable params object")

    params_obj = getattr(strategy, "params")
    apply_preset_overrides(params_obj, normalized)

    if hasattr(strategy, "signal_engine"):
        strategy.signal_engine.params = params_obj

    new_status = strategy.get_strategy_status()
    new_params = new_status.get("parameters", {}) or {}
    strategy_name = new_status.get("strategy", "")
    symbol = str(new_params.get("symbol", "")).upper()
    if strategy_name and symbol:
        await trade_tracker_db.save_strategy_preset(strategy_name, symbol, new_params)

    return {"success": True, "parameters": new_params}


@router.post("/order")
@limiter.limit("10/minute")
async def place_manual_order(
    request: Request,
    order: Optional[ManualOrderRequest] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    qty: Optional[float] = None,
    _auth=Depends(require_api_key),
):
    """수동 주문"""
    trading_client = get_trading_client(request)

    try:
        payload_symbol = (order.symbol if order else symbol) or "BTCUSDT"
        payload_side = (order.side if order else side) or "Buy"
        payload_qty = float(order.qty if order else (qty if qty is not None else 0.001))

        if payload_side not in ("Buy", "Sell"):
            raise HTTPException(status_code=422, detail="side must be Buy or Sell")
        if payload_qty <= 0:
            raise HTTPException(status_code=422, detail="qty must be > 0")

        result = await trading_client.place_order(
            symbol=payload_symbol,
            side=payload_side,
            order_type="Market",
            qty=f"{payload_qty}",
        )
        if result.get("success"):
            current_price = await trading_client.get_current_price(payload_symbol)
            await trade_tracker_db.add_trade(
                payload_symbol,
                payload_side,
                payload_qty,
                current_price,
                signal="manual",
            )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/history")
async def get_portfolio_history(period: str = "7d"):
    """포트폴리오 히스토리 조회"""
    try:
        history = await trade_tracker_db.get_portfolio_history(period)
        return {"history": history, "period": period}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/performance")
async def get_portfolio_performance():
    """포트폴리오 수익률 통계"""
    try:
        stats = await trade_tracker_db.get_portfolio_performance()
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
async def get_positions():
    """현재 포지션 조회"""
    try:
        positions = await trade_tracker_db.get_current_positions()
        return {"positions": positions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
async def get_recent_signals(request: Request, limit: int = 5):
    """최근 거래 신호 조회 (기본 5개)"""
    try:
        rows = await trade_tracker_db.get_trade_signals(limit)
        normalized = []
        for row in rows:
            signal = str(row.get("signal") or "").lower()
            side = str(row.get("side") or "").lower()
            symbol = row.get("symbol", "BTCUSDT")
            price = float(row.get("price") or 0.0)

            if "buy" in signal or side == "buy":
                msg = f"BUY signal executed on {symbol} @ {price:.4f}"
                signal_type = "exec"
            elif "sell" in signal or side == "sell":
                msg = f"SELL signal executed on {symbol} @ {price:.4f}"
                signal_type = "exec"
            elif "hold" in signal:
                msg = f"HOLD signal on {symbol}: waiting for confirmation"
                signal_type = "signal"
            else:
                msg = f"Signal update on {symbol}: {row.get('signal', 'n/a')}"
                signal_type = "info"

            normalized.append(
                {
                    "timestamp": row.get("ts"),
                    "type": signal_type,
                    "message": msg,
                    "signal": row.get("signal"),
                    "side": row.get("side"),
                }
            )

        trading_strategy = getattr(request.app.state, "trading_strategy", None)
        if trading_strategy:
            status = trading_strategy.get_strategy_status()
            is_active = status.get("is_active", False)
            last_signal = status.get("last_signal") or "hold"
            last_reason = status.get("last_reason") or ""
            indicators = status.get("indicators") or {}

            # 항상 현재 전략 결정을 맨 위에 삽입
            signal_label = str(last_signal).upper()
            if is_active:
                if signal_label in ("BUY", "SELL"):
                    signal_type = "exec"
                    msg = f"Strategy decision: {signal_label}"
                    if last_reason:
                        msg += f" ({last_reason})"
                else:
                    signal_type = "signal"
                    msg = f"Strategy decision: HOLD"
                    if last_reason:
                        msg += f" — {last_reason}"
            else:
                signal_type = "warn"
                msg = "Strategy is STOPPED"

            normalized.insert(0, {
                "timestamp": datetime.utcnow().isoformat(),
                "type": signal_type,
                "message": msg,
            })

            # 인디케이터 정보 — 전략별 동적 포매팅
            strategy_key = status.get("strategy", "")
            if indicators:
                close_v = float(indicators.get("close") or 0.0)
                atr_v = float(indicators.get("atr") or 0.0)

                if strategy_key == "regime_trend":
                    ema_fast_v = float(indicators.get("ema_fast") or 0.0)
                    ema_slow_v = float(indicators.get("ema_slow") or 0.0)
                    gap_v = float(indicators.get("trend_gap_pct") or 0.0)
                    ind_msg = (
                        f"close={close_v:.2f}  "
                        f"EMA({ema_fast_v:.2f}/{ema_slow_v:.2f})  "
                        f"gap={gap_v * 100:.3f}%  "
                        f"ATR={atr_v:.2f}"
                    )

                elif strategy_key == "breakout_volume":
                    prior_high_v = float(indicators.get("prior_high") or 0.0)
                    volume_v = float(indicators.get("volume") or 0.0)
                    volume_ma_v = float(indicators.get("volume_ma") or 0.0)
                    vol_ratio = (volume_v / volume_ma_v) if volume_ma_v > 0 else 0.0
                    breakout = close_v > prior_high_v
                    ind_msg = (
                        f"close={close_v:.2f}  "
                        f"high={prior_high_v:.2f}  "
                        f"{'✓breakout' if breakout else '✗no-break'}  "
                        f"vol={vol_ratio:.2f}x MA  "
                        f"ATR={atr_v:.2f}"
                    )

                elif strategy_key == "mean_reversion":
                    rsi_v = float(indicators.get("rsi") or 0.0)
                    bb_upper_v = float(indicators.get("bb_upper") or 0.0)
                    bb_mid_v = float(indicators.get("bb_mid") or 0.0)
                    bb_lower_v = float(indicators.get("bb_lower") or 0.0)
                    ind_msg = (
                        f"close={close_v:.2f}  "
                        f"RSI={rsi_v:.1f}  "
                        f"BB[{bb_lower_v:.2f}/{bb_mid_v:.2f}/{bb_upper_v:.2f}]  "
                        f"ATR={atr_v:.2f}"
                    )

                elif strategy_key == "dual_timeframe":
                    rsi_v = float(indicators.get("rsi") or 0.0)
                    rsi_prev_v = float(indicators.get("rsi_prev") or 0.0)
                    ema_v = float(indicators.get("ema") or 0.0)
                    htf_bullish = bool(indicators.get("htf_bullish", 0))
                    htf_ema_f = float(indicators.get("htf_ema_fast") or 0.0)
                    htf_ema_s = float(indicators.get("htf_ema_slow") or 0.0)
                    ind_msg = (
                        f"close={close_v:.2f}  "
                        f"RSI={rsi_prev_v:.1f}→{rsi_v:.1f}  "
                        f"EMA={ema_v:.2f}  "
                        f"HTF={'↑bull' if htf_bullish else '↓bear'}({htf_ema_f:.0f}/{htf_ema_s:.0f})  "
                        f"ATR={atr_v:.2f}"
                    )

                else:
                    # 알 수 없는 전략: key=value 나열
                    parts = [f"{k}={float(v):.4g}" for k, v in indicators.items()]
                    ind_msg = "  ".join(parts)

                normalized.insert(1, {
                    "timestamp": datetime.utcnow().isoformat(),
                    "type": "info",
                    "message": ind_msg,
                })
            elif is_active:
                # 전략은 실행 중이지만 아직 첫 루프 미완료
                normalized.insert(1, {
                    "timestamp": datetime.utcnow().isoformat(),
                    "type": "info",
                    "message": "Fetching market data... (first cycle in progress)",
                })

        if not normalized:
            normalized = [
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "type": "warn",
                    "message": "No signals yet. Start the strategy to begin.",
                }
            ]
        return {"signals": normalized[:limit]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pnl/{symbol}")
async def get_pnl(request: Request, symbol: str = "BTCUSDT"):
    """특정 심볼의 손익 조회"""
    trading_client = get_trading_client(request)
    
    try:
        current_price = await trading_client.get_current_price(symbol)
        pnl_data = await trade_tracker_db.get_pnl(symbol, current_price)
        return pnl_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/multi-asset")
async def get_multi_asset_portfolio(request: Request):
    """다중 자산 포트폴리오 현황 조회"""
    trading_client = get_trading_client(request)
    
    try:
        current_prices = {}
        for symbol in SUPPORTED_ASSETS:
            try:
                price = await trading_client.get_current_price(symbol)
                current_prices[symbol] = price
            except Exception as e:
                logger.warning("가격 조회 실패 (%s): %s", symbol, e)
                current_prices[symbol] = 0.0

        current_positions = await trade_tracker_db.get_current_positions()
        assets = {}
        total_portfolio_value = 0.0
        total_invested = 0.0
        total_unrealized_pnl = 0.0

        for symbol in SUPPORTED_ASSETS:
            current_price = current_prices.get(symbol, 0.0)
            symbol_positions = current_positions.get(symbol, {})
            spot = symbol_positions.get("spot", {})
            qty = float(spot.get("total_quantity", 0.0))
            invested = float(spot.get("total_invested", 0.0))
            current_value = qty * current_price
            pnl = current_value - invested
            pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0

            total_portfolio_value += current_value
            total_invested += invested
            total_unrealized_pnl += pnl

            assets[symbol] = {
                "symbol": symbol,
                "total_quantity": qty,
                "total_invested": invested,
                "current_value": current_value,
                "average_price": float(spot.get("average_price", 0.0)),
                "current_price": current_price,
                "unrealized_pnl": pnl,
                "unrealized_pnl_percent": pnl_pct,
                "percentage_of_portfolio": 0.0,
            }

        if total_portfolio_value > 0:
            for symbol in assets:
                assets[symbol]["percentage_of_portfolio"] = (
                    assets[symbol]["current_value"] / total_portfolio_value * 100
                )

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "supported_assets": SUPPORTED_ASSETS,
            "assets": assets,
            "total_portfolio_value": total_portfolio_value,
            "total_invested": total_invested,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_unrealized_pnl_percent": (total_unrealized_pnl / total_invested * 100)
            if total_invested > 0
            else 0.0,
            "asset_count": len([a for a in assets.values() if a["total_quantity"] > 0]),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/allocation")
async def get_asset_allocation(request: Request):
    """자산 배분 현황 조회 (파이 차트용)"""
    try:
        portfolio = await get_multi_asset_portfolio(request)
        allocation = {
            symbol: data.get("percentage_of_portfolio", 0.0)
            for symbol, data in portfolio.get("assets", {}).items()
            if data.get("current_value", 0.0) > 0
        }
        return {"allocation": allocation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades/markers/{symbol}")
async def get_trade_markers(symbol: str = "BTCUSDT"):
    """특정 심볼의 거래 마커 데이터 조회 (차트용)"""
    try:
        markers = await trade_tracker_db.get_trade_markers(symbol)
        return {"symbol": symbol, "markers": markers}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Position Management Endpoints

@router.post("/positions/open")
async def open_position(
    request: Request,
    position_request: OpenPositionRequest,
    _auth=Depends(require_api_key),
):
    """새 포지션 열기"""
    try:
        trading_client = get_trading_client(request)
        
        result = await position_service.open_position(
            symbol=position_request.symbol,
            position_type=position_request.position_type,
            entry_price=position_request.entry_price,
            quantity=position_request.quantity,
            dollar_amount=position_request.dollar_amount,
            trading_client=trading_client
        )
        
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/positions/close")
async def close_position(
    request: Request,
    close_request: ClosePositionRequest,
    _auth=Depends(require_api_key),
):
    """포지션 닫기"""
    try:
        trading_client = get_trading_client(request)
        
        result = await position_service.close_position(
            position_id=close_request.position_id,
            close_price=close_request.close_price,
            trading_client=trading_client
        )
        
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions/open")
async def get_open_positions(symbol: Optional[str] = None):
    """열린 포지션 조회"""
    try:
        positions = await position_service.get_open_positions(symbol)
        return {"positions": positions, "count": len(positions)}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions/closed")
async def get_closed_positions(symbol: Optional[str] = None, limit: int = 50):
    """닫힌 포지션 조회"""
    try:
        positions = await position_service.get_closed_positions(symbol, limit)
        return {"positions": positions, "count": len(positions)}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions/summary")
async def get_positions_summary(symbol: Optional[str] = None):
    """포지션 요약 및 통계"""
    try:
        summary = await position_service.get_position_summary(symbol)
        return summary
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions/{position_id}")
async def get_position_details(position_id: str):
    """특정 포지션 상세 정보"""
    try:
        position = await position_service.get_position_by_id(position_id)
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        
        return {"position": position}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/positions/update-prices")
async def update_position_prices(request: Request, _auth=Depends(require_api_key)):
    """포지션 가격 업데이트 (실시간 가격 반영)"""
    try:
        trading_client = get_trading_client(request)
        
        # Get current prices for all symbols with open positions
        open_positions = await position_service.get_open_positions()
        symbols = list(set([pos["symbol"] for pos in open_positions]))
        
        price_updates = {}
        for symbol in symbols:
            try:
                current_price = await trading_client.get_current_price(symbol)
                price_updates[symbol] = current_price
            except Exception as e:
                logger.warning("Failed to get price for %s: %s", symbol, e)
        
        # Update position prices
        await position_service.update_position_prices(price_updates)
        
        return {
            "success": True,
            "updated_symbols": list(price_updates.keys()),
            "price_updates": price_updates
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/positions/auto-close")
async def auto_close_positions(
    request: Request,
    _auth=Depends(require_api_key),
    symbol: Optional[str] = None,
    max_loss_percent: Optional[float] = None,
    min_profit_percent: Optional[float] = None,
    max_days_open: Optional[int] = None
):
    """조건에 따른 자동 포지션 종료"""
    try:
        trading_client = get_trading_client(request)
        
        # Update prices first
        open_positions = await position_service.get_open_positions(symbol)
        symbols = list(set([pos["symbol"] for pos in open_positions]))
        price_updates = {}
        for item_symbol in symbols:
            try:
                current_price = await trading_client.get_current_price(item_symbol)
                price_updates[item_symbol] = current_price
            except Exception as e:
                logger.warning("Failed to get price for %s: %s", item_symbol, e)
        await position_service.update_position_prices(price_updates)
        
        # Auto-close positions based on criteria
        results = await position_service.auto_close_positions_by_criteria(
            symbol=symbol,
            max_loss_percent=max_loss_percent,
            min_profit_percent=min_profit_percent,
            max_days_open=max_days_open,
            trading_client=trading_client,
        )
        
        return {
            "success": True,
            "closed_positions": len(results),
            "results": results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 전략 관리 ──────────────────────────────────────────────────────────────

@router.get("/trading/strategies")
async def list_strategies(request: Request):
    """사용 가능한 전략 목록 및 현재 선택된 전략 반환"""
    trading_strategy = getattr(request.app.state, "trading_strategy", None)
    current = trading_strategy.get_strategy_status().get("strategy", "unknown") if trading_strategy else "unknown"
    strategies = {
        "regime_trend": {
            "name": "Regime Trend Engine",
            "description": "Dual EMA regime filter with ATR trailing stop. Best in trending markets.",
        },
        "breakout_volume": {
            "name": "Breakout + Volume",
            "description": "Enters on prior-high breakout confirmed by volume surge. Reduces false breakouts.",
        },
        "mean_reversion": {
            "name": "Mean Reversion",
            "description": "Buys when RSI is oversold and price is below Bollinger lower band. Best in ranging markets.",
        },
        "dual_timeframe": {
            "name": "Dual Timeframe",
            "description": "Uses 1H trend direction + 15M pullback entry. Filters noise with multi-TF confluence.",
        },
    }
    return {"current": current, "available": strategies}


@router.get("/trading/presets")
async def list_trading_presets(
    strategy: Optional[str] = None,
    _auth=Depends(require_api_key),
):
    """프리셋 목록 조회 (전략 필터 optional)."""
    strategy_name = _validate_strategy_name(strategy) if strategy else None
    presets = await trade_tracker_db.list_strategy_presets(strategy_name)
    return {"presets": presets, "count": len(presets)}


@router.get("/trading/presets/{strategy}/{symbol}")
async def get_trading_preset(
    strategy: str,
    symbol: str,
    _auth=Depends(require_api_key),
):
    """특정 strategy+symbol 프리셋 조회."""
    strategy_name = _validate_strategy_name(strategy)
    normalized_symbol = _validate_symbol(symbol)

    preset = await trade_tracker_db.get_strategy_preset(strategy_name, normalized_symbol)
    source = "db"
    if not isinstance(preset, dict):
        preset = BUILTIN_PRESETS.get((strategy_name, normalized_symbol))
        source = "builtin" if preset else "default"

    return {
        "strategy": strategy_name,
        "symbol": normalized_symbol,
        "params": preset or {},
        "source": source,
    }


@router.post("/trading/presets/{strategy}/{symbol}")
@limiter.limit("10/minute")
async def save_trading_preset(
    request: Request,
    strategy: str,
    symbol: str,
    payload: StrategyPresetSaveRequest,
    _auth=Depends(require_api_key),
):
    """strategy+symbol 프리셋 저장(upsert)."""
    strategy_name = _validate_strategy_name(strategy)
    normalized_symbol = _validate_symbol(symbol)
    params = payload.params or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="params must be an object")

    params["symbol"] = normalized_symbol
    await trade_tracker_db.save_strategy_preset(strategy_name, normalized_symbol, params)
    return {"success": True, "strategy": strategy_name, "symbol": normalized_symbol, "params": params}


@router.post("/trading/strategy")
@limiter.limit("5/minute")
async def change_strategy(
    request: Request,
    body: StrategyChangeRequest,
    _auth=Depends(require_api_key),
):
    """전략 변경 (자동매매가 멈춘 상태에서만 허용)"""
    trading_strategy = getattr(request.app.state, "trading_strategy", None)

    if trading_strategy and trading_strategy.is_active:
        raise HTTPException(
            status_code=400,
            detail="Stop trading before changing strategy.",
        )

    strategy_name = _validate_strategy_name(body.strategy)

    build_strategy = getattr(request.app.state, "build_strategy", None)
    if not build_strategy:
        raise HTTPException(status_code=500, detail="Strategy factory not available.")

    current_symbol = "BTCUSDT"
    if trading_strategy:
        current_status = trading_strategy.get_strategy_status()
        current_params = current_status.get("parameters", {}) or {}
        current_symbol = _validate_symbol(str(current_params.get("symbol", "BTCUSDT")))

    preset_overrides = await _load_preset_overrides(strategy_name, current_symbol)
    preset_overrides["symbol"] = current_symbol

    trading_client = get_trading_client(request)
    new_strategy = build_strategy(
        strategy_name,
        trading_client,
        preset_overrides=preset_overrides,
    )
    request.app.state.trading_strategy = new_strategy

    logger.info("Strategy changed to: %s (%s)", strategy_name, current_symbol)
    return {
        "success": True,
        "strategy": strategy_name,
        "status": new_strategy.get_strategy_status(),
    }


@router.post("/trading/symbol")
@limiter.limit("10/minute")
async def change_symbol(
    request: Request,
    body: SymbolChangeRequest,
    _auth=Depends(require_api_key),
):
    """코인 변경 (트레이딩 중지 상태에서만 허용)."""
    trading_strategy = getattr(request.app.state, "trading_strategy", None)
    if trading_strategy and trading_strategy.is_active:
        raise HTTPException(status_code=400, detail="Stop trading first.")

    strategy_status = trading_strategy.get_strategy_status() if trading_strategy else {}
    strategy_name = _validate_strategy_name(strategy_status.get("strategy", "regime_trend"))
    next_symbol = _validate_symbol(body.symbol)

    build_strategy = getattr(request.app.state, "build_strategy", None)
    if not build_strategy:
        raise HTTPException(status_code=500, detail="Strategy factory not available.")

    preset_overrides = await _load_preset_overrides(strategy_name, next_symbol)
    preset_overrides["symbol"] = next_symbol

    trading_client = get_trading_client(request)
    new_strategy = build_strategy(
        strategy_name,
        trading_client,
        preset_overrides=preset_overrides,
    )
    request.app.state.trading_strategy = new_strategy

    logger.info("Symbol changed to: %s (strategy=%s)", next_symbol, strategy_name)
    return {
        "success": True,
        "strategy": strategy_name,
        "symbol": next_symbol,
        "status": new_strategy.get_strategy_status(),
    }


# ── DB 조회 API ───────────────────────────────────────────────────────────

@router.get("/db/trades")
async def db_get_trades(limit: int = 100, _auth=Depends(require_api_key)):
    """trades 테이블 조회"""
    try:
        rows = await trade_tracker_db.recent_trades(limit)
        return {"trades": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/db/signal-log")
async def db_get_signal_log(
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    signal: Optional[str] = None,
    limit: int = 200,
    _auth=Depends(require_api_key),
):
    """signal_log 테이블 조회 (전략별 지표 이력)"""
    try:
        rows = await trade_tracker_db.get_signal_logs(
            strategy=strategy, symbol=symbol, limit=limit, signal_filter=signal
        )
        return {"logs": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/db/signal-log/stats")
async def db_signal_log_stats(strategy: Optional[str] = None, _auth=Depends(require_api_key)):
    """전략별 신호 통계 (HOLD/BUY/SELL 비율 등)"""
    try:
        stats = await trade_tracker_db.get_signal_log_stats(strategy=strategy)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/db/positions")
async def db_get_positions(
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 100,
    _auth=Depends(require_api_key),
):
    """positions 테이블 조회"""
    try:
        rows = await trade_tracker_db.get_positions(status=status, symbol=symbol, limit=limit)
        return {"positions": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/db/summary")
async def db_summary(_auth=Depends(require_api_key)):
    """DB 전체 현황 요약"""
    try:
        trades = await trade_tracker_db.recent_trades(1)
        signal_stats = await trade_tracker_db.get_signal_log_stats()
        positions_open = await trade_tracker_db.get_positions(status="open", limit=1000)
        positions_closed = await trade_tracker_db.get_positions(status="closed", limit=1000)
        return {
            "signal_log_total": signal_stats.get("total", 0),
            "signal_log_by_signal": signal_stats.get("by_signal", {}),
            "open_positions": len(positions_open),
            "closed_positions": len(positions_closed),
            "latest_trade_ts": trades[0].get("ts") if trades else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
