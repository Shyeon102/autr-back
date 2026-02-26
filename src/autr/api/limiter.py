"""Rate limiter 단일 인스턴스 — main.py와 routes.py가 공유"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
