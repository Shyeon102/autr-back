import os
from dataclasses import dataclass
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "AUTR Backend")
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    )

    # DB: sqlite:///./data/trades.db (dev 기본값) | postgresql://user:pass@host/db
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")

    # Redis: redis://localhost:6379/0 (없으면 in-memory fallback)
    redis_url: str = os.getenv("REDIS_URL", "")


def get_settings() -> Settings:
    return Settings()
