import json
import time
import logging
from typing import List, Dict, Any

import numpy as np
import pandas as pd

from autr.infra.bybit.client import BybitClient
from autr.infra.db.quant_store import QuantSQLiteStore

logger = logging.getLogger(__name__)


class QuantDataCollector:
    def __init__(self, client: BybitClient, store: QuantSQLiteStore):
        self.client = client
        self.store = store

    def collect_symbol(self, symbol: str, timeframes: List[str]):
        self._collect_ticker(symbol)
        self._collect_orderbook_top(symbol)
        self._collect_public_trades(symbol)
        self._collect_derivatives_metrics(symbol)
        for tf in timeframes:
            self._collect_ohlcv(symbol, tf)
            self._compute_features(symbol, tf)

    def _collect_ohlcv(self, symbol: str, timeframe: str):
        try:
            resp = self.client.session.get_kline(
                category="spot",
                symbol=symbol,
                interval=timeframe,
                limit=300,
            )
            if resp.get("retCode") != 0:
                logger.warning("kline error %s %s: %s", symbol, timeframe, resp.get("retMsg"))
                return
            rows = []
            for r in resp["result"]["list"]:
                rows.append(
                    {
                        "symbol": symbol,
                        "market_type": "spot",
                        "timeframe": timeframe,
                        "ts": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "turnover": float(r[6]) if len(r) > 6 else None,
                    }
                )
            self.store.insert_many(
                """
                INSERT OR REPLACE INTO raw_ohlcv
                (symbol, market_type, timeframe, ts, open, high, low, close, volume, turnover)
                VALUES (:symbol, :market_type, :timeframe, :ts, :open, :high, :low, :close, :volume, :turnover)
                """,
                rows,
            )
        except Exception as exc:
            logger.exception("collect ohlcv failed %s %s: %s", symbol, timeframe, exc)

    def _collect_ticker(self, symbol: str):
        now_ms = int(time.time() * 1000)
        rows: List[Dict[str, Any]] = []
        try:
            # spot ticker
            spot = self.client.session.get_tickers(category="spot", symbol=symbol)
            if spot.get("retCode") == 0 and spot["result"]["list"]:
                t = spot["result"]["list"][0]
                rows.append(
                    {
                        "symbol": symbol,
                        "market_type": "spot",
                        "ts": now_ms,
                        "last_price": _f(t.get("lastPrice")),
                        "mark_price": None,
                        "index_price": None,
                        "bid1": _f(t.get("bid1Price")),
                        "ask1": _f(t.get("ask1Price")),
                        "bid1_size": _f(t.get("bid1Size")),
                        "ask1_size": _f(t.get("ask1Size")),
                        "open_interest": None,
                        "funding_rate": None,
                        "raw_json": json.dumps(t, ensure_ascii=True),
                    }
                )
            # linear ticker for derivatives fields
            linear = self.client.session.get_tickers(category="linear", symbol=symbol)
            if linear.get("retCode") == 0 and linear["result"]["list"]:
                t = linear["result"]["list"][0]
                rows.append(
                    {
                        "symbol": symbol,
                        "market_type": "linear",
                        "ts": now_ms,
                        "last_price": _f(t.get("lastPrice")),
                        "mark_price": _f(t.get("markPrice")),
                        "index_price": _f(t.get("indexPrice")),
                        "bid1": _f(t.get("bid1Price")),
                        "ask1": _f(t.get("ask1Price")),
                        "bid1_size": _f(t.get("bid1Size")),
                        "ask1_size": _f(t.get("ask1Size")),
                        "open_interest": _f(t.get("openInterest")),
                        "funding_rate": _f(t.get("fundingRate")),
                        "raw_json": json.dumps(t, ensure_ascii=True),
                    }
                )
            self.store.insert_many(
                """
                INSERT OR REPLACE INTO raw_ticker
                (symbol, market_type, ts, last_price, mark_price, index_price, bid1, ask1, bid1_size, ask1_size, open_interest, funding_rate, raw_json)
                VALUES (:symbol, :market_type, :ts, :last_price, :mark_price, :index_price, :bid1, :ask1, :bid1_size, :ask1_size, :open_interest, :funding_rate, :raw_json)
                """,
                rows,
            )
        except Exception as exc:
            logger.exception("collect ticker failed %s: %s", symbol, exc)

    def _collect_orderbook_top(self, symbol: str):
        now_ms = int(time.time() * 1000)
        rows: List[Dict[str, Any]] = []
        for market in ("spot", "linear"):
            try:
                resp = self.client.session.get_orderbook(category=market, symbol=symbol, limit=1)
                if resp.get("retCode") != 0:
                    continue
                data = resp.get("result", {})
                bids = data.get("b", [])
                asks = data.get("a", [])
                if not bids or not asks:
                    continue
                bid_price = _f(bids[0][0])
                bid_size = _f(bids[0][1])
                ask_price = _f(asks[0][0])
                ask_size = _f(asks[0][1])
                spread = (ask_price - bid_price) if bid_price and ask_price else None
                mid = ((ask_price + bid_price) / 2.0) if bid_price and ask_price else None
                denom = (bid_size or 0) + (ask_size or 0)
                imbalance = ((bid_size or 0) - (ask_size or 0)) / denom if denom else None
                rows.append(
                    {
                        "symbol": symbol,
                        "market_type": market,
                        "ts": int(data.get("ts", now_ms)),
                        "bid_price": bid_price,
                        "bid_size": bid_size,
                        "ask_price": ask_price,
                        "ask_size": ask_size,
                        "spread": spread,
                        "mid_price": mid,
                        "imbalance": imbalance,
                    }
                )
            except Exception:
                continue
        self.store.insert_many(
            """
            INSERT OR REPLACE INTO raw_orderbook_top
            (symbol, market_type, ts, bid_price, bid_size, ask_price, ask_size, spread, mid_price, imbalance)
            VALUES (:symbol, :market_type, :ts, :bid_price, :bid_size, :ask_price, :ask_size, :spread, :mid_price, :imbalance)
            """,
            rows,
        )

    def _collect_public_trades(self, symbol: str):
        rows: List[Dict[str, Any]] = []
        for market in ("spot", "linear"):
            try:
                resp = self.client.session.get_public_trade_history(
                    category=market,
                    symbol=symbol,
                    limit=50,
                )
                if resp.get("retCode") != 0:
                    continue
                for t in resp["result"]["list"]:
                    rows.append(
                        {
                            "symbol": symbol,
                            "market_type": market,
                            "trade_id": str(t.get("i") or t.get("execId") or f"{t.get('T')}_{t.get('p')}"),
                            "ts": int(t.get("T") or t.get("time") or 0),
                            "side": t.get("S") or t.get("side"),
                            "price": _f(t.get("p") or t.get("price")),
                            "size": _f(t.get("v") or t.get("size")),
                            "raw_json": json.dumps(t, ensure_ascii=True),
                        }
                    )
            except Exception:
                continue

        self.store.insert_many(
            """
            INSERT OR REPLACE INTO raw_public_trades
            (symbol, market_type, trade_id, ts, side, price, size, raw_json)
            VALUES (:symbol, :market_type, :trade_id, :ts, :side, :price, :size, :raw_json)
            """,
            rows,
        )

    def _collect_derivatives_metrics(self, symbol: str):
        # funding
        try:
            funding = self.client.session.get_funding_rate_history(
                category="linear",
                symbol=symbol,
                limit=20,
            )
            if funding.get("retCode") == 0:
                rows = []
                for x in funding["result"]["list"]:
                    rows.append(
                        {
                            "symbol": symbol,
                            "ts": int(x.get("fundingRateTimestamp", 0)),
                            "funding_rate": _f(x.get("fundingRate")),
                            "mark_price": _f(x.get("markPrice")),
                            "raw_json": json.dumps(x, ensure_ascii=True),
                        }
                    )
                self.store.insert_many(
                    """
                    INSERT OR REPLACE INTO raw_funding_rates
                    (symbol, ts, funding_rate, mark_price, raw_json)
                    VALUES (:symbol, :ts, :funding_rate, :mark_price, :raw_json)
                    """,
                    rows,
                )
        except Exception:
            pass

        # open interest
        try:
            oi = self.client.session.get_open_interest(
                category="linear",
                symbol=symbol,
                intervalTime="5min",
                limit=50,
            )
            if oi.get("retCode") == 0:
                rows = []
                for x in oi["result"]["list"]:
                    rows.append(
                        {
                            "symbol": symbol,
                            "interval_time": "5min",
                            "ts": int(x.get("timestamp", 0)),
                            "open_interest": _f(x.get("openInterest")),
                            "raw_json": json.dumps(x, ensure_ascii=True),
                        }
                    )
                self.store.insert_many(
                    """
                    INSERT OR REPLACE INTO raw_open_interest
                    (symbol, interval_time, ts, open_interest, raw_json)
                    VALUES (:symbol, :interval_time, :ts, :open_interest, :raw_json)
                    """,
                    rows,
                )
        except Exception:
            pass

    def _compute_features(self, symbol: str, timeframe: str):
        rows = self.store.fetch_ohlcv(symbol, "spot", timeframe, limit=450)
        if len(rows) < 220:
            return

        df = pd.DataFrame([dict(r) for r in rows]).sort_values("ts").reset_index(drop=True)
        df["ret_1"] = df["close"].pct_change()
        df["ret_log_1"] = (df["close"] / df["close"].shift(1)).map(
            lambda x: None if pd.isna(x) or x <= 0 else np.log(x)
        )
        df["vol_20"] = df["ret_1"].rolling(20).std()

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr_14"] = tr.rolling(14).mean()
        df["ema_fast_50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["ema_slow_200"] = df["close"].ewm(span=200, adjust=False).mean()
        df["trend_gap_pct"] = (df["ema_fast_50"] - df["ema_slow_200"]) / df["ema_slow_200"]
        roll_mean = df["close"].rolling(50).mean()
        roll_std = df["close"].rolling(50).std()
        df["zscore_50"] = (df["close"] - roll_mean) / roll_std

        feat_rows = []
        for _, r in df.dropna().iterrows():
            feat_rows.append(
                {
                    "symbol": symbol,
                    "market_type": "spot",
                    "timeframe": timeframe,
                    "ts": int(r["ts"]),
                    "close": float(r["close"]),
                    "ret_1": _f(r["ret_1"]),
                    "ret_log_1": _f(r["ret_log_1"]),
                    "vol_20": _f(r["vol_20"]),
                    "atr_14": _f(r["atr_14"]),
                    "ema_fast_50": _f(r["ema_fast_50"]),
                    "ema_slow_200": _f(r["ema_slow_200"]),
                    "trend_gap_pct": _f(r["trend_gap_pct"]),
                    "zscore_50": _f(r["zscore_50"]),
                }
            )
        self.store.insert_many(
            """
            INSERT OR REPLACE INTO feature_bar
            (symbol, market_type, timeframe, ts, close, ret_1, ret_log_1, vol_20, atr_14, ema_fast_50, ema_slow_200, trend_gap_pct, zscore_50)
            VALUES (:symbol, :market_type, :timeframe, :ts, :close, :ret_1, :ret_log_1, :vol_20, :atr_14, :ema_fast_50, :ema_slow_200, :trend_gap_pct, :zscore_50)
            """,
            feat_rows,
        )


def _f(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None
