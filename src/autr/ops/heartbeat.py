"""
Heartbeat helpers.

서비스별 heartbeat를 Redis에 기록한다.
키: hb:{service_name}  (TTL = ttl 초, 기본 90초)

Watchdog는 이 키가 만료되면 서비스 이상으로 판단한다.
"""
import logging
from datetime import datetime, timezone
from typing import Union

from autr.infra.redis import InMemoryRedis, RealRedis

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 90  # Watchdog 기준보다 충분히 긴 값


async def record_heartbeat(
    service_name: str,
    redis: Union[InMemoryRedis, RealRedis],
    ttl: int = _DEFAULT_TTL,
) -> None:
    """
    Redis에 heartbeat 기록.
    Watchdog가 `hb:{service_name}` 키 존재 여부로 생존 판단.
    """
    try:
        await redis.set_heartbeat(service_name, ttl=ttl)
        logger.debug("[Heartbeat] %s 기록 완료 (ttl=%ds)", service_name, ttl)
    except Exception as exc:
        logger.warning("[Heartbeat] %s 기록 실패: %s", service_name, exc)


def heartbeat() -> dict[str, str]:
    """기존 HTTP /health 엔드포인트용 동기 응답."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
