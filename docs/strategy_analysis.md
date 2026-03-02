# 전략 분석 리포트

> **데이터 소스**: PostgreSQL `signal_log` 테이블
> **업데이트 방법**: 아래 SQL을 DataGrip 또는 psql로 직접 실행

---

## signal_log 테이블 구조

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | SERIAL | 기본키 |
| `ts` | TIMESTAMPTZ | 신호 생성 시각 |
| `symbol` | TEXT | 심볼 (BTCUSDT 등) |
| `strategy` | TEXT | 전략 이름 |
| `signal` | TEXT | 신호 종류 (`buy` / `sell` / `hold`) |
| `reason` | TEXT | 신호 발생 이유 (전략별 로직 설명) |
| `indicators` | JSONB | 지표 값 스냅샷 `{"close": 95420.5}` |
| `trailing_stop` | REAL | 트레일링 스탑 가격 (있을 경우) |
| `in_position` | BOOLEAN | 신호 발생 시점의 포지션 여부 |
| `params` | JSONB | 해당 틱의 전략 파라미터 |

---

## 1. 전체 신호 분포 요약

```sql
-- 전략별 BUY/SELL/HOLD 비율
SELECT
    strategy,
    COUNT(*)                                                  AS total,
    COUNT(CASE WHEN signal = 'buy'  THEN 1 END)               AS buys,
    COUNT(CASE WHEN signal = 'sell' THEN 1 END)               AS sells,
    COUNT(CASE WHEN signal = 'hold' THEN 1 END)               AS holds,
    ROUND(COUNT(CASE WHEN signal = 'buy'  THEN 1 END)::NUMERIC / COUNT(*) * 100, 2) AS buy_rate_pct,
    ROUND(COUNT(CASE WHEN signal = 'sell' THEN 1 END)::NUMERIC / COUNT(*) * 100, 2) AS sell_rate_pct
FROM signal_log
GROUP BY strategy
ORDER BY total DESC;
```

---

## 2. 시간대별 신호 빈도

```sql
-- 시간대별 신호 분포 (UTC 기준)
SELECT
    strategy,
    EXTRACT(HOUR FROM ts)::INT                                AS hour_utc,
    COUNT(*)                                                  AS total_signals,
    COUNT(CASE WHEN signal = 'buy' THEN 1 END)                AS buys,
    COUNT(CASE WHEN signal = 'sell' THEN 1 END)               AS sells
FROM signal_log
GROUP BY strategy, EXTRACT(HOUR FROM ts)
ORDER BY strategy, hour_utc;
```

---

## 3. BUY 신호 발생 이유 분석

```sql
-- BUY 신호의 reason별 빈도 (전략별)
SELECT
    strategy,
    reason,
    COUNT(*)                                                  AS count,
    ROUND(COUNT(*)::NUMERIC /
          SUM(COUNT(*)) OVER (PARTITION BY strategy) * 100, 2) AS pct_of_strategy
FROM signal_log
WHERE signal = 'buy'
GROUP BY strategy, reason
ORDER BY strategy, count DESC;
```

---

## 4. SELL 신호 발생 이유 분석

```sql
-- SELL 신호의 reason별 빈도
SELECT
    strategy,
    reason,
    COUNT(*)                                                  AS count
FROM signal_log
WHERE signal = 'sell'
GROUP BY strategy, reason
ORDER BY strategy, count DESC;
```

---

## 5. 일별 신호 추이

```sql
-- 날짜별 전략 활성도 (신호 수)
SELECT
    DATE(ts)                                                  AS signal_date,
    strategy,
    COUNT(*)                                                  AS total_signals,
    COUNT(CASE WHEN signal = 'buy'  THEN 1 END)               AS buys,
    COUNT(CASE WHEN signal = 'sell' THEN 1 END)               AS sells,
    COUNT(CASE WHEN signal = 'hold' THEN 1 END)               AS holds
FROM signal_log
GROUP BY DATE(ts), strategy
ORDER BY signal_date DESC, strategy;
```

---

## 6. 전략별 평균 틱 간격 (실행 주기 확인)

```sql
-- 연속 신호 간 평균 시간 간격 (전략 실행 주기 확인용)
WITH ordered AS (
    SELECT
        strategy,
        symbol,
        ts,
        LAG(ts) OVER (PARTITION BY strategy, symbol ORDER BY ts) AS prev_ts
    FROM signal_log
)
SELECT
    strategy,
    symbol,
    COUNT(*)                                                  AS total_signals,
    ROUND(AVG(EXTRACT(EPOCH FROM (ts - prev_ts)))::NUMERIC, 1) AS avg_interval_sec,
    MIN(EXTRACT(EPOCH FROM (ts - prev_ts)))::INT              AS min_interval_sec,
    MAX(EXTRACT(EPOCH FROM (ts - prev_ts)))::INT              AS max_interval_sec
FROM ordered
WHERE prev_ts IS NOT NULL
GROUP BY strategy, symbol
ORDER BY strategy, symbol;
```

---

## 7. 포지션 상태별 신호 분포

```sql
-- in_position=true/false 시점의 신호 분포
-- (포지션 보유 중 vs 비보유 중 전략 판단)
SELECT
    strategy,
    in_position,
    signal,
    COUNT(*)                                                  AS count
FROM signal_log
GROUP BY strategy, in_position, signal
ORDER BY strategy, in_position, signal;
```

---

## 8. Regime Trend 전략 — EMA Gap 분포

```sql
-- regime_trend 전략의 BUY 신호 시점 close 가격
-- (indicators JSONB에서 close 추출)
SELECT
    DATE_TRUNC('hour', ts)                                    AS hour,
    signal,
    COUNT(*)                                                  AS signals,
    ROUND(AVG((indicators->>'close')::NUMERIC), 2)            AS avg_close,
    ROUND(MIN((indicators->>'close')::NUMERIC), 2)            AS min_close,
    ROUND(MAX((indicators->>'close')::NUMERIC), 2)            AS max_close
FROM signal_log
WHERE strategy = 'regime_trend'
  AND indicators ? 'close'
GROUP BY DATE_TRUNC('hour', ts), signal
ORDER BY hour DESC;
```

---

## 9. 쿨다운 효율 분석

```sql
-- BUY 신호 직후 다음 BUY까지의 간격 (쿨다운 바 효과 측정)
WITH buy_signals AS (
    SELECT
        strategy,
        symbol,
        ts,
        LEAD(ts) OVER (
            PARTITION BY strategy, symbol ORDER BY ts
        ) AS next_buy_ts
    FROM signal_log
    WHERE signal = 'buy'
)
SELECT
    strategy,
    symbol,
    COUNT(*)                                                  AS buy_count,
    ROUND(AVG(EXTRACT(EPOCH FROM (next_buy_ts - ts)) / 60)::NUMERIC, 1)
                                                              AS avg_min_between_buys,
    ROUND(MIN(EXTRACT(EPOCH FROM (next_buy_ts - ts)) / 60)::NUMERIC, 1)
                                                              AS min_min_between_buys
FROM buy_signals
WHERE next_buy_ts IS NOT NULL
GROUP BY strategy, symbol
ORDER BY strategy, symbol;
```

---

## 10. 최근 24시간 신호 피드

```sql
-- 최근 24시간 신호 (시간 역순)
SELECT
    ts AT TIME ZONE 'Asia/Seoul'                              AS ts_kst,
    strategy,
    symbol,
    signal,
    reason,
    in_position,
    ROUND((indicators->>'close')::NUMERIC, 4)                AS close_price
FROM signal_log
WHERE ts >= NOW() - INTERVAL '24 hours'
ORDER BY ts DESC
LIMIT 100;
```

---

## 11. 전략 전환 이력

```sql
-- 전략 이름이 변경된 시점 추적
-- (연속된 신호에서 strategy가 달라지는 지점)
WITH changes AS (
    SELECT
        ts,
        strategy,
        symbol,
        LAG(strategy) OVER (PARTITION BY symbol ORDER BY ts) AS prev_strategy
    FROM signal_log
)
SELECT
    ts AT TIME ZONE 'Asia/Seoul'                              AS change_time_kst,
    symbol,
    prev_strategy                                             AS from_strategy,
    strategy                                                  AS to_strategy
FROM changes
WHERE strategy <> prev_strategy
  AND prev_strategy IS NOT NULL
ORDER BY ts DESC;
```

---

## 12. API를 통한 신호 조회

### 전략별 신호 통계 (API)
```bash
# 전체 통계
curl http://localhost:8000/api/db/signal-log/stats \
  -H "X-API-KEY: {YOUR_API_KEY}"

# 특정 전략 통계
curl "http://localhost:8000/api/db/signal-log/stats?strategy=regime_trend" \
  -H "X-API-KEY: {YOUR_API_KEY}"
```

### 최근 signal_log 조회
```bash
# BUY 신호만 200건
curl "http://localhost:8000/api/db/signal-log?signal=buy&limit=200" \
  -H "X-API-KEY: {YOUR_API_KEY}"

# 특정 심볼 + 전략
curl "http://localhost:8000/api/db/signal-log?symbol=BTCUSDT&strategy=regime_trend&limit=100" \
  -H "X-API-KEY: {YOUR_API_KEY}"
```

### DB 전체 요약
```bash
curl http://localhost:8000/api/db/summary \
  -H "X-API-KEY: {YOUR_API_KEY}"
```

---

## 전략별 핵심 지표 참고

| 전략 | 주요 signal reason 키워드 | 권장 분석 포인트 |
|------|--------------------------|----------------|
| `regime_trend` | `ema_gap_above_threshold`, `trailing_stop_hit`, `ema_gap_below_threshold` | EMA gap 임계값 조정 효과 확인 |
| `breakout_volume` | `breakout_volume_confirmed`, `stop_loss_hit`, `no_breakout` | breakout 확인율 vs 거짓 돌파 비율 |
| `mean_reversion` | `rsi_oversold_bb_lower`, `rsi_overbought`, `stop_loss_hit` | RSI 임계값 과최적화 여부 확인 |
| `dual_timeframe` | `ltf_pullback_entry`, `htf_bearish_filter`, `trailing_stop_hit` | HTF 필터 기각률 확인 |

---

## 성과 체크리스트

| 항목 | 목표 | 확인 쿼리 |
|------|------|-----------|
| **BUY 신호 빈도** | 전략 파라미터에 따라 적정 수준 | Section 1 |
| **HOLD 비율** | ≥ 85% (과매매 방지) | Section 1 `hold_rate_pct` |
| **신호 실행 주기** | ≤ 120초 (캔들 간격 내) | Section 6 `avg_interval_sec` |
| **in_position 시 SELL 비율** | SELL 신호의 대부분이 in_position=true에서 발생 | Section 7 |
| **쿨다운 작동** | min_between_buys ≥ cooldown_bars × 캔들 간격 | Section 9 |

---

> **참고**: `signal_log.indicators` 는 JSONB 타입입니다.
> 전략마다 기록하는 지표 키가 다를 수 있으니 `SELECT DISTINCT jsonb_object_keys(indicators) FROM signal_log WHERE strategy = 'xxx'` 로 실제 키를 먼저 확인하세요.
