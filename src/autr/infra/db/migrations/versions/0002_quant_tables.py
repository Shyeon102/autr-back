"""Add quant time-series tables (raw_ohlcv, feature_bar, etc.)

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-02

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_ohlcv (
        symbol TEXT NOT NULL,
        market_type TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        ts BIGINT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL NOT NULL,
        turnover REAL,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, market_type, timeframe, ts)
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_symbol_tf_ts ON raw_ohlcv(symbol, timeframe, ts)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_ticker (
        symbol TEXT NOT NULL,
        market_type TEXT NOT NULL,
        ts BIGINT NOT NULL,
        last_price REAL,
        mark_price REAL,
        index_price REAL,
        bid1 REAL,
        ask1 REAL,
        bid1_size REAL,
        ask1_size REAL,
        open_interest REAL,
        funding_rate REAL,
        raw_json TEXT,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, market_type, ts)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_orderbook_top (
        symbol TEXT NOT NULL,
        market_type TEXT NOT NULL,
        ts BIGINT NOT NULL,
        bid_price REAL,
        bid_size REAL,
        ask_price REAL,
        ask_size REAL,
        spread REAL,
        mid_price REAL,
        imbalance REAL,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, market_type, ts)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_public_trades (
        symbol TEXT NOT NULL,
        market_type TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        ts BIGINT NOT NULL,
        side TEXT,
        price REAL,
        size REAL,
        raw_json TEXT,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, market_type, trade_id)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_funding_rates (
        symbol TEXT NOT NULL,
        ts BIGINT NOT NULL,
        funding_rate REAL,
        mark_price REAL,
        raw_json TEXT,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, ts)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS raw_open_interest (
        symbol TEXT NOT NULL,
        interval_time TEXT NOT NULL,
        ts BIGINT NOT NULL,
        open_interest REAL,
        raw_json TEXT,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, interval_time, ts)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS feature_bar (
        symbol TEXT NOT NULL,
        market_type TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        ts BIGINT NOT NULL,
        close REAL NOT NULL,
        ret_1 REAL,
        ret_log_1 REAL,
        vol_20 REAL,
        atr_14 REAL,
        ema_fast_50 REAL,
        ema_slow_200 REAL,
        trend_gap_pct REAL,
        zscore_50 REAL,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, market_type, timeframe, ts)
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_feature_bar_symbol_tf_ts ON feature_bar(symbol, timeframe, ts)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id SERIAL PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at TIMESTAMPTZ,
        status TEXT NOT NULL,
        message TEXT
    )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pipeline_runs")
    op.execute("DROP TABLE IF EXISTS feature_bar")
    op.execute("DROP TABLE IF EXISTS raw_open_interest")
    op.execute("DROP TABLE IF EXISTS raw_funding_rates")
    op.execute("DROP TABLE IF EXISTS raw_public_trades")
    op.execute("DROP TABLE IF EXISTS raw_orderbook_top")
    op.execute("DROP TABLE IF EXISTS raw_ticker")
    op.execute("DROP TABLE IF EXISTS raw_ohlcv")
