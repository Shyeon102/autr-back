"""
Watchdog — Heartbeat 모니터링 (arch doc Section 11)

감시 대상 heartbeat 키:
  hb:collector  — Collector 생존 확인
  hb:strategy   — Strategy 루프 생존 확인
  hb:executor   — Executor 마지막 동작 확인

기준: 키가 만료(없음)되면 "서비스 이상"으로 판단하고 Discord 알림.
서비스 재시작은 Docker restart 정책(unless-stopped)에 위임.

설정 환경변수:
  WATCHDOG_CHECK_INTERVAL_SEC  — 체크 주기 (기본 30초)
  WATCHDOG_HEARTBEAT_TTL_SEC   — heartbeat 만료 기준 (기본 120초)
  REDIS_URL
  DISCORD_WEBHOOK_URL
"""
import asyncio
import logging
import os
from typing import Union

from dotenv import load_dotenv

from autr.infra.notifier_discord import DiscordNotifier
from autr.infra.redis import InMemoryRedis, RealRedis, create_redis_adapter

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("watchdog")

_SERVICES = ["collector", "strategy", "executor"]
_CHECK_INTERVAL = int(os.getenv("WATCHDOG_CHECK_INTERVAL_SEC", "30"))

# 서비스별 최대 허용 무응답 시간 (TTL보다 약간 넉넉하게 설정)
_ALERT_THRESHOLD = {
    "collector": int(os.getenv("WATCHDOG_COLLECTOR_THRESHOLD_SEC", "120")),
    "strategy": int(os.getenv("WATCHDOG_STRATEGY_THRESHOLD_SEC", "180")),
    "executor": int(os.getenv("WATCHDOG_EXECUTOR_THRESHOLD_SEC", "300")),
}


class Watchdog:
    def __init__(
        self,
        redis: Union[InMemoryRedis, RealRedis],
        notifier: DiscordNotifier,
    ) -> None:
        self.redis = redis
        self.notifier = notifier
        # 중복 알림 방지: 마지막 알림 시각 (서비스별)
        self._last_alert: dict[str, float] = {}

    async def check_once(self) -> None:
        import time

        now = time.time()

        for service in _SERVICES:
            hb_val = await self.redis.get_heartbeat(service)

            if hb_val is None:
                # heartbeat 키 없음 → 서비스 이상
                threshold = _ALERT_THRESHOLD.get(service, 120)
                last = self._last_alert.get(service, 0)

                if now - last >= threshold:
                    msg = (
                        f"⚠️ [Watchdog] `{service}` heartbeat 없음 — "
                        f"서비스가 멈췄거나 재시작 중입니다. "
                        f"Docker restart 정책으로 복구 시도 중."
                    )
                    logger.warning(msg)
                    self.notifier.notify(msg)
                    self._last_alert[service] = now
            else:
                # heartbeat 있음 — 이전에 알림 보냈다면 복구 알림
                if service in self._last_alert and self._last_alert[service] > 0:
                    import time as _t
                    elapsed = now - self._last_alert[service]
                    msg = f"✅ [Watchdog] `{service}` 복구 확인 ({elapsed:.0f}초 후)"
                    logger.info(msg)
                    self.notifier.notify(msg)
                    self._last_alert[service] = 0  # 복구 후 초기화

    async def run(self) -> None:
        logger.info(
            "Watchdog 시작 — 서비스=%s 체크주기=%ds",
            _SERVICES,
            _CHECK_INTERVAL,
        )
        while True:
            try:
                await self.check_once()
            except Exception as exc:
                logger.exception("Watchdog 체크 오류: %s", exc)
            await asyncio.sleep(_CHECK_INTERVAL)


async def main() -> None:
    redis_url = os.getenv("REDIS_URL", "")
    redis = create_redis_adapter(redis_url)
    notifier = DiscordNotifier()

    watchdog = Watchdog(redis=redis, notifier=notifier)
    await watchdog.run()


if __name__ == "__main__":
    asyncio.run(main())
