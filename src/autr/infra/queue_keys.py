"""
Redis 키 상수.

데이터 흐름:
  Collector → queue:ticks:{symbol}  → Strategy Consumer
  Strategy  → queue:signals         → Executor Consumer

제어 흐름 (API 서버 → Consumer):
  trading:enabled:{symbol}   — "1" 이면 거래 활성, 없으면 비활성
  state:strategy:{symbol}    — Consumer가 기록하는 전략 상태 JSON
  config:strategy:{symbol}   — 활성 전략 이름 (API 서버가 설정)
"""


def tick_queue(symbol: str) -> str:
    """1분봉 tick 큐 키. e.g. queue:ticks:BTCUSDT"""
    return f"queue:ticks:{symbol.upper()}"


def trading_enabled_key(symbol: str) -> str:
    """거래 활성 여부 키. e.g. trading:enabled:BTCUSDT"""
    return f"trading:enabled:{symbol.upper()}"


def strategy_state_key(symbol: str) -> str:
    """Consumer가 기록하는 전략 상태 키. e.g. state:strategy:BTCUSDT"""
    return f"state:strategy:{symbol.upper()}"


def active_strategy_key(symbol: str) -> str:
    """활성 전략 이름 키. e.g. config:strategy:BTCUSDT"""
    return f"config:strategy:{symbol.upper()}"


SIGNAL_QUEUE = "queue:signals"


def regime_state_key(symbol: str) -> str:
    """Regime 분류기 상태 키. e.g. state:regime:BTCUSDT"""
    return f"state:regime:{symbol.upper()}"


def auto_switch_key(symbol: str) -> str:
    """자동 전략 전환 활성 키. e.g. config:auto_switch:BTCUSDT"""
    return f"config:auto_switch:{symbol.upper()}"


def funding_arb_state_key(symbol: str) -> str:
    """펀딩비 아비트라지 상태 키. e.g. state:funding_arb:BTCUSDT"""
    return f"state:funding_arb:{symbol.upper()}"


def funding_arb_enabled_key(symbol: str) -> str:
    """펀딩비 아비트라지 활성 키. e.g. trading:funding_arb:BTCUSDT"""
    return f"trading:funding_arb:{symbol.upper()}"
