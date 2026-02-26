"""Initial schema - signal_log, trades, positions, portfolio_snapshots, strategy_presets

Revision ID: 0001
Revises:
Create Date: 2026-02-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS signal_log (
        id SERIAL PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        symbol TEXT NOT NULL,
        strategy TEXT NOT NULL,
        signal TEXT NOT NULL,
        reason TEXT,
        close REAL,
        indicators_json TEXT,
        params_json TEXT,
        trailing_stop REAL,
        in_position INTEGER DEFAULT 0
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_ts ON signal_log(ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_strategy ON signal_log(strategy, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_strategy_signal ON signal_log(strategy, signal)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        signal TEXT,
        position_type TEXT DEFAULT 'spot',
        status TEXT DEFAULT 'filled',
        dollar_amount REAL,
        fees REAL DEFAULT 0,
        order_id TEXT
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_side_signal ON trades(side, signal)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_side ON trades(symbol, side)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS positions (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        position_type TEXT NOT NULL,
        entry_price REAL NOT NULL,
        quantity REAL NOT NULL,
        dollar_amount REAL NOT NULL,
        current_price REAL NOT NULL,
        unrealized_pnl REAL NOT NULL DEFAULT 0,
        unrealized_pnl_percent REAL NOT NULL DEFAULT 0,
        open_time TIMESTAMPTZ NOT NULL,
        close_time TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'open',
        entry_trade_id INTEGER,
        exit_trade_id INTEGER
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions(status, symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_positions_open_time ON positions(open_time DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_positions_close_time ON positions(close_time DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id SERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        total_value_usd REAL NOT NULL DEFAULT 0,
        current_btc_price REAL NOT NULL DEFAULT 0,
        balances_json TEXT,
        live_trading INTEGER NOT NULL DEFAULT 1
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(ts DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS strategy_presets (
        id SERIAL PRIMARY KEY,
        strategy TEXT NOT NULL,
        symbol TEXT NOT NULL,
        params_json TEXT NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(strategy, symbol)
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_strategy_presets_key ON strategy_presets(strategy, symbol)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS strategy_presets")
    op.execute("DROP TABLE IF EXISTS portfolio_snapshots")
    op.execute("DROP TABLE IF EXISTS positions")
    op.execute("DROP TABLE IF EXISTS trades")
    op.execute("DROP TABLE IF EXISTS signal_log")
