import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    signal TEXT NOT NULL,
    reason TEXT,
    close REAL,
    indicators_json TEXT,
    params_json TEXT,
    trailing_stop REAL,
    in_position INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signal_log_ts ON signal_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_strategy ON signal_log(strategy, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_strategy_signal ON signal_log(strategy, signal);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
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
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_side_signal ON trades(side, signal);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_side ON trades(symbol, side);

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
    open_time TEXT NOT NULL,
    close_time TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    entry_trade_id INTEGER,
    exit_trade_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions(status, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open_time ON positions(open_time DESC);
CREATE INDEX IF NOT EXISTS idx_positions_close_time ON positions(close_time DESC);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    total_value_usd REAL NOT NULL DEFAULT 0,
    current_btc_price REAL NOT NULL DEFAULT 0,
    balances_json TEXT,
    live_trading INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(ts DESC);

CREATE TABLE IF NOT EXISTS strategy_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    params_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(strategy, symbol)
);

CREATE INDEX IF NOT EXISTS idx_strategy_presets_key ON strategy_presets(strategy, symbol);
"""


class TradeTrackerDB:
    """SQLite 기반 거래/포지션 저장소."""

    def __init__(self, db_path: str = "./data/trades.db"):
        self.db_path = db_path
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _ensure_db_dir(self):
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    async def _init(self):
        if self._initialized:
            return
        self._ensure_db_dir()
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(SCHEMA)
                # 기존 DB 마이그레이션: params_json 컬럼이 없으면 추가
                try:
                    await db.execute("SELECT params_json FROM signal_log LIMIT 1")
                except Exception:
                    await db.execute("ALTER TABLE signal_log ADD COLUMN params_json TEXT")
                await db.commit()
            self._initialized = True

    @staticmethod
    def _calc_pnl(position_type: str, entry_price: float, current_price: float, quantity: float) -> float:
        if position_type == "short":
            return (entry_price - current_price) * quantity
        return (current_price - entry_price) * quantity

    async def add_signal_log(
        self,
        symbol: str,
        strategy: str,
        signal: str,
        reason: str = "",
        indicators: Optional[Dict[str, float]] = None,
        trailing_stop: Optional[float] = None,
        in_position: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """매 전략 루프마다 신호 및 지표값 + 파라미터 기록 (퀀트 분석용)"""
        await self._init()
        close = float(indicators.get("close", 0.0)) if indicators else 0.0
        indicators_json = json.dumps(indicators or {}, ensure_ascii=True)
        params_json = json.dumps(params or {}, ensure_ascii=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO signal_log(symbol, strategy, signal, reason, close, indicators_json, params_json, trailing_stop, in_position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, strategy, signal, reason, close, indicators_json, params_json, trailing_stop, int(in_position)),
            )
            await db.commit()

    async def get_signal_logs(
        self,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
        signal_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """signal_log 조회 (전략 분석용)"""
        await self._init()
        clauses = []
        params: List[Any] = []
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if signal_filter:
            clauses.append("signal = ?")
            params.append(signal_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM signal_log {where} ORDER BY ts DESC LIMIT ?",
                tuple(params),
            )
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["indicators"] = json.loads(d.get("indicators_json") or "{}")
            except Exception:
                d["indicators"] = {}
            try:
                d["params"] = json.loads(d.get("params_json") or "{}")
            except Exception:
                d["params"] = {}
            result.append(d)
        return result

    async def get_signal_log_stats(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        """전략별 신호 통계 (퀀트 분석용)"""
        await self._init()
        where = "WHERE strategy = ?" if strategy else ""
        params = (strategy,) if strategy else ()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT strategy, signal, COUNT(*) as cnt
                FROM signal_log {where}
                GROUP BY strategy, signal
                ORDER BY strategy, cnt DESC
                LIMIT 100
                """,
                params,
            )
            rows = await cursor.fetchall()
            cursor2 = await db.execute(
                f"SELECT COUNT(*) as total FROM signal_log {where}",
                params,
            )
            total_row = await cursor2.fetchone()
        stats: Dict[str, Any] = {"total": int(total_row["total"]) if total_row else 0, "by_signal": {}}
        for row in rows:
            key = f"{row['strategy']}:{row['signal']}"
            stats["by_signal"][key] = int(row["cnt"])
        return stats

    async def cleanup_old_signal_logs(self, retention_days: int = 7) -> int:
        """오래된 signal_log 레코드 삭제 (보관 정책)"""
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM signal_log WHERE ts < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            await db.commit()
            return cursor.rowcount

    async def cleanup_old_snapshots(self, retention_days: int = 30) -> int:
        """오래된 portfolio_snapshots 레코드 삭제"""
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM portfolio_snapshots WHERE ts < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            await db.commit()
            return cursor.rowcount

    # ── Strategy Presets (코인별/전략별 파라미터 저장) ──────────────

    async def get_strategy_preset(self, strategy: str, symbol: str) -> Optional[Dict[str, Any]]:
        """strategy+symbol 조합의 프리셋 조회. 없으면 None."""
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT params_json FROM strategy_presets WHERE strategy = ? AND symbol = ?",
                (strategy, symbol),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["params_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    async def save_strategy_preset(self, strategy: str, symbol: str, params: Dict[str, Any]) -> None:
        """strategy+symbol 프리셋 upsert."""
        await self._init()
        params_json = json.dumps(params, ensure_ascii=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO strategy_presets(strategy, symbol, params_json, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    params_json = excluded.params_json,
                    updated_at = excluded.updated_at
                """,
                (strategy, symbol, params_json),
            )
            await db.commit()

    async def list_strategy_presets(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """프리셋 목록 조회. strategy 지정 시 해당 전략만."""
        await self._init()
        if strategy:
            sql = "SELECT strategy, symbol, params_json, updated_at FROM strategy_presets WHERE strategy = ? ORDER BY symbol"
            params: tuple = (strategy,)
        else:
            sql = "SELECT strategy, symbol, params_json, updated_at FROM strategy_presets ORDER BY strategy, symbol"
            params = ()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            try:
                p = json.loads(row["params_json"])
            except Exception:
                p = {}
            result.append({
                "strategy": row["strategy"],
                "symbol": row["symbol"],
                "params": p,
                "updated_at": row["updated_at"],
            })
        return result

    async def add_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        signal: Optional[str] = None,
        position_type: str = "spot",
        dollar_amount: Optional[float] = None,
        order_id: Optional[str] = None,
        status: str = "filled",
        fees: float = 0.0,
    ) -> Dict[str, Any]:
        await self._init()
        dollar_amount = dollar_amount if dollar_amount is not None else qty * price

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO trades(symbol, side, quantity, price, signal, position_type, status, dollar_amount, fees, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, side, qty, price, signal, position_type, status, dollar_amount, fees, order_id),
            )
            await db.commit()
            trade_id = int(cursor.lastrowid)

        return {
            "id": trade_id,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "signal": signal,
            "position_type": position_type,
            "status": status,
            "dollar_amount": dollar_amount,
            "fees": fees,
            "order_id": order_id,
            "ts": datetime.utcnow().isoformat(),
        }

    async def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_trades_by_symbol(self, symbol: str, limit: int = 200) -> List[Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_trade_signals(self, limit: int = 5) -> List[Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM trades
                WHERE signal IS NOT NULL
                  AND LOWER(signal) != 'manual'
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_current_positions(self) -> Dict[str, Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    symbol,
                    COALESCE(position_type, 'spot') AS position_type,
                    SUM(CASE
                        WHEN LOWER(side) = 'buy' THEN quantity
                        WHEN LOWER(side) = 'sell' THEN -quantity
                        ELSE 0
                    END) AS total_quantity,
                    SUM(CASE
                        WHEN LOWER(side) = 'buy' THEN COALESCE(dollar_amount, quantity * price)
                        WHEN LOWER(side) = 'sell' THEN -COALESCE(dollar_amount, quantity * price)
                        ELSE 0
                    END) AS total_invested
                FROM trades
                WHERE LOWER(COALESCE(status, 'filled')) = 'filled'
                  AND LOWER(side) IN ('buy', 'sell')
                GROUP BY symbol, COALESCE(position_type, 'spot')
                """
            )
            rows = await cursor.fetchall()

        positions: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = row["symbol"]
            position_type = row["position_type"] or "spot"
            total_quantity = float(row["total_quantity"] or 0.0)
            total_invested = float(row["total_invested"] or 0.0)
            average_price = (total_invested / total_quantity) if total_quantity > 0 else 0.0

            if symbol not in positions:
                positions[symbol] = {}
            positions[symbol][position_type] = {
                "symbol": symbol,
                "position_type": position_type,
                "total_quantity": total_quantity,
                "average_price": average_price,
                "total_invested": total_invested,
                "dollar_amount": total_invested,
            }
        return positions

    async def get_pnl(self, symbol: str, current_price: float, position_type: str = "spot") -> Dict[str, Any]:
        positions = await self.get_current_positions()
        position = positions.get(symbol, {}).get(position_type)
        if not position or position["total_quantity"] <= 0:
            return {
                "unrealized_pnl": 0,
                "unrealized_pnl_percent": 0,
                "average_price": 0,
                "current_price": current_price,
                "quantity": 0,
                "invested_value": 0,
                "current_value": 0,
                "dollar_amount": 0,
            }

        quantity = position["total_quantity"]
        invested_value = position["total_invested"]
        current_value = quantity * current_price
        unrealized_pnl = current_value - invested_value
        unrealized_pnl_percent = (unrealized_pnl / invested_value * 100) if invested_value > 0 else 0
        return {
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_percent": unrealized_pnl_percent,
            "average_price": position["average_price"],
            "current_price": current_price,
            "quantity": quantity,
            "invested_value": invested_value,
            "current_value": current_value,
            "dollar_amount": position["dollar_amount"],
        }

    async def create_position(
        self,
        symbol: str,
        position_type: str,
        entry_price: float,
        quantity: float,
        dollar_amount: float,
        entry_trade_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        await self._init()
        pos_id = str(uuid.uuid4())
        open_time = datetime.utcnow().isoformat()
        current_price = entry_price
        unrealized_pnl = self._calc_pnl(position_type, entry_price, current_price, quantity)
        unrealized_pnl_percent = (unrealized_pnl / dollar_amount * 100) if dollar_amount > 0 else 0.0

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO positions(
                    id, symbol, position_type, entry_price, quantity, dollar_amount,
                    current_price, unrealized_pnl, unrealized_pnl_percent,
                    open_time, close_time, status, entry_trade_id, exit_trade_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, NULL)
                """,
                (
                    pos_id,
                    symbol,
                    position_type,
                    entry_price,
                    quantity,
                    dollar_amount,
                    current_price,
                    unrealized_pnl,
                    unrealized_pnl_percent,
                    open_time,
                    entry_trade_id,
                ),
            )
            await db.commit()

        return {
            "id": pos_id,
            "symbol": symbol,
            "position_type": position_type,
            "entry_price": entry_price,
            "quantity": quantity,
            "dollar_amount": dollar_amount,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_percent": unrealized_pnl_percent,
            "open_time": open_time,
            "close_time": None,
            "status": "open",
            "entry_trade_id": entry_trade_id,
            "exit_trade_id": None,
        }

    async def get_position_by_id(self, position_id: str) -> Optional[Dict[str, Any]]:
        await self._init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_positions(
        self,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        await self._init()
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = "ORDER BY open_time DESC" if status != "closed" else "ORDER BY close_time DESC"
        sql = f"SELECT * FROM positions {where_sql} {order_sql} LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, tuple(params))
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_open_position_prices(self, price_updates: Dict[str, float]) -> int:
        await self._init()
        if not price_updates:
            return 0

        updated = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            for symbol, current_price in price_updates.items():
                cursor = await db.execute(
                    "SELECT id, position_type, entry_price, quantity, dollar_amount FROM positions WHERE status = 'open' AND symbol = ?",
                    (symbol,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    pnl = self._calc_pnl(row["position_type"], float(row["entry_price"]), current_price, float(row["quantity"]))
                    pnl_pct = (pnl / float(row["dollar_amount"]) * 100) if float(row["dollar_amount"]) > 0 else 0.0
                    await db.execute(
                        """
                        UPDATE positions
                        SET current_price = ?, unrealized_pnl = ?, unrealized_pnl_percent = ?
                        WHERE id = ?
                        """,
                        (current_price, pnl, pnl_pct, row["id"]),
                    )
                    updated += 1
            await db.commit()
        return updated

    async def close_position(
        self,
        position_id: str,
        close_price: float,
        exit_trade_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        await self._init()
        position = await self.get_position_by_id(position_id)
        if not position or position.get("status") != "open":
            return None

        current_price = float(close_price)
        pnl = self._calc_pnl(
            position["position_type"],
            float(position["entry_price"]),
            current_price,
            float(position["quantity"]),
        )
        pnl_pct = (pnl / float(position["dollar_amount"]) * 100) if float(position["dollar_amount"]) > 0 else 0.0
        close_time = datetime.utcnow().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    close_time = ?,
                    current_price = ?,
                    unrealized_pnl = ?,
                    unrealized_pnl_percent = ?,
                    exit_trade_id = ?
                WHERE id = ?
                """,
                (close_time, current_price, pnl, pnl_pct, exit_trade_id, position_id),
            )
            await db.commit()

        return await self.get_position_by_id(position_id)

    async def get_position_summary(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        open_positions = await self.get_positions(status="open", symbol=symbol, limit=1000)
        closed_positions = await self.get_positions(status="closed", symbol=symbol, limit=1000)

        total_unrealized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions)
        total_invested = sum(float(p.get("dollar_amount") or 0.0) for p in open_positions)
        realized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in closed_positions)

        winning_positions = [p for p in closed_positions if float(p.get("unrealized_pnl") or 0.0) > 0]
        losing_positions = [p for p in closed_positions if float(p.get("unrealized_pnl") or 0.0) < 0]
        win_rate = (len(winning_positions) / len(closed_positions) * 100.0) if closed_positions else 0.0

        return {
            "open_positions_count": len(open_positions),
            "closed_positions_count": len(closed_positions),
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_invested": total_invested,
            "realized_pnl": realized_pnl,
            "open_positions": open_positions,
            "symbol_filter": symbol,
            "statistics": {
                "total_positions": len(open_positions) + len(closed_positions),
                "winning_positions": len(winning_positions),
                "losing_positions": len(losing_positions),
                "win_rate": round(win_rate, 2),
                "total_realized_pnl": realized_pnl,
                "best_trade": max([float(p.get("unrealized_pnl") or 0.0) for p in closed_positions], default=0.0),
                "worst_trade": min([float(p.get("unrealized_pnl") or 0.0) for p in closed_positions], default=0.0),
            },
            "recent_closed_positions": closed_positions[:10],
        }

    async def get_trade_markers(self, symbol: str, limit: int = 500) -> List[Dict[str, Any]]:
        trades = await self.get_trades_by_symbol(symbol, limit=limit)
        markers: List[Dict[str, Any]] = []
        for trade in reversed(trades):
            if str(trade.get("status", "filled")).lower() != "filled":
                continue
            markers.append(
                {
                    "id": trade.get("id"),
                    "timestamp": trade.get("ts"),
                    "price": trade.get("price"),
                    "type": f"{str(trade.get('side', '')).lower()}_{trade.get('position_type', 'spot')}",
                    "side": trade.get("side"),
                    "position_type": trade.get("position_type", "spot"),
                    "quantity": trade.get("quantity"),
                    "dollar_amount": trade.get("dollar_amount"),
                    "signal": trade.get("signal"),
                    "fees": trade.get("fees", 0.0),
                }
            )
        return markers

    async def add_portfolio_snapshot(self, portfolio_data: Dict[str, Any]) -> None:
        await self._init()
        balances = portfolio_data.get("balances") or {}
        if not balances:
            return

        total_value_usd = float(portfolio_data.get("total_value_usd") or 0.0)
        current_btc_price = float(portfolio_data.get("current_btc_price") or 0.0)
        balances_json = json.dumps(balances, ensure_ascii=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO portfolio_snapshots(total_value_usd, current_btc_price, balances_json, live_trading)
                VALUES (?, ?, ?, 1)
                """,
                (total_value_usd, current_btc_price, balances_json),
            )
            await db.commit()

    async def get_portfolio_history(self, period: str = "7d") -> List[Dict[str, Any]]:
        await self._init()
        where_sql = "WHERE datetime(ts) >= datetime('now', ?)"
        params: List[Any] = []
        period_map = {
            "1h": "-1 hour",
            "24h": "-24 hour",
            "1d": "-1 day",
            "7d": "-7 day",
            "30d": "-30 day",
        }
        if period in period_map:
            params.append(period_map[period])
        elif period == "ytd":
            where_sql = "WHERE datetime(ts) >= datetime('now', 'start of year')"
        elif period == "all":
            where_sql = ""
        else:
            params.append("-7 day")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT ts, total_value_usd, current_btc_price, balances_json
                FROM portfolio_snapshots
                """
                + where_sql
                + """
                ORDER BY datetime(ts) ASC
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()

        history: List[Dict[str, Any]] = []
        for row in rows:
            balances = {}
            if row["balances_json"]:
                try:
                    balances = json.loads(row["balances_json"])
                except json.JSONDecodeError:
                    balances = {}
            history.append(
                {
                    "timestamp": row["ts"],
                    "total_value_usd": float(row["total_value_usd"] or 0.0),
                    "balances": balances,
                    "btc_price": float(row["current_btc_price"] or 0.0),
                    "live_trading": True,
                }
            )
        return history

    async def get_portfolio_performance(self) -> Dict[str, Any]:
        history = await self.get_portfolio_history("30d")
        if len(history) < 2:
            return {
                "daily_change": 0,
                "weekly_change": 0,
                "monthly_change": 0,
                "daily_change_percent": 0,
                "weekly_change_percent": 0,
                "monthly_change_percent": 0,
            }

        current = float(history[-1]["total_value_usd"])

        daily_data = await self.get_portfolio_history("1d")
        weekly_data = await self.get_portfolio_history("7d")
        monthly_data = history

        daily_start = float(daily_data[0]["total_value_usd"]) if daily_data else current
        weekly_start = float(weekly_data[0]["total_value_usd"]) if weekly_data else current
        monthly_start = float(monthly_data[0]["total_value_usd"]) if monthly_data else current

        return {
            "daily_change": current - daily_start,
            "weekly_change": current - weekly_start,
            "monthly_change": current - monthly_start,
            "daily_change_percent": ((current - daily_start) / daily_start * 100) if daily_start > 0 else 0,
            "weekly_change_percent": ((current - weekly_start) / weekly_start * 100) if weekly_start > 0 else 0,
            "monthly_change_percent": ((current - monthly_start) / monthly_start * 100) if monthly_start > 0 else 0,
        }
