"""
DB 팩토리 — DATABASE_URL 기반으로 SQLite 또는 PostgreSQL 인스턴스 반환.

사용법:
    from autr.infra.db.factory import create_db
    db = create_db(os.getenv("DATABASE_URL", ""))

규칙:
    - DATABASE_URL이 "postgresql://" 또는 "postgres://" 로 시작 → PostgresDB
    - 그 외 (sqlite:///.., 빈 문자열 포함) → TradeTrackerDB (SQLite)
"""
import os
from typing import Union


def create_db(
    database_url: str = "",
) -> Union["TradeTrackerDB", "PostgresDB"]:  # type: ignore[name-defined]
    """
    DATABASE_URL에 맞는 DB 인스턴스를 반환한다.
    두 클래스는 완전히 동일한 async 인터페이스를 제공한다.
    """
    url = database_url or os.getenv("DATABASE_URL", "")

    if url.startswith("postgresql://") or url.startswith("postgres://"):
        from autr.infra.db.postgres import PostgresDB
        return PostgresDB(url)

    # SQLite fallback
    # "sqlite:///./data/trades.db" → "./data/trades.db"
    if url.startswith("sqlite:///"):
        db_path = url[len("sqlite:///"):]
    elif url:
        db_path = url  # 그대로 경로로 취급
    else:
        db_path = "./data/trades.db"

    from autr.infra.db.sqlite import TradeTrackerDB
    return TradeTrackerDB(db_path)
