import os
import time
import requests

TARGET = os.getenv("WATCHDOG_TARGET", "http://api:8000/health")
INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "30"))


while True:
    try:
        resp = requests.get(TARGET, timeout=5)
        print(f"watchdog status={resp.status_code}")
    except Exception as exc:
        print(f"watchdog error={exc}")
    time.sleep(max(5, INTERVAL))
