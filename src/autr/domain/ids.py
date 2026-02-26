from datetime import datetime, timezone
from uuid import uuid4


def signal_id(strategy: str, symbol: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = uuid4().hex[:8]
    return f"sig-{strategy.lower()}-{symbol.upper()}-{ts}-{suffix}"


def client_order_id(strategy: str, symbol: str, side: str) -> str:
    suffix = uuid4().hex[:10]
    return f"coid-{strategy.lower()}-{symbol.upper()}-{side.lower()}-{suffix}"
