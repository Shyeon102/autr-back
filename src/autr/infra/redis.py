"""
Redis adapter: real Redis (redis.asyncio) or in-memory fallback.

Factory:
    adapter = create_redis_adapter(redis_url)

Lock:
    acquired = await adapter.acquire_lock("lock:executor:BTCUSDT", ttl=10)
    await adapter.release_lock("lock:executor:BTCUSDT")

Heartbeat:
    await adapter.set_heartbeat("collector", ttl=60)
    ts = await adapter.get_heartbeat("collector")

Queue (FIFO via LPUSH / BRPOP):
    await adapter.queue_push("queue:ticks:BTCUSDT", json_str)
    payload = await adapter.queue_pop("queue:ticks:BTCUSDT", timeout=30)
"""
import asyncio
import logging
import time
from typing import Optional, Union

logger = logging.getLogger(__name__)

_HB_PREFIX = "hb:"
_PROCESSED_TTL = 3600   # 1h idempotency cache
_QUEUE_MAX_LEN = 200    # 큐 최대 길이 (초과 시 오래된 항목 제거)


class InMemoryRedis:
    """In-memory fallback — TTL semantics + asyncio.Queue for pub/sub."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, Optional[float]]] = {}
        # asyncio.Queue per queue key (생성은 lazy)
        self._queues: dict[str, asyncio.Queue] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _expired(self, key: str) -> bool:
        if key not in self._store:
            return True
        _, exp = self._store[key]
        return exp is not None and time.monotonic() > exp

    def _evict(self, key: str) -> None:
        if self._expired(key):
            self._store.pop(key, None)

    def _get_queue(self, key: str) -> asyncio.Queue:
        if key not in self._queues:
            self._queues[key] = asyncio.Queue(maxsize=_QUEUE_MAX_LEN)
        return self._queues[key]

    # ------------------------------------------------------------------ #
    # Core KV API
    # ------------------------------------------------------------------ #

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        self._evict(key)
        entry = self._store.get(key)
        return entry[0] if entry else default

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        exp = (time.monotonic() + ex) if ex else None
        self._store[key] = (value, exp)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    # ------------------------------------------------------------------ #
    # Lock API (SETNX semantics)
    # ------------------------------------------------------------------ #

    async def acquire_lock(self, key: str, ttl: int = 10) -> bool:
        self._evict(key)
        if key in self._store:
            return False
        self._store[key] = ("1", time.monotonic() + ttl)
        return True

    async def release_lock(self, key: str) -> None:
        self._store.pop(key, None)

    # ------------------------------------------------------------------ #
    # Heartbeat API
    # ------------------------------------------------------------------ #

    async def set_heartbeat(self, service_name: str, ttl: int = 60) -> None:
        await self.set(f"{_HB_PREFIX}{service_name}", str(time.time()), ex=ttl)

    async def get_heartbeat(self, service_name: str) -> Optional[str]:
        return await self.get(f"{_HB_PREFIX}{service_name}")

    # ------------------------------------------------------------------ #
    # Idempotency helpers
    # ------------------------------------------------------------------ #

    async def mark_processed(self, sig_id: str, ttl: int = _PROCESSED_TTL) -> None:
        await self.set(f"processed:{sig_id}", "1", ex=ttl)

    async def is_processed(self, sig_id: str) -> bool:
        return bool(await self.get(f"processed:{sig_id}"))

    # ------------------------------------------------------------------ #
    # Queue API (FIFO)
    # ------------------------------------------------------------------ #

    async def queue_push(self, key: str, value: str) -> None:
        """메시지를 큐의 왼쪽(tail)에 삽입."""
        q = self._get_queue(key)
        if q.full():
            try:
                q.get_nowait()  # 가장 오래된 항목 제거
            except asyncio.QueueEmpty:
                pass
        await q.put(value)

    async def queue_pop(self, key: str, timeout: float = 30) -> Optional[str]:
        """
        큐에서 메시지를 꺼냄 (blocking).
        timeout 초 내 메시지 없으면 None 반환.
        """
        q = self._get_queue(key)
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class RealRedis:
    """Real Redis via redis.asyncio. Lazy connection."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as aioredis  # lazy import
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        client = await self._conn()
        val = await client.get(key)
        return val if val is not None else default

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        client = await self._conn()
        await client.set(key, value, ex=ex)

    async def delete(self, key: str) -> None:
        client = await self._conn()
        await client.delete(key)

    async def acquire_lock(self, key: str, ttl: int = 10) -> bool:
        client = await self._conn()
        result = await client.set(key, "1", nx=True, ex=ttl)
        return result is True

    async def release_lock(self, key: str) -> None:
        client = await self._conn()
        await client.delete(key)

    async def set_heartbeat(self, service_name: str, ttl: int = 60) -> None:
        client = await self._conn()
        await client.setex(f"{_HB_PREFIX}{service_name}", ttl, str(time.time()))

    async def get_heartbeat(self, service_name: str) -> Optional[str]:
        client = await self._conn()
        return await client.get(f"{_HB_PREFIX}{service_name}")

    async def mark_processed(self, sig_id: str, ttl: int = _PROCESSED_TTL) -> None:
        client = await self._conn()
        await client.setex(f"processed:{sig_id}", ttl, "1")

    async def is_processed(self, sig_id: str) -> bool:
        client = await self._conn()
        return bool(await client.get(f"processed:{sig_id}"))

    # ------------------------------------------------------------------ #
    # Queue API (LPUSH / BRPOP = FIFO)
    # ------------------------------------------------------------------ #

    async def queue_push(self, key: str, value: str) -> None:
        """메시지를 큐 왼쪽에 삽입 후 최대 길이로 trim."""
        client = await self._conn()
        await client.lpush(key, value)
        await client.ltrim(key, 0, _QUEUE_MAX_LEN - 1)

    async def queue_pop(self, key: str, timeout: float = 30) -> Optional[str]:
        """
        큐 오른쪽에서 blocking pop.
        timeout(초) 내 항목 없으면 None 반환.
        """
        client = await self._conn()
        result = await client.brpop(key, timeout=int(timeout))
        if result:
            return result[1]  # (key, value) 튜플
        return None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# Type alias for backward compat
RedisAdapter = Union[InMemoryRedis, RealRedis]


def create_redis_adapter(redis_url: str = "") -> Union[InMemoryRedis, RealRedis]:
    """
    Factory. Call once at startup.
    - REDIS_URL 있으면 RealRedis
    - 없으면 InMemoryRedis (개발/테스트)
    """
    if redis_url:
        logger.info("[Redis] 실제 Redis 연결: %s", redis_url)
        return RealRedis(redis_url)
    logger.info("[Redis] In-memory fallback 사용 (REDIS_URL 미설정)")
    return InMemoryRedis()
