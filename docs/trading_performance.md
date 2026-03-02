# 거래 성과 분석 리포트

> **데이터 소스**: PostgreSQL `trades` 테이블
> **업데이트 방법**: 아래 SQL을 DataGrip 또는 psql로 직접 실행

---

## 1. 기본 통계 요약

```sql
-- 전체 거래 현황 개요
SELECT
    COUNT(*)                                          AS total_trades,
    COUNT(CASE WHEN side = 'Buy'  THEN 1 END)         AS buy_count,
    COUNT(CASE WHEN side = 'Sell' THEN 1 END)         AS sell_count,
    ROUND(AVG(price)::NUMERIC, 2)                     AS avg_price,
    ROUND(SUM(price * quantity)::NUMERIC, 2)          AS total_volume_usd,
    MIN(ts)::DATE                                     AS first_trade_date,
    MAX(ts)::DATE                                     AS last_trade_date
FROM trades;
```

---

## 2. 일별 거래 횟수

```sql
-- 날짜별 BUY/SELL 횟수 및 거래대금
SELECT
    DATE(ts)                                          AS trade_date,
    COUNT(*)                                          AS total,
    COUNT(CASE WHEN side = 'Buy'  THEN 1 END)         AS buys,
    COUNT(CASE WHEN side = 'Sell' THEN 1 END)         AS sells,
    ROUND(SUM(price * quantity)::NUMERIC, 2)          AS volume_usd
FROM trades
GROUP BY DATE(ts)
ORDER BY trade_date DESC;
```

---

## 3. 심볼별 성과

```sql
-- 심볼별 거래 횟수 및 평균 단가
SELECT
    symbol,
    COUNT(*)                                          AS total_trades,
    COUNT(CASE WHEN side = 'Buy'  THEN 1 END)         AS buys,
    COUNT(CASE WHEN side = 'Sell' THEN 1 END)         AS sells,
    ROUND(AVG(CASE WHEN side = 'Buy'  THEN price END)::NUMERIC, 4) AS avg_buy_price,
    ROUND(AVG(CASE WHEN side = 'Sell' THEN price END)::NUMERIC, 4) AS avg_sell_price,
    ROUND(SUM(quantity)::NUMERIC, 6)                  AS total_qty
FROM trades
GROUP BY symbol
ORDER BY total_trades DESC;
```

---

## 4. 전략별 거래 성과

```sql
-- 전략 신호별 거래 횟수 (signal 컬럼 기준)
SELECT
    signal,
    COUNT(*)                                          AS trade_count,
    COUNT(CASE WHEN side = 'Buy'  THEN 1 END)         AS buys,
    COUNT(CASE WHEN side = 'Sell' THEN 1 END)         AS sells,
    ROUND(AVG(price * quantity)::NUMERIC, 2)          AS avg_trade_usd
FROM trades
WHERE signal IS NOT NULL
GROUP BY signal
ORDER BY trade_count DESC;
```

---

## 5. 매수/매도 쌍 기반 실현 PnL

```sql
-- trades 테이블에서 BUY→SELL 쌍을 만들어 실현 손익 계산
-- (같은 symbol에서 시간순으로 BUY와 SELL을 매칭)
WITH buys AS (
    SELECT
        id,
        symbol,
        price  AS buy_price,
        quantity,
        ts     AS buy_ts,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts) AS rn
    FROM trades
    WHERE side = 'Buy'
),
sells AS (
    SELECT
        id,
        symbol,
        price  AS sell_price,
        quantity,
        ts     AS sell_ts,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts) AS rn
    FROM trades
    WHERE side = 'Sell'
),
paired AS (
    SELECT
        b.symbol,
        b.buy_price,
        s.sell_price,
        LEAST(b.quantity, s.quantity)                         AS matched_qty,
        b.buy_ts,
        s.sell_ts,
        (s.sell_ts - b.buy_ts)                                AS hold_duration
    FROM buys b
    JOIN sells s ON b.symbol = s.symbol AND b.rn = s.rn
)
SELECT
    symbol,
    COUNT(*)                                                  AS closed_trades,
    ROUND(SUM((sell_price - buy_price) * matched_qty)::NUMERIC, 4) AS total_realized_pnl,
    ROUND(AVG((sell_price - buy_price) / buy_price * 100)::NUMERIC, 4) AS avg_return_pct,
    COUNT(CASE WHEN sell_price > buy_price THEN 1 END)        AS wins,
    COUNT(CASE WHEN sell_price < buy_price THEN 1 END)        AS losses,
    ROUND(
        COUNT(CASE WHEN sell_price > buy_price THEN 1 END)::NUMERIC /
        NULLIF(COUNT(*), 0) * 100, 2
    )                                                         AS win_rate_pct,
    ROUND(AVG(EXTRACT(EPOCH FROM hold_duration) / 3600)::NUMERIC, 2) AS avg_hold_hours
FROM paired
GROUP BY symbol
ORDER BY total_realized_pnl DESC;
```

---

## 6. 최대 낙폭 (Max Drawdown) 추정

```sql
-- 포트폴리오 스냅샷 기반 최대 낙폭 계산
WITH snapshots AS (
    SELECT
        ts,
        total_value_usd,
        MAX(total_value_usd) OVER (ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_max
    FROM portfolio_snapshots
    ORDER BY ts
)
SELECT
    MAX(running_max)                                          AS peak_value,
    MIN(total_value_usd)                                      AS trough_value,
    ROUND(
        (1 - MIN(total_value_usd) / NULLIF(MAX(running_max), 0)) * 100, 2
    )                                                         AS max_drawdown_pct,
    (SELECT ts FROM snapshots ORDER BY total_value_usd ASC LIMIT 1) AS drawdown_date
FROM snapshots;
```

---

## 7. 미실현 PnL (현재 포지션)

```sql
-- 열린 포지션 기준 현재 미실현 손익
-- current_price는 실시간 가격 조회 후 대입 필요
SELECT
    symbol,
    position_type,
    entry_price,
    quantity,
    -- current_price 자리에 실제 가격 입력 (예: 95000)
    ROUND((95000 - entry_price) * quantity, 4)                AS unrealized_pnl_usd,
    ROUND((95000 - entry_price) / entry_price * 100, 4)       AS unrealized_pnl_pct,
    opened_at
FROM positions
WHERE status = 'open'
ORDER BY opened_at DESC;
```

---

## 8. 최근 거래 내역

```sql
-- 최근 20개 거래 (시간 역순)
SELECT
    ts,
    symbol,
    side,
    ROUND(price::NUMERIC, 4)                                  AS price,
    ROUND(quantity::NUMERIC, 6)                               AS quantity,
    ROUND((price * quantity)::NUMERIC, 2)                     AS amount_usd,
    signal
FROM trades
ORDER BY ts DESC
LIMIT 20;
```

---

## 9. 시간대별 거래 패턴

```sql
-- 시간대별 BUY/SELL 분포 (UTC 기준)
SELECT
    EXTRACT(HOUR FROM ts)::INT                                AS hour_utc,
    COUNT(*)                                                  AS total,
    COUNT(CASE WHEN side = 'Buy'  THEN 1 END)                 AS buys,
    COUNT(CASE WHEN side = 'Sell' THEN 1 END)                 AS sells
FROM trades
GROUP BY EXTRACT(HOUR FROM ts)
ORDER BY hour_utc;
```

---

## 10. API를 통한 성과 조회

### 전체 수익률 통계
```bash
curl http://localhost:8000/api/portfolio/performance \
  -H "X-API-KEY: {YOUR_API_KEY}"
```

### 포트폴리오 히스토리 (7일)
```bash
curl "http://localhost:8000/api/portfolio/history?period=7d"
```

### 심볼별 PnL
```bash
curl http://localhost:8000/api/pnl/BTCUSDT
curl http://localhost:8000/api/pnl/XRPUSDT
curl http://localhost:8000/api/pnl/SOLUSDT
```

### 거래 내역 (최근 100건)
```bash
curl http://localhost:8000/api/db/trades?limit=100 \
  -H "X-API-KEY: {YOUR_API_KEY}"
```

---

## 성과 지표 체크리스트

| 지표 | 목표 | 확인 방법 |
|------|------|-----------|
| **승률 (Win Rate)** | ≥ 50% | Section 5 쿼리 |
| **평균 수익률** | ≥ 0.5% per trade | Section 5 `avg_return_pct` |
| **최대 낙폭** | ≤ 15% | Section 6 쿼리 |
| **평균 보유 시간** | 전략별 다름 | Section 5 `avg_hold_hours` |
| **일 거래 횟수** | 전략별 다름 | Section 2 쿼리 |

---

> **참고**: `trades` 테이블의 `ts` 컬럼은 `TIMESTAMPTZ` 타입입니다.
> 한국 시간(KST, UTC+9)으로 보려면 `ts AT TIME ZONE 'Asia/Seoul'` 을 사용하세요.
