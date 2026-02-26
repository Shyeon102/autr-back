import os
import requests
from typing import Optional


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")

    def notify(self, message: str) -> bool:
        if not self.webhook_url:
            return False
        resp = requests.post(self.webhook_url, json={"content": message}, timeout=5)
        return 200 <= resp.status_code < 300
