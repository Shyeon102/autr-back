import os
import logging
import asyncio
from typing import Dict, Any, Optional

from pybit.unified_trading import HTTP
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

# .env 파일 명시적 로드
load_dotenv()

logger = logging.getLogger(__name__)


def _is_retryable_exception(exc: BaseException) -> bool:
    transient_types = (
        TimeoutError,
        OSError,
        ConnectionError,
        RequestsTimeout,
        RequestsConnectionError,
    )
    return isinstance(exc, transient_types)

class BybitClient:
    def __init__(self):
        """Bybit API 클라이언트 초기화"""
        self.api_key = os.getenv("BYBIT_API_KEY", "")
        self.api_secret = os.getenv("BYBIT_API_SECRET", "")
        self.testnet = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

        # 지원되는 암호화폐 심볼 정의
        self.supported_symbols = {
            "BTC": "BTCUSDT",
            "XRP": "XRPUSDT", 
            "SOL": "SOLUSDT"
        }
        
        # 안전 거래 설정
        self.max_trade_amount = float(os.getenv("MAX_TRADE_AMOUNT_USD", "30.0"))
        self.min_order_size = float(os.getenv("MIN_ORDER_SIZE_USD", "5.0"))
        self.max_position_percentage = float(os.getenv("MAX_POSITION_PERCENTAGE", "80.0"))
        self.stop_loss_percentage = float(os.getenv("STOP_LOSS_PERCENTAGE", "5.0"))
        
        # API 키가 있는 경우에만 인증 사용
        if self.api_key and self.api_secret:
            self.session = HTTP(
                testnet=self.testnet,
                api_key=self.api_key,
                api_secret=self.api_secret,
                timeout=10,
            )
            self.authenticated = True
            logger.info("Bybit client initialized (auth on, testnet=%s)", self.testnet)
        else:
            # 공개 API만 사용 (가격 조회 등)
            self.session = HTTP(testnet=self.testnet, timeout=10)
            self.authenticated = False
            logger.info("Bybit client initialized (public only, testnet=%s)", self.testnet)

    async def _call(self, fn, *args, **kwargs):
        """Run blocking pybit call in a thread with retries/backoff."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=16),
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
        ):
            with attempt:
                return await asyncio.to_thread(fn, *args, **kwargs)
    
    async def get_current_price(self, symbol: str = "BTCUSDT") -> float:
        """현재 암호화폐 가격 조회 (BTC, XRP, SOL 지원)"""
        try:
            response = await self._call(self.session.get_tickers, category="spot", symbol=symbol)
            if response["retCode"] == 0:
                price = float(response["result"]["list"][0]["lastPrice"])
                return price
            return 0.0
        except Exception as e:
            logger.warning("가격 조회 오류 (%s): %s", symbol, e)
            return 0.0
    
    async def get_multiple_prices(self, symbols: list = None) -> Dict[str, float]:
        """여러 암호화폐 가격 동시 조회"""
        if symbols is None:
            symbols = list(self.supported_symbols.values())
        
        prices = {}
        try:
            tasks = [self.get_current_price(symbol) for symbol in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, value in zip(symbols, results):
                prices[symbol] = value if isinstance(value, (int, float)) else 0.0
            return prices
        except Exception as e:
            logger.warning("다중 가격 조회 오류: %s", e)
            return {symbol: 0.0 for symbol in symbols}
    
    async def get_balance(self) -> Dict[str, Any]:
        """계정 잔고 조회"""
        if not self.authenticated:
            logger.warning("API 키가 설정되지 않아 잔고를 조회할 수 없습니다.")
            return {}
        
        try:
            response = await self._call(self.session.get_wallet_balance, accountType="UNIFIED")
            
            if response["retCode"] == 0 and response["result"]["list"]:
                balances = {}
                coin_list = response["result"]["list"][0]["coin"]
                
                for coin in coin_list:
                    try:
                        coin_name = coin.get("coin", "Unknown")
                        wallet_balance = coin.get("walletBalance", "0") or "0"
                        available_balance = coin.get("availableToWithdraw", "") or coin.get("equity", "0") or "0"
                        
                        # availableToWithdraw가 빈 문자열인 경우 equity 사용
                        if available_balance == "":
                            available_balance = coin.get("equity", "0") or "0"
                        
                        balance_float = float(wallet_balance)
                        available_float = float(available_balance)
                        
                        if balance_float > 0:
                            balances[coin_name] = {
                                "balance": balance_float,
                                "available": available_float
                            }
                    except (ValueError, TypeError) as e:
                        logger.warning("코인 %s 잔고 파싱 오류: %s", coin_name, e)
                        continue
                        
                logger.info("실제 잔고 조회 완료: %s개 코인", len(balances))
                return balances
            else:
                logger.warning("잔고 조회 응답 오류: %s", response)
                return {}
        except Exception as e:
            logger.warning("잔고 조회 오류: %s", e)
            return {}
    
    async def calculate_safe_order_size(self, symbol: str = "BTCUSDT", side: str = "Buy") -> Optional[str]:
        """안전한 주문 크기 계산 (다중 암호화폐 지원)"""
        try:
            current_price = await self.get_current_price(symbol)
            if current_price <= 0:
                return None
            
            balance = await self.get_balance()
            usdt_balance = balance.get("USDT", {}).get("available", 0)
            
            # 사용 가능한 금액 계산 (최대 거래 금액과 잔고 중 작은 값)
            available_amount = min(usdt_balance, self.max_trade_amount)
            
            if side == "Buy":
                # 매수: USDT 잔고 기준으로 계산
                if available_amount < self.min_order_size:
                    logger.warning(
                        "잔고 부족: $%.2f (최소 주문: $%.2f)",
                        available_amount,
                        self.min_order_size,
                    )
                    return None
                
                # 최대 포지션 비율 적용
                max_buy_amount = available_amount * (self.max_position_percentage / 100)
                crypto_qty = max_buy_amount / current_price
                
                # 최소 주문 크기 확인
                if max_buy_amount < self.min_order_size:
                    logger.warning("주문 금액이 너무 작음: $%.2f", max_buy_amount)
                    return None
                
                # 심볼별 정밀도 조정
                precision = self._get_symbol_precision(symbol)
                return f"{crypto_qty:.{precision}f}"
                
            else:  # Sell
                # 매도: 해당 암호화폐 잔고 기준으로 계산
                base_currency = symbol.replace("USDT", "")
                crypto_balance = balance.get(base_currency, {}).get("available", 0)
                if crypto_balance <= 0:
                    logger.warning("매도할 %s가 없습니다", base_currency)
                    return None
                
                # 전체 암호화폐 매도
                precision = self._get_symbol_precision(symbol)
                return f"{crypto_balance:.{precision}f}"
                
        except Exception as e:
            logger.warning("주문 크기 계산 오류 (%s): %s", symbol, e)
            return None
    
    def _get_symbol_precision(self, symbol: str) -> int:
        """심볼별 소수점 정밀도 반환"""
        precision_map = {
            "BTCUSDT": 6,
            "XRPUSDT": 1,  # XRP는 소수점 1자리
            "SOLUSDT": 3   # SOL은 소수점 3자리
        }
        return precision_map.get(symbol, 6)

    async def place_order(self, 
                         symbol: str = "BTCUSDT",
                         side: str = "Buy",  # Buy or Sell
                         order_type: str = "Market",
                         qty: Optional[str] = None) -> Dict[str, Any]:
        """주문 실행 (안전 장치 포함)"""
        # 주문 크기가 지정되지 않은 경우 안전한 크기 계산
        if qty is None:
            qty = await self.calculate_safe_order_size(symbol, side)
            if qty is None:
                return {"success": False, "error": "안전한 주문 크기를 계산할 수 없습니다"}
        
        if not self.authenticated:
            current_price = await self.get_current_price(symbol)
            order_value = float(qty) * current_price
            logger.warning(
                "실거래 주문 불가(키 없음): %s %s %s (약 $%.2f)",
                side,
                qty,
                symbol,
                order_value,
            )
            return {
                "success": False,
                "error": "API 키가 설정되지 않았습니다. .env 파일에 BYBIT_API_KEY와 BYBIT_API_SECRET을 설정하세요."
            }
        
        try:
            # 주문 전 최종 안전 검사
            current_price = await self.get_current_price(symbol)
            order_value = float(qty) * current_price
            
            if order_value > self.max_trade_amount:
                return {
                    "success": False, 
                    "error": f"주문 금액이 최대 한도를 초과합니다: ${order_value:.2f} > ${self.max_trade_amount}"
                }
            
            if order_value < self.min_order_size:
                return {
                    "success": False,
                    "error": f"주문 금액이 최소 한도보다 작습니다: ${order_value:.2f} < ${self.min_order_size}"
                }
            
            logger.info("주문 실행: %s %s %s ($%.2f)", side, qty, symbol, order_value)
            
            response = await self._call(
                self.session.place_order,
                category="spot",
                symbol=symbol,
                side=side,
                orderType=order_type,
                qty=qty,
            )
            
            if response["retCode"] == 0:
                logger.info("주문 성공: %s %s %s ($%.2f)", side, qty, symbol, order_value)
                return {
                    "success": True,
                    "order_id": response["result"]["orderId"],
                    "data": response["result"]
                }
            else:
                logger.warning("주문 실패: %s", response['retMsg'])
                return {"success": False, "error": response["retMsg"]}
                
        except Exception as e:
            logger.error("주문 실행 오류: %s", e)
            return {"success": False, "error": str(e)}
    
    async def get_order_history(self, symbol: str = "BTCUSDT", limit: int = 50) -> list:
        """주문 내역 조회"""
        if not self.authenticated:
            logger.warning("API 키가 설정되지 않아 거래 내역을 조회할 수 없습니다.")
            return []
        
        try:
            response = await self._call(
                self.session.get_order_history,
                category="spot",
                symbol=symbol,
                limit=limit,
            )
            
            if response["retCode"] == 0:
                return response["result"]["list"]
            else:
                logger.warning("주문 내역 조회 응답 오류: %s", response)
                return []
            
        except Exception as e:
            logger.warning("주문 내역 조회 오류: %s", e)
            return []
    
    async def get_kline_data(self, 
                           symbol: str = "BTCUSDT",
                           interval: str = "1",  # 1분봉
                           limit: int = 200) -> list:
        """캔들스틱 데이터 조회 (다중 암호화폐 지원)"""
        try:
            response = await self._call(
                self.session.get_kline,
                category="spot",
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            
            if response["retCode"] == 0:
                return response["result"]["list"]
            return []
            
        except Exception as e:
            logger.warning("캔들 데이터 조회 오류 (%s): %s", symbol, e)
            return []
    
    async def get_multiple_kline_data(self, symbols: list = None, interval: str = "1", limit: int = 200) -> Dict[str, list]:
        """여러 암호화폐의 캔들스틱 데이터 동시 조회"""
        if symbols is None:
            symbols = list(self.supported_symbols.values())
        
        kline_data = {}
        try:
            tasks = [self.get_kline_data(symbol, interval, limit) for symbol in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, value in zip(symbols, results):
                kline_data[symbol] = value if isinstance(value, list) else []
            return kline_data
        except Exception as e:
            logger.warning("다중 캔들 데이터 조회 오류: %s", e)
            return {symbol: [] for symbol in symbols}
