import os
import sqlite3
from typing import Iterable, Dict, Any, List


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS raw_ohlcv (
    symbol TEXT NOT NULL,
    market_type TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    turnover REAL,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, market_type, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_symbol_tf_ts ON raw_ohlcv(symbol, timeframe, ts);

CREATE TABLE IF NOT EXISTS raw_ticker (
    symbol TEXT NOT NULL,
    market_type TEXT NOT NULL,
    ts INTEGER NOT NULL,
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
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, market_type, ts)
);

CREATE TABLE IF NOT EXISTS raw_orderbook_top (
    symbol TEXT NOT NULL,
    market_type TEXT NOT NULL,
    ts INTEGER NOT NULL,
    bid_price REAL,
    bid_size REAL,
    ask_price REAL,
    ask_size REAL,
    spread REAL,
    mid_price REAL,
    imbalance REAL,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, market_type, ts)
);

CREATE TABLE IF NOT EXISTS raw_public_trades (
    symbol TEXT NOT NULL,
    market_type TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    side TEXT,
    price REAL,
    size REAL,
    raw_json TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, market_type, trade_id)
);

CREATE TABLE IF NOT EXISTS raw_funding_rates (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    funding_rate REAL,
    mark_price REAL,
    raw_json TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS raw_open_interest (
    symbol TEXT NOT NULL,
    interval_time TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open_interest REAL,
    raw_json TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, interval_time, ts)
);

CREATE TABLE IF NOT EXISTS feature_bar (
    symbol TEXT NOT NULL,
    market_type TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    close REAL NOT NULL,
    ret_1 REAL,
    ret_log_1 REAL,
    vol_20 REAL,
    atr_14 REAL,
    ema_fast_50 REAL,
    ema_slow_200 REAL,
    trend_gap_pct REAL,
    zscore_50 REAL,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, market_type, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_feature_bar_symbol_tf_ts ON feature_bar(symbol, timeframe, ts);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    status TEXT NOT NULL,
    message TEXT
);
"""


class QuantSQLiteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_parent_dir()
        self._init_schema()

    def _ensure_parent_dir(self):
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def insert_many(self, sql: str, rows: Iterable[Dict[str, Any]]):
        rows = list(rows)
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(sql, rows)
            conn.commit()

    def fetch_ohlcv(self, symbol: str, market_type: str, timeframe: str, limit: int = 400) -> List[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM raw_ohlcv
                WHERE symbol = ? AND market_type = ? AND timeframe = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (symbol, market_type, timeframe, limit),
            )
            return cur.fetchall()

    def start_run(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_runs(status, message) VALUES ('running', 'started')"
            )
            conn.commit()
            return int(cur.lastrowid)

    def end_run(self, run_id: int, status: str, message: str):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_runs
                SET status = ?, message = ?, ended_at = datetime('now')
                WHERE run_id = ?
                """,
                (status, message, run_id),
            )
            conn.commit()

