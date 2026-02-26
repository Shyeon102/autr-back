"""
PostgreSQL 기반 TradeTrackerDB 구현.
sqlite.py와 동일한 인터페이스, asyncpg 백엔드.

DATABASE_URL 예시: postgresql://autr:autr@localhost:5432/autr
"""
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

import asyncpg


SCHEMA_SQL = """
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
);

CREATE INDEX IF NOT EXISTS idx_signal_log_ts ON signal_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_strategy ON signal_log(strategy, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_strategy_signal ON signal_log(strategy, signal);

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
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'open',
    entry_trade_id INTEGER,
    exit_trade_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions(status, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open_time ON positions(open_time DESC);
CREATE INDEX IF NOT EXISTS idx_positions_close_time ON positions(close_time DESC);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_value_usd REAL NOT NULL DEFAULT 0,
    current_btc_price REAL NOT NULL DEFAULT 0,
    balances_json TEXT,
    live_trading INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(ts DESC);

CREATE TABLE IF NOT EXISTS strategy_presets (
    id SERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    params_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(strategy, symbol)
);

CREATE INDEX IF NOT EXISTS idx_strategy_presets_key ON strategy_presets(strategy, symbol);
"""


class PostgresDB:
    """PostgreSQL 기반 거래/포지션 저장소 (TradeTrackerDB와 동일 인터페이스)."""

    def __init__(self, database_url: str):
        self._url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._url)
            async with self._pool.acquire() as conn:
                # 각 CREATE TABLE을 개별 실행
                for stmt in SCHEMA_SQL.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await conn.execute(stmt)
        return self._pool

    @staticmethod
    def _calc_pnl(position_type: str, entry_price: float, current_price: float, quantity: float) -> float:
        if position_type == "short":
            return (entry_price - current_price) * quantity
        return (current_price - entry_price) * quantity

    # ── Signal Log ──────────────────────────────────────────────────────

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
        pool = await self._get_pool()
        close = float(indicators.get("close", 0.0)) if indicators else 0.0
        indicators_json = json.dumps(indicators or {})
        params_json = json.dumps(params or {})
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signal_log(symbol, strategy, signal, reason, close, indicators_json, params_json, trailing_stop, in_position)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                symbol, strategy, signal, reason, close,
                indicators_json, params_json, trailing_stop, int(in_position),
            )

    async def get_signal_logs(
        self,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
        signal_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        clauses, args = [], []
        if strategy:
            args.append(strategy)
            clauses.append(f"strategy = ${len(args)}")
        if symbol:
            args.append(symbol)
            clauses.append(f"symbol = ${len(args)}")
        if signal_filter:
            args.append(signal_filter)
            clauses.append(f"signal = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM signal_log {where} ORDER BY ts DESC LIMIT ${len(args)}",
                *args,
            )
        result = []
        for row in rows:
            d = dict(row)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
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
        pool = await self._get_pool()
        where = "WHERE strategy = $1" if strategy else ""
        args = (strategy,) if strategy else ()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT strategy, signal, COUNT(*) as cnt
                FROM signal_log {where}
                GROUP BY strategy, signal
                ORDER BY strategy, cnt DESC
                LIMIT 100
                """,
                *args,
            )
            total_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM signal_log {where}", *args
            )
        stats: Dict[str, Any] = {"total": int(total_row["total"]) if total_row else 0, "by_signal": {}}
        for row in rows:
            key = f"{row['strategy']}:{row['signal']}"
            stats["by_signal"][key] = int(row["cnt"])
        return stats

    async def cleanup_old_signal_logs(self, retention_days: int = 7) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM signal_log WHERE ts < NOW() - INTERVAL '{retention_days} days'"
            )
        return int(result.split()[-1]) if result else 0

    async def cleanup_old_snapshots(self, retention_days: int = 30) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM portfolio_snapshots WHERE ts < NOW() - INTERVAL '{retention_days} days'"
            )
        return int(result.split()[-1]) if result else 0

    # ── Strategy Presets ────────────────────────────────────────────────

    async def get_strategy_preset(self, strategy: str, symbol: str) -> Optional[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT params_json FROM strategy_presets WHERE strategy = $1 AND symbol = $2",
                strategy, symbol,
            )
        if not row:
            return None
        try:
            return json.loads(row["params_json"])
        except Exception:
            return None

    async def save_strategy_preset(self, strategy: str, symbol: str, params: Dict[str, Any]) -> None:
        pool = await self._get_pool()
        params_json = json.dumps(params)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_presets(strategy, symbol, params_json, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    params_json = EXCLUDED.params_json,
                    updated_at = EXCLUDED.updated_at
                """,
                strategy, symbol, params_json,
            )

    async def list_strategy_presets(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        if strategy:
            sql = "SELECT strategy, symbol, params_json, updated_at FROM strategy_presets WHERE strategy = $1 ORDER BY symbol"
            args = (strategy,)
        else:
            sql = "SELECT strategy, symbol, params_json, updated_at FROM strategy_presets ORDER BY strategy, symbol"
            args = ()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
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
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            })
        return result

    # ── Trades ──────────────────────────────────────────────────────────

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
        pool = await self._get_pool()
        dollar_amount = dollar_amount if dollar_amount is not None else qty * price
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO trades(symbol, side, quantity, price, signal, position_type, status, dollar_amount, fees, order_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id, ts
                """,
                symbol, side, qty, price, signal,
                position_type, status, dollar_amount, fees, order_id,
            )
        return {
            "id": row["id"], "symbol": symbol, "side": side, "quantity": qty, "price": price,
            "signal": signal, "position_type": position_type, "status": status,
            "dollar_amount": dollar_amount, "fees": fees, "order_id": order_id,
            "ts": row["ts"].isoformat(),
        }

    async def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
        if not row:
            return None
        d = dict(row)
        d["ts"] = d["ts"].isoformat() if d.get("ts") else None
        return d

    async def recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM trades ORDER BY ts DESC LIMIT $1", limit)
        result = []
        for row in rows:
            d = dict(row)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
            result.append(d)
        return result

    async def get_trades_by_symbol(self, symbol: str, limit: int = 200) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trades WHERE symbol = $1 ORDER BY ts DESC LIMIT $2",
                symbol, limit,
            )
        result = []
        for row in rows:
            d = dict(row)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
            result.append(d)
        return result

    async def get_trade_signals(self, limit: int = 5) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM trades
                WHERE signal IS NOT NULL AND LOWER(signal) != 'manual'
                ORDER BY ts DESC LIMIT $1
                """,
                limit,
            )
        result = []
        for row in rows:
            d = dict(row)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
            result.append(d)
        return result

    # ── Positions ───────────────────────────────────────────────────────

    async def get_current_positions(self) -> Dict[str, Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
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
                "symbol": symbol, "position_type": position_type,
                "total_quantity": total_quantity, "average_price": average_price,
                "total_invested": total_invested, "dollar_amount": total_invested,
            }
        return positions

    async def get_pnl(self, symbol: str, current_price: float, position_type: str = "spot") -> Dict[str, Any]:
        positions = await self.get_current_positions()
        position = positions.get(symbol, {}).get(position_type)
        if not position or position["total_quantity"] <= 0:
            return {
                "unrealized_pnl": 0, "unrealized_pnl_percent": 0, "average_price": 0,
                "current_price": current_price, "quantity": 0, "invested_value": 0,
                "current_value": 0, "dollar_amount": 0,
            }
        quantity = position["total_quantity"]
        invested_value = position["total_invested"]
        current_value = quantity * current_price
        unrealized_pnl = current_value - invested_value
        unrealized_pnl_percent = (unrealized_pnl / invested_value * 100) if invested_value > 0 else 0
        return {
            "unrealized_pnl": unrealized_pnl, "unrealized_pnl_percent": unrealized_pnl_percent,
            "average_price": position["average_price"], "current_price": current_price,
            "quantity": quantity, "invested_value": invested_value,
            "current_value": current_value, "dollar_amount": position["dollar_amount"],
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
        pool = await self._get_pool()
        pos_id = str(uuid.uuid4())
        open_time = datetime.utcnow()
        current_price = entry_price
        unrealized_pnl = self._calc_pnl(position_type, entry_price, current_price, quantity)
        unrealized_pnl_percent = (unrealized_pnl / dollar_amount * 100) if dollar_amount > 0 else 0.0
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO positions(
                    id, symbol, position_type, entry_price, quantity, dollar_amount,
                    current_price, unrealized_pnl, unrealized_pnl_percent,
                    open_time, close_time, status, entry_trade_id, exit_trade_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NULL, 'open', $11, NULL)
                """,
                pos_id, symbol, position_type, entry_price, quantity, dollar_amount,
                current_price, unrealized_pnl, unrealized_pnl_percent, open_time, entry_trade_id,
            )
        return {
            "id": pos_id, "symbol": symbol, "position_type": position_type,
            "entry_price": entry_price, "quantity": quantity, "dollar_amount": dollar_amount,
            "current_price": current_price, "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_percent": unrealized_pnl_percent,
            "open_time": open_time.isoformat(), "close_time": None,
            "status": "open", "entry_trade_id": entry_trade_id, "exit_trade_id": None,
        }

    async def get_position_by_id(self, position_id: str) -> Optional[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM positions WHERE id = $1", position_id)
        if not row:
            return None
        d = dict(row)
        for key in ("open_time", "close_time"):
            if d.get(key):
                d[key] = d[key].isoformat()
        return d

    async def get_positions(
        self,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        clauses, args = [], []
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if symbol:
            args.append(symbol)
            clauses.append(f"symbol = ${len(args)}")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = "ORDER BY open_time DESC" if status != "closed" else "ORDER BY close_time DESC"
        args.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM positions {where_sql} {order_sql} LIMIT ${len(args)}",
                *args,
            )
        result = []
        for row in rows:
            d = dict(row)
            for key in ("open_time", "close_time"):
                if d.get(key):
                    d[key] = d[key].isoformat()
            result.append(d)
        return result

    async def update_open_position_prices(self, price_updates: Dict[str, float]) -> int:
        if not price_updates:
            return 0
        pool = await self._get_pool()
        updated = 0
        async with pool.acquire() as conn:
            for symbol, current_price in price_updates.items():
                rows = await conn.fetch(
                    "SELECT id, position_type, entry_price, quantity, dollar_amount FROM positions WHERE status = 'open' AND symbol = $1",
                    symbol,
                )
                for row in rows:
                    pnl = self._calc_pnl(
                        row["position_type"], float(row["entry_price"]), current_price, float(row["quantity"])
                    )
                    pnl_pct = (pnl / float(row["dollar_amount"]) * 100) if float(row["dollar_amount"]) > 0 else 0.0
                    await conn.execute(
                        "UPDATE positions SET current_price = $1, unrealized_pnl = $2, unrealized_pnl_percent = $3 WHERE id = $4",
                        current_price, pnl, pnl_pct, row["id"],
                    )
                    updated += 1
        return updated

    async def close_position(
        self,
        position_id: str,
        close_price: float,
        exit_trade_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        position = await self.get_position_by_id(position_id)
        if not position or position.get("status") != "open":
            return None
        current_price = float(close_price)
        pnl = self._calc_pnl(
            position["position_type"], float(position["entry_price"]), current_price, float(position["quantity"])
        )
        pnl_pct = (pnl / float(position["dollar_amount"]) * 100) if float(position["dollar_amount"]) > 0 else 0.0
        close_time = datetime.utcnow()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'closed', close_time = $1, current_price = $2,
                    unrealized_pnl = $3, unrealized_pnl_percent = $4, exit_trade_id = $5
                WHERE id = $6
                """,
                close_time, current_price, pnl, pnl_pct, exit_trade_id, position_id,
            )
        return await self.get_position_by_id(position_id)

    async def get_position_summary(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        open_positions = await self.get_positions(status="open", symbol=symbol, limit=1000)
        closed_positions = await self.get_positions(status="closed", symbol=symbol, limit=1000)
        total_unrealized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions)
        total_invested = sum(float(p.get("dollar_amount") or 0.0) for p in open_positions)
        realized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in closed_positions)
        winning = [p for p in closed_positions if float(p.get("unrealized_pnl") or 0.0) > 0]
        losing = [p for p in closed_positions if float(p.get("unrealized_pnl") or 0.0) < 0]
        win_rate = (len(winning) / len(closed_positions) * 100.0) if closed_positions else 0.0
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
                "winning_positions": len(winning),
                "losing_positions": len(losing),
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
            markers.append({
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
            })
        return markers

    # ── Portfolio Snapshots ─────────────────────────────────────────────

    async def add_portfolio_snapshot(self, portfolio_data: Dict[str, Any]) -> None:
        balances = portfolio_data.get("balances") or {}
        if not balances:
            return
        pool = await self._get_pool()
        total_value_usd = float(portfolio_data.get("total_value_usd") or 0.0)
        current_btc_price = float(portfolio_data.get("current_btc_price") or 0.0)
        balances_json = json.dumps(balances)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO portfolio_snapshots(total_value_usd, current_btc_price, balances_json, live_trading) VALUES ($1, $2, $3, 1)",
                total_value_usd, current_btc_price, balances_json,
            )

    async def get_portfolio_history(self, period: str = "7d") -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        period_map = {
            "1h": "1 hour", "24h": "24 hours", "1d": "1 day",
            "7d": "7 days", "30d": "30 days",
        }
        if period in period_map:
            where_sql = f"WHERE ts >= NOW() - INTERVAL '{period_map[period]}'"
        elif period == "ytd":
            where_sql = "WHERE ts >= date_trunc('year', NOW())"
        elif period == "all":
            where_sql = ""
        else:
            where_sql = "WHERE ts >= NOW() - INTERVAL '7 days'"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT ts, total_value_usd, current_btc_price, balances_json FROM portfolio_snapshots {where_sql} ORDER BY ts ASC"
            )
        history: List[Dict[str, Any]] = []
        for row in rows:
            balances = {}
            if row["balances_json"]:
                try:
                    balances = json.loads(row["balances_json"])
                except Exception:
                    balances = {}
            history.append({
                "timestamp": row["ts"].isoformat(),
                "total_value_usd": float(row["total_value_usd"] or 0.0),
                "balances": balances,
                "btc_price": float(row["current_btc_price"] or 0.0),
                "live_trading": True,
            })
        return history

    async def get_portfolio_performance(self) -> Dict[str, Any]:
        history = await self.get_portfolio_history("30d")
        if len(history) < 2:
            return {
                "daily_change": 0, "weekly_change": 0, "monthly_change": 0,
                "daily_change_percent": 0, "weekly_change_percent": 0, "monthly_change_percent": 0,
            }
        current = float(history[-1]["total_value_usd"])
        daily_data = await self.get_portfolio_history("1d")
        weekly_data = await self.get_portfolio_history("7d")
        daily_start = float(daily_data[0]["total_value_usd"]) if daily_data else current
        weekly_start = float(weekly_data[0]["total_value_usd"]) if weekly_data else current
        monthly_start = float(history[0]["total_value_usd"])
        return {
            "daily_change": current - daily_start,
            "weekly_change": current - weekly_start,
            "monthly_change": current - monthly_start,
            "daily_change_percent": ((current - daily_start) / daily_start * 100) if daily_start > 0 else 0,
            "weekly_change_percent": ((current - weekly_start) / weekly_start * 100) if weekly_start > 0 else 0,
            "monthly_change_percent": ((current - monthly_start) / monthly_start * 100) if monthly_start > 0 else 0,
        }
